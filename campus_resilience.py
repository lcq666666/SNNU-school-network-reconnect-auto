"""Persistent three-stage recovery with injected Windows operations."""
from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from campus_install import atomic_json


class RepairDeferred(Exception):
    """A temporary prerequisite failed before Portal authentication began."""


@dataclass
class RecoveryOps:
    network_ok: Callable
    uu_ok: Callable
    restart_uu: Callable
    proxy_snapshot: Callable
    proxy_disable: Callable
    proxy_restore: Callable
    repair: Callable
    reboot_ready: Callable
    reboot: Callable
    cancel_reboot: Callable
    # Optional first-layer action.  Older callers fall back to ``repair``;
    # production supplies a network-only reconnect so proxy/auth work remains
    # isolated to layer two.
    network_repair: Callable | None = None
    standby_repair: Callable | None = None


@dataclass(frozen=True)
class RecoveryPolicy:
    """Timing budget for one bounded recovery pipeline.

    The legacy defaults preserve the older offline controller contract.  The
    packaged tray supplies the V0.11 values so continuous failure confirmation,
    authentication and UU verification fit the roughly 60 second target.
    """

    confirmation_delay: float = 30.0
    network_repair_delay: float = 60.0
    repair_retry_delay: float = 180.0
    uu_observe_delay: float = 30.0
    uu_restart_delay: float = 30.0
    uu_confirmation_delay: float = 60.0
    manage_system_proxy: bool = True
    reboot_countdown: float = 0.0
    transient_repair_retry_delay: float = 15.0


