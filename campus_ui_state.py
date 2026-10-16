# -*- coding: utf-8 -*-
"""V0.5-5 前端 UI 状态映射（纯逻辑：**零 I/O / 零 Tk / 零时钟 / 零副作用**）。

方案 §5.5：主窗口仪表盘只消费**状态映射**，网络操作仍在后台线程；
``tests/unit/test_v05_ui_state.py`` 不依赖截图。

职责边界
--------
- **只读**：不建 socket、不发请求、不清退进程、不读注册表、不碰 Tk；
  ``now`` 一律由调用方传入（本模块不持有时钟，便于离线测试）。
- 把四类既有真值映射成界面可直接渲染的视图模型：
  ``HealthState``（:mod:`campus_network`）/ ``FailoverState``（:mod:`campus_failover`）/
  整合层验证记录（``DualPathStatus.ethernet`` / ``.wifi``）/ UU 恢复状态。
- **不重写判定**：``Connected ≠ Internet`` 的真值仍由 V0.5-1 的接口受限验证给出
  （``allows_recovery``）；本模块只把它翻译成"已验证 / 未验证 / 探测不可信"，
  并且**永不**把 ``PROBE_UNRELIABLE`` 显示成"网络可用"或"网络不可用"。
- 视图模型是 ``frozen`` 数据对象，选择项极少；便于单元测试逐字段断言。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from campus_dual_path import DEFAULT_VERIFICATION_MAX_AGE
from campus_failover import PATH_LABEL, FailoverState
from campus_network import HealthState
from campus_reconnect import PATH_PROBE_UNRELIABLE

__all__ = [
    "TONE_OK", "TONE_WARN", "TONE_BAD", "TONE_BUSY", "TONE_IDLE",
    "TONE_UNKNOWN", "TONE_COLOR",
    "OVERALL_HEALTHY", "OVERALL_DEGRADED", "OVERALL_RECOVERING",
    "OVERALL_OFFLINE", "OVERALL_PAUSED", "OVERALL_UNKNOWN",
    "OVERALL_LABEL", "OVERALL_TONE",
    "STEP_PENDING", "STEP_ACTIVE", "STEP_DONE", "STEP_FAILED",
    "STEP_SKIPPED", "STEP_DISABLED", "STEP_TONE", "PIPELINE_LABELS",
    "STEP_STATUS_LABEL", "CARD_ROWS",
    "ROLE_PRIMARY", "ROLE_STANDBY",
    "PathCard", "StepView", "UuView", "UiState",
    "health_view", "axis_state", "build_path_card", "build_pipeline",
    "overall_status", "build_ui_state",
    "verification_age", "verification_verified", "verification_fresh_pass",
    "verification_lines",
]

# --------------------------------------------------------------------------- #
# 语义色调（Tk 直接消费；不进业务判定）
# --------------------------------------------------------------------------- #
TONE_OK = "ok"              # 绿：确认为真
TONE_WARN = "warn"          # 黄：连接可用但 Internet 未证实 / 需认证 / 已过期
TONE_BAD = "bad"            # 红：明确失败
TONE_BUSY = "busy"          # 蓝：进行中（切换 / 验证 / 恢复）
TONE_IDLE = "idle"          # 灰：未开始 / 不适用
TONE_UNKNOWN = "unknown"    # 深灰：探测不可信（不猜）

TONE_COLOR: Dict[str, str] = {
    TONE_OK: "#3fb950",
    TONE_WARN: "#d29922",
    TONE_BAD: "#f85149",
    TONE_BUSY: "#58a6ff",
    TONE_IDLE: "#8b949e",
    TONE_UNKNOWN: "#6e7681",
}

# --------------------------------------------------------------------------- #
# 网络总体状态灯（方案 §5.5：Healthy / Degraded / Recovering / Offline / Unknown）
# --------------------------------------------------------------------------- #
OVERALL_HEALTHY = "HEALTHY"
OVERALL_DEGRADED = "DEGRADED"
OVERALL_RECOVERING = "RECOVERING"
OVERALL_OFFLINE = "OFFLINE"
OVERALL_PAUSED = "PAUSED"
OVERALL_UNKNOWN = "UNKNOWN"

OVERALL_LABEL: Dict[str, str] = {
    OVERALL_HEALTHY: "网络正常",
    OVERALL_DEGRADED: "网络降级",
    OVERALL_RECOVERING: "恢复中",
    OVERALL_OFFLINE: "离线",
    OVERALL_PAUSED: "已暂停",
    OVERALL_UNKNOWN: "未知",
}
OVERALL_TONE: Dict[str, str] = {
    OVERALL_HEALTHY: TONE_OK,
    OVERALL_DEGRADED: TONE_WARN,
    OVERALL_RECOVERING: TONE_BUSY,
    OVERALL_OFFLINE: TONE_BAD,
    OVERALL_PAUSED: TONE_IDLE,
    OVERALL_UNKNOWN: TONE_UNKNOWN,
}

# --------------------------------------------------------------------------- #
# 恢复流水线（方案 §5.5：检测 → Wi-Fi Ready → Portal → Internet Verify → Route Switch → UU Restart）
# --------------------------------------------------------------------------- #
STEP_PENDING = "pending"
STEP_ACTIVE = "active"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_DISABLED = "disabled"

STEP_TONE: Dict[str, str] = {
    STEP_DONE: TONE_OK,
    STEP_ACTIVE: TONE_BUSY,
    STEP_FAILED: TONE_BAD,
    STEP_SKIPPED: TONE_IDLE,
    STEP_DISABLED: TONE_IDLE,
    STEP_PENDING: TONE_IDLE,
}

PIPELINE_LABELS: Tuple[Tuple[str, str], ...] = (
    ("detect", "检测"),
    ("wifi_ready", "Wi-Fi Ready"),
    ("portal", "Portal"),
    ("internet", "Internet Verify"),
    ("route", "Route Switch"),
    ("uu", "UU Restart"),
)

#: 流水线阶段状态的中文标签（仪表盘直接渲染）。
STEP_STATUS_LABEL: Dict[str, str] = {
    STEP_PENDING: "待执行",
    STEP_ACTIVE: "进行中",
    STEP_DONE: "完成",
    STEP_FAILED: "失败",
    STEP_SKIPPED: "已跳过",
    STEP_DISABLED: "未启用",
}

#: 网卡卡片固定行（顺序即展示顺序；"链路"行对 Wi-Fi 显示为 "Wi-Fi"）。
CARD_ROWS: Tuple[str, ...] = (
    "角色", "状态", "源地址", "网关", "ifIndex", "DNS", "链路",
    "Portal", "Internet", "最近验证",
)

ROLE_PRIMARY = "PRIMARY"
ROLE_STANDBY = "STANDBY"

#: 明确"链路都没起来"的健康态（用于 OFFLINE / Wi-Fi Ready 判定）。
_HARD_DOWN = frozenset({HealthState.LINK_DOWN, HealthState.NO_IP})

#: 已具备 IP/路由（可能仍需认证或上不了公网）的健康态 ⇒ Wi-Fi Ready 成立。
_READY = frozenset({
    HealthState.HEALTHY, HealthState.CAPTIVE_PORTAL,
    HealthState.INTERNET_UNREACHABLE, HealthState.DNS_FAILURE,
    HealthState.GATEWAY_UNREACHABLE,
})

#: 状态机处于"正在切换/恢复"的状态（⇒ 总体灯 RECOVERING）。
_TRANSITION_STATES = frozenset({
    FailoverState.WIFI_PREPARING, FailoverState.WIFI_AUTHENTICATING,
    FailoverState.WIFI_SWITCHING, FailoverState.ETHERNET_RECOVERY_CHECK,
    FailoverState.ETHERNET_FAILBACK,
})

#: 健康态 →（色调，中文标签）。**区分 LINK_UP 与 INTERNET_VERIFIED**（方案 §5.2）。
_HEALTH_VIEW: Dict[HealthState, Tuple[str, str]] = {
    HealthState.HEALTHY: (TONE_OK, "正常"),
    HealthState.LINK_DOWN: (TONE_BAD, "未连接"),
    HealthState.NO_IP: (TONE_BAD, "已连接（无有效地址）"),
    HealthState.GATEWAY_UNREACHABLE: (TONE_BAD, "网关不可达"),
    HealthState.DNS_FAILURE: (TONE_WARN, "DNS 异常"),
    HealthState.CAPTIVE_PORTAL: (TONE_WARN, "已连接（需要认证）"),
    HealthState.INTERNET_UNREACHABLE: (TONE_BAD, "已连接（公网不可达）"),
    HealthState.PROBE_UNRELIABLE: (TONE_UNKNOWN, "不可判定（探测不可信）"),
    HealthState.UNKNOWN: (TONE_UNKNOWN, "未知"),
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """鸭子类型取属性（对 ``None`` 安全）—— 与 ``campus_dual_path._attr`` 同义。"""
    if obj is None:
        return default
    return getattr(obj, name, default)


def axis_state(health: Any) -> Optional[HealthState]:
    """接口轴健康结论（``None`` 表示角色未解析 / 快照缺失）。"""
    return _attr(health, "state", None)


def health_view(state: Any) -> Tuple[str, str]:
    """健康态 →（色调，标签）；未知取值回退到"未知"。"""
    if isinstance(state, HealthState):
        return _HEALTH_VIEW.get(state, (TONE_UNKNOWN, "未知"))
    return (TONE_UNKNOWN, "未知")


def _verification_record(record: Any) -> Tuple[Optional[float], Any]:
    """把整合层的 ``(at, result)`` 记录归一为 ``(at, result)``（容错）。"""
    if record is None:
        return None, None
    if isinstance(record, (tuple, list)) and len(record) >= 2:
        at = record[0]
        return (None if at is None else float(at)), record[1]
    return None, record


def verification_age(record: Any, now: float) -> Optional[float]:
    """验证记录的年龄（秒）；无记录返回 ``None``。"""
    at, _result = _verification_record(record)
    if at is None:
        return None
    return max(0.0, float(now) - at)


def verification_verified(record: Any) -> bool:
    """该记录是否"接口受限验证通过"（只看 ``allows_recovery``，fail-closed）。"""
    _at, result = _verification_record(record)
    return bool(_attr(result, "allows_recovery", False))


def verification_fresh_pass(record: Any, now: float,
                            max_age: float = DEFAULT_VERIFICATION_MAX_AGE) -> bool:
    """是否有一条**新鲜的**接口受限验证通过记录（门禁与总体灯共用同一口径）。"""
    if not verification_verified(record):
        return False
    age = verification_age(record, now)
    return age is not None and age <= float(max_age)


def _internet_view(record: Any, now: float,
                   max_age: float) -> Tuple[str, str, Optional[bool]]:
    """Internet 行文案 →（文案，色调，是否已验证）。

    **禁止**把 ``PROBE_UNRELIABLE`` / ``CAPTIVE_PORTAL`` 展示成"已验证"。
    """
    at, result = _verification_record(record)
    if at is None and result is None:
        return "未验证", TONE_IDLE, None
    allows = bool(_attr(result, "allows_recovery", False))
    reliability = str(_attr(result, "reliability", "") or "")
    captive = bool(_attr(result, "captive_portal", False))
    age = None if at is None else max(0.0, float(now) - at)
    if allows:
        if age is not None and age > float(max_age):
            return "已验证（已过期 %.0fs）" % age, TONE_WARN, True
        return ("已验证（%.0fs 前）" % age) if age is not None else "已验证", TONE_OK, True
    if reliability == PATH_PROBE_UNRELIABLE:
        return "探测不可信（不做破坏性动作）", TONE_UNKNOWN, False
    if captive:
        return "门户劫持（未真正上网）", TONE_WARN, False
    return ("未通过（%s）" % (_attr(result, "reason", "") or "探测失败"),
            TONE_BAD, False)


def _portal_view(state: Any) -> Tuple[str, str]:
    if state is HealthState.CAPTIVE_PORTAL:
        return "需要认证", TONE_WARN
    if state is HealthState.HEALTHY:
        return "已通过", TONE_OK
    return "—", TONE_IDLE


def _sites_text(result: Any) -> str:
    """逐站结果摘要（"立即验证"按钮展示用）。"""
    ok = [str(site) for site in (_attr(result, "successful_sites", ()) or ())]
    bad = [str(site) for site in (_attr(result, "failed_sites", ()) or ())]
    if not ok and not bad:
        return ""
    parts = []
    if ok:
        parts.append("通过 %d（%s）" % (len(ok), "、".join(ok)))
    if bad:
        parts.append("失败 %d（%s）" % (len(bad), "、".join(bad)))
    return "；".join(parts)


def _result_tone(result: Any) -> str:
    if bool(_attr(result, "allows_recovery", False)):
        return TONE_OK
    reliability = str(_attr(result, "reliability", "") or "")
    if reliability == PATH_PROBE_UNRELIABLE:
        return TONE_UNKNOWN
    if bool(_attr(result, "captive_portal", False)):
        return TONE_WARN
    return TONE_BAD


def verification_lines(result: Any, *, role: str = "") -> List[str]:
    """一次（手动或自动）验证的人类可读逐站明细。"""
    if result is None:
        return []
    title = PATH_LABEL.get(role, role or _attr(result, "role", "") or "路径")
    verdict = "接口受限验证通过" if bool(_attr(result, "allows_recovery", False)) \
        else "接口受限验证未通过"
    out = ["%s：%s" % (title, verdict)]
    reason = str(_attr(result, "reason", "") or "")
    if reason:
        out.append("判据：%s" % reason)
    sites = _sites_text(result)
    if sites:
        out.append("逐站：%s" % sites)
    source_ip = str(_attr(result, "source_ip", "") or "")
    if source_ip:
        out.append("源地址：%s" % source_ip)
    return out


# --------------------------------------------------------------------------- #
# 视图模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PathCard:
    """一张网卡卡片（Ethernet / SNNU Wi-Fi）的只读视图模型。"""

    role: str
    title: str
    assignment: str                       # PRIMARY / STANDBY / ""
    tone: str
    state_label: str
    internet_tone: str
    verified: Optional[bool]              # True/False/None(=未验证)
    fields: Tuple[Tuple[str, str], ...] = ()
    manual: Tuple[str, ...] = ()          # 最近一次手动验证的逐站明细

    @property
    def assignment_label(self) -> str:
        return self.assignment or "—"

    @property
    def color(self) -> str:
        """卡片状态色调（Tk 直接消费）。"""
        return TONE_COLOR.get(self.tone, TONE_COLOR[TONE_UNKNOWN])

    def field(self, label: str, default: str = "—") -> str:
        for name, value in self.fields:
            if name == label:
                return value
        return default

    def lines(self) -> List[str]:
        out = ["%s（%s）" % (self.title, self.assignment_label)]
        out.extend("  %-10s: %s" % (name, value) for name, value in self.fields)
        out.extend("  » %s" % line for line in self.manual)
        return out


@dataclass(frozen=True)
class StepView:
    """恢复流水线的一个阶段。"""

    key: str
    label: str
    status: str
    detail: str = ""

    @property
    def tone(self) -> str:
        return STEP_TONE.get(self.status, TONE_IDLE)

    @property
    def color(self) -> str:
        """阶段色调（Tk 直接消费）。"""
        return TONE_COLOR.get(self.tone, TONE_COLOR[TONE_IDLE])

    @property
    def display(self) -> str:
        """仪表盘两行文案：阶段名 + 状态（中文）。"""
        return "%s\n%s" % (self.label,
                           STEP_STATUS_LABEL.get(self.status, self.status))

    def lines(self) -> List[str]:
        return ["%s：%s%s" % (self.label, self.status,
                              ("（%s）" % self.detail) if self.detail else "")]


@dataclass(frozen=True)
class UuView:
    """UU 远程恢复状态（方案 §5.4）。"""

    enabled: bool = False
    label: str = "未开启（默认关闭）"
    tone: str = TONE_IDLE
    last_ok: Optional[bool] = None
    detail: str = ""

    def lines(self) -> List[str]:
        return ["UU 远程自动恢复：%s%s"
                % (self.label, ("（%s）" % self.detail) if self.detail else "")]


@dataclass(frozen=True)
class UiState:
    """整个仪表盘的一次只读渲染输入（``build_ui_state`` 的产物）。"""

    overall: str = OVERALL_UNKNOWN
    headline: str = ""
    active_path: Optional[str] = None
    standby_path: Optional[str] = None
    mode_label: str = ""
    monitor_label: str = ""
    network_epoch: int = 0
    ethernet: Optional[PathCard] = None
    wifi: Optional[PathCard] = None
    pipeline: Tuple[StepView, ...] = ()
    uu: UuView = field(default_factory=UuView)
    events: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    verification_roles: Tuple[str, ...] = ()

    @property
    def overall_label(self) -> str:
        return OVERALL_LABEL.get(self.overall, OVERALL_LABEL[OVERALL_UNKNOWN])

    @property
    def overall_tone(self) -> str:
        return OVERALL_TONE.get(self.overall, TONE_UNKNOWN)

    @property
    def overall_color(self) -> str:
        return TONE_COLOR[self.overall_tone]

    def card(self, role: str) -> Optional[PathCard]:
        if role == "ethernet":
            return self.ethernet
        if role == "wifi":
            return self.wifi
        return None

    def step(self, key: str) -> Optional[StepView]:
        for view in self.pipeline:
            if view.key == key:
                return view
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall": self.overall,
            "overall_label": self.overall_label,
            "headline": self.headline,
            "active_path": self.active_path,
            "standby_path": self.standby_path,
            "mode_label": self.mode_label,
            "monitor_label": self.monitor_label,
            "network_epoch": self.network_epoch,
            "pipeline": [{"key": s.key, "status": s.status, "detail": s.detail}
                         for s in self.pipeline],
            "uu": {"enabled": self.uu.enabled, "label": self.uu.label,
                   "last_ok": self.uu.last_ok},
            "events": list(self.events),
            "warnings": list(self.warnings),
            "verification_roles": list(self.verification_roles),
            "ethernet": (self.ethernet.state_label if self.ethernet else None),
            "wifi": (self.wifi.state_label if self.wifi else None),
        }

    def lines(self) -> List[str]:
        out = ["网络总体：%s（%s）" % (self.overall_label, self.overall),
               "概要：%s" % (self.headline or "—")]
        if self.ethernet is not None:
            out.extend(self.ethernet.lines())
        if self.wifi is not None:
            out.extend(self.wifi.lines())
        out.append("恢复流水线：%s"
                   % " → ".join("%s[%s]" % (s.label, s.status) for s in self.pipeline))
        out.extend(self.uu.lines())
        for line in self.events:
            out.append("事件：%s" % line)
        for line in self.warnings:
            out.append("告警：%s" % line)
        return out


# --------------------------------------------------------------------------- #
# 映射函数
# --------------------------------------------------------------------------- #
def _assignment(role: str, active_path: Optional[str]) -> str:
    if active_path is None:
        return ""
    return ROLE_PRIMARY if role == active_path else ROLE_STANDBY


def build_path_card(*, role: str, health: Any, assignment: str,
                    verification: Any = None, now: float = 0.0,
                    max_age: float = DEFAULT_VERIFICATION_MAX_AGE,
                    ssid: str = "SNNU", manual: Sequence[str] = ()) -> PathCard:
    """由接口健康结论 + 验证记录构造一张网卡卡片（**零 I/O**）。"""
    state = axis_state(health)
    tone, label = health_view(state)
    internet, internet_tone, verified = _internet_view(verification, now, max_age)
    portal, _portal_tone = _portal_view(state)
    if_index = _attr(health, "if_index", None)
    source_ip = str(_attr(health, "source_ip", "") or "") or "—"
    dns = [str(server) for server in (_attr(health, "dns_servers", ()) or ())]
    gateway = str(_attr(health, "gateway", "") or "") or "—"
    if role == "wifi":
        title = "SNNU Wi-Fi"
        link_label, link_value = "Wi-Fi", (ssid or "SNNU")
    else:
        title = "以太网"
        link_label = "网卡"
        link_value = str(_attr(health, "interface", "") or "") or "—"
    age = verification_age(verification, now)
    fields = (
        ("角色", assignment or "—"),
        ("状态", label),
        ("源地址", source_ip),
        ("网关", gateway),
        ("ifIndex", "—" if if_index is None else str(if_index)),
        ("DNS", "、".join(dns) or "—"),
        (link_label, link_value),
        ("Portal", portal),
        ("Internet", internet),
        ("最近验证", "—" if age is None else "%.0fs 前" % age),
    )
    return PathCard(role=role, title=title, assignment=assignment, tone=tone,
                    state_label=label, internet_tone=internet_tone,
                    verified=verified, fields=fields, manual=tuple(manual))


def _step(key: str, status: str, detail: str = "") -> StepView:
    label = dict(PIPELINE_LABELS).get(key, key)
    return StepView(key=key, label=label, status=status, detail=detail)


def build_pipeline(*, failover_state: Any, ethernet: Any, wifi: Any,
                   verification: Mapping[str, Any], active_path: Optional[str],
                   verify_roles: Sequence[str] = (), uu_enabled: bool = False,
                   uu_last_ok: Optional[bool] = None, uu_pending: bool = False,
                   uu_detail: str = "", now: float = 0.0,
                   max_age: float = DEFAULT_VERIFICATION_MAX_AGE
                   ) -> Tuple[StepView, ...]:
    """把状态机当前状态 / 验证记录 / UU 状态映射成 6 段恢复流水线。"""
    roles = tuple(str(role) for role in (verify_roles or ()))
    eth_state = axis_state(ethernet)
    wifi_state = axis_state(wifi)

    # 1) 检测
    if failover_state is None and eth_state is None and wifi_state is None:
        detect = _step("detect", STEP_PENDING, "等待首轮检测")
    else:
        detect = _step("detect", STEP_DONE, "双链路已观测")

    # 2) Wi-Fi Ready
    if wifi_state is None:
        wifi_ready = _step("wifi_ready", STEP_PENDING, "尚未解析无线网卡")
    elif wifi_state in _READY:
        wifi_ready = _step("wifi_ready", STEP_DONE, "链路/IP 已就绪")
    elif wifi_state in _HARD_DOWN:
        wifi_ready = _step("wifi_ready", STEP_PENDING, "未连接")
    else:
        wifi_ready = _step("wifi_ready", STEP_PENDING, str(_attr(wifi_state, "value", "")))

    # 3) Portal
    if failover_state is FailoverState.WIFI_AUTHENTICATING:
        portal = _step("portal", STEP_ACTIVE, "SNNU 认证中")
    elif eth_state is HealthState.CAPTIVE_PORTAL or wifi_state is HealthState.CAPTIVE_PORTAL:
        portal = _step("portal", STEP_ACTIVE, "需要认证")
    elif eth_state is HealthState.HEALTHY or wifi_state is HealthState.HEALTHY:
        portal = _step("portal", STEP_DONE, "无需认证")
    else:
        portal = _step("portal", STEP_PENDING, "未认证")

    # 4) Internet Verify（真值来自接口受限验证，不看 Windows Connected）
    active_record = verification.get(active_path) if active_path else None
    verifying = bool(roles)
    if verification_fresh_pass(active_record, now, max_age):
        internet = _step("internet", STEP_DONE, "接口受限验证通过")
    elif verifying:
        internet = _step("internet", STEP_ACTIVE,
                         "验证中：%s" % "、".join(PATH_LABEL.get(r, r) for r in roles))
    elif active_record is not None:
        _text, _tone, _verified = _internet_view(active_record, now, max_age)
        internet = _step("internet", STEP_FAILED, _text)
    else:
        internet = _step("internet", STEP_PENDING, "未验证")

    # 5) Route Switch
    if failover_state is FailoverState.WIFI_BACKUP_ACTIVE:
        route = _step("route", STEP_DONE, "Wi-Fi 已接管")
    elif failover_state is FailoverState.ETHERNET_FAILBACK:
        route = _step("route", STEP_ACTIVE, "切回有线中")
    elif failover_state in (FailoverState.WIFI_PREPARING, FailoverState.WIFI_SWITCHING):
        route = _step("route", STEP_ACTIVE, "切换中")
    elif failover_state is FailoverState.ETHERNET_RECOVERY_CHECK:
        route = _step("route", STEP_PENDING, "有线恢复观察中")
    elif failover_state is FailoverState.ETHERNET_PRIMARY:
        route = _step("route", STEP_DONE, "以太网出口")
    elif failover_state is FailoverState.PAUSED:
        route = _step("route", STEP_SKIPPED, "监测已暂停")
    elif failover_state is FailoverState.BOTH_OFFLINE:
        route = _step("route", STEP_PENDING, "两条路径均不可用")
    elif failover_state is FailoverState.MANUAL_REQUIRED:
        route = _step("route", STEP_PENDING, "需人工处理")
    else:
        route = _step("route", STEP_PENDING, "等待切换条件")

    # 6) UU Restart（恢复流水线**最后一步**）
    if not uu_enabled:
        uu = _step("uu", STEP_DISABLED, "UU 自动恢复未开启")
    elif uu_pending:
        uu = _step("uu", STEP_ACTIVE, uu_detail or "准备清退重启")
    elif uu_last_ok is True:
        uu = _step("uu", STEP_DONE, uu_detail or "已清退重启")
    elif uu_last_ok is False:
        uu = _step("uu", STEP_FAILED, uu_detail or "上次未成功")
    else:
        uu = _step("uu", STEP_PENDING, "等待验证通过")

    return (detect, wifi_ready, portal, internet, route, uu)


def overall_status(*, failover_state: Any, ethernet: Any, wifi: Any,
                   verification: Mapping[str, Any], active_path: Optional[str],
                   verify_roles: Sequence[str] = (), recovery_event: bool = False,
                   paused: bool = False, monitor_alive: bool = True,
                   warnings: Sequence[str] = (), now: float = 0.0,
                   max_age: float = DEFAULT_VERIFICATION_MAX_AGE) -> str:
    """网络总体状态灯（单一确定口径；所有输入都是只读状态）。"""
    if paused or failover_state is FailoverState.PAUSED:
        return OVERALL_PAUSED
    eth_state = axis_state(ethernet)
    wifi_state = axis_state(wifi)
    if failover_state is None and eth_state is None and wifi_state is None:
        return OVERALL_UNKNOWN
    if failover_state is FailoverState.BOTH_OFFLINE:
        return OVERALL_OFFLINE
    if failover_state in _TRANSITION_STATES or verify_roles or recovery_event:
        return OVERALL_RECOVERING
    eth_down = eth_state is None or eth_state in _HARD_DOWN
    wifi_down = wifi_state is None or wifi_state in _HARD_DOWN
    if eth_down and wifi_down:
        return OVERALL_OFFLINE
    if not monitor_alive or warnings:
        return OVERALL_DEGRADED
    if active_path is None:
        return OVERALL_DEGRADED
    if verification_fresh_pass(verification.get(active_path), now, max_age):
        return OVERALL_HEALTHY
    # 链路在、但 Internet 未被证实（假死 / 探测不可信 / 验证过期）⇒ 降级。
    return OVERALL_DEGRADED


def _headline(*, active_path: Optional[str], standby_path: Optional[str],
              verification: Mapping[str, Any], now: float, max_age: float,
              monitor_alive: bool, overall: str) -> str:
    parts: List[str] = []
    if active_path:
        parts.append("%s PRIMARY" % PATH_LABEL.get(active_path, active_path))
    if standby_path:
        parts.append("%s STANDBY" % PATH_LABEL.get(standby_path, standby_path))
    if active_path is None:
        parts.append("无活动路径")
    elif verification_fresh_pass(verification.get(active_path), now, max_age):
        parts.append("Internet 已验证")
    else:
        parts.append("Internet 未验证")
    if overall is OVERALL_PAUSED:
        parts.append("监测已暂停")
    elif not monitor_alive:
        parts.append("监测已停止")
    return " · ".join(parts)


def build_ui_state(*, now: float, ethernet: Any = None, wifi: Any = None,
                   verification: Optional[Mapping[str, Any]] = None,
                   active_path: Optional[str] = None,
                   standby_path: Optional[str] = None,
                   failover_state: Any = None,
                   mode_label: str = "", monitor_label: str = "",
                   monitor_alive: bool = True, paused: bool = False,
                   recovery_event: Optional[str] = None,
                   verify_roles: Sequence[str] = (), uu_enabled: bool = False,
                   uu_last_ok: Optional[bool] = None, uu_pending: bool = False,
                   uu_detail: str = "", events: Sequence[str] = (),
                   warnings: Sequence[str] = (), network_epoch: int = 0,
                   ssid: str = "SNNU", manual: Optional[Mapping[str, Any]] = None,
                   max_age: float = DEFAULT_VERIFICATION_MAX_AGE) -> UiState:
    """把全部只读状态映射成一次仪表盘渲染输入（**纯函数**）。"""
    records: Dict[str, Any] = dict(verification or {})
    roles = tuple(str(role) for role in (verify_roles or ()))
    warning_list = tuple(str(item) for item in (warnings or ()) if str(item))
    manual_map: Mapping[str, Any] = manual or {}
    event_list = list(str(item) for item in (events or ()) if str(item))
    if recovery_event:
        event_list.insert(0, str(recovery_event))

    overall = overall_status(
        failover_state=failover_state, ethernet=ethernet, wifi=wifi,
        verification=records, active_path=active_path, verify_roles=roles,
        recovery_event=bool(recovery_event), paused=paused,
        monitor_alive=monitor_alive, warnings=warning_list, now=now,
        max_age=max_age)
    pipeline = build_pipeline(
        failover_state=failover_state, ethernet=ethernet, wifi=wifi,
        verification=records, active_path=active_path, verify_roles=roles,
        uu_enabled=uu_enabled, uu_last_ok=uu_last_ok, uu_pending=uu_pending,
        uu_detail=uu_detail, now=now, max_age=max_age)

    if uu_enabled:
        if uu_pending:
            uu_label, uu_tone = "已开启（准备清退重启）", TONE_BUSY
        elif uu_last_ok is True:
            uu_label, uu_tone = "已开启（最近一次成功）", TONE_OK
        elif uu_last_ok is False:
            uu_label, uu_tone = "已开启（最近一次失败）", TONE_BAD
        else:
            uu_label, uu_tone = "已开启（等待验证通过）", TONE_IDLE
    else:
        uu_label, uu_tone = "未开启（默认关闭）", TONE_IDLE

    return UiState(
        overall=overall,
        headline=_headline(active_path=active_path, standby_path=standby_path,
                           verification=records, now=now, max_age=max_age,
                           monitor_alive=monitor_alive, overall=overall),
        active_path=active_path, standby_path=standby_path,
        mode_label=mode_label, monitor_label=monitor_label,
        network_epoch=int(network_epoch),
        ethernet=build_path_card(
            role="ethernet", health=ethernet,
            assignment=_assignment("ethernet", active_path),
            verification=records.get("ethernet"), now=now, max_age=max_age,
            manual=verification_lines(manual_map.get("ethernet"), role="ethernet")),
        wifi=build_path_card(
            role="wifi", health=wifi, assignment=_assignment("wifi", active_path),
            verification=records.get("wifi"), now=now, max_age=max_age,
            ssid=ssid,
            manual=verification_lines(manual_map.get("wifi"), role="wifi")),
        pipeline=pipeline,
        uu=UuView(enabled=bool(uu_enabled), label=uu_label, tone=uu_tone,
                  last_ok=uu_last_ok, detail=uu_detail),
        events=tuple(event_list), warnings=warning_list,
        verification_roles=roles)
