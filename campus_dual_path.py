# -*- coding: utf-8 -*-
"""V0.5-4 双链路恢复整合层。

把四件既有能力接成一条流水线（方案 §3、§5.3、§5.4）：

``NetworkFailoverManager``（状态机，V0.3 P3）
    → :class:`DualPathRecoveryController`（**本模块**：状态迁移 → 恢复事件 → 验证安排）
    → ``PathVerifier``（接口受限真实验证，V0.5-1）
    → ``RecoveryCoordinator``（何时验证 / 何时允许重启 UU，V0.5-2）
    → ``restart_after_verified_recovery``（清退重启 UU，V0.5-3）

**只新增接缝，不重写判定**：状态机的 11 个状态、滞回与冷却、单一副作用门、
``InterfaceTransport`` 的 socket 约束、``campus_remote_app`` 的进程清退实现全部原样复用。

本模块新增的两个能力
--------------------

1. :class:`VerifiedActionGate` —— 既有 ``NetworkOpScheduler`` 的**子类**，在"动作即将执行"
   的位置加一道**验证前置门**：``METRIC_WRITE``（= 改默认出口 = 切换链路）的**目标路径**
   必须有一条**新鲜的**「接口受限验证通过」记录，否则**拒绝执行**并如实说明。
   状态机既有的"执行态必须动作已执行"逻辑会因此停在原状态（不谎报已切换）；下一轮
   验证结果到达后自然放行 —— 这就是方案 §1 的「先证实再切换」。
2. :class:`DualPathRecoveryController` —— 把「本轮状态迁移」翻译成恢复事件、推进协调器、
   产出"该验证哪条路径 / 该不该重启 UU"，并回答"目标路径此刻是否已被证实"。

职责边界
--------

- **零 I/O**：本模块不建 socket、不发请求、不清退进程。验证与清退都由调用方（托盘）
  在自己的线程里用注入能力执行；:meth:`DualPathRecoveryController.tick` /
  :meth:`~DualPathRecoveryController.on_verification` 只做纯逻辑判定（内含一把锁，
  因为托盘会从「监测线程」与「验证线程」两处调用）。
- **fail-closed**：快照不可信 ⇒ 本轮不做任何恢复判定；缺 ifIndex/源地址/该接口 DNS ⇒
  不安排验证；``PROBE_UNRELIABLE`` ⇒ 既不切换也不重启 UU。
- 真值口径：``Connected ≠ Internet``；``verify(Ethernet)`` 只走 Ethernet socket，
  ``verify(WiFi)`` 只走 Wi-Fi socket（由 V0.5-1 保证）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from campus_failover import (FailoverMode, FailoverState, NetworkOpScheduler,
                             OpKind, OpOutcome, PATH_LABEL, TickResult)
from campus_network import HealthState
from campus_recovery import (PATH_ETHERNET, PATH_WIFI, RecoveryCoordinator,
                             RecoveryEvent, RecoveryPlan, RecoveryStep,
                             UuRestartRequest)

__all__ = [
    "CONTRACT_VERSION", "DEFAULT_VERIFICATION_MAX_AGE",
    "DEFAULT_VERIFICATION_RETRY_INTERVAL", "CONNECTED_STATES",
    "VerifiedActionGate", "DualPathTick", "DualPathStatus",
    "DualPathRecoveryController", "path_request_from_snapshot",
]

CONTRACT_VERSION = "v05-4-dual-path-1"

#: 一条「接口受限验证通过」记录的**新鲜度上限**（秒）。超过即视为过期：
#: 路由变更前必须重新验证（宁可暂不切换，也不拿旧证据切换）。
DEFAULT_VERIFICATION_MAX_AGE = 60.0

#: 同一路径"证据不可用"时**重新验证的最小间隔**（秒）。没有它，门禁会每轮都要求
#: 重新验证（探测风暴）；有它，在"想切换但目标路径尚未证实"期间最多每 30s 验证一次
#: —— 既能等到 Portal 认证后转好，也不会把探测打成风暴。
DEFAULT_VERIFICATION_RETRY_INTERVAL = 30.0

#: 「链路已连接」的健康结论（= 有链路、有有效 IP，但**仍不等于**能上公网）。
#: 出现这些结论即说明"连接成功"，必须做一次**接口受限真实验证**（方案 §1 需求 A）。
CONNECTED_STATES = frozenset({
    HealthState.HEALTHY,
    HealthState.CAPTIVE_PORTAL,
    HealthState.INTERNET_UNREACHABLE,
    HealthState.DNS_FAILURE,
})

#: 状态机「切回已完成」以外的、进入 ``ETHERNET_PRIMARY`` 的恢复来源（方案 §30）。
_RECOVERED_INTO_PRIMARY = frozenset({
    FailoverState.ETHERNET_SUSPECT,
    FailoverState.WIFI_SWITCHING,
    FailoverState.BOTH_OFFLINE,
})


def _always_false() -> bool:
    return False


def _flag(value: Any) -> bool:
    """``bool`` 或 ``() -> bool`` 兼容读取（配置可在运行时被用户改档）。"""
    if callable(value):
        try:
            return bool(value())
        except Exception:                                  # noqa: BLE001 - 配置读取失败即视为关闭
            return False
    return bool(value)


def _target_role(request: Any) -> Optional[str]:
    """从 ``OpRequest.payload['target']`` 取目标路径（非法值视为未指定）。"""
    payload = getattr(request, "payload", None)
    target = payload.get("target") if isinstance(payload, dict) else None
    return target if target in (PATH_ETHERNET, PATH_WIFI) else None


def _axis_state(snapshot: Any, role: str) -> Optional[HealthState]:
    health = (getattr(snapshot, "ethernet", None) if role == PATH_ETHERNET
              else getattr(snapshot, "wifi", None))
    return getattr(health, "state", None) if health is not None else None


def _if_index(snapshot: Any, role: str) -> Optional[int]:
    health = (getattr(snapshot, "ethernet", None) if role == PATH_ETHERNET
              else getattr(snapshot, "wifi", None))
    value = getattr(health, "if_index", None) if health is not None else None
    return value if isinstance(value, int) else None


def _egress_if_index(snapshot: Any) -> Optional[int]:
    system = getattr(snapshot, "system", None)
    egress = getattr(system, "egress", None) if system is not None else None
    value = getattr(egress, "if_index", None) if egress is not None else None
    return value if isinstance(value, int) else None


def path_request_from_snapshot(snapshot: Any, role: str, *,
                               generation: int = 0) -> Optional[Any]:
    """由三轴快照构造**接口受限**验证请求（V0.5-4）。

    缺 ifIndex / 源地址 / **该接口自己的 DNS** 时返回 ``None``（调用方 fail-closed：
    既不验证、也不做任何破坏性动作）。**绝不**回退到系统全局解析器或默认出口
    （方案 §4「验证必须绑定接口」）。

    ``ssid`` 不由快照承载（快照只带网卡别名），因此恒为 ``None``；它只用于展示，
    不参与接口约束。
    """
    if role == PATH_ETHERNET:
        health = getattr(snapshot, "ethernet", None)
    elif role == PATH_WIFI:
        health = getattr(snapshot, "wifi", None)
    else:
        return None                      # 未知路径：fail-closed，绝不猜接口
    if health is None:
        return None
    if_index = getattr(health, "if_index", None)
    source_ip = str(getattr(health, "source_ip", "") or "")
    dns = tuple(str(value) for value in
                (getattr(health, "dns_servers", None) or ()))
    if not isinstance(if_index, int) or not source_ip or not dns:
        return None
    from campus_reconnect import PathVerificationRequest   # 延迟 import：避免循环依赖

    return PathVerificationRequest(
        role=role, interface_index=if_index, source_ip=source_ip,
        interface_alias=str(getattr(health, "interface", "") or ""),
        network_generation=int(generation), dns_servers=dns)


# --------------------------------------------------------------------------- #
# 单一副作用门 + 验证前置门
# --------------------------------------------------------------------------- #
class VerifiedActionGate(NetworkOpScheduler):
    """单一副作用门 + **验证前置门**（V0.5-4）。

    只加一道门，不改既有门禁（模式 / 互斥串行 / 无执行器语义全部沿用父类）：

    - ``METRIC_WRITE``（默认出口与 Metric 变更 = 链路切换）的**目标路径**必须有一条
      **新鲜的**、``allows_recovery`` 为真的接口受限验证记录，否则**拒绝执行**；
    - 其余动作（Portal POST / Wi-Fi 重连 / Clash 触碰）原样交回既有门禁 —— 它们
      恰恰是**用来把网络修好**的，若也要求"先上网"会形成死锁；它们的前置条件仍是
      既有规则（AUTO + 授权 + 快照可信 + 互斥串行）；
    - 拒绝时**不执行、不谎报**：``OpOutcome(executed=False)``，理由放在 ``detail``
      （状态机据此停在上一个状态，下一轮再试）。
    """

    #: 被验证前置门拦下的动作类型（方案 §1 需求 3：路由切换的前置条件）。
    GATED_KINDS = (OpKind.METRIC_WRITE,)

    def __init__(self, mode: FailoverMode = FailoverMode.MONITOR,
                 executor: Optional[Any] = None, *,
                 max_age: float = DEFAULT_VERIFICATION_MAX_AGE,
                 clock: Optional[Callable[[], float]] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(mode, executor)
        self.max_age = float(max_age)
        self._clock = clock or time.monotonic
        self._log = log
        #: 路径 → (记录时刻, PathVerificationResult)：**完整**验证证据的唯一台账。
        self._verified: Dict[str, Tuple[float, Any]] = {}
        #: 被前置门拦下的 ``(请求, 理由)``，供展示与审计（与既有 ``rejected`` 并存）。
        self.gate_blocked: List[Tuple[Any, str]] = []

    # ---- 证据台账 ---- #
    def note_verification(self, result: Any, now: Optional[float] = None
                          ) -> Optional[str]:
        """登记一次**完整**接口受限验证的结果；返回登记的路径（未识别则 ``None``）。

        轻量闸门确认（``PathVerifier.verify_gate``）的结果**不得**登记到此处：
        它没跑交叉站点，不足以作为切换依据（见 V0.5-1 边界）。
        """
        role = str(getattr(result, "role", "") or "")
        if role not in (PATH_ETHERNET, PATH_WIFI):
            return None
        at = self._clock() if now is None else float(now)
        self._verified[role] = (at, result)
        return role

    def evidence(self, role: str) -> Optional[Tuple[float, Any]]:
        """该路径最近一次验证记录（**不论是否过期**；UI / 日志用）。"""
        return self._verified.get(role)

    def verified_for(self, role: str, now: Optional[float] = None) -> Optional[Any]:
        """返回该路径**新鲜的**验证结果（无记录 / 已过期 ⇒ ``None``）。"""
        record = self._verified.get(role)
        if record is None:
            return None
        at, result = record
        moment = self._clock() if now is None else float(now)
        if moment - at > self.max_age:
            return None
        return result

    def needs_reverification(self, role: str, now: Optional[float] = None) -> bool:
        """该路径的现有证据**不足以放行路由变更**吗（无记录 / 过期 / 上次未通过）。

        只读判定，供整合层决定"是否再安排一次接口受限验证"。注意：返回 ``True``
        只代表"证据不可用"，不代表请求一定放行 —— 验证结果仍可能是未通过。
        """
        record = self._verified.get(role)
        if record is None:
            return True
        at, result = record
        moment = self._clock() if now is None else float(now)
        if moment - at > self.max_age:
            return True
        return not bool(getattr(result, "allows_recovery", False))

    # ---- 门禁 ---- #
    def block_reason(self, request: Any, now: Optional[float] = None
                     ) -> Optional[str]:
        """该请求是否被验证前置门拦下；``None`` = 放行（交给父类门禁）。"""
        if getattr(request, "kind", None) not in self.GATED_KINDS:
            return None
        role = _target_role(request)
        if role is None:
            return ("路由变更未指定目标路径：不执行任何路由变更"
                    "（无法证明目标接口真的能上网）")
        label = PATH_LABEL.get(role, role)
        record = self._verified.get(role)
        moment = self._clock() if now is None else float(now)
        if record is None:
            return ("目标路径 %s 尚无「接口受限验证通过」记录：先证实再切换"
                    % label)
        at, result = record
        elapsed = moment - at
        if elapsed > self.max_age:
            return ("目标路径 %s 的验证已过期（%.0fs > %.0fs）：先重新验证再切换"
                    % (label, elapsed, self.max_age))
        if not bool(getattr(result, "allows_recovery", False)):
            return ("目标路径 %s 验证未通过（%s）：不执行路由变更"
                    % (label, getattr(result, "reason", "") or "未验证"))
        return None

    def submit(self, request: Any) -> OpOutcome:
        """先过验证前置门；放行则完全交给父类（单一副作用门）语义。"""
        reason = self.block_reason(request)
        if reason is None:
            return super().submit(request)
        # 本轮的动作名额被这次"计划但未执行"占用（互斥串行语义不变）。
        self.plan.append(request)
        self.gate_blocked.append((request, reason))
        self.rejected.append((request, reason))
        if self._log is not None:
            self._log("验证前置门拦下 %s：%s" % (request.kind.value, reason))
        return OpOutcome(request.kind, gate_passed=True, attempted=False,
                         executed=False, detail=reason)


# --------------------------------------------------------------------------- #
# 一次推进的结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DualPathTick:
    """一次整合层推进的结果（纯数据；不含任何副作用）。"""

    at: float = 0.0
    network_epoch: int = 0
    active_path: Optional[str] = None
    standby_path: Optional[str] = None
    state: Any = None
    previous_state: Any = None
    trusted: bool = True
    event: Optional[RecoveryEvent] = None
    #: 本轮要求调用方执行的**接口受限验证请求**（``PathVerificationRequest``）。
    #: 两条链路可能同一轮都需要验证（例如"活动链路刚恢复 + 备用链路刚连上"）。
    verify_requests: Tuple[Any, ...] = ()
    verification: Optional[Any] = None        # PathVerificationResult
    uu_request: Optional[UuRestartRequest] = None
    uu_enabled: bool = False
    recovery: Optional[RecoveryPlan] = None
    steps: Tuple[RecoveryStep, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def verify_roles(self) -> Tuple[str, ...]:
        return tuple(str(getattr(request, "role", "") or "")
                     for request in self.verify_requests)

    @property
    def needs_verification(self) -> bool:
        """是否要求调用方立刻跑接口受限验证（``PROBE_UNRELIABLE`` 不算）。"""
        return bool(self.verify_requests)

    @property
    def schedules_uu_restart(self) -> bool:
        """是否**已经**安排一次 UU 重启（请求 + 用户已开启自动恢复）。"""
        return self.uu_request is not None and self.uu_enabled

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at, "network_epoch": self.network_epoch,
            "active_path": self.active_path, "standby_path": self.standby_path,
            "state": getattr(self.state, "value", None),
            "previous_state": getattr(self.previous_state, "value", None),
            "trusted": self.trusted,
            "event": self.event.value if self.event else None,
            "verify_roles": list(self.verify_roles),
            "verified": (getattr(self.verification, "allows_recovery", None)
                         if self.verification is not None else None),
            "uu_restart": self.uu_request.to_dict() if self.uu_request else None,
            "uu_enabled": self.uu_enabled,
            "steps": [step.value for step in self.steps],
            "notes": list(self.notes),
        }

    def lines(self) -> List[str]:
        """人类可读要点（托盘 / 日志 / 后续 UI 用）。"""
        out = ["恢复整合（epoch=%d，活动路径 %s，备用 %s）"
               % (self.network_epoch, self.active_path or "未知",
                  self.standby_path or "未知")]
        if self.state is not None:
            out.append("状态机：%s" % getattr(self.state, "value", self.state))
        if self.event is not None:
            out.append("恢复事件：%s" % self.event.value)
        if self.verify_requests:
            out.append("接口受限验证：%s" % "、".join(self.verify_roles))
        if self.uu_request is not None:
            out.append("UU 重启：%s（%s）"
                       % ("已开启" if self.uu_enabled else "未开启（默认关闭）",
                          self.uu_request.reason))
        return out


@dataclass(frozen=True)
class DualPathStatus:
    """整合层的只读快照（UI / 托盘 tooltip 用）。"""

    contract_version: str = CONTRACT_VERSION
    network_epoch: int = 0
    active_path: Optional[str] = None
    standby_path: Optional[str] = None
    state: Any = None
    event: Optional[RecoveryEvent] = None
    steps: Tuple[RecoveryStep, ...] = ()
    ethernet: Optional[Tuple[float, Any]] = None
    wifi: Optional[Tuple[float, Any]] = None
    uu_enabled: bool = False
    last_uu_ok: Optional[bool] = None
    verification_requests: int = 0
    verifications: int = 0
    notes: Tuple[str, ...] = ()

    def _path_text(self, label: str, record: Optional[Tuple[float, Any]],
                   now: float) -> str:
        if record is None:
            return "%s：尚未验证" % label
        at, result = record
        verdict = ("接口受限验证通过"
                   if getattr(result, "allows_recovery", False) else "未通过")
        return "%s：%s（%.0fs 前；%s）" % (
            label, verdict, max(0.0, now - at),
            getattr(result, "reason", "") or "")

    def lines(self, *, now: Optional[float] = None) -> List[str]:
        moment = time.monotonic() if now is None else float(now)
        out = ["V0.5 恢复整合（epoch=%d）" % self.network_epoch,
               "活动路径：%s；备用路径：%s"
               % (self.active_path or "未知", self.standby_path or "未知")]
        out.append(self._path_text("以太网", self.ethernet, moment))
        out.append(self._path_text("Wi-Fi", self.wifi, moment))
        if self.event is not None:
            out.append("最近恢复事件：%s" % self.event.value)
        if self.steps:
            out.append("恢复阶段：%s" % " → ".join(s.value for s in self.steps))
        out.append("UU 自动恢复：%s%s"
                   % ("已开启" if self.uu_enabled else "未开启（默认关闭）",
                      "" if self.last_uu_ok is None
                      else ("；最近一次%s" % ("成功" if self.last_uu_ok else "失败"))))
        return out


# --------------------------------------------------------------------------- #
# 整合控制器
# --------------------------------------------------------------------------- #
class DualPathRecoveryController:
    """把状态机迁移接成「恢复事件 → 接口受限验证 → UU 恢复」的整合层（V0.5-4）。

    典型接线（托盘）::

        controller = DualPathRecoveryController(
            verifier=path_verifier.verify,          # 完整验证（闸门 + 交叉 ≥2）
            gate_verifier=path_verifier.verify_gate,  # 轻量闸门确认（kill 前再确认）
            uu_restart_enabled=lambda: cfg["remote_restart_enabled"],
            log=...)
        bridge = TrayFailoverBridge(..., action_gate=controller.gate)

        result = bridge.tick(now, snapshot)          # 既有状态机（不重写）
        tick = controller.tick(result, snapshot, trusted=observation.trusted, now=now)
        for request in tick.verify_requests:         # 后台线程里执行：
            verification = path_verifier.verify(request)
            follow = controller.on_verification(verification, now=now2)
            if follow.schedules_uu_restart:
                restart_after_verified_recovery(..., verification=verification,
                    recheck=controller.build_recheck(follow.uu_request.active_path))

    ``verifier`` / ``gate_verifier`` 是**调用方**的能力（真实实现含 I/O）；
    本类自己**零 I/O**，因此可以完整离线测试。
    """

    def __init__(self, *, coordinator: Optional[RecoveryCoordinator] = None,
                 verifier: Optional[Callable[[Any], Any]] = None,
                 gate_verifier: Optional[Callable[[Any], Any]] = None,
                 gate: Optional[VerifiedActionGate] = None,
                 uu_restart_enabled: Any = _always_false,
                 clock: Optional[Callable[[], float]] = None,
                 verification_max_age: float = DEFAULT_VERIFICATION_MAX_AGE,
                 verification_retry_interval: float = DEFAULT_VERIFICATION_RETRY_INTERVAL,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.coordinator = coordinator or RecoveryCoordinator()
        #: 时钟先于门禁设定：**自动**创建的门禁必须与整合层共用同一时钟，否则
        #: 「验证记录时刻」与「门禁判新鲜度时刻」会来自两个时间源（真机都是
        #: ``time.monotonic`` 无碍；虚拟时钟测试里会出现整段错位）。
        self._clock = clock or time.monotonic
        self.gate = (gate if gate is not None
                     else VerifiedActionGate(clock=self._clock))
        self.gate.max_age = float(verification_max_age)
        self.verification_retry_interval = float(verification_retry_interval)
        self._verify = verifier
        self._verify_gate = gate_verifier
        self.uu_restart_enabled = uu_restart_enabled
        self._log = log
        #: 托盘会从「监测线程」与「验证线程」两处调用 ⇒ 用锁保护纯逻辑状态。
        self._lock = threading.RLock()
        self.network_epoch = 0
        self.active_path: Optional[str] = None
        self.standby_path: Optional[str] = None
        self._signature: Optional[Tuple[Any, ...]] = None
        self._axis_states: Dict[str, HealthState] = {}
        #: 在途验证：路径 → 发起时刻（防止同一路径并发重复验证）。
        self._pending: Dict[str, float] = {}
        #: 最近一次**发起**验证的时刻：路径 → 时刻（限速用，见 ``_should_reverify``）。
        self._attempted: Dict[str, float] = {}
        self._last_event: Optional[RecoveryEvent] = None
        self._last_snapshot: Any = None
        self._last_tick: Optional[DualPathTick] = None
        self.verification_requests = 0
        self.verifications = 0

    # ---- 只读视图 ---- #
    @property
    def last_snapshot(self) -> Any:
        return self._last_snapshot

    @property
    def last_tick(self) -> Optional[DualPathTick]:
        return self._last_tick

    def status(self, *, now: Optional[float] = None) -> DualPathStatus:
        """只读状态快照（UI / tooltip）。"""
        with self._lock:
            tick = self._last_tick
            return DualPathStatus(
                network_epoch=self.network_epoch, active_path=self.active_path,
                standby_path=self.standby_path,
                state=getattr(tick, "state", None), event=self._last_event,
                steps=getattr(tick, "steps", ()),
                ethernet=self.gate.evidence(PATH_ETHERNET),
                wifi=self.gate.evidence(PATH_WIFI),
                uu_enabled=_flag(self.uu_restart_enabled),
                last_uu_ok=self.coordinator.last_uu_restart_ok,
                verification_requests=self.verification_requests,
                verifications=self.verifications)

    def verification_request(self, role: Optional[str], *,
                             now: Optional[float] = None) -> Optional[Any]:
        """按**最近一次快照**为 ``role`` 构造接口受限验证请求（缺要素 ⇒ ``None``）。"""
        del now                                            # 接口一致性的占位参数
        if role not in (PATH_ETHERNET, PATH_WIFI):
            return None
        with self._lock:
            snapshot = self._last_snapshot
            generation = self.network_epoch
        if snapshot is None:
            return None
        return path_request_from_snapshot(snapshot, role, generation=generation)

    def build_recheck(self, role: Optional[str]) -> Callable[[], Tuple[bool, str]]:
        """构造「清退前轻量再确认」：活动路径仍可用 + 百度闸门仍 PASS（方案 §5.4）。

        返回的回调**自带 I/O**（走接口受限通道再做一次闸门），因此只能在执行清退的
        线程里调用（托盘放在后台线程）。未接线轻量能力 / 缺接口要素 ⇒ 一律"不通过"
        （fail-closed：网络可能刚断，不清退）。
        """
        def _recheck() -> Tuple[bool, str]:
            request = self.verification_request(role)
            if request is None:
                return False, "无法构造接口受限请求（缺 ifIndex/源地址/该接口 DNS）"
            if not callable(self._verify_gate):
                return False, "未接线轻量再确认能力（不假定刚刚的验证仍成立）"
            try:
                result = self._verify_gate(request)
            except Exception as exc:                       # noqa: BLE001 - 再确认不可信
                return False, "轻量再确认异常（%s）" % type(exc).__name__
            if bool(getattr(result, "allows_recovery", False)):
                return True, "活动路径仍可用（接口受限轻量确认：%s）" % (
                    getattr(result, "reason", "") or "闸门 PASS")
            return False, str(getattr(result, "reason", "")
                              or getattr(result, "reliability", "") or "闸门未通过")

        return _recheck

    # ---- 主流程（零 I/O） ---- #
    def tick(self, result: TickResult, snapshot: Any, *,
             trusted: bool = True, now: Optional[float] = None) -> DualPathTick:
        """推进一次整合判定；``result`` 为既有状态机本轮的 :class:`TickResult`。

        本方法**不推进**状态机（状态机由 ``TrayFailoverBridge.tick`` 推进），只把它
        的迁移翻译成恢复事件，并回答"接下来该验证哪条路径 / 该不该重启 UU"。
        """
        moment = float(getattr(result, "at", 0.0)) if now is None else float(now)
        with self._lock:
            notes: List[str] = []
            self._last_snapshot = snapshot
            if not trusted:
                notes.append("三轴快照不可信：本轮不做任何恢复判定"
                             "（不安排验证、不切换、不重启 UU）")
                return self._finish(DualPathTick(
                    at=moment, network_epoch=self.network_epoch,
                    active_path=self.active_path, standby_path=self.standby_path,
                    state=getattr(result, "state", None),
                    previous_state=getattr(result, "previous_state", None),
                    trusted=False, notes=tuple(notes)))

            self._sync_network(result, snapshot, notes)
            events = self._events_for(result, snapshot)
            plans: List[Tuple[RecoveryEvent, str, RecoveryPlan]] = []
            for event, role in events:
                plan = self.coordinator.on_event(event, now=moment, path=role)
                notes.extend(plan.notes)
                plans.append((event, role, plan))
            #: 展示/UI 用「最有证据」的那一个事件（顺序即优先级：切换/切回 > 活动恢复 > 备用）。
            primary = plans[0] if plans else None
            event = primary[0] if primary is not None else None
            plan = primary[2] if primary is not None else None
            if event is not None:
                self._last_event = event

            requests = self._choose_verifications(moment, result, plans, notes)
            self._remember_axis_states(snapshot)

            return self._finish(DualPathTick(
                at=moment, network_epoch=self.network_epoch,
                active_path=self.active_path, standby_path=self.standby_path,
                state=getattr(result, "state", None),
                previous_state=getattr(result, "previous_state", None),
                trusted=True, event=event, verify_requests=requests,
                recovery=plan, steps=(plan.steps if plan is not None else ()),
                notes=tuple(notes)))

    def on_verification(self, verification: Any, *,
                        now: Optional[float] = None) -> DualPathTick:
        """消费一次**完整**接口受限验证结果：登记证据 + 推进协调器（零 I/O）。"""
        moment = self._clock() if now is None else float(now)
        with self._lock:
            notes: List[str] = []
            role = self.gate.note_verification(verification, now=moment)
            if role is not None:
                self._pending.pop(role, None)
            self.verifications += 1
            allowed = bool(getattr(verification, "allows_recovery", False))
            notes.append("接口受限验证（%s）：%s"
                         % (PATH_LABEL.get(role or "", role or "未指定路径"),
                            getattr(verification, "reason", "") or "无结论"))
            if not allowed:
                notes.append("未通过 ⇒ 不切换、不重启 UU（fail-closed）")
            plan = self.coordinator.on_verification(verification, now=moment)
            notes.extend(plan.notes)
            enabled = _flag(self.uu_restart_enabled)
            if plan.uu_restart is not None and not enabled:
                notes.append("UU 自动恢复未开启（remote_restart_enabled=False）："
                             "只登记、不清退")
            return self._finish(DualPathTick(
                at=moment, network_epoch=self.network_epoch,
                active_path=self.active_path, standby_path=self.standby_path,
                event=self._last_event, verification=verification,
                uu_request=plan.uu_restart, uu_enabled=enabled, recovery=plan,
                steps=plan.steps, notes=tuple(notes)))

    def note_uu_outcome(self, ok: bool, *, detail: str = "",
                        now: Optional[float] = None) -> None:
        """回报一次 UU 重启的**实际结局**（只用于状态展示与日志）。"""
        del now                                            # 冷却语义由 V0.5-3 台账承载
        with self._lock:
            self.coordinator.note_uu_restart_outcome(bool(ok), detail=detail)

    # ---- 内部：世代与事件 ---- #
    def _finish(self, tick: DualPathTick) -> DualPathTick:
        self._last_tick = tick
        if self._log is not None:
            for note in tick.notes:
                self._log(note)
        return tick

    def _sync_network(self, result: TickResult, snapshot: Any,
                      notes: List[str]) -> None:
        """同步活动路径 / 备用路径 / 网络世代（接口、源地址或活动出口变化即 +1）。"""
        active = self._active_role(result, snapshot)
        standby = None
        if active == PATH_ETHERNET:
            standby = PATH_WIFI
        elif active == PATH_WIFI:
            standby = PATH_ETHERNET
        signature = (
            _if_index(snapshot, PATH_ETHERNET),
            str(getattr(getattr(snapshot, "ethernet", None), "source_ip", "") or ""),
            _if_index(snapshot, PATH_WIFI),
            str(getattr(getattr(snapshot, "wifi", None), "source_ip", "") or ""),
            _egress_if_index(snapshot), active)
        if signature != self._signature:
            self._signature = signature
            self.network_epoch += 1
            if self.network_epoch > 1:
                notes.append("网络世代 +1（epoch=%d）：接口 / 源地址 / 活动出口发生变化"
                             % self.network_epoch)
        self.active_path = active
        self.standby_path = standby
        self.coordinator.update_network(network_epoch=self.network_epoch,
                                        active_path=active, standby_path=standby)

    def _remember_axis_states(self, snapshot: Any) -> None:
        """记录本轮两条链路的健康结论，作为**下一轮**的"之前状态"。

        必须在 :meth:`_events_for` **之后**调用：否则"之前状态"会被本轮观测覆盖，
        永远看不到迁移（P0-T6 三表分离式陷阱）。
        """
        for role in (PATH_ETHERNET, PATH_WIFI):
            state = _axis_state(snapshot, role)
            if state is not None:
                self._axis_states[role] = state

    def _active_role(self, result: TickResult, snapshot: Any) -> Optional[str]:
        """活动路径 = Windows **实际**出口（入口证实）优先，其次状态机 preferred。"""
        egress_if = _egress_if_index(snapshot)
        if egress_if is not None:
            for role in (PATH_ETHERNET, PATH_WIFI):
                if _if_index(snapshot, role) == egress_if:
                    return role
        preferred = getattr(result, "preferred", None)
        return preferred if preferred in (PATH_ETHERNET, PATH_WIFI) else None

    def _events_for(self, result: TickResult, snapshot: Any
                    ) -> List[Tuple[RecoveryEvent, str]]:
        """把本轮迁移翻译成恢复事件（可能两条链路各一个，按证据强度排序）。

        规则（方案 §30）：

        - **切换已落地**：``WIFI_SWITCHING → WIFI_BACKUP_ACTIVE``（该迁移本身就要求
          "出口已变成 Wi-Fi"才可能发生）⇒ ``FAILOVER_COMPLETED``；
        - **切回已落地**：``ETHERNET_FAILBACK → ETHERNET_PRIMARY`` ⇒
          ``FAILBACK_COMPLETED``；两者**才**授权重启 UU；
        - **活动路径恢复**：``ETHERNET_SUSPECT / WIFI_SWITCHING / BOTH_OFFLINE →
          ETHERNET_PRIMARY``，或活动链路的健康结论由"未连接"变为"已连接"；
        - **备用链路连接成功**：非活动链路由"未连接"变为"已连接" ⇒ 只验证，不重启 UU；
        - **首次观测**（没有"之前状态"）：只有"连接成功"、没有"恢复"证据 ⇒ 一律按
          备用链路处理（只验证），避免"启动即重启 UU"。
        """
        previous = getattr(result, "previous_state", None)
        state = getattr(result, "state", None)
        events: List[Tuple[RecoveryEvent, str]] = []
        if state is FailoverState.WIFI_BACKUP_ACTIVE \
                and previous is FailoverState.WIFI_SWITCHING:
            events.append((RecoveryEvent.FAILOVER_COMPLETED, PATH_WIFI))
        if state is FailoverState.ETHERNET_PRIMARY \
                and previous is FailoverState.ETHERNET_FAILBACK:
            events.append((RecoveryEvent.FAILBACK_COMPLETED, PATH_ETHERNET))
        if state is FailoverState.ETHERNET_PRIMARY \
                and previous in _RECOVERED_INTO_PRIMARY:
            events.append((RecoveryEvent.ACTIVE_PATH_RECOVERED, PATH_ETHERNET))

        covered = {role for _, role in events}
        for role in (PATH_ETHERNET, PATH_WIFI):
            if role in covered:
                continue
            current = _axis_state(snapshot, role)
            if current not in CONNECTED_STATES:
                continue
            before = self._axis_states.get(role)
            if before in CONNECTED_STATES:
                continue
            if role == self.active_path and before is not None:
                events.append((RecoveryEvent.ACTIVE_PATH_RECOVERED, role))
            else:
                events.append((RecoveryEvent.STANDBY_CONNECTED, role))
        return events

    def _choose_verifications(self, moment: float, result: TickResult,
                              plans: List[Tuple[RecoveryEvent, str, RecoveryPlan]],
                              notes: List[str]) -> Tuple[Any, ...]:
        """决定本轮要安排哪些接口受限验证（0 个、1 个或两条链路各一个）。

        两个入口，都只在**证据不足**时才真正发起：

        1. **恢复事件要求**（协调器计划 ``needs_verification``）—— 事件驱动；
        2. **验证前置门会拦下本轮的切换**（目标路径证据不可用）—— 动作驱动，
           受 :attr:`verification_retry_interval` 限速，避免探测风暴。
        """
        wanted: List[Tuple[str, str]] = []
        for event, role, plan in plans:
            if plan.needs_verification and role in (PATH_ETHERNET, PATH_WIFI):
                wanted.append((role, plan.verify_reason or event.value))
        target = self._pending_switch_target(result)
        if target is not None and all(role != target for role, _ in wanted) \
                and self._should_reverify(target, moment):
            wanted.append((target, "切换前必须先证实目标路径能直连上网"
                                   "（验证前置门）"))

        requests: List[Any] = []
        for role, reason in wanted:
            if role in self._pending:
                notes.append("接口受限验证已在途：%s（%s）"
                             % (PATH_LABEL.get(role, role), reason))
                continue
            request = path_request_from_snapshot(self._last_snapshot, role,
                                                 generation=self.network_epoch)
            if request is None:
                notes.append("无法构造 %s 的接口受限验证请求"
                             "（缺 ifIndex / 源地址 / 该接口 DNS）：fail-closed，不验证"
                             % PATH_LABEL.get(role, role))
                continue
            self._pending[role] = moment
            self._attempted[role] = moment
            self.verification_requests += 1
            requests.append(request)
            notes.append("安排接口受限验证：%s（%s）"
                         % (PATH_LABEL.get(role, role), reason))
        return tuple(requests)

    def _should_reverify(self, role: str, moment: float) -> bool:
        """该路径是否需要（并且现在允许）重新验证：证据不可用 + 过限速窗口。"""
        if role in self._pending:
            return False
        if not self.gate.needs_reverification(role, now=moment):
            return False
        last = self._attempted.get(role)
        return last is None or (moment - last) >= self.verification_retry_interval

    def _pending_switch_target(self, result: TickResult) -> Optional[str]:
        """本轮状态机**想切换**到的目标路径（有 ``METRIC_WRITE`` 计划才非空）。"""
        for request in getattr(result, "plan", ()) or ():
            if getattr(request, "kind", None) is OpKind.METRIC_WRITE:
                role = _target_role(request)
                if role is not None:
                    return role
        return None