class RecoveryController:
    """Own all recovery side effects while enabled, independently of snapshots."""

    def __init__(self, path: Path, ops: RecoveryOps, *, clock=time.time, log=None,
                 policy: RecoveryPolicy | None = None):
        self.path = Path(path)
        self.ops = ops
        self.clock = clock
        self.log = log or (lambda message: None)
        self.policy = policy or RecoveryPolicy()
        self.state = {"stage": "healthy", "outage_at": None, "next_action": 0,
                      "last_reboot": None, "proxy_before": None,
                      "reboot_pending": False}
        if self.path.exists():
            # A damaged journal must be visible, since it may hold proxy rollback data.
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("恢复记录损坏，请检查 recovery.json")
            self.state.update({key: loaded[key] for key in self.state if key in loaded})
        self.detail = "等待网络与 UU 连接检查"
        self.last_network = None
        self.last_uu = None
        self._reboot_at = None
        self.lock = threading.RLock()

    def save(self):
        atomic_json(self.path, self.state)

    def recover_proxy(self):
        before = self.state.get("proxy_before")
        if before is not None:
            self.ops.proxy_restore(before)
            self.state["proxy_before"] = None
            self.save()
            self.log("已恢复上次中断前的系统代理配置")

    def tick(self, now=None, *, allow_reboot=False):
        with self.lock:
            self._tick(now, allow_reboot=allow_reboot)

    def cancel_pending(self):
        with self.lock:
            self._reboot_at = None
            if self.state["reboot_pending"]:
                self.ops.cancel_reboot()
                self.state["reboot_pending"] = False
                self.save()

    def _tick(self, now=None, *, allow_reboot=False):
        now = self.clock() if now is None else float(now)
        if self.state["reboot_pending"] and (not allow_reboot or not self.ops.reboot_ready()):
            self.cancel_pending()
        self.recover_proxy()
        network = self.ops.network_ok()
        if self.state["reboot_pending"] and network is not False:
            self.cancel_pending()
        if self._reboot_at is not None:
            if network is not False or not allow_reboot or not self.ops.reboot_ready():
                self.cancel_pending()
                self.log("整机重启倒计时已取消")
            else:
                now = max(now, self.clock())
                self.last_network, self.last_uu = False, None
                if now < self._reboot_at:
                    self.detail = "持续断网，正在重新确认整机重启条件"
                else:
                    self._reboot_at = None
                    self._execute_reboot(now, legacy_pending=False)
                return
        # UU is only meaningful after the active network has been verified.  A
        # network outage must not be mislabeled as a UU outage and must not
        # trigger a remote-client restart.
        uu = self.ops.uu_ok() if network is True else None
        self.last_network, self.last_uu = network, uu
        if network is True and self.ops.standby_repair is not None:
            try:
                self.ops.standby_repair()
            except Exception as exc:
                self.log("备用链路维护未完成（%s），下轮继续检查" % type(exc).__name__)
        if network is None:
            if self.state["outage_at"] is not None:
                self.state.update(stage="healthy", outage_at=None, next_action=0)
                self.save()
            self.detail = "网络状态采集失败，等待复核"
            return
        if network is True and uu is None:
            if self.state["outage_at"] is not None:
                self.state.update(outage_at=None)
                self.save()
            self.detail = "网络已确认，UU 状态采集失败，等待复核"
            return
        if network is True and uu is True:
            self.state.update(stage="healthy", outage_at=None, next_action=0,
                              reboot_pending=False)
            self.save()
            self.detail = "公网校验通过，UU 已建立外部连接（远端会话可用性待控制端确认）"
            return

        if network is True and uu is False:
            # A healthy network with a missing UU connection is a separate
            # fault.  Do not reconnect Wi-Fi or re-authenticate the portal.
            if self.state["outage_at"] is not None:
                self.state["outage_at"] = None
                self.save()
            if self.state["stage"] == "healthy":
                self.state.update(stage="uu_observe",
                                   next_action=now + self.policy.uu_observe_delay)
                self.save()
                self.detail = "网络已恢复，UU 外连未建立，等待稳定复核"
                return
            if self.state["stage"] == "uu_wait":
                # A launch attempt is not proof that UU recovered.  Once the
                # confirmation window expires, return to the restart gate so
                # a still-missing or still-disconnected client gets another
                # bounded attempt.  The shared RestartGuard still enforces
                # the five-minute cooldown between real attempts.
                if now < self.state["next_action"]:
                    self.detail = "网络已恢复，但 UU 正在等待启动确认"
                    return
                self.state.update(stage="uu_restart", next_action=now)
                self.save()
            if self.state["stage"] in ("confirming", "network_repair", "uu_observe"):
                # The first verified network result after a network outage is
                # the hand-off point to the final UU-only recovery step.
                self.state.update(stage="uu_restart",
                                  next_action=now + self.policy.uu_restart_delay)
                self.save()
            self.detail = "网络已恢复，但 UU 外连未建立，阶段 %s" % self.state["stage"]
            if self.state["stage"] != "uu_restart" or now < self.state["next_action"]:
                return
            self.state.update(stage="uu_wait",
                              next_action=now + self.policy.uu_confirmation_delay)
            self.save()
            self.log("网络恢复后 UU 兜底：网络已确认恢复，UU 仍未建立外连，才重启 UU")
            attempted = self.ops.restart_uu()
            if attempted is False:
                # A disabled/cooldown-limited fallback is not a successful
                # restart.  Keep observing the already healthy network and
                # retry after the controller's normal interval instead of
                # getting stuck in ``uu_wait`` until the next outage.
                self.state.update(stage="uu_observe",
                                  next_action=now + self.policy.uu_confirmation_delay)
                self.detail = "网络已恢复，UU 兜底暂未执行，等待下一轮复核"
            self.state["next_action"] = max(now, self.clock()) + self.policy.uu_confirmation_delay
            self.save()
            return

        # A real network outage starts or continues the network-only recovery
        # path.  The UU state remains unknown until the network comes back.
        if (self.state["outage_at"] is None
                or self.state["stage"] in ("uu_observe", "uu_restart", "uu_wait")):
            self.state.update(stage="confirming", outage_at=now,
                              next_action=now + self.policy.confirmation_delay)
            self.save()
        self.detail = "网络或 UU 连接未恢复，阶段 %s" % self.state["stage"]
        if now < self.state["next_action"]:
            return
        stage = self.state["stage"]
        if stage == "confirming":
            self.state.update(stage="network_repair",
                              next_action=now + self.policy.network_repair_delay)
            self.save()
            self.log("第一层恢复：只尝试常规网络修复，不重启 UU")
            try:
                action = self.ops.network_repair or self.ops.repair
                _ok, self.detail = action()
            except Exception as exc:
                self.log("常规网络修复未完成（%s），等待第二层复核" % type(exc).__name__)
            self.state["next_action"] = max(now, self.clock()) + self.policy.network_repair_delay
            self.save()
            return
        if stage == "network_repair":
            self.state.update(stage="network_repair",
                              next_action=now + self.policy.repair_retry_delay)
            self.save()
            # Give the current recovery cycle one final portal/network attempt
            # before scheduling a machine restart.
            elapsed = now - self.state["outage_at"]
            last_reboot = self.state["last_reboot"]
            reboot_due = last_reboot is None or now - last_reboot >= 6 * 3600
            if elapsed >= 900 and allow_reboot and reboot_due:
                repair_ok = self._repair(now)
                if repair_ok is None:
                    return
                network_after_repair = self.ops.network_ok()
                if network_after_repair is True:
                    self.log("最后一轮网络恢复后公网已通过，继续检查 UU，不安排电脑重启")
                    self.state["next_action"] = now
                    self.save()
                    return
                if network_after_repair is not False:
                    self.log("最后一轮网络探测结果不确定，暂不安排电脑重启")
                    return
                if repair_ok:
                    self.log("网络恢复结果和接口探测不一致，暂不安排电脑重启")
                    return
                if not self.ops.reboot_ready():
                    self.log("Portal 登录仍需处理或 Windows 重启条件未满足，继续按恢复周期重试")
                    return
                if self.policy.reboot_countdown > 0:
                    self._reboot_at = max(now, self.clock()) + self.policy.reboot_countdown
                    self.detail = "校园网持续断开，已开始整机重启复核倒计时"
                    self.log(self.detail)
                else:
                    self._execute_reboot(now, legacy_pending=True)
                return
            self._repair(now)

    def _execute_reboot(self, now, *, legacy_pending):
        self.log("第三层恢复：最后一轮校园网恢复仍失败，申请 Windows 重启")
        previous = self.state.copy()
        self.state.update(last_reboot=now, reboot_pending=legacy_pending,
                          stage="network_repair", next_action=now + 120)
        self.save()
        try:
            self.ops.reboot()
        except Exception:
            self.state = previous
            self.save()
            raise

    def _repair(self, now):
        change_proxy = self.policy.manage_system_proxy
        self.log("第二层恢复：校园网重连和认证")
        if change_proxy:
            self.state["proxy_before"] = self.ops.proxy_snapshot()
            self.save()
        delay = self.policy.repair_retry_delay
        try:
            if change_proxy:
                self.ops.proxy_disable()
            ok, detail = self.ops.repair()
            self.detail = detail
            self.log("网络重连完成：%s" % ("已恢复" if ok else "等待后续验证"))
        except RepairDeferred as exc:
            ok = None
            delay = min(delay, self.policy.transient_repair_retry_delay)
            self.detail = "%s，%g 秒后重新检查" % (exc, delay)
            self.log(self.detail)
        finally:
            if change_proxy:
                self.recover_proxy()
        self.state["next_action"] = max(now, self.clock()) + delay
        self.save()
        return ok


