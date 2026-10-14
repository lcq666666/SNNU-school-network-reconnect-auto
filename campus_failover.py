# -*- coding: utf-8 -*-
"""V0.3 P3 故障切换状态机与只读监测（campus_failover）。

本模块实现方案 §5 的**全局状态机**（11 个状态）、§4 的滞回/冷却策略，以及
执行计划 §0.4 的**单一副作用门**（``NetworkOpScheduler``）。

职责边界（V0.3 专项规则 2「职责分离」）：

- 本模块**只做决策与状态迁移**：不切换路由、不发 Portal 认证、不写 Metric、
  不触碰 Clash、不重连 Wi-Fi。所有动作先经 :class:`NetworkOpScheduler` 门禁，
  且只有在 :attr:`FailoverMode.AUTO` **且**注入了执行器时才真正执行；
- P3 不提供真实执行器 —— 真实路由/Metric 执行器由 P5 的
  ``campus_route_guard`` 实现。P3 只冻结**执行器协议**（``executor(request)``
  → ``True``/``None`` 成功、``False``/``str`` 失败、``OpOutcome`` 完整结局），
  并用离线假件（``tests.fakes.FakeOpExecutor``）覆盖全部 11 个状态；
- 只读监测模式（:attr:`FailoverMode.MONITOR`，P3 默认）下**任何动作都不执行**，
  需要执行才能进入的状态（``WIFI_AUTHENTICATING`` / ``WIFI_SWITCHING`` /
  ``ETHERNET_FAILBACK``）不进入，仅通过 :attr:`TickResult.predicted_state`
  报告"如果允许切换将进入哪个状态"（对应方案 §12 P1 的"只读监测"语义）。

硬约束（AGENTS §3 / 方案 §3.3、§4.3、§6）：

1. ``UNKNOWN`` / ``PROBE_UNRELIABLE`` 与单次探测失败**绝不**触发破坏性动作；
2. 主链路故障需**连续 3 次可靠失败**才进入疑似异常；
3. Portal 认证冷却（≥180s）只限制认证，网卡切换冷却（≥300s）只限制路由变更，
   二者都由 :class:`campus_core.TimerTable` 承载；**任何冷却都不得推迟网络检测**；
4. 有线恢复判据 = 连续稳定 ≥120s 且 ≥5 次成功探测，且距上次切换 ≥300s；
5. 一轮检测最多安排 **1 个**副作用动作（四类动作互斥串行）；
6. 双断时不在两张网卡之间来回切换，恢复尝试按冷却限频。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from campus_core import TimerTable
from campus_network import HealthState, NetworkHealthSnapshot

__all__ = [
    "CONTRACT_VERSION", "RELIABLE_FAILURE_STATES", "UNRELIABLE_STATES",
    "CAPTIVE_STATES", "EXECUTION_STATES", "LOCK_TARGETS", "PATH_LABEL",
    "FailoverState", "FailoverMode",
    "OpKind", "FailoverPolicy", "OpRequest", "OpOutcome",
    "NetworkOpScheduler", "TickResult", "NetworkFailoverManager",
    "format_tick",
]

CONTRACT_VERSION = "v03-p3-failover-1"

#: 手动锁定（``FailoverMode.LOCKED``）可选的优先路径（方案 §11「手动锁定」）。
LOCK_TARGETS = ("ethernet", "wifi")

#: 路径中文名（托盘/CLI 展示用）。
PATH_LABEL = {"ethernet": "以太网", "wifi": "Wi-Fi"}

#: 「可靠失败」：可作为连续失败计数与切换依据的健康结论（方案 §3.3）。
RELIABLE_FAILURE_STATES = frozenset({
    HealthState.LINK_DOWN,
    HealthState.NO_IP,
    HealthState.GATEWAY_UNREACHABLE,
    HealthState.INTERNET_UNREACHABLE,
    HealthState.DNS_FAILURE,
})

#: 「不可判定」：绝不触发任何动作，也不计入失败计数（方案 §3.3）。
UNRELIABLE_STATES = frozenset({
    HealthState.UNKNOWN,
    HealthState.PROBE_UNRELIABLE,
})

#: 「需要认证」：路径已连通但被 Portal 拦截，尚不可用于公网。
CAPTIVE_STATES = frozenset({HealthState.CAPTIVE_PORTAL})


class FailoverState(Enum):
    """方案 §5 的 11 个全局状态。"""

    ETHERNET_PRIMARY = "ETHERNET_PRIMARY"
    ETHERNET_SUSPECT = "ETHERNET_SUSPECT"
    WIFI_PREPARING = "WIFI_PREPARING"
    WIFI_AUTHENTICATING = "WIFI_AUTHENTICATING"
    WIFI_SWITCHING = "WIFI_SWITCHING"
    WIFI_BACKUP_ACTIVE = "WIFI_BACKUP_ACTIVE"
    ETHERNET_RECOVERY_CHECK = "ETHERNET_RECOVERY_CHECK"
    ETHERNET_FAILBACK = "ETHERNET_FAILBACK"
    BOTH_OFFLINE = "BOTH_OFFLINE"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    PAUSED = "PAUSED"

    @property
    def is_execution(self) -> bool:
        """该状态是否必须由"动作已执行"进入（只读监测下不可达）。"""
        return self in EXECUTION_STATES

    @property
    def preferred(self) -> Optional[str]:
        """该状态下的优先出口（``ethernet`` / ``wifi`` / ``None``=无可用路径）。"""
        return _STATE_PREFERRED.get(self)


class FailoverMode(Enum):
    """运行模式（方案 §11 三模式；P3 只启用 MONITOR）。

    - :attr:`MONITOR`：**只读监测** —— 观察双网络状态，不执行任何动作（P3 默认）；
    - :attr:`AUTO`：智能自动 —— 允许执行已批准的动作（真实执行层由 P5 接入）；
    - :attr:`LOCKED`：手动锁定 —— 与 MONITOR 同样**不执行任何动作**；P6 接入
      锁定语义：``lock_target`` 非空时优先路径固定为该目标，仍照常显示健康告警，
      且**不自动禁用**另一张网卡（方案 §11）。
    """

    MONITOR = "MONITOR"
    AUTO = "AUTO"
    LOCKED = "LOCKED"

    @property
    def executes(self) -> bool:
        """该模式是否允许执行动作（P3 仅 AUTO 允许，且仍需执行器在位）。"""
        return self is FailoverMode.AUTO

    @property
    def label(self) -> str:
        """方案 §11 的中文模式名（托盘与 CLI 展示用）。"""
        return _MODE_LABEL[self]


class OpKind(Enum):
    """方案 §5 末段列举的四类副作用动作（互斥串行）。"""

    WIFI_RECONNECT = "wifi_reconnect"
    PORTAL_POST = "portal_post"
    METRIC_WRITE = "metric_write"
    CLASH_TOUCH = "clash_touch"


#: 必须"动作已执行"才能进入的状态（只读监测下不可达，仅作预测上报）。
EXECUTION_STATES = frozenset({
    FailoverState.WIFI_AUTHENTICATING,
    FailoverState.WIFI_SWITCHING,
    FailoverState.ETHERNET_FAILBACK,
})

_MODE_LABEL: Dict[FailoverMode, str] = {
    FailoverMode.MONITOR: "只读监测",
    FailoverMode.AUTO: "智能自动",
    FailoverMode.LOCKED: "手动锁定",
}

_STATE_PREFERRED: Dict[FailoverState, Optional[str]] = {
    FailoverState.ETHERNET_PRIMARY: "ethernet",
    FailoverState.ETHERNET_SUSPECT: "ethernet",
    FailoverState.ETHERNET_RECOVERY_CHECK: "ethernet",
    FailoverState.ETHERNET_FAILBACK: "ethernet",
    FailoverState.WIFI_PREPARING: "wifi",
    FailoverState.WIFI_AUTHENTICATING: "wifi",
    FailoverState.WIFI_SWITCHING: "wifi",
    FailoverState.WIFI_BACKUP_ACTIVE: "wifi",
    FailoverState.BOTH_OFFLINE: None,
    FailoverState.MANUAL_REQUIRED: None,
    FailoverState.PAUSED: None,
}


# --------------------------------------------------------------------------- #
# 策略：滞回与冷却参数（AGENTS §3.8 建议值，可按验证结果调整并记录理由）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FailoverPolicy:
    """切换/恢复策略参数。

    默认值取 AGENTS §3.8 与方案 §4.1/§4.3/§6.3 的建议值；``validate()`` 只给
    出**告警**而不阻断（文档明确这些是"建议值，可按验证结果调整"）。
    """

    detection_interval: float = 15.0        # 网络检测间隔（建议 5–15s）
    failure_threshold: int = 3              # 主链路故障：连续可靠失败次数
    auth_cooldown: float = 180.0            # Portal 认证冷却（只限制认证）
    switch_min_interval: float = 300.0      # 链路切换最短间隔（只限制路由变更）
    recovery_stable_seconds: float = 120.0  # 有线恢复：连续稳定时长
    recovery_min_successes: int = 5         # 有线恢复：最少成功探测次数
    wifi_reconnect_cooldown: float = 60.0   # Wi-Fi 重连尝试最小间隔（限频）
    suspect_timeout: float = 60.0           # ETHERNET_SUSPECT 等待确认超时
    prepare_timeout: float = 90.0           # WIFI_PREPARING 备用链路准备超时
    switch_timeout: float = 30.0            # WIFI_SWITCHING / ETHERNET_FAILBACK 验证超时
    auth_max_attempts: int = 2              # 认证重复失败上限（超出转人工）
    ethernet_interface_metric: int = 10     # 建议值（方案 §4.1，须实机验证）
    wifi_interface_metric: int = 50         # 建议值（方案 §4.1，须实机验证）

    def validate(self) -> List[str]:
        """返回参数告警列表（不抛异常；用于启动日志与验收报告）。"""
        issues: List[str] = []
        if not 5.0 <= self.detection_interval <= 15.0:
            issues.append("检测间隔 %.0fs 超出建议区间 5–15s（方案 §6.3）"
                          % self.detection_interval)
        if self.failure_threshold < 1:
            issues.append("连续失败阈值必须 ≥1")
        if self.auth_cooldown < 180.0:
            issues.append("Portal 认证冷却 %.0fs 低于下限 180s（方案 §6.3）"
                          % self.auth_cooldown)
        if self.switch_min_interval < 300.0:
            issues.append("切换最短间隔 %.0fs 低于下限 300s（方案 §4.3）"
                          % self.switch_min_interval)
        if self.recovery_stable_seconds <= 0 or self.recovery_min_successes < 1:
            issues.append("有线恢复判据必须为正（稳定时长 >0 且成功次数 ≥1）")
        return issues


# --------------------------------------------------------------------------- #
# 单一副作用门
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OpRequest:
    """一次副作用动作请求（只描述意图，不含凭据）。"""

    kind: OpKind
    reason: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "reason": self.reason,
                "payload": dict(self.payload)}


@dataclass(frozen=True)
class OpOutcome:
    """动作门禁/执行结局。

    - ``gate_passed``：通过门禁（互斥串行 + 模式允许）；
    - ``attempted``：执行器确实被调用（据此登记冷却）；
    - ``executed``：执行成功。
    """

    kind: OpKind
    gate_passed: bool
    attempted: bool = False
    executed: bool = False
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "gate_passed": self.gate_passed,
                "attempted": self.attempted, "executed": self.executed,
                "detail": self.detail}


class NetworkOpScheduler:
    """**单一副作用门**（执行计划 §0.4）。

    规则：

    1. 四类动作（Wi-Fi 重连 / Portal POST / Metric 写入 / Clash 触碰）
       **互斥串行**：同一轮（round）内最多通过 1 个动作，第二个一律拒绝并记录；
    2. 只有 :attr:`FailoverMode.AUTO` 允许执行；``MONITOR`` / ``LOCKED``
       下动作只登记为计划（P3 的只读监测即依赖此规则）；
    3. 无执行器（``executor=None``）时动作"已批准但未执行"——P3 无真实执行层，
       真实执行器由 P5 ``campus_route_guard`` 提供。

    ``executor`` 协议：``executor(request) -> True|None``（成功）、
    ``False``（失败）、``str``（失败详情）或 :class:`OpOutcome`（完整结局）；
    亦可为带 ``execute(request)`` 方法的对象。执行器抛错被捕获并转为失败结局，
    不让调度器崩溃。
    """

    def __init__(self, mode: FailoverMode = FailoverMode.MONITOR,
                 executor: Optional[Any] = None) -> None:
        self.mode = mode
        self.executor = executor
        self.round_id = 0
        self.plan: List[OpRequest] = []
        self.executed: List[OpOutcome] = []
        self.rejected: List[Tuple[OpRequest, str]] = []

    def begin_round(self, round_id: int) -> None:
        """开始新一轮检测（清空本轮门禁状态）。"""
        self.round_id = int(round_id)
        self.plan = []
        self.executed = []
        self.rejected = []

    def submit(self, request: OpRequest) -> OpOutcome:
        """提交一个动作请求，返回门禁/执行结局。"""
        if self.plan:
            other = self.plan[0]
            detail = ("互斥串行（执行计划 §0.4）：本轮已安排 %s，拒绝 %s"
                      % (other.kind.value, request.kind.value))
            self.rejected.append((request, detail))
            return OpOutcome(request.kind, gate_passed=False, detail=detail)
        self.plan.append(request)
        if not self.mode.executes:
            return OpOutcome(request.kind, gate_passed=True,
                             detail="只读监测（%s）：动作仅登记为计划，不执行"
                                    % self.mode.value)
        if self.executor is None:
            return OpOutcome(request.kind, gate_passed=True,
                             detail="已批准但未执行：真实执行层未接入（P5 "
                                    "campus_route_guard）")
        outcome = self._invoke(request)
        if outcome.executed:
            self.executed.append(outcome)
        return outcome

    def _invoke(self, request: OpRequest) -> OpOutcome:
        executor = self.executor
        try:
            if hasattr(executor, "execute"):
                raw = executor.execute(request)
            else:
                raw = executor(request)
        except Exception as exc:                       # 执行器故障不得上抛
            return OpOutcome(request.kind, gate_passed=True, attempted=True,
                             detail="执行器异常：%s: %s"
                                    % (type(exc).__name__, exc))
        if isinstance(raw, OpOutcome):
            return raw
        if raw is None or raw is True:
            return OpOutcome(request.kind, gate_passed=True, attempted=True,
                             executed=True, detail="已执行")
        if raw is False:
            return OpOutcome(request.kind, gate_passed=True, attempted=True,
                             detail="执行失败")
        return OpOutcome(request.kind, gate_passed=True, attempted=True,
                         executed=True, detail=str(raw))


# --------------------------------------------------------------------------- #
# 一次 tick 的结果
# --------------------------------------------------------------------------- #
@dataclass
class TickResult:
    """一次状态机推进（= 一轮检测）的完整结果。"""

    contract_version: str = CONTRACT_VERSION
    mode: FailoverMode = FailoverMode.MONITOR
    at: float = 0.0
    state: FailoverState = FailoverState.ETHERNET_PRIMARY
    previous_state: FailoverState = FailoverState.ETHERNET_PRIMARY
    changed: bool = False
    predicted_state: Optional[FailoverState] = None
    preferred: Optional[str] = None
    would_switch: bool = False
    plan: List[OpRequest] = field(default_factory=list)
    executed: List[OpOutcome] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    counters: Dict[str, Any] = field(default_factory=dict)

    @property
    def readonly(self) -> bool:
        """本轮是否完全未执行任何动作。"""
        return not self.executed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "mode": self.mode.value,
            "at": self.at,
            "state": self.state.value,
            "previous_state": self.previous_state.value,
            "changed": self.changed,
            "predicted_state": (self.predicted_state.value
                                if self.predicted_state else None),
            "preferred": self.preferred,
            "would_switch": self.would_switch,
            "plan": [p.to_dict() for p in self.plan],
            "executed": [o.to_dict() for o in self.executed],
            "notes": list(self.notes),
            "counters": dict(self.counters),
        }


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def _health_state(health: Any) -> Optional[HealthState]:
    """取接口健康结论（``None`` 表示角色未解析）。"""
    return getattr(health, "state", None) if health is not None else None


def _egress_if_index(snapshot: Any) -> Optional[int]:
    """系统当前公网出口的 ifIndex（无法识别时为 ``None``）。"""
    system = getattr(snapshot, "system", None)
    egress = getattr(system, "egress", None) if system is not None else None
    return getattr(egress, "if_index", None) if egress is not None else None


class NetworkFailoverManager:
    """有线优先的故障切换状态机（方案 §5）。

    纯逻辑 + 显式 ``now``：不持有时钟、不做 I/O，所有副作用都必须经
    :class:`NetworkOpScheduler`。``tick(now, snapshot)`` 推进一轮，输入为 P1 的
    :class:`campus_network.NetworkHealthSnapshot`。
    """

    def __init__(self, policy: Optional[FailoverPolicy] = None,
                 mode: FailoverMode = FailoverMode.MONITOR, *,
                 scheduler: Optional[NetworkOpScheduler] = None,
                 timer_table: Optional[TimerTable] = None,
                 executor: Optional[Any] = None,
                 lock_target: Optional[str] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.policy = policy or FailoverPolicy()
        self.mode = mode
        #: 手动锁定目标（P6）：仅 ``FailoverMode.LOCKED`` 下生效；非 ``None`` 时
        #: 优先路径固定为该目标，**绝不**执行任何路由变更，也不自动禁用另一网卡。
        self.lock_target = lock_target if lock_target in LOCK_TARGETS else None
        self.timers = timer_table if timer_table is not None else TimerTable()
        self.scheduler = (scheduler if scheduler is not None
                          else NetworkOpScheduler(mode, executor))
        self.scheduler.mode = mode
        self._log = log
        self.state = FailoverState.ETHERNET_PRIMARY
        self.state_since = 0.0
        self.last_tick_at: Optional[float] = None
        self.eth_failure_streak = 0
        self.recovery_since: Optional[float] = None
        self.recovery_successes = 0
        self.auth_attempts = 0
        self.last_switch_at: Optional[float] = None
        self.manual_reason = ""
        self.history: List[Tuple[float, FailoverState]] = []
        self._last_wifi_reconnect_at: Optional[float] = None
        self._round = 0
        self._executed: List[OpOutcome] = []
        for issue in self.policy.validate():
            self._note("策略参数告警：%s" % issue)

    # ---- 外部控制 ---- #
    def pause(self, now: float, reason: str = "用户暂停") -> None:
        """用户暂停自动调度（方案 §11 场景：不发生自动认证或路由修改）。"""
        self._note("暂停自动调度：%s" % reason)
        self._enter(FailoverState.PAUSED, now, [])

    def resume(self, now: float) -> None:
        """恢复自动调度：回到以太网优先并重新评估。"""
        self._note("恢复自动调度：回到以太网优先重新评估")
        self._enter(FailoverState.ETHERNET_PRIMARY, now, [])

    def require_manual(self, now: float, reason: str) -> None:
        """进入需人工处理（凭据/Portal 结构/权限等问题）。"""
        self.manual_reason = reason
        self._note("需人工处理：%s" % reason)
        self._enter(FailoverState.MANUAL_REQUIRED, now, [])

    def clear_manual(self, now: float) -> None:
        """人工处理完成后解除（回到以太网优先重新评估）。"""
        self._note("人工处理已解除：%s" % (self.manual_reason or "未说明"))
        self.manual_reason = ""
        self._enter(FailoverState.ETHERNET_PRIMARY, now, [])

    def set_lock_target(self, target: Optional[str]) -> None:
        """设置/清除手动锁定的优先路径（``ethernet`` / ``wifi`` / ``None``）。

        仅在 :attr:`FailoverMode.LOCKED` 下生效：非 ``None`` 时优先路径固定为该
        目标，仍照常显示健康告警，但**不执行任何路由变更**，也**不自动禁用**
        另一张网卡（方案 §11）。非法取值一律视为"未锁定"。
        """
        self.lock_target = target if target in LOCK_TARGETS else None
        self._note("手动锁定目标：%s"
                   % (PATH_LABEL.get(self.lock_target, "未锁定") if self.lock_target
                      else "未锁定"))

    def next_detection_at(self) -> float:
        """下一次网络检测时刻（**恒**与认证/切换冷却无关）。"""
        return self.timers.next_detection_at

    # ---- 主循环 ---- #
    def tick(self, now: float, snapshot: NetworkHealthSnapshot) -> TickResult:
        """推进一轮检测，返回 :class:`TickResult`（不执行任何未授权动作）。"""
        if self.last_tick_at is None:
            self.state_since = now
        self._round += 1
        self.scheduler.begin_round(self._round)
        self._executed = []
        # 网络检测计时器持续运行：任何冷却都不得推迟（AGENTS §3.4 / P2 契约）
        self.timers.schedule_detection(now, self.policy.detection_interval)

        previous = self.state
        plan: List[OpRequest] = []
        notes: List[str] = []
        target = self._decide(now, snapshot, plan, notes)
        predicted: Optional[FailoverState] = None
        if target is not self.state:
            if target.is_execution and not self.mode.executes:
                predicted = target
                notes.append(
                    "只读监测（%s）：本应进入 %s，但不执行任何动作，状态保持 %s"
                    % (self.mode.value, target.value, self.state.value))
            else:
                self._enter(target, now, notes)

        self.last_tick_at = now
        self.history.append((now, self.state))
        preferred = self.state.preferred
        if self.mode is FailoverMode.LOCKED and self.lock_target:
            # 手动锁定：优先路径固定为锁定目标（方案 §11），零执行。
            preferred = self.lock_target
        result = TickResult(
            mode=self.mode, at=now, state=self.state, previous_state=previous,
            changed=self.state is not previous, predicted_state=predicted,
            preferred=preferred,
            would_switch=any(p.kind is OpKind.METRIC_WRITE for p in plan),
            plan=list(plan), executed=list(self._executed), notes=notes,
            counters=self._counters(now))
        for line in notes:
            self._note(line)
        return result

    # ---- 内部：工具 ---- #
    def _note(self, message: str) -> None:
        if self._log is not None:
            self._log(message)

    def _counters(self, now: float) -> Dict[str, Any]:
        return {
            "eth_failure_streak": self.eth_failure_streak,
            "failure_threshold": self.policy.failure_threshold,
            "recovery_successes": self.recovery_successes,
            "recovery_min_successes": self.policy.recovery_min_successes,
            "auth_attempts": self.auth_attempts,
            "auth_ready_in": max(0.0, self.timers.auth_ready_at - now),
            "switch_ready_in": max(0.0, self.timers.switch_ready_at - now),
            "state_elapsed": max(0.0, now - self.state_since),
            "lock_target": self.lock_target,
            "locked": bool(self.mode is FailoverMode.LOCKED and self.lock_target),
        }

    def _enter(self, state: FailoverState, now: float,
               notes: List[str]) -> None:
        previous = self.state
        self.state = state
        self.state_since = now
        if state is FailoverState.ETHERNET_PRIMARY:
            self.eth_failure_streak = 0
            self.auth_attempts = 0
            self.recovery_since = None
            self.recovery_successes = 0
        elif state is FailoverState.ETHERNET_RECOVERY_CHECK:
            # 进入即记为第 1 次成功探测（连续稳定窗口从这里开始计）
            self.recovery_since = now
            self.recovery_successes = 1
        elif state is FailoverState.WIFI_BACKUP_ACTIVE:
            self.recovery_since = None
            self.recovery_successes = 0
        if previous is not state:
            notes.append("状态转换：%s → %s" % (previous.value, state.value))

    def _timeout(self, now: float, limit: float) -> bool:
        return (now - self.state_since) >= limit

    def _submit(self, plan: List[OpRequest], kind: OpKind, reason: str,
                **payload: Any) -> OpOutcome:
        request = OpRequest(kind=kind, reason=reason, payload=dict(payload))
        outcome = self.scheduler.submit(request)
        plan.append(request)
        if outcome.executed:
            self._executed.append(outcome)
        return outcome

    def _reconnect_due(self, now: float) -> bool:
        if self._last_wifi_reconnect_at is None:
            return True
        return (now - self._last_wifi_reconnect_at
                >= self.policy.wifi_reconnect_cooldown)

    def _note_reconnect(self, now: float, outcome: OpOutcome) -> None:
        if outcome.attempted:
            self._last_wifi_reconnect_at = now

    # ---- 内部：状态迁移 ---- #
    def _decide(self, now: float, snapshot: Any, plan: List[OpRequest],
                notes: List[str]) -> FailoverState:
        policy = self.policy
        state = self.state
        eth_state = _health_state(getattr(snapshot, "ethernet", None))
        wifi_state = _health_state(getattr(snapshot, "wifi", None))

        eth_ok = eth_state is HealthState.HEALTHY
        wifi_ok = wifi_state is HealthState.HEALTHY
        eth_fail = (eth_state in RELIABLE_FAILURE_STATES
                    or eth_state in CAPTIVE_STATES)
        wifi_fail = wifi_state in RELIABLE_FAILURE_STATES
        wifi_captive = wifi_state in CAPTIVE_STATES
        eth_unknown = eth_state is None or eth_state in UNRELIABLE_STATES
        wifi_unknown = wifi_state is None or wifi_state in UNRELIABLE_STATES

        # ---- 粘性状态：只记录，不产生任何动作 ---- #
        if state is FailoverState.PAUSED:
            notes.append("用户已暂停自动调度：只记录观测，不产生任何动作")
            return state
        if state is FailoverState.MANUAL_REQUIRED:
            notes.append("需人工处理（%s）：自动调度保持停止"
                         % (self.manual_reason or "未说明"))
            return state

        # ---- 手动锁定（P6）：优先路径固定，仍显示健康告警，零执行 ---- #
        if self.mode is FailoverMode.LOCKED and self.lock_target:
            label = PATH_LABEL.get(self.lock_target, self.lock_target)
            notes.append("手动锁定：优先路径固定为 %s；不执行任何路由变更，"
                         "也不自动禁用另一张网卡（方案 §11）" % label)
            locked_state = (eth_state if self.lock_target == "ethernet"
                            else wifi_state)
            if locked_state is None:
                notes.append("锁定路径 %s 角色未解析：无法评估健康" % label)
            elif locked_state is HealthState.HEALTHY:
                notes.append("锁定路径 %s 健康" % label)
            else:
                notes.append("健康告警：锁定路径 %s 异常（%s）"
                             % (label, locked_state.value))
            other = "wifi" if self.lock_target == "ethernet" else "ethernet"
            other_state = wifi_state if self.lock_target == "ethernet" else eth_state
            if other_state is not None and other_state is not HealthState.HEALTHY:
                notes.append("提示：另一条路径 %s 异常（%s），但锁定模式不自动切换"
                             % (PATH_LABEL.get(other, other), other_state.value))
            return state

        # ---- ETHERNET_PRIMARY：有线主用 ---- #
        if state is FailoverState.ETHERNET_PRIMARY:
            if eth_ok:
                self.eth_failure_streak = 0
                notes.append("以太网健康：保持以太网优先（不触发任何切换或认证）")
                return state
            if eth_unknown:
                notes.append("以太网状态不可判定（%s）：不计数、不动作"
                             % self._state_text(eth_state))
                return state
            self.eth_failure_streak += 1
            if self.eth_failure_streak >= policy.failure_threshold:
                notes.append("以太网连续可靠失败 %d 次（阈值 %d）→ 进入疑似异常"
                             % (self.eth_failure_streak, policy.failure_threshold))
                return FailoverState.ETHERNET_SUSPECT
            notes.append("以太网可靠失败 %d/%d（未达阈值，继续观察）"
                         % (self.eth_failure_streak, policy.failure_threshold))
            return state

        # ---- ETHERNET_SUSPECT：疑似异常，等待进一步确认 ---- #
        if state is FailoverState.ETHERNET_SUSPECT:
            if eth_ok:
                notes.append("以太网已恢复：回到以太网优先")
                return FailoverState.ETHERNET_PRIMARY
            if eth_unknown:
                notes.append("以太网状态不可判定（%s）：保持等待确认"
                             % self._state_text(eth_state))
                if self._timeout(now, policy.suspect_timeout):
                    return self._manual(
                        "疑似异常超时且以太网状态不可判定", notes)
                return state
            if wifi_ok or wifi_captive:
                notes.append("确认有线故障（%s）；无线可用（%s）→ 准备备用链路"
                             % (self._state_text(eth_state),
                                self._state_text(wifi_state)))
                return FailoverState.WIFI_PREPARING
            if wifi_fail:
                notes.append("有线故障（%s）且无线不可用（%s）→ 两条路径均不可用"
                             % (self._state_text(eth_state),
                                self._state_text(wifi_state)))
                return FailoverState.BOTH_OFFLINE
            notes.append("有线故障，无线状态不可判定（%s）：不动作"
                         % self._state_text(wifi_state))
            if self._timeout(now, policy.suspect_timeout):
                return self._manual("疑似异常超时且无线状态不可判定", notes)
            return state

        # ---- WIFI_PREPARING：准备无线备用链路（方案 §4.2 阶段 A）---- #
        if state is FailoverState.WIFI_PREPARING:
            if eth_ok:
                notes.append("备用链路准备中止：以太网已恢复 → 回到以太网优先")
                return FailoverState.ETHERNET_PRIMARY
            if wifi_ok:
                if not self.timers.switch_allowed(now):
                    notes.append("备用路径已证实可用，但切换冷却未过（剩余 %.0fs）"
                                 "：推迟切换" % (self.timers.switch_ready_at - now))
                    return state
                outcome = self._submit(
                    plan, OpKind.METRIC_WRITE,
                    "以太网确认故障且 Wi-Fi 路径已证实可用：将 Wi-Fi 设为优先出口",
                    target="wifi",
                    ethernet_metric=policy.ethernet_interface_metric,
                    wifi_metric=policy.wifi_interface_metric)
                if outcome.attempted:
                    self._note_switch(now)
                if self.mode.executes and not outcome.executed:
                    # P5 契约：执行态必须"动作已执行"才能进入（否则会在动作失败时
                    # 谎报已切换）。此处保持准备状态，由超时/冷却兜底。
                    notes.append("路由切换动作未执行（%s）：不进入 WIFI_SWITCHING，"
                                 "保持备用链路准备" % (outcome.detail or "未执行"))
                    return state
                return FailoverState.WIFI_SWITCHING
            if wifi_captive:
                if not self.timers.auth_allowed(now):
                    notes.append("需要 Portal 认证，但认证冷却未过（剩余 %.0fs）"
                                 "：推迟认证" % (self.timers.auth_ready_at - now))
                    return state
                outcome = self._submit(plan, OpKind.PORTAL_POST,
                                       "SNNU 需要认证：经 SNNU 接口提交 Portal 认证")
                if outcome.attempted:
                    self.auth_attempts += 1
                    self._note_auth(now)
                if self.mode.executes and not outcome.executed:
                    notes.append("Portal 认证动作未执行（%s）：不进入 WIFI_AUTHENTICATING，"
                                 "保持备用链路准备" % (outcome.detail or "未执行"))
                    return state
                return FailoverState.WIFI_AUTHENTICATING
            if wifi_fail:
                if self._reconnect_due(now):
                    outcome = self._submit(
                        plan, OpKind.WIFI_RECONNECT,
                        "SNNU 未连接或无有效地址：尝试重新关联无线")
                    self._note_reconnect(now, outcome)
                else:
                    notes.append("无线重连冷却中（限频），本轮不重复尝试")
                notes.append("等待无线链路就绪（Wi-Fi=%s）"
                             % self._state_text(wifi_state))
                if self._timeout(now, policy.prepare_timeout):
                    notes.append("备用链路准备超时（无线仍不可用）→ 两条路径均不可用")
                    return FailoverState.BOTH_OFFLINE
                return state
            notes.append("无线状态不可判定（%s）：不动作" % self._state_text(wifi_state))
            if self._timeout(now, policy.prepare_timeout):
                return self._manual("备用链路准备超时且无线状态不可判定", notes)
            return state

        # ---- WIFI_AUTHENTICATING：SNNU 需要认证，正在执行（需已执行）---- #
        if state is FailoverState.WIFI_AUTHENTICATING:
            if eth_ok:
                notes.append("认证过程中以太网恢复：中止无线认证路径，回到以太网优先")
                return FailoverState.ETHERNET_PRIMARY
            if wifi_ok:
                notes.append("SNNU 认证完成：无线路径可用，继续准备切换")
                return FailoverState.WIFI_PREPARING
            if wifi_captive:
                if self.auth_attempts >= policy.auth_max_attempts:
                    return self._manual(
                        "SNNU 认证重复失败（%d 次），停止自动认证"
                        % self.auth_attempts, notes)
                if self.timers.auth_allowed(now):
                    outcome = self._submit(plan, OpKind.PORTAL_POST,
                                           "SNNU 仍未认证成功：按冷却窗口重试认证")
                    if outcome.attempted:
                        self.auth_attempts += 1
                        self._note_auth(now)
                    return state
                notes.append("认证未生效，等待认证冷却窗口（剩余 %.0fs）"
                             % (self.timers.auth_ready_at - now))
                return state
            if wifi_fail:
                notes.append("无线链路断开：回到备用链路准备")
                return FailoverState.WIFI_PREPARING
            notes.append("无线状态不可判定（%s）：保持等待" % self._state_text(wifi_state))
            if self._timeout(now, policy.prepare_timeout):
                return self._manual("认证阶段超时且无线状态不可判定", notes)
            return state

        # ---- WIFI_SWITCHING：执行经过批准的路由切换（需已执行）---- #
        if state is FailoverState.WIFI_SWITCHING:
            wifi_if = getattr(getattr(snapshot, "wifi", None), "if_index", None)
            if _egress_if_index(snapshot) is not None and wifi_if is not None \
                    and _egress_if_index(snapshot) == wifi_if:
                notes.append("已证实系统默认出口改为 Wi-Fi（ifIndex=%s）：Wi-Fi 接管"
                             % wifi_if)
                return FailoverState.WIFI_BACKUP_ACTIVE
            if eth_ok:
                notes.append("切换验证期间以太网恢复：撤销本次切换（回退以太网优先）")
                outcome = self._submit(plan, OpKind.METRIC_WRITE,
                                       "撤销 Wi-Fi 优先：回退以太网优先",
                                       target="ethernet",
                                       ethernet_metric=policy.ethernet_interface_metric,
                                       wifi_metric=policy.wifi_interface_metric)
                if outcome.attempted:
                    self._note_switch(now)
                return FailoverState.ETHERNET_PRIMARY
            if self._timeout(now, policy.switch_timeout):
                return self._manual("切换未生效且无法确认实际出口（未通过验证）", notes)
            notes.append("等待切换生效与出口验证")
            return state

        # ---- WIFI_BACKUP_ACTIVE：Wi-Fi 已接管 ---- #
        if state is FailoverState.WIFI_BACKUP_ACTIVE:
            if eth_ok:
                notes.append("有线独立探测成功：进入恢复观察期（需连续稳定 %d 次/"
                             "≥%.0fs）" % (policy.recovery_min_successes,
                                           policy.recovery_stable_seconds))
                return FailoverState.ETHERNET_RECOVERY_CHECK
            if wifi_fail:
                notes.append("无线接管链路已不可用，且有线未恢复 → 两条路径均不可用")
                return FailoverState.BOTH_OFFLINE
            if wifi_unknown:
                notes.append("无线状态不可判定（%s）：保持观察，不动作"
                             % self._state_text(wifi_state))
            else:
                notes.append("Wi-Fi 接管中：继续独立探测以太网恢复情况")
            return state

        # ---- ETHERNET_RECOVERY_CHECK：检查有线恢复稳定性 ---- #
        if state is FailoverState.ETHERNET_RECOVERY_CHECK:
            if eth_ok:
                self.recovery_successes += 1
                elapsed = (now - self.recovery_since
                           if self.recovery_since is not None else 0.0)
                stable = (elapsed >= policy.recovery_stable_seconds
                          and self.recovery_successes
                          >= policy.recovery_min_successes)
                if not stable:
                    notes.append("有线恢复观察中：成功 %d/%d 次，稳定 %.0f/%.0fs"
                                 % (self.recovery_successes,
                                    policy.recovery_min_successes, elapsed,
                                    policy.recovery_stable_seconds))
                    return state
                if not self.timers.switch_allowed(now):
                    notes.append("有线已满足恢复判据，但切换冷却未过（剩余 %.0fs）"
                                 "：推迟切回" % (self.timers.switch_ready_at - now))
                    return state
                outcome = self._submit(
                    plan, OpKind.METRIC_WRITE,
                    "有线已连续稳定 %d 次/≥%.0fs：切回以太网优先"
                    % (self.recovery_successes, policy.recovery_stable_seconds),
                    target="ethernet",
                    ethernet_metric=policy.ethernet_interface_metric,
                    wifi_metric=policy.wifi_interface_metric)
                if outcome.attempted:
                    self._note_switch(now)
                if self.mode.executes and not outcome.executed:
                    notes.append("切回动作未执行（%s）：不进入 ETHERNET_FAILBACK，"
                                 "保持恢复观察" % (outcome.detail or "未执行"))
                    return state
                return FailoverState.ETHERNET_FAILBACK
            if wifi_fail:
                notes.append("恢复观察期无线中断且有线未恢复 → 两条路径均不可用")
                return FailoverState.BOTH_OFFLINE
            if eth_fail:
                notes.append("恢复观察期有线再次失败：回到 Wi-Fi 接管并重置观察窗口")
                return FailoverState.WIFI_BACKUP_ACTIVE
            notes.append("恢复观察期有线状态不可判定（%s）：保持观察"
                         % self._state_text(eth_state))
            return state

        # ---- ETHERNET_FAILBACK：准备安全切回有线（需已执行）---- #
        if state is FailoverState.ETHERNET_FAILBACK:
            eth_if = getattr(getattr(snapshot, "ethernet", None), "if_index", None)
            if _egress_if_index(snapshot) is not None and eth_if is not None \
                    and _egress_if_index(snapshot) == eth_if:
                notes.append("已证实系统默认出口回到以太网（ifIndex=%s）：切回完成"
                             % eth_if)
                return FailoverState.ETHERNET_PRIMARY
            if eth_fail:
                notes.append("切回后发现有线仍不稳定：撤销本次切换，重新进入观察期")
                return FailoverState.WIFI_BACKUP_ACTIVE
            if self._timeout(now, policy.switch_timeout):
                notes.append("切回未通过出口验证：撤销本次切换，重新进入观察期")
                return FailoverState.WIFI_BACKUP_ACTIVE
            notes.append("等待切回生效与出口验证")
            return state

        # ---- BOTH_OFFLINE：两条路径均不可用 ---- #
        if state is FailoverState.BOTH_OFFLINE:
            if eth_ok:
                notes.append("有线恢复：回到以太网优先")
                return FailoverState.ETHERNET_PRIMARY
            if wifi_ok or wifi_captive:
                notes.append("无线路径恢复（%s）：准备备用链路"
                             % self._state_text(wifi_state))
                return FailoverState.WIFI_PREPARING
            if wifi_unknown and eth_unknown:
                notes.append("两条路径均不可判定：不动作（等待下次检测）")
                return state
            if self._reconnect_due(now):
                outcome = self._submit(plan, OpKind.WIFI_RECONNECT,
                                       "双断恢复尝试：限频重新关联无线")
                self._note_reconnect(now, outcome)
                notes.append("双断恢复尝试（限频，间隔 %.0fs）"
                             % policy.wifi_reconnect_cooldown)
            else:
                notes.append("双断恢复尝试冷却中：本轮不重复尝试（禁止在两张网卡"
                             "之间来回切换）")
            return state

        return state

    def _state_text(self, state: Optional[HealthState]) -> str:
        return state.value if state is not None else "角色未解析"

    def _manual(self, reason: str, notes: List[str]) -> FailoverState:
        self.manual_reason = reason
        notes.append("需人工处理：%s（停止自动调度，等待人工确认）" % reason)
        return FailoverState.MANUAL_REQUIRED

    def _note_switch(self, now: float) -> None:
        self.last_switch_at = now
        self.timers.note_switch(now, self.policy.switch_min_interval)

    def _note_auth(self, now: float) -> None:
        """登记认证冷却：**只限制认证**，不影响网络检测（AGENTS §3.4）。"""
        self.timers.note_auth(now, self.policy.auth_cooldown)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def format_tick(result: TickResult, *, title: str = "") -> str:
    """人类可读的一轮监测输出（``failover`` 命令用）。"""
    lines: List[str] = []
    add = lines.append
    add("=" * 72)
    add("V0.3 P3 故障切换监测（%s %s：%s）"
        % (result.mode.value, result.mode.label,
           "不执行任何动作" if not result.mode.executes else "允许执行已批准动作"))
    add("契约: %s    时间: t=%.1fs%s"
        % (result.contract_version, result.at, ("    " + title) if title else ""))
    add("=" * 72)
    add("")
    add("当前状态: %s%s" % (result.state.value,
                          "" if not result.changed else
                          "（本轮由 %s 转入）" % result.previous_state.value))
    if result.predicted_state is not None:
        add("预测状态: %s（若允许切换；只读监测下不进入）"
            % result.predicted_state.value)
    add("优先出口: %s" % (result.preferred or "（无可用路径）"))
    add("是否将改变出口: %s" % ("是" if result.would_switch else "否"))
    add("")
    add("[计数] 以太网连续失败 %s/%s；有线恢复成功 %s/%s；认证尝试 %s 次；"
        "认证冷却剩余 %.0fs；切换冷却剩余 %.0fs"
        % (result.counters.get("eth_failure_streak"),
           result.counters.get("failure_threshold"),
           result.counters.get("recovery_successes"),
           result.counters.get("recovery_min_successes"),
           result.counters.get("auth_attempts"),
           result.counters.get("auth_ready_in", 0.0),
           result.counters.get("switch_ready_in", 0.0)))
    add("")
    add("[本轮计划] %s" % ("（无动作）" if not result.plan else
                          "%d 项（已执行 %d 项）"
                          % (len(result.plan), len(result.executed))))
    for request in result.plan:
        add("  - [%s] %s" % (request.kind.value, request.reason))
    for outcome in result.executed:
        add("  * 已执行 [%s] %s" % (outcome.kind.value, outcome.detail))
    if result.plan and not result.executed:
        add("  注：只读监测模式不执行任何动作，以上仅为计划/预测。")
    add("")
    add("[说明]")
    for note in result.notes:
        add("  - %s" % note)
    add("=" * 72)
    return "\n".join(lines)
