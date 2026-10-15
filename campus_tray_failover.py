# -*- coding: utf-8 -*-
"""V0.3 P6 托盘三模式与手动锁定（campus_tray_failover）。

本模块实现方案 §11 的**托盘三模式**（智能自动 / 仅监测 / 手动锁定）与
**双网络状态展示模型**，并把 P3 的 :class:`campus_failover.NetworkFailoverManager`
接线为托盘可消费的桥接层（:class:`TrayFailoverBridge`）。

职责边界（V0.3 专项规则 2「职责分离」）：

- 本模块**纯逻辑**：不导入 tkinter / pystray / winreg / requests，不做任何 I/O；
  所有副作用都必须经 P3 的单一副作用门（``NetworkOpScheduler``）；
- **默认仅监测**（``FailoverMode.MONITOR``）：零动作、零路由变更；
- **智能自动（AUTO）**：仅当用户**显式授权**（``authorized=True``）**且**注入
  执行器工厂时，才把真实执行器接入单一副作用门；否则动作"已批准但未执行"
  （P3 语义），并在状态中如实上报（P6 用户决策：显式授权后接入）；
- **手动锁定（LOCKED）**：优先路径固定为 ``lock_target``，仍显示健康告警，
  **绝不**执行任何动作，也**不自动禁用**另一张网卡（方案 §11）；
- **Clash 保持原样**（``clash_keep``，方案 §9）：默认开启；本模块不做任何
  Clash 触碰，仅记录该策略（``CLASH_TOUCH`` 由 P5 显式拒绝）。

测试分级：``U-6-*`` 单元 / 离线 Mock；``I-6-*`` 离线集成闭合回路。
约束：零真实网络 / 零真实路由写入 / 零真实注册表写入 / 零真实凭据读取 /
零真实浏览器启动 / 零真实 sleep。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from campus_failover import (RELIABLE_FAILURE_STATES, UNRELIABLE_STATES,
                             FailoverMode, FailoverPolicy, FailoverState,
                             NetworkFailoverManager, OpKind, PATH_LABEL,
                             TickResult)
from campus_network import (HealthState, InterfaceHealth,
                            NetworkHealthSnapshot)
from campus_tray_observer import RefusingExecutor

__all__ = [
    "CONTRACT_VERSION", "MODE_ORDER", "DEFAULT_MODE", "DEFAULT_CLASH_KEEP",
    "REMOTE_ENABLED", "REMOTE_PAUSED", "REMOTE_MANUAL", "REMOTE_OFF",
    "REMOTE_SENSITIVE", "STATE_LABEL", "TRAY_MODE_LABEL", "parse_mode",
    "parse_lock", "tray_mode_label", "light_snapshot", "FailoverStatus",
    "TrayFailoverBridge", "format_status",
]

CONTRACT_VERSION = "v03-p6-tray-1"

#: 三模式顺序（方案 §11 列举顺序；托盘菜单与设置面板共用）。
MODE_ORDER = (FailoverMode.MONITOR, FailoverMode.AUTO, FailoverMode.LOCKED)

#: 托盘模式名（方案 §11 命名）。``MONITOR`` 在 P3 内部标签为"只读监测"，
#: 语义与方案 §11 的"仅监测"**完全相同**（P3 标签已被 CLI 测试固化，不改）。
TRAY_MODE_LABEL: Dict[FailoverMode, str] = {
    FailoverMode.MONITOR: "仅监测",
    FailoverMode.AUTO: "智能自动",
    FailoverMode.LOCKED: "手动锁定",
}

#: 默认模式 = 仅监测（P6 用户决策：默认不执行任何动作，严格符合「否」定性）。
DEFAULT_MODE = FailoverMode.MONITOR

#: Clash 保持原样默认值（方案 §9：作为默认值）。
DEFAULT_CLASH_KEEP = True

#: 远程保障状态文案（方案 §11）。前四个由 P6 固化（既有测试看守，不可改名）；
#: ``REMOTE_SENSITIVE`` 为 V0.3 P7 新增：远程会话进行中（推迟自动切回）。
REMOTE_ENABLED = "启用"
REMOTE_PAUSED = "已暂停"
REMOTE_MANUAL = "需要人工处理"
REMOTE_OFF = "未启用"
REMOTE_SENSITIVE = "启用（远程会话敏感期：推迟切回）"

#: 11 个状态的中文标签（托盘展示用；不改动 P3 契约）。
STATE_LABEL: Dict[FailoverState, str] = {
    FailoverState.ETHERNET_PRIMARY: "以太网主用",
    FailoverState.ETHERNET_SUSPECT: "以太网疑似异常",
    FailoverState.WIFI_PREPARING: "准备无线备用链路",
    FailoverState.WIFI_AUTHENTICATING: "SNNU 认证中",
    FailoverState.WIFI_SWITCHING: "切换中",
    FailoverState.WIFI_BACKUP_ACTIVE: "Wi-Fi 已接管",
    FailoverState.ETHERNET_RECOVERY_CHECK: "有线恢复观察中",
    FailoverState.ETHERNET_FAILBACK: "切回有线中",
    FailoverState.BOTH_OFFLINE: "两条路径均不可用",
    FailoverState.MANUAL_REQUIRED: "需人工处理",
    FailoverState.PAUSED: "已暂停",
}


# --------------------------------------------------------------------------- #
# 解析辅助（配置值 -> 模式 / 锁定目标；非法一律回退安全值）
# --------------------------------------------------------------------------- #
def parse_mode(value: Any, default: FailoverMode = DEFAULT_MODE) -> FailoverMode:
    """把配置值解析为 :class:`FailoverMode`（非法一律回退 ``default``）。"""
    if isinstance(value, FailoverMode):
        return value
    if isinstance(value, str):
        upper = value.strip().upper()
        for mode in FailoverMode:
            if mode.value == upper:
                return mode
    return default


def parse_lock(value: Any) -> Optional[str]:
    """把配置值解析为锁定目标（``ethernet`` / ``wifi`` / ``None``）。"""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in PATH_LABEL:
            return lowered
    return None


def tray_mode_label(mode: FailoverMode) -> str:
    """托盘展示用的模式名（方案 §11：仅监测 / 智能自动 / 手动锁定）。"""
    return TRAY_MODE_LABEL.get(mode, mode.label)


def _axis_state(health: Any) -> Optional[HealthState]:
    return getattr(health, "state", None) if health is not None else None


# --------------------------------------------------------------------------- #
# 轻量快照（托盘展示用；不启动重型 netdiag 管道）
# --------------------------------------------------------------------------- #
def light_snapshot(*, system_online: Optional[bool] = None,
                   system_source: str = "",
                   ethernet: Optional[InterfaceHealth] = None,
                   wifi: Optional[InterfaceHealth] = None,
                   egress_if: Optional[int] = None,
                   host: str = "") -> NetworkHealthSnapshot:
    """构造一个轻量 :class:`NetworkHealthSnapshot`（**不做任何 I/O**）。

    托盘的快速探测循环复用既有轻量信号（如 ``internet_ok``）构造系统轴，
    各接口轴按调用方提供（缺省为 ``None`` = 角色未解析，展示为"未解析"）。
    权威的三轴分类仍以 P1 的完整只读观察（``netcheck`` / ``failover``）为准；
    需要完整数据时，向 :class:`TrayFailoverBridge` 注入完整快照提供者即可。
    """
    from campus_network import EgressInfo, SystemHealth

    system = SystemHealth(online=(None if system_online is None
                                  else bool(system_online)),
                          source=system_source)
    if egress_if is not None:
        system.egress = EgressInfo(target="", if_index=int(egress_if))
    return NetworkHealthSnapshot(generated_at="", host=host, ethernet=ethernet,
                                 wifi=wifi, system=system)


# --------------------------------------------------------------------------- #
# 展示模型
# --------------------------------------------------------------------------- #
def _ethernet_status_text(health: Any) -> str:
    """有线状态文案（方案 §11：正常 / 异常 / 恢复检测中）。"""
    state = _axis_state(health)
    if state is None:
        return "未解析"
    if state is HealthState.HEALTHY:
        return "正常"
    if state is HealthState.CAPTIVE_PORTAL:
        return "需要认证"
    if state in RELIABLE_FAILURE_STATES:
        return "异常（%s）" % state.value
    if state in UNRELIABLE_STATES:
        return "不可判定（%s）" % state.value
    return state.value


def _wifi_status_text(health: Any) -> str:
    """Wi-Fi 状态文案（方案 §11：已连接 / 需要认证 / 已认证 / 不可用）。"""
    state = _axis_state(health)
    if state is None:
        return "未解析"
    if state is HealthState.HEALTHY:
        return "已认证"
    if state is HealthState.CAPTIVE_PORTAL:
        return "需要认证"
    if state is HealthState.LINK_DOWN:
        return "不可用（未连接）"
    if state is HealthState.NO_IP:
        return "已连接（无有效地址）"
    if state in UNRELIABLE_STATES:
        return "不可判定（%s）" % state.value
    return "不可用（%s）" % state.value


def _primary_backup(preferred: Optional[str]) -> "tuple[Optional[str], Optional[str]]":
    """由优先出口推导（当前主网络，当前备用网络）。"""
    if preferred == "ethernet":
        return "ethernet", "wifi"
    if preferred == "wifi":
        return "wifi", "ethernet"
    return None, None


def _egress_text(snapshot: Any, ethernet: Any, wifi: Any) -> str:
    system = getattr(snapshot, "system", None) if snapshot is not None else None
    egress = getattr(system, "egress", None) if system is not None else None
    if_index = getattr(egress, "if_index", None) if egress is not None else None
    if if_index is None:
        return "未识别"
    eth_if = getattr(ethernet, "if_index", None) if ethernet is not None else None
    wifi_if = getattr(wifi, "if_index", None) if wifi is not None else None
    if eth_if is not None and if_index == eth_if:
        return "%s（ifIndex=%s）" % (PATH_LABEL["ethernet"], if_index)
    if wifi_if is not None and if_index == wifi_if:
        return "%s（ifIndex=%s）" % (PATH_LABEL["wifi"], if_index)
    return "ifIndex=%s" % if_index


@dataclass
class FailoverStatus:
    """托盘故障切换状态展示模型（方案 §11 全部推荐字段）。"""

    contract_version: str = CONTRACT_VERSION
    mode: FailoverMode = DEFAULT_MODE
    state: FailoverState = FailoverState.ETHERNET_PRIMARY
    preferred: Optional[str] = None
    primary: Optional[str] = None
    backup: Optional[str] = None
    ethernet_status: str = "未解析"
    wifi_status: str = "未解析"
    egress: str = "未识别"
    remote_guard: str = REMOTE_OFF
    #: V0.3 P7：远控详情单行摘要（``""`` = 未接入远程保障探测）。
    remote_detail: str = ""
    #: V0.3 P7：无人值守就绪缺口（来自远程保障的就绪自检；空列表 = 无缺口）。
    remote_warnings: List[str] = field(default_factory=list)
    lock_target: Optional[str] = None
    locked: bool = False
    readonly: bool = True
    clash_keep: bool = DEFAULT_CLASH_KEEP
    authorized: bool = False
    executor_attached: bool = False
    last_switch_at: Optional[float] = None
    last_switch_reason: str = ""
    last_switch_result: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def mode_label(self) -> str:
        return tray_mode_label(self.mode)

    @property
    def state_label(self) -> str:
        return STATE_LABEL.get(self.state, self.state.value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "mode": self.mode.value,
            "mode_label": self.mode_label,
            "state": self.state.value,
            "state_label": self.state_label,
            "preferred": self.preferred,
            "primary": self.primary,
            "backup": self.backup,
            "ethernet_status": self.ethernet_status,
            "wifi_status": self.wifi_status,
            "egress": self.egress,
            "remote_guard": self.remote_guard,
            "remote_detail": self.remote_detail,
            "remote_warnings": list(self.remote_warnings),
            "lock_target": self.lock_target,
            "locked": self.locked,
            "readonly": self.readonly,
            "clash_keep": self.clash_keep,
            "authorized": self.authorized,
            "executor_attached": self.executor_attached,
            "last_switch_at": self.last_switch_at,
            "last_switch_reason": self.last_switch_reason,
            "last_switch_result": self.last_switch_result,
            "warnings": list(self.warnings),
        }

    def lines(self) -> List[str]:
        """人类可读状态块（托盘设置面板 / R-6 界面走查输出）。"""
        lines: List[str] = []
        add = lines.append
        add("V0.3 P6 托盘故障切换状态（模式：%s）" % self.mode_label)
        add("  运行模式    : %s" % self.mode_label)
        add("  当前状态    : %s（%s）" % (self.state.value, self.state_label))
        add("  当前主网络  : %s" % (PATH_LABEL.get(self.primary, "无可用路径")
                                   if self.primary else "无可用路径"))
        add("  当前备用网络: %s" % (PATH_LABEL.get(self.backup, "无")
                                   if self.backup else "无"))
        add("  有线状态    : %s" % self.ethernet_status)
        add("  Wi-Fi 状态  : %s" % self.wifi_status)
        add("  当前系统出口: %s" % self.egress)
        add("  远程保障    : %s" % self.remote_guard)
        if self.remote_detail:
            add("  远控详情    : %s" % self.remote_detail)
        for warning in self.remote_warnings:
            add("  无人值守缺口: %s" % warning)
        add("  手动锁定    : %s"
            % (PATH_LABEL.get(self.lock_target, self.lock_target)
               if self.locked else "未锁定"))
        if self.last_switch_at is not None:
            add("  最近切换    : t=%.0fs · %s · %s"
                % (self.last_switch_at, self.last_switch_reason or "未记录",
                   self.last_switch_result or "未记录"))
        else:
            add("  最近切换    : 无")
        add("  Clash 策略  : %s"
            % ("网络切换期间保持原样" if self.clash_keep else "允许 Clash 联动"))
        add("  本轮副作用  : %s" % ("只读（零执行）" if self.readonly else "已执行动作"))
        add("  执行层      : %s"
            % ("已接入（显式授权）" if self.executor_attached else "未接入"))
        for warning in self.warnings:
            add("  告警        : %s" % warning)
        return lines


def format_status(status: FailoverStatus) -> str:
    """把 :class:`FailoverStatus` 渲染为文本块。"""
    return "\n".join(status.lines())


# --------------------------------------------------------------------------- #
# 桥接层
# --------------------------------------------------------------------------- #
class TrayFailoverBridge:
    """托盘与 P3 状态机之间的桥接（P6）。

    - 持有 :class:`NetworkFailoverManager`，把只读快照喂给它并回传
      :class:`TickResult`；
    - 依据「模式 + 显式授权」决定是否接入执行器工厂产出的真实执行器
      （默认**不接入**，动作"已批准但未执行"）；
    - 记录最近一次**实际执行**的路由切换（时间 / 原因 / 结果）供展示；
    - **单一副作用门**由 P3 的 ``NetworkOpScheduler`` 保证，本层不新增执行点。
    """

    def __init__(self, *, mode: Any = DEFAULT_MODE,
                 lock_target: Any = None,
                 clash_keep: bool = DEFAULT_CLASH_KEEP,
                 authorized: bool = False,
                 executor_factory: Optional[Callable[[], Any]] = None,
                 policy: Optional[FailoverPolicy] = None,
                 timer_table: Optional[Any] = None,
                 remote_guard: Optional[Any] = None,
                 defer_failback: bool = True,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.mode = parse_mode(mode)
        self.lock_target = parse_lock(lock_target)
        self.clash_keep = bool(clash_keep)
        self.authorized = bool(authorized)
        self.executor_factory = executor_factory
        self.policy = policy or FailoverPolicy()
        self._log = log
        #: V0.3 P7 远程保障守卫（``campus_remote.RemoteSafetyGuard``）；为 ``None``
        #: 时不接入远程会话延迟切回，展示层保持 P6 语义（零行为变化）。
        self.remote_guard = remote_guard
        self.defer_failback = bool(defer_failback)
        self.manager = NetworkFailoverManager(
            policy=self.policy, mode=self.mode, timer_table=timer_table,
            executor=None, lock_target=self.lock_target,
            failback_gate=self._failback_gate(), log=log)
        self.last_switch_at: Optional[float] = None
        self.last_switch_reason: str = ""
        self.last_switch_result: str = ""
        self.last_result: Optional[TickResult] = None
        self._last_snapshot: Optional[NetworkHealthSnapshot] = None
        self._executor: Optional[Any] = None
        #: V0.3 P7.5-1 快照可信度门禁：``None`` = 未评估；``False`` = 数据不足，
        #: AUTO 下**拒绝**执行任何网络副作用（F75-01）。
        self.snapshot_trust: Optional[bool] = None
        self.snapshot_reason: str = ""
        self._sync_executor()

    # ---- 执行器接线（默认不接入；显式授权 + 工厂齐备才接入） ---- #
    @property
    def executor_attached(self) -> bool:
        """单一副作用门当前是否已接入真实执行器（未接入=动作仅计划不执行）。"""
        return self.manager.scheduler.executor is not None

    def set_snapshot_trust(self, trusted: bool, reason: str = "") -> bool:
        """登记本轮快照的可信度，据此门控单一副作用门（P7.5-1）。

        - ``trusted=True`` → 按「模式 + 授权 + 工厂」恢复真实执行器；
        - ``trusted=False`` → 换成 :class:`RefusingExecutor`：动作照常登记为
          "已尝试"，但 ``executed`` 恒为 ``False``，Portal POST / Wi-Fi 重连 /
          Metric 写入**一个都不会发生**。
        """
        value = bool(trusted)
        if self.snapshot_trust is not value or reason != self.snapshot_reason:
            self._note("三轴快照可信度：%s（%s）"
                       % ("可信" if value else "不可信", reason or "未说明"))
        self.snapshot_trust = value
        self.snapshot_reason = str(reason or "")
        self._sync_executor()
        return value

    def _sync_executor(self) -> None:
        """按「快照可信度 + 模式 + 授权 + 工厂」决定单一副作用门的执行器。

        门禁顺序（P7.5-1：**数据可信优先于授权**）：

        1. 快照不可信 → :class:`RefusingExecutor`（零副作用，如实登记拒绝）；
        2. 非 AUTO / 未授权 / 无工厂 → ``None``（动作仅计划不执行，P6 语义）；
        3. AUTO + 已授权 + 工厂齐备 → 真实执行器。
        """
        if self.snapshot_trust is False:
            self.manager.scheduler.executor = RefusingExecutor(
                "三轴快照不可信：%s" % (self.snapshot_reason or "数据不足"),
                log=self._log)
            return
        want = (self.mode is FailoverMode.AUTO and self.authorized
                and self.executor_factory is not None)
        if want:
            if self._executor is None:
                try:
                    self._executor = self.executor_factory()
                except Exception as exc:            # 工厂故障不得让托盘崩溃
                    self._note("执行器工厂构造失败：%s: %s"
                               % (type(exc).__name__, exc))
                    self._executor = None
            self.manager.scheduler.executor = self._executor
        else:
            self.manager.scheduler.executor = None

    # ---- 远程保障接线（V0.3 P7）---- #
    def _failback_gate(self) -> Optional[Callable[[float], Any]]:
        """按「守卫在位 + 延迟开关」决定是否把切回门禁接入状态机。"""
        guard = self.remote_guard
        if guard is None or not self.defer_failback:
            return None
        gate = getattr(guard, "failback_gate", None)
        return gate if callable(gate) else None

    def set_remote_guard(self, guard: Optional[Any],
                         defer_failback: Optional[bool] = None) -> Optional[Any]:
        """接入/摘除远程保障守卫（并同步切回门禁）。"""
        self.remote_guard = guard
        if defer_failback is not None:
            self.defer_failback = bool(defer_failback)
        self.manager.failback_gate = self._failback_gate()
        self._note("远程保障守卫：%s（切回门禁 %s）"
                   % ("已接入" if guard is not None else "未接入",
                      "开启" if self.manager.failback_gate else "关闭"))
        return guard

    def remote_status(self) -> Optional[Any]:
        """远程保障守卫的最近状态（未接入时为 ``None``）。"""
        guard = self.remote_guard
        if guard is None:
            return None
        getter = getattr(guard, "status", None)
        try:
            return getter() if callable(getter) else None
        except Exception:                        # 守卫故障不得让托盘崩溃
            return None

    # ---- 外部控制 ---- #
    def set_mode(self, mode: Any) -> FailoverMode:
        self.mode = parse_mode(mode)
        self.manager.mode = self.mode
        self.manager.scheduler.mode = self.mode
        self._sync_executor()
        self._note("运行模式切换为：%s" % self.mode.label)
        return self.mode

    def set_lock_target(self, target: Any) -> Optional[str]:
        self.lock_target = parse_lock(target)
        self.manager.set_lock_target(self.lock_target)
        return self.lock_target

    def set_clash_keep(self, flag: bool) -> bool:
        self.clash_keep = bool(flag)
        return self.clash_keep

    def set_authorized(self, flag: bool) -> bool:
        self.authorized = bool(flag)
        self._sync_executor()
        self._note("智能自动授权：%s" % ("已开启" if self.authorized else "已关闭"))
        return self.authorized

    def pause(self, now: float, reason: str = "用户暂停") -> None:
        self.manager.pause(now, reason)

    def resume(self, now: float) -> None:
        self.manager.resume(now)

    def next_detection_at(self) -> float:
        return self.manager.next_detection_at()

    # ---- 主循环 ---- #
    def tick(self, now: float, snapshot: NetworkHealthSnapshot) -> TickResult:
        """推进一轮：喂入只读快照，回传 :class:`TickResult`。"""
        self._last_snapshot = snapshot
        result = self.manager.tick(now, snapshot)
        self.last_result = result
        self._record_switch(now, result)
        return result

    def _record_switch(self, now: float, result: TickResult) -> None:
        """记录最近一次**实际执行**的路由切换（时间 / 原因 / 结果）。"""
        plans = [p for p in result.plan if p.kind is OpKind.METRIC_WRITE]
        executed = [o for o in result.executed if o.kind is OpKind.METRIC_WRITE]
        if executed:
            self.last_switch_at = now
            self.last_switch_reason = plans[0].reason if plans else ""
            self.last_switch_result = executed[0].detail or "已执行"

    # ---- 展示 ---- #
    def status(self, snapshot: Optional[NetworkHealthSnapshot] = None
               ) -> FailoverStatus:
        snap = snapshot if snapshot is not None else self._last_snapshot
        result = self.last_result
        manager = self.manager
        locked = bool(manager.mode is FailoverMode.LOCKED and manager.lock_target)
        if result is not None:
            preferred = result.preferred
        elif locked:
            preferred = manager.lock_target
        else:
            preferred = manager.state.preferred
        primary, backup = _primary_backup(preferred)

        ethernet = getattr(snap, "ethernet", None) if snap is not None else None
        wifi = getattr(snap, "wifi", None) if snap is not None else None
        warnings: List[str] = []
        if result is not None:
            planned = any(p.kind is OpKind.METRIC_WRITE for p in result.plan)
            done = any(o.kind is OpKind.METRIC_WRITE for o in result.executed)
            if planned and not done:
                warnings.append("本轮已计划路由切换但未执行（模式 %s）"
                                % result.mode.value)
            warnings.extend(n for n in result.notes if "告警" in n)
        if self.snapshot_trust is False:
            # V0.3 P7.5-1：数据不可信时明确告知"不会执行"，而不是静默无动作。
            warnings.append("三轴快照不可信（%s）：本轮不执行任何网络副作用"
                            % (self.snapshot_reason or "数据不足"))
        if manager.mode is FailoverMode.AUTO and not self.authorized:
            warnings.append("智能自动未授权：动作仅计划不执行（默认仅监测语义）")
        if locked and result is None:
            warnings.append("手动锁定：等待首次只读观察")

        return FailoverStatus(
            mode=manager.mode, state=manager.state, preferred=preferred,
            primary=primary, backup=backup,
            ethernet_status=_ethernet_status_text(ethernet),
            wifi_status=_wifi_status_text(wifi),
            egress=_egress_text(snap, ethernet, wifi),
            remote_guard=self._remote_guard(),
            remote_detail=self._remote_detail(),
            remote_warnings=self._remote_warnings(),
            lock_target=manager.lock_target, locked=locked,
            readonly=(result.readonly if result is not None else True),
            clash_keep=self.clash_keep, authorized=self.authorized,
            executor_attached=self.executor_attached,
            last_switch_at=self.last_switch_at,
            last_switch_reason=self.last_switch_reason,
            last_switch_result=self.last_switch_result,
            warnings=warnings)

    def _remote_guard(self) -> str:
        state = self.manager.state
        if state is FailoverState.PAUSED:
            return REMOTE_PAUSED
        if state is FailoverState.MANUAL_REQUIRED:
            return REMOTE_MANUAL
        if self.manager.mode is FailoverMode.AUTO and self.authorized \
                and self.executor_attached:
            status = self.remote_status()
            if status is not None and getattr(status, "sensitive", False):
                # P7：远程会话进行中 —— 保障启用但推迟自动切回（方案 §4.3）。
                return REMOTE_SENSITIVE
            return REMOTE_ENABLED
        return REMOTE_OFF

    def _remote_detail(self) -> str:
        """远控详情单行摘要（未接入守卫时为 ``""``，展示层保持 P6 语义）。"""
        status = self.remote_status()
        if status is None:
            return ""
        clients = ", ".join(getattr(status, "clients", None) or []) or "无"
        return ("远控 %s · 客户端 %s · 已建立 %d 条 · 监听 %s · UDP %d 个"
                % (getattr(status, "activity", "UNKNOWN"), clients,
                   int(getattr(status, "established", 0) or 0),
                   "有" if getattr(status, "listening", False) else "无",
                   int(getattr(status, "udp_endpoints", 0) or 0)))

    def _remote_warnings(self) -> List[str]:
        """无人值守就绪缺口（未执行就绪自检时为空）。"""
        guard = self.remote_guard
        report = getattr(guard, "last_readiness", None) if guard else None
        if report is None:
            return []
        return list(getattr(report, "warnings", None) or [])

    def _note(self, message: str) -> None:
        if self._log is not None:
            self._log(message)
