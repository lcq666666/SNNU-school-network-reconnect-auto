"""Persistent three-stage recovery with injected Windows operations."""
from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from campus_install import atomic_json


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


class RecoveryController:
    """Own all recovery side effects while enabled, independently of snapshots."""

    def __init__(self, path: Path, ops: RecoveryOps, *, clock=time.time, log=None):
        self.path = Path(path)
        self.ops = ops
        self.clock = clock
        self.log = log or (lambda message: None)
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
            if self.state["reboot_pending"]:
                self.ops.cancel_reboot()
                self.state["reboot_pending"] = False
                self.save()

    def _tick(self, now=None, *, allow_reboot=False):
        now = self.clock() if now is None else float(now)
        self.recover_proxy()
        network = self.ops.network_ok()
        uu = self.ops.uu_ok() if network else False
        self.last_network, self.last_uu = network, uu
        if network is None or uu is None:
            self.detail = "网络或 UU 状态采集失败，等待复核"
            return
        if network and uu:
            if self.state["reboot_pending"]:
                self.ops.cancel_reboot()
            self.state.update(stage="healthy", outage_at=None, next_action=0,
                              reboot_pending=False)
            self.save()
            self.detail = "公网校验通过，UU 已建立外部连接（远端会话可用性待控制端确认）"
            if self.ops.standby_repair is not None:
                try:
                    self.ops.standby_repair()
                except Exception as exc:
                    self.log("备用链路维护未完成（%s），下轮继续检查" % type(exc).__name__)
            return
        if self.state["outage_at"] is None:
            self.state.update(stage="confirming", outage_at=now, next_action=now + 30)
            self.save()
        self.detail = "网络或 UU 连接未恢复，阶段 %s" % self.state["stage"]
        if now < self.state["next_action"]:
            return
        stage = self.state["stage"]
        if stage == "confirming":
            self.state.update(stage="uu_restart", next_action=now + 60)
            self.save()
            self.log("第一层恢复：尝试常规网络重连，再重启 UU")
            try:
                action = self.ops.network_repair or self.ops.repair
                _ok, self.detail = action()
            except Exception as exc:
                self.log("常规网络重连未完成（%s），继续 UU 恢复" % type(exc).__name__)
            self.ops.restart_uu()
            self.state["next_action"] = max(now, self.clock()) + 60
            self.save()
            return
        if stage == "uu_restart" or stage == "network_repair":
            self.state.update(stage="network_repair", next_action=now + 180)
            self.save()
            # The reboot decision is made before another repair attempt, after 15 minutes.
            elapsed = now - self.state["outage_at"]
            last_reboot = self.state["last_reboot"]
            reboot_due = last_reboot is None or now - last_reboot >= 6 * 3600
            if elapsed >= 900 and allow_reboot and reboot_due and self.ops.reboot_ready():
                self.log("第三层恢复：60 秒后强制重启，恢复记录已保存")
                previous = self.state.copy()
                self.state.update(last_reboot=now, reboot_pending=True,
                                  stage="network_repair", next_action=now + 120)
                self.save()
                try:
                    self.ops.reboot()
                except Exception:
                    self.state = previous
                    self.save()
                    raise
                return
            self._repair(now)

    def _repair(self, now):
        self.log("第二层恢复：备份代理、关闭代理、重连认证、恢复代理、重启 UU")
        self.state["proxy_before"] = self.ops.proxy_snapshot()
        self.save()
        try:
            self.ops.proxy_disable()
            ok, detail = self.ops.repair()
            self.detail = detail
            self.log("网络重连完成：%s" % ("已恢复" if ok else "等待后续验证"))
        finally:
            self.recover_proxy()
        self.ops.restart_uu()
        self.state["next_action"] = max(now + 180, self.clock() + 180)
        self.save()


class StandbyWifiGuard:
    """Maintain Wi-Fi behind a verified primary, with bounded serial retries."""

    def __init__(self, repair, *, log=None):
        self.repair = repair
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
        self.log("无线备用维护结果：%s" % (getattr(result, "detail", "") or "等待独立验证"))