class StandbyWifiGuard:
    """Maintain Wi-Fi behind a verified primary, with bounded serial retries."""

    def __init__(self, repair, *, reset=None, log=None):
        self.repair = repair
        self.reset = reset
        self.log = log or (lambda message: None)
        self.failures = 0
        self.attempts = []

    def tick(self, snapshot, *, trusted, now):
        from campus_network import HealthState

        eth = getattr(snapshot, "ethernet", None)
        wifi = getattr(snapshot, "wifi", None)
        egress = getattr(getattr(snapshot, "system", None), "egress", None)
        if (not trusted or eth is None or wifi is None
                or eth.state is not HealthState.HEALTHY
                or getattr(egress, "if_index", None) != eth.if_index
                or not wifi.if_index or not wifi.interface):
            self.failures = 0
            return
        failures = {HealthState.CAPTIVE_PORTAL, HealthState.INTERNET_UNREACHABLE,
                    HealthState.DNS_FAILURE, HealthState.LINK_DOWN, HealthState.NO_IP}
        if wifi.state not in failures:
            self.failures = 0
            return
        self.failures += 1
        if self.failures < 3:
            return
        self.attempts = [stamp for stamp in self.attempts if now - stamp < 600]
        if (len(self.attempts) >= 2
                or self.attempts and now - self.attempts[-1] < 180):
            return
        self.attempts.append(now)
        self.log("有线正常，维护 SNNU 无线备用链路（限定无线接口）")
        result = self.repair(wifi)
        repair = getattr(result, "repair", None)
        if (self.reset is not None and not getattr(result, "ok", False)
                and getattr(repair, "auth_ok", False) is True
                and getattr(repair, "network_ok", None) is False):
            renewed = self.reset(wifi)
            if renewed is not None:
                result = renewed
        self.log("无线备用维护结果：%s" % (getattr(result, "detail", "") or "等待独立验证"))
        return result
