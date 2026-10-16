# -*- coding: utf-8 -*-
"""V0.5-2 恢复事件协调器（纯逻辑层）。

本模块只回答两个问题（方案 §28–§32）：

1. **什么时候验证？** —— 恢复事件（standby 连接 / 活动路径恢复 / 接管 / 切回）发生时，
   对**哪条路径**安排一次接口受限验证；
2. **什么时候重启 UU？** —— 只有「活动路径 + 直连 Internet 验证通过 + 恢复事件」三条件
   同时成立，且**同一网络世代未重启过**、**不在冷却期**，才产出一个
   :class:`UuRestartRequest`。

设计约束（与 AGENTS §1「V0.5 追加规则」一致）：

- **零 I/O、零副作用**：不建 socket、不切路由、不认证、不杀进程；只产出**计划**
  （:class:`RecoveryPlan`），执行留给 V0.5-3 / V0.5-4；
- **零依赖**：只 import 标准库。验证结果按**鸭子类型**消费
  （``allows_recovery`` / ``role`` / ``reliability`` / ``reason`` / ``source_ip``），
  因此本模块不会与 ``campus_reconnect`` 形成循环依赖；
- **去重**：``network_epoch`` 变化即开启新世代；同一世代内同一路径**只验证一次**、
  UU **最多重启一次**（方案 §31），杜绝 tick 级重复执行与 kill/start 抖动；
- **fail-closed**：``reliability=PROBE_UNRELIABLE`` 或验证未通过一律**不**产出重启请求。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "CONTRACT_VERSION", "PATH_ETHERNET", "PATH_WIFI",
    "DEFAULT_UU_RESTART_COOLDOWN", "RecoveryEvent", "RecoveryStep",
    "UuRestartRequest", "RecoveryPlan", "RecoveryCoordinator",
]

CONTRACT_VERSION = "v05-2-recovery-1"

#: 路径名（与 ``campus_reconnect.PATH_ROLE_*`` / ``PathVerificationResult.role`` 同值）。
PATH_ETHERNET = "ethernet"
PATH_WIFI = "wifi"
#: UU 重启冷却（方案 §14/§34 建议值 300s）；与 ``campus_remote_app`` 的默认冷却同源。
DEFAULT_UU_RESTART_COOLDOWN = 300.0


class RecoveryEvent(Enum):
    """一次「恢复事件」（方案 §29）。"""

    #: 备用链路（Wi-Fi standby）刚刚在链路层连上：只验证，不切路由、不重启 UU。
    STANDBY_CONNECTED = "STANDBY_CONNECTED"
    #: 活动路径恢复可用（例如故障后 Wi-Fi 接管完成）。
    ACTIVE_PATH_RECOVERED = "ACTIVE_PATH_RECOVERED"
    #: 故障切换完成（Ethernet → Wi-Fi）。
    FAILOVER_COMPLETED = "FAILOVER_COMPLETED"
    #: 切回完成（Wi-Fi → Ethernet）。
    FAILBACK_COMPLETED = "FAILBACK_COMPLETED"

    @property
    def is_active_path(self) -> bool:
        """该事件描述的路径是否就是（新的）活动路径。"""
        return self is not RecoveryEvent.STANDBY_CONNECTED

    @property
    def restarts_uu(self) -> bool:
        """该事件在「验证通过」后是否允许重启 UU（方案 §10/§30）。"""
        return self is not RecoveryEvent.STANDBY_CONNECTED


class RecoveryStep(Enum):
    """恢复流水线的阶段（方案 §47 的 UI/日志阶段名）。"""

    DETECTION = "detection"
    BACKUP_READY = "backup_ready"
    PORTAL = "portal"
    INTERNET_VERIFY = "internet_verify"
    ROUTE_SWITCH = "route_switch"
    UU_RESTART = "uu_restart"


def _attr(value: Any, name: str, default: Any = None) -> Any:
    """对象属性 / 字典键双兼容读取（验证结果按鸭子类型消费）。"""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True)
class UuRestartRequest:
    """一次「网络恢复后重启 UU」的请求（**协调器只产出请求，不执行**）。"""

    network_epoch: int
    active_path: Optional[str]
    reason: str
    verification_epoch: int = 0
    source_ip: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"network_epoch": self.network_epoch, "active_path": self.active_path,
                "reason": self.reason, "verification_epoch": self.verification_epoch,
                "source_ip": self.source_ip}


@dataclass(frozen=True)
class RecoveryPlan:
    """协调器对一次输入给出的**计划**（纯数据；是否执行由上层决定）。"""

    network_epoch: int = 0
    event: Optional[RecoveryEvent] = None
    active_path: Optional[str] = None
    verify_path: Optional[str] = None
    verify_reason: str = ""
    verified: Optional[bool] = None
    verification_epoch: int = 0
    uu_restart: Optional[UuRestartRequest] = None
    deduped: bool = False
    steps: Tuple[RecoveryStep, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def needs_verification(self) -> bool:
        """本计划是否要求再跑一次接口受限验证。

        去重（``deduped``）的计划虽然仍记录 ``verify_path``（便于日志说明"哪条路径
        本世代已验过"），但**不**要求动作。
        """
        return self.verify_path is not None and not self.deduped

    @property
    def schedules_uu_restart(self) -> bool:
        """本计划是否产出了 UU 重启请求。"""
        return self.uu_restart is not None

    def to_dict(self) -> Dict[str, Any]:
        return {"network_epoch": self.network_epoch,
                "event": self.event.value if self.event else None,
                "active_path": self.active_path,
                "verify_path": self.verify_path,
                "verify_reason": self.verify_reason,
                "verified": self.verified,
                "verification_epoch": self.verification_epoch,
                "uu_restart": self.uu_restart.to_dict() if self.uu_restart else None,
                "deduped": self.deduped,
                "steps": [step.value for step in self.steps],
                "notes": list(self.notes)}


class RecoveryCoordinator:
    """恢复事件协调器（纯逻辑 + 显式 ``now``；不持有时钟、不做 I/O）。

    典型用法（V0.5-4 接线）::

        coordinator.update_network(network_epoch=epoch, active_path="ethernet",
                                   standby_path="wifi")
        plan = coordinator.on_event(RecoveryEvent.STANDBY_CONNECTED, now=now,
                                    path="wifi")
        if plan.needs_verification:
            ...  # 交给 PathVerifier 验证 plan.verify_path
            follow_up = coordinator.on_verification(result, now=now)
            if follow_up.schedules_uu_restart:
                ...  # 交给 V0.5-3 执行 restart_after_verified_recovery
    """

    def __init__(self, *, uu_cooldown: float = DEFAULT_UU_RESTART_COOLDOWN) -> None:
        if uu_cooldown < 0:
            raise ValueError("uu_cooldown must be non-negative")
        self.uu_cooldown = float(uu_cooldown)
        self.network_epoch = 0
        self.active_path: Optional[str] = None
        self.previous_active_path: Optional[str] = None
        self.standby_path: Optional[str] = None
        self.verification_epoch = 0
        self.uu_restart_epoch = 0
        self.last_uu_restart_at: Optional[float] = None
        self.last_uu_restart_ok: Optional[bool] = None
        self.last_uu_restart_detail = ""
        #: 每个路径最近一次被安排验证的世代（世代内去重）。
        self._verify_requested: Dict[str, int] = {}
        #: 每个路径在哪一世代被「恢复事件」授权重启 UU（验证通过后消费）。
        self._uu_armed: Dict[str, int] = {}

    # ---- 状态声明 ---- #
    def update_network(self, *, network_epoch: int, active_path: Optional[str],
                       standby_path: Optional[str] = None) -> bool:
        """声明当前**网络世代**与主/备路径；返回是否发生了世代切换。

        ``network_epoch`` 由上层在网络情形变化（接口重连 / 换 IP / 活动路径切换）时递增。
        世代切换会清空本世代的去重状态（验证名额与 UU 授权），因此新一代可以重新验证、
        重新重启 UU（这正是「下一次真正新的 recovery event」的语义，方案 §36）。
        """
        epoch = int(network_epoch)
        changed = epoch != self.network_epoch
        if active_path != self.active_path:
            self.previous_active_path = self.active_path
            self.active_path = active_path
        if standby_path is not None:
            self.standby_path = standby_path
        if changed:
            self.network_epoch = epoch
            self._verify_requested.clear()
            self._uu_armed.clear()
        return changed

    # ---- 恢复事件 ---- #
    def on_event(self, event: RecoveryEvent, *, now: float,
                 path: Optional[str] = None) -> RecoveryPlan:
        """处理一次恢复事件，返回「接下来该验证哪条路径」。

        - :attr:`RecoveryEvent.STANDBY_CONNECTED`：只安排验证，**不**授权 UU 重启；
        - 其余事件：安排验证，并通过验证后授权**一次** UU 重启（方案 §30）。
        """
        epoch = self.network_epoch
        target = path if path is not None else self.active_path
        notes = []
        if event.is_active_path and path is not None and path != self.active_path:
            self.previous_active_path = self.active_path
            self.active_path = path
        if event is RecoveryEvent.STANDBY_CONNECTED and path is not None:
            self.standby_path = path

        if target is None:
            notes.append("未解析到可用路径：只能记录事件，无法验证（%s）"
                         % event.value)
            return RecoveryPlan(network_epoch=epoch, event=event,
                                active_path=self.active_path,
                                verification_epoch=self.verification_epoch,
                                notes=tuple(notes))

        if self._verify_requested.get(target) == epoch:
            notes.append("同一网络世代（epoch=%d）已安排过 %s 验证：去重，不再重复验证"
                         % (epoch, target))
            return RecoveryPlan(network_epoch=epoch, event=event,
                                active_path=self.active_path, verify_path=target,
                                verified=None,
                                verification_epoch=self.verification_epoch,
                                deduped=True, notes=tuple(notes))

        self._verify_requested[target] = epoch
        self.verification_epoch += 1
        if event.restarts_uu:
            self._uu_armed[target] = epoch
            steps = (RecoveryStep.INTERNET_VERIFY, RecoveryStep.UU_RESTART)
            notes.append("%s：验证通过后允许重启一次 UU 远程" % event.value)
        else:
            steps = (RecoveryStep.INTERNET_VERIFY,)
            notes.append("备用链路刚连接：只验证，不切路由、不重启 UU（方案 §30）")
        return RecoveryPlan(
            network_epoch=epoch, event=event, active_path=self.active_path,
            verify_path=target, verify_reason=event.value, verified=None,
            verification_epoch=self.verification_epoch, steps=steps,
            notes=tuple(notes))

    # ---- 验证结果 ---- #
    def on_verification(self, result: Any, *, now: float) -> RecoveryPlan:
        """消费一次接口受限验证结果，决定是否产出 UU 重启请求。

        ``result`` 为 :class:`campus_reconnect.PathVerificationResult`（鸭子类型）：
        只读取 ``allows_recovery`` / ``role`` / ``reliability`` / ``reason`` / ``source_ip``。
        """
        epoch = self.network_epoch
        path = _attr(result, "role", None) or self.active_path
        allowed = bool(_attr(result, "allows_recovery", False))
        detail = str(_attr(result, "reason", "") or _attr(result, "reliability", "") or "")
        base = dict(network_epoch=epoch, active_path=self.active_path,
                    verify_path=path, verification_epoch=self.verification_epoch)

        if not allowed:
            reason = "接口受限验证未通过（%s）：不重启 UU" % (detail or "未验证")
            return RecoveryPlan(verified=False, steps=(), notes=(reason,), **base)

        if self.uu_restart_epoch == epoch:
            note = "同一网络世代（epoch=%d）已重启过 UU：去重" % epoch
            return RecoveryPlan(verified=True, deduped=True, steps=(),
                                notes=(note,), **base)

        if self._uu_armed.get(path) != epoch:
            note = ("本次验证（%s/epoch=%d）不属于任何可用路径恢复事件：不重启 UU"
                    % (path, epoch))
            return RecoveryPlan(verified=True, steps=(), notes=(note,), **base)

        if (self.last_uu_restart_at is not None
                and float(now) - float(self.last_uu_restart_at) < self.uu_cooldown):
            self.uu_restart_epoch = epoch        # 消耗本世代名额，避免抖动弹回
            note = ("UU 重启冷却中（距上次 %.0fs < %.0fs）：跳过本次重启（等待下一次"
                    "新的网络恢复事件）"
                    % (float(now) - float(self.last_uu_restart_at), self.uu_cooldown))
            return RecoveryPlan(verified=True, deduped=True, steps=(),
                                notes=(note,), **base)

        self.uu_restart_epoch = epoch
        self.last_uu_restart_at = float(now)
        self._uu_armed.pop(path, None)
        request = UuRestartRequest(
            network_epoch=epoch, active_path=path,
            reason="网络恢复且接口受限验证通过（%s）" % (detail or "VERIFIED"),
            verification_epoch=self.verification_epoch,
            source_ip=str(_attr(result, "source_ip", "") or ""))
        return RecoveryPlan(verified=True, steps=(RecoveryStep.UU_RESTART,),
                            uu_restart=request,
                            notes=("已安排 UU 远程重启（epoch=%d，路径=%s）"
                                   % (epoch, path),), **base)

    # ---- 执行回报 ---- #
    def note_uu_restart_outcome(self, ok: bool, *, detail: str = "") -> None:
        """记录一次 UU 重启的**实际结局**（V0.5-3 执行后回报）。

        只用于状态展示 / 日志；冷却与去重语义不变（失败同样进入冷却，方案 §36）。
        """
        self.last_uu_restart_ok = bool(ok)
        self.last_uu_restart_detail = str(detail or "")

    # ---- 只读视图 ---- #
    def snapshot(self) -> Dict[str, Any]:
        """协调器状态的只读快照（UI / 日志用）。"""
        return {"contract_version": CONTRACT_VERSION,
                "network_epoch": self.network_epoch,
                "active_path": self.active_path,
                "previous_active_path": self.previous_active_path,
                "standby_path": self.standby_path,
                "verification_epoch": self.verification_epoch,
                "uu_restart_epoch": self.uu_restart_epoch,
                "last_uu_restart_at": self.last_uu_restart_at,
                "last_uu_restart_ok": self.last_uu_restart_ok,
                "uu_cooldown": self.uu_cooldown}
