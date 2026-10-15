# -*- coding: utf-8 -*-
"""V0.3 P4 SNNU 备用链路与接口绑定认证（campus_snnu）。

本模块实现主方案 §4.2「阶段 A：准备备用路径」与 §6.2「所有 SNNU 请求必须限定
接口」，即执行计划 §0.2 中 P4 的行级定义：

> **P4 SNNU 备用链路与接口绑定认证**：Wi-Fi 关联、Portal 识别、按网卡绑定认证；
> 允许真实副作用（受控、前台上人）；前置依赖 P3。

职责边界
========

1. **不重写认证引擎**（AGENTS §3.1）：Portal 端口解析 / 状态读取 / 认证请求 /
   认证后验证四步由 V0.2 的 ``campus_core.repair_service`` 及其适配器完成，本模块
   只**封装调用**，并把"绑定到 SNNU 接口的会话"注入其中。
2. **接口绑定是硬前置**（AGENTS §3.6 / 方案 §6.2）：在提交任何真实凭据之前，必须
   用 ``Find-NetRoute`` 证明 Portal 目标地址的实际出口 = SNNU 接口；无法证明时返回
   :attr:`SnnuAuthError.AUTH_PATH_UNCONFIRMED`（"无线认证路径未确认"），**不发送
   真实凭据**。仅"绑定源地址"不算证明（方案 §3.2）。
3. **默认只读**：:meth:`SnnuBackupLink.run` 的 ``allow_auth`` 默认 ``False``，只做
   "关联 → IP 就绪 → 出口证明 → Portal 识别"，不提交凭据；受控真机认证必须显式
   ``allow_auth=True`` 且经用户授权（AGENTS §6）。
4. **不接入 AUTO 循环**：P4 只提供能力与 P3 冻结的执行器协议在 SNNU 侧的实现
   （``PORTAL_POST`` / ``WIFI_RECONNECT``）；AUTO 启用与路由/Metric 变更
   （``METRIC_WRITE``）属 P5 ``campus_route_guard``（P3 报告 §9.3）。

依赖注入
========

所有副作用（读 Wi-Fi 状态、重连、等待 IP、路由查询、Portal 请求、认证引擎）都经
:class:`SnnuOps` 注入，默认测试集合注入假件即可实现**零真实网络 / 零真实凭据 /
零真实注册表 / 零真实 sleep**。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from campus_core import PortalResolution, PortalStatus, RepairOptions, RepairResult
from campus_failover import OpKind
from campus_network import _valid_ipv4

__all__ = [
    "CONTRACT_VERSION", "DEFAULT_TARGET_SSID", "PORTAL_TARGET",
    "AUTH_PATH_UNCONFIRMED_TEXT", "SnnuLinkState", "EgressProof", "BackupStage",
    "SnnuAuthError", "InterfaceBinding", "EgressEvidence", "SnnuLinkInfo",
    "BackupLinkResult", "SnnuOps", "classify_snnu_link", "confirm_egress",
    "SnnuBackupLink", "SnnuBackupExecutor", "format_backup_result",
]

CONTRACT_VERSION = "v03-p4-snnu-1"
DEFAULT_TARGET_SSID = "SNNU"

#: 出口证明的目标：Portal 入口 IP（经 SNNU 的 Portal 请求必须走该出口）。
PORTAL_TARGET = "202.117.144.205"

#: 无法可靠约束出口时的对外结论（方案 §6.2 原文措辞）。
AUTH_PATH_UNCONFIRMED_TEXT = "无线认证路径未确认"


class SnnuLinkState(Enum):
    """SNNU 无线关联状态（方案 §4.2 阶段 A 第 2–4 步）。"""

    ASSOCIATED = "ASSOCIATED"                  # 已关联目标 SSID 且有有效 IPv4
    ASSOCIATED_NO_IP = "ASSOCIATED_NO_IP"      # 已关联但尚无有效 IPv4
    DISCONNECTED = "DISCONNECTED"              # 未关联任何无线网络
    NON_TARGET = "NON_TARGET"                  # 关联了非目标网络
    NO_WIRELESS = "NO_WIRELESS"                # 无无线网卡
    UNKNOWN = "UNKNOWN"                        # 无法可靠判定

    @property
    def associated(self) -> bool:
        """是否已关联目标 SSID（含尚无 IP 的情形）。"""
        return self in (SnnuLinkState.ASSOCIATED,
                        SnnuLinkState.ASSOCIATED_NO_IP)

    @property
    def usable(self) -> bool:
        """是否可作为备用链路（已关联且 IP 就绪）。"""
        return self is SnnuLinkState.ASSOCIATED


class EgressProof(Enum):
    """出口证明结论（方案 §3.2 / AGENTS §3.6）。"""

    CONFIRMED = "CONFIRMED"        # Find-NetRoute 证明出口 = SNNU 接口
    UNCONFIRMED = "UNCONFIRMED"    # 出口被证明为**其他**接口
    UNAVAILABLE = "UNAVAILABLE"    # 无路由查询手段 / ifIndex 未知 / 查询失败

    @property
    def ok(self) -> bool:
        return self is EgressProof.CONFIRMED


class BackupStage(Enum):
    """备用链路准备流程的阶段（用于失败定位与验收证据）。"""

    LINK = "LINK"        # Wi-Fi 关联
    IP = "IP"            # IP 就绪
    EGRESS = "EGRESS"    # 出口证明 + 会话绑定
    PORTAL = "PORTAL"    # Portal 端口解析 / 可达 / 状态识别
    AUTH = "AUTH"        # 受控认证（仅 allow_auth=True）
    DONE = "DONE"


class SnnuAuthError(str, Enum):
    """P4 结构化错误码（不含密码/Cookie）。"""

    NO_WIRELESS = "E_SNNU_NO_WIRELESS"
    NON_TARGET = "E_SNNU_NON_TARGET"
    LINK_NOT_ASSOCIATED = "E_SNNU_LINK_NOT_ASSOCIATED"
    WIFI_RECONNECT_FAILED = "E_SNNU_WIFI_RECONNECT_FAILED"
    NO_IP = "E_SNNU_NO_IP"
    AUTH_PATH_UNCONFIRMED = "E_SNNU_AUTH_PATH_UNCONFIRMED"
    PORTAL_UNREACHABLE = "E_SNNU_PORTAL_UNREACHABLE"
    AUTH_FAILED = "E_SNNU_AUTH_FAILED"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InterfaceBinding:
    """SNNU 接口绑定描述（源地址 + ifIndex + 适配器名）。"""

    interface: str = ""
    if_index: Optional[int] = None
    source_ip: Optional[str] = None

    @property
    def ok(self) -> bool:
        """是否具备绑定所需的全部要素（源地址 + ifIndex）。"""
        return (_valid_ipv4(self.source_ip) and type(self.if_index) is int
                and 0 < self.if_index <= 0xFFFFFFFF)

    def to_dict(self) -> Dict[str, Any]:
        return {"interface": self.interface, "if_index": self.if_index,
                "source_ip": self.source_ip, "ok": self.ok}


@dataclass(frozen=True)
class EgressEvidence:
    """一次出口证明的证据。"""

    target: str
    proof: EgressProof
    egress_if_index: Optional[int] = None
    egress_source_ip: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        """是否已证明出口 = SNNU 接口。"""
        return self.proof.ok

    def to_dict(self) -> Dict[str, Any]:
        return {"target": self.target, "proof": self.proof.value,
                "egress_if_index": self.egress_if_index,
                "egress_source_ip": self.egress_source_ip,
                "detail": self.detail}


@dataclass
class SnnuLinkInfo:
    """SNNU 无线链路（关联）观察结果。"""

    ssid: Optional[str] = None
    state: SnnuLinkState = SnnuLinkState.UNKNOWN
    interface: str = ""
    if_index: Optional[int] = None
    source_ip: Optional[str] = None
    has_ipv4: Optional[bool] = None
    reconnected: bool = False
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ssid": self.ssid, "state": self.state.value,
                "interface": self.interface, "if_index": self.if_index,
                "source_ip": self.source_ip, "has_ipv4": self.has_ipv4,
                "reconnected": self.reconnected,
                "evidence": list(self.evidence)}


@dataclass
class BackupLinkResult:
    """备用链路准备（阶段 A）的完整结果。

    ``ok`` 语义：流程**走完且未遇错误码**（不代表"公网已恢复"）。是否提交凭据由
    :attr:`credentials_sent` 单独如实报告（``allow_auth`` 且出口已确认才为 ``True``）。
    """

    contract_version: str = CONTRACT_VERSION
    target_ssid: str = DEFAULT_TARGET_SSID
    allow_auth: bool = False
    stage: BackupStage = BackupStage.LINK
    ok: bool = False
    error: Optional[str] = None
    detail: str = ""
    link: SnnuLinkInfo = field(default_factory=SnnuLinkInfo)
    binding: Optional[InterfaceBinding] = None
    egress: Optional[EgressEvidence] = None
    portal: Optional[PortalResolution] = None
    status: Optional[PortalStatus] = None
    repair: Optional[RepairResult] = None
    credentials_sent: bool = False
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "target_ssid": self.target_ssid,
            "allow_auth": self.allow_auth,
            "stage": self.stage.value,
            "ok": self.ok,
            "error": self.error,
            "detail": self.detail,
            "link": self.link.to_dict(),
            "binding": self.binding.to_dict() if self.binding else None,
            "egress": self.egress.to_dict() if self.egress else None,
            "portal": ({"port": self.portal.port, "source": self.portal.source,
                        "error": self.portal.error,
                        "evidence": self.portal.evidence}
                       if self.portal else None),
            "status": self.status.value if self.status else None,
            "repair": ({"ok": self.repair.ok, "network_ok": self.repair.network_ok,
                        "auth_ok": self.repair.auth_ok,
                        "error_code": self.repair.error_code,
                        "status": (self.repair.status.value
                                   if self.repair.status else None),
                        "port": self.repair.port,
                        "needs_user": self.repair.needs_user,
                        "already_authenticated":
                            self.repair.already_authenticated,
                        "detail": self.repair.detail}
                       if self.repair else None),
            "credentials_sent": self.credentials_sent,
            "evidence": list(self.evidence),
        }


# --------------------------------------------------------------------------- #
# 注入式能力集合（默认测试集合全部替换为假件）
# --------------------------------------------------------------------------- #
@dataclass
class SnnuOps:
    """P4 依赖的 V0.2 / P1 能力（全部可注入，便于离线测试）。

    - ``wifi_state() -> (ssid|None, 状态文本)``：复用
      ``campus_reconnect.wifi_state``（F17 语义：完整比较、无接口返回
      ``no-wireless``）；
    - ``wifi_reconnect(ssid) -> bool``：复用 ``campus_reconnect.wifi_reconnect``；
    - ``ip_ready() -> bool``：无线接口是否已有可用 IPv4（等待就绪用）；
    - ``route_lookup(target) -> 出口对象|None``：``Find-NetRoute`` 只读查询
      （复用 P0 ``netdiag`` / P1 ``NetworkObserver.route_lookup``）；
    - ``resolve_port(session, requested) -> (port|None, 来源)``：复用
      ``campus_reconnect.resolve_port``（F16 动态端口解析）；
    - ``portal_reachable(session, port) -> bool``：复用同名 V0.2 函数；
    - ``read_status(session, port) -> 状态页对象|None``：复用
      ``campus_reconnect.read_status_page``（只读，不发凭据）；
    - ``repair(session, options) -> RepairResult``：**封装调用**
      ``campus_core.repair_service``（已绑定时钟/适配器/凭据提供者）；``None`` 表示
      未接入认证引擎，此时 ``allow_auth=True`` 将被拒绝。
    """

    wifi_state: Callable[[], Tuple[Optional[str], str]]
    wifi_reconnect: Callable[[str], bool]
    ip_ready: Callable[[], bool]
    route_lookup: Optional[Callable[[str], Any]]
    resolve_port: Callable[[Any, Optional[int]], Tuple[Optional[int], str]]
    portal_reachable: Callable[[Any, int], bool]
    read_status: Callable[[Any, int], Any]
    repair: Optional[Callable[[Any, RepairOptions], RepairResult]] = None
    bind_interface: Optional[Callable[[Any, InterfaceBinding], bool]] = None


# --------------------------------------------------------------------------- #
# 纯函数：关联分类与出口证明
# --------------------------------------------------------------------------- #
def classify_snnu_link(ssid: Optional[str], raw_state: Any, *,
                       target_ssid: str = DEFAULT_TARGET_SSID,
                       has_ipv4: Optional[bool] = None) -> SnnuLinkState:
    """把 ``(ssid, 状态文本)`` 归类为 :class:`SnnuLinkState`。

    ``raw_state`` 语义沿用 ``campus_reconnect.wifi_state``：``no-wireless`` /
    ``non-windows`` / ``error`` / ``unknown`` / ``connected`` / ``disconnected``。
    SSID 与目标**完整比较**（F17：大小写不同即非目标），不把 ``snnu`` 误当 ``SNNU``。
    """
    text = str(raw_state or "").strip().lower()
    if text == "no-wireless":
        return SnnuLinkState.NO_WIRELESS
    if text in ("non-windows", "error"):
        return SnnuLinkState.UNKNOWN
    if not ssid:
        if text in ("", "unknown"):
            return SnnuLinkState.UNKNOWN
        return SnnuLinkState.DISCONNECTED
    if ssid.strip() != (target_ssid or "").strip():
        return SnnuLinkState.NON_TARGET
    if has_ipv4 is False:
        return SnnuLinkState.ASSOCIATED_NO_IP
    return SnnuLinkState.ASSOCIATED


def confirm_egress(route_lookup: Optional[Callable[[str], Any]],
                   if_index: Optional[int], *,
                   target: str = PORTAL_TARGET) -> EgressEvidence:
    """证明 Portal 目标地址的实际出口是否为 SNNU 接口（只读，不发包）。

    判定规则（方案 §3.2：无法证明时**不假定成功**）：

    - 无路由查询手段 / ifIndex 未知 / 查询失败 → :attr:`EgressProof.UNAVAILABLE`；
    - 查得的出口 ifIndex 等于 SNNU 接口 → :attr:`EgressProof.CONFIRMED`；
    - 查得的出口为其他接口 → :attr:`EgressProof.UNCONFIRMED`。
    """
    if route_lookup is None:
        return EgressEvidence(target, EgressProof.UNAVAILABLE,
                              detail="无路由查询手段，无法证明实际出口")
    if if_index is None:
        return EgressEvidence(target, EgressProof.UNAVAILABLE,
                              detail="SNNU 接口 ifIndex 未知，无法证明实际出口")
    try:
        info = route_lookup(target)
    except Exception as exc:
        return EgressEvidence(target, EgressProof.UNAVAILABLE,
                              detail="路由查询异常：%s" % type(exc).__name__)
    if info is None:
        return EgressEvidence(target, EgressProof.UNAVAILABLE,
                              detail="路由查询无结果（Find-NetRoute 未返回出口）")
    egress_if = getattr(info, "if_index", None)
    egress_src = getattr(info, "source_ip", None)
    if egress_if is None:
        return EgressEvidence(target, EgressProof.UNAVAILABLE,
                              egress_if_index=None, egress_source_ip=egress_src,
                              detail="未能识别出口 ifIndex")
    if egress_if == if_index:
        return EgressEvidence(
            target, EgressProof.CONFIRMED, egress_if, egress_src,
            "Portal 目标 %s 的实际出口为 SNNU 接口（ifIndex=%s）"
            % (target, egress_if))
    return EgressEvidence(
        target, EgressProof.UNCONFIRMED, egress_if, egress_src,
        "Portal 目标 %s 的实际出口为 ifIndex=%s，非 SNNU 接口（ifIndex=%s）"
        % (target, egress_if, if_index))


# --------------------------------------------------------------------------- #
# 编排：SNNU 备用链路（方案 §4.2 阶段 A）
# --------------------------------------------------------------------------- #
class SnnuBackupLink:
    """SNNU 备用链路准备（方案 §4.2 阶段 A）。

    流程：关联检查/重连 → 等待 IP 就绪 → **出口证明（硬前置）** → 绑定源地址 →
    Portal 识别（只读）或受控认证（``allow_auth=True``）。

    纯编排 + 显式注入：不持有时钟、不直接做 I/O；所有副作用经 :class:`SnnuOps`。
    """

    def __init__(self, *, ops: SnnuOps, session: Any,
                 target_ssid: str = DEFAULT_TARGET_SSID,
                 interface: str = "", if_index: Optional[int] = None,
                 source_ip: Optional[str] = None,
                 allow_auth: bool = False, reconnect_wifi: bool = True,
                 port: Optional[int] = None, operator: str = "mobile",
                 account_format: str = "plain",
                 reconnect_attempts: int = 3, wait_interval: float = 5.0,
                 log: Optional[Callable[[str], None]] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.ops = ops
        self.session = session
        self.target_ssid = target_ssid
        self.interface = interface
        self.if_index = if_index
        self.source_ip = source_ip
        self.allow_auth = allow_auth
        self.reconnect_wifi = reconnect_wifi
        self.port = port
        self.operator = operator
        self.account_format = account_format
        self.reconnect_attempts = max(0, int(reconnect_attempts))
        self.wait_interval = max(0.0, float(wait_interval))
        self._log = log
        self.sleep = sleep

    # ---- 主入口 ---- #
    def run(self) -> BackupLinkResult:
        """执行阶段 A，返回 :class:`BackupLinkResult`（默认不提交凭据）。"""
        result = BackupLinkResult(target_ssid=self.target_ssid,
                                  allow_auth=self.allow_auth)
        link = SnnuLinkInfo(interface=self.interface, if_index=self.if_index,
                            source_ip=self.source_ip)
        result.link = link

        self._observe(link)
        if not link.state.usable and self.reconnect_wifi \
                and link.state in (SnnuLinkState.DISCONNECTED,
                                   SnnuLinkState.UNKNOWN):
            self._reconnect(link)
            self._observe(link)
        if link.state is SnnuLinkState.ASSOCIATED_NO_IP:
            self._await_ip(link)

        error = self._link_error(link)
        if error is not None:
            return self._fail(result, error[0], error[1])

        # ---- 出口证明（硬前置；AGENTS §3.6 / 方案 §6.2）---- #
        binding = InterfaceBinding(interface=self.interface,
                                   if_index=self.if_index,
                                   source_ip=self.source_ip)
        result.binding = binding
        result.stage = BackupStage.EGRESS
        bound = False
        if binding.ok and self.ops.bind_interface is not None:
            try:
                bound = self.ops.bind_interface(self.session, binding) is True
            except Exception as exc:
                result.evidence.append("接口绑定失败：%s" % type(exc).__name__)
        result.egress = EgressEvidence(
            PORTAL_TARGET, EgressProof.CONFIRMED if bound else EgressProof.UNAVAILABLE,
            self.if_index if bound else None, self.source_ip if bound else None,
            "IP_UNICAST_IF 回读 + 源地址约束" if bound else AUTH_PATH_UNCONFIRMED_TEXT)
        if not bound:
            return self._fail(result, SnnuAuthError.AUTH_PATH_UNCONFIRMED,
                              AUTH_PATH_UNCONFIRMED_TEXT + "：受限会话未建立，不发送凭据")
        result.evidence.append("会话已按 ifIndex=%s 与源地址 %s 限定接口"
                               % (self.if_index, self.source_ip))

        if not self.allow_auth:
            return self._identify(result)
        return self._authenticate(result)

    # ---- 阶段：关联 / IP ---- #
    def _read_link(self) -> Tuple[Optional[str], str, bool]:
        try:
            ssid, raw = self.ops.wifi_state()
        except Exception as exc:
            self._note("读取无线关联状态异常：%s" % type(exc).__name__)
            ssid, raw = None, "error"
        try:
            has_ip = bool(self.ops.ip_ready())
        except Exception as exc:
            self._note("读取无线 IPv4 就绪状态异常：%s" % type(exc).__name__)
            has_ip = False
        return ssid, str(raw or ""), has_ip

    def _observe(self, link: SnnuLinkInfo) -> None:
        ssid, raw, has_ip = self._read_link()
        link.ssid = ssid
        link.has_ipv4 = has_ip
        link.state = classify_snnu_link(ssid, raw, target_ssid=self.target_ssid,
                                        has_ipv4=has_ip)
        link.evidence.append("无线关联观察：SSID=%s，状态=%s，IPv4 就绪=%s"
                             % (ssid or "未连接", raw or "未知", has_ip))

    def _reconnect(self, link: SnnuLinkInfo) -> None:
        self._note("SNNU 未关联：尝试重新关联 %s" % self.target_ssid)
        ok = False
        try:
            ok = bool(self.ops.wifi_reconnect(self.target_ssid))
        except Exception as exc:
            self._note("Wi-Fi 重连异常：%s" % type(exc).__name__)
        link.reconnected = ok
        link.evidence.append("已发起 netsh 重连 %s：%s"
                             % (self.target_ssid, "成功" if ok else "失败"))

    def _await_ip(self, link: SnnuLinkInfo) -> None:
        """已关联但尚无 IPv4：有界等待（不真实 sleep，经注入的 ``sleep``）。"""
        for _ in range(self.reconnect_attempts):
            self.sleep(self.wait_interval)
            self._observe(link)
            if link.state.usable:
                return
            if link.state is not SnnuLinkState.ASSOCIATED_NO_IP:
                return

    def _link_error(
            self, link: SnnuLinkInfo) -> Optional[Tuple[SnnuAuthError, str]]:
        state = link.state
        if state is SnnuLinkState.NO_WIRELESS:
            return (SnnuAuthError.NO_WIRELESS,
                    "未发现无线网卡：不支持 SNNU 备用链路")
        if state is SnnuLinkState.NON_TARGET:
            return (SnnuAuthError.NON_TARGET,
                    "当前无线网络为 %s，非目标 %s"
                    % (link.ssid or "未知", self.target_ssid))
        if state is SnnuLinkState.DISCONNECTED:
            code = (SnnuAuthError.LINK_NOT_ASSOCIATED if not self.reconnect_wifi
                    else SnnuAuthError.WIFI_RECONNECT_FAILED)
            detail = ("未连接目标 Wi-Fi（%s），且未授权主动重连" % self.target_ssid
                      if not self.reconnect_wifi else
                      "重新关联 %s 失败：仍无有效关联" % self.target_ssid)
            return (code, detail)
        if state is SnnuLinkState.UNKNOWN:
            return (SnnuAuthError.LINK_NOT_ASSOCIATED,
                    "无线关联状态不可判定：不发送凭据")
        if state is SnnuLinkState.ASSOCIATED_NO_IP:
            return (SnnuAuthError.NO_IP,
                    "SNNU 已关联但无有效 IPv4：无法限定出口，不发送凭据")
        return None

    # ---- 阶段：Portal 识别（只读）---- #
    def _identify(self, result: BackupLinkResult) -> BackupLinkResult:
        result.stage = BackupStage.PORTAL
        try:
            port, source = self.ops.resolve_port(self.session, self.port)
        except Exception as exc:
            return self._fail(result, SnnuAuthError.PORTAL_UNREACHABLE,
                              "Portal 端口解析异常：%s" % type(exc).__name__)
        if port is None:
            return self._fail(result, SnnuAuthError.PORTAL_UNREACHABLE,
                              "无法确定 Portal 认证端口：%s" % source)
        result.portal = PortalResolution(port=port, source=source)
        reachable = False
        try:
            reachable = bool(self.ops.portal_reachable(self.session, port))
        except Exception as exc:
            self._note("Portal 可达性检查异常：%s" % type(exc).__name__)
        if not reachable:
            return self._fail(result, SnnuAuthError.PORTAL_UNREACHABLE,
                              "Portal 登录页不可达（端口 %d）" % port)
        result.evidence.append("Portal 端口 %d 可达（%s），且经 SNNU 接口"
                               % (port, source))

        page = None
        try:
            page = self.ops.read_status(self.session, port)
        except Exception as exc:
            self._note("读取 Portal 状态页异常：%s" % type(exc).__name__)
        status = getattr(page, "status", None) if page is not None else None
        result.status = status
        if status is PortalStatus.ONLINE:
            result.detail = "SNNU 已关联且 Portal 显示在线（只读识别，未提交凭据）"
        elif status is PortalStatus.AUTH_REQUIRED:
            result.detail = "SNNU 已关联但需要 Portal 认证（只读识别，未提交凭据）"
        elif status is PortalStatus.NEEDS_USER:
            result.detail = "SNNU Portal 需人工处理（验证码/密码等）"
        else:
            result.detail = ("SNNU Portal 状态：%s（只读识别，未提交凭据）"
                             % (status.value if status else "未识别"))
        result.stage = BackupStage.DONE
        result.ok = True
        return result

    # ---- 阶段：受控认证（封装调用 repair_service）---- #
    def _authenticate(self, result: BackupLinkResult) -> BackupLinkResult:
        if self.ops.repair is None:
            return self._fail(result, SnnuAuthError.AUTH_FAILED,
                              "未注入认证引擎（repair_service）：拒绝提交凭据")
        options = RepairOptions(ssid=self.target_ssid, operator=self.operator,
                                account_format=self.account_format, port=self.port,
                                reconnect_wifi=False, browser_fallback=False,
                                force=False)
        result.stage = BackupStage.AUTH
        result.credentials_sent = True
        self._note("出口已确认为 SNNU 接口：经绑定会话提交受控认证"
                   "（allow_auth=True，前台上人）")
        try:
            repair = self.ops.repair(self.session, options)
        except Exception as exc:
            return self._fail(result, SnnuAuthError.AUTH_FAILED,
                              "认证引擎异常：%s: %s" % (type(exc).__name__, exc))
        result.repair = repair
        result.status = getattr(repair, "status", None)
        result.portal = PortalResolution(port=getattr(repair, "port", None),
                                         source="repair_service")
        result.detail = getattr(repair, "detail", "") or ""
        result.stage = BackupStage.DONE
        result.ok = bool(getattr(repair, "ok", False))
        if result.ok:
            result.evidence.append("受控认证成功：%s" % result.detail)
        else:
            result.error = SnnuAuthError.AUTH_FAILED.value
            result.evidence.append("受控认证未成功：%s（%s）"
                                   % (result.error, result.detail))
        return result

    # ---- 内部：失败与日志 ---- #
    def _fail(self, result: BackupLinkResult, error: SnnuAuthError,
              detail: str) -> BackupLinkResult:
        result.ok = False
        result.error = error.value
        result.detail = detail
        result.evidence.append("中止于 %s：%s（%s）"
                               % (result.stage.value, error.value, detail))
        self._note(detail)
        return result

    def _note(self, message: str) -> None:
        if self._log is not None:
            self._log(message)


# --------------------------------------------------------------------------- #
# P3 执行器协议在 SNNU 侧的实现
# --------------------------------------------------------------------------- #
class SnnuBackupExecutor:
    """P3 冻结的执行器协议在 SNNU 侧的实现（``PORTAL_POST`` / ``WIFI_RECONNECT``）。

    - ``PORTAL_POST`` → ``portal_action()``（返回 :class:`BackupLinkResult`）：
      成功返回 ``True``；失败返回说明字符串（P3 协议：``str`` = 失败详情）；
    - ``WIFI_RECONNECT`` → ``wifi_action()``（返回 ``bool``）；
    - ``METRIC_WRITE`` / ``CLASH_TOUCH`` → 返回失败说明：路由/Metric 变更属 P5
      ``campus_route_guard``，P4 不处理。

    本类**不**被任何运行入口接入 AUTO 循环（P3 报告 §9.3：AUTO 启用属 P5）；
    仅为 P3 冻结的执行器协议提供 SNNU 侧实现，供 P5 组合使用。
    """

    def __init__(self, *, portal_action: Callable[[], BackupLinkResult],
                 wifi_action: Callable[[], bool],
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.portal_action = portal_action
        self.wifi_action = wifi_action
        self._log = log

    def execute(self, request: Any) -> Any:
        kind = getattr(request, "kind", None)
        if kind is OpKind.PORTAL_POST:
            try:
                result = self.portal_action()
            except Exception as exc:
                return "SNNU 认证执行异常：%s: %s" % (type(exc).__name__, exc)
            if getattr(result, "ok", False):
                return True
            return ("SNNU 认证未成功：%s"
                    % (getattr(result, "error", None)
                       or getattr(result, "detail", None) or "未知原因"))
        if kind is OpKind.WIFI_RECONNECT:
            try:
                ok = bool(self.wifi_action())
            except Exception as exc:
                return "SNNU 重新关联异常：%s: %s" % (type(exc).__name__, exc)
            return True if ok else "SNNU 重新关联失败"
        return ("P4 不处理该动作（%s）：路由/Metric 变更属 P5 campus_route_guard"
                % getattr(kind, "value", kind))


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def format_backup_result(result: BackupLinkResult) -> str:
    """人类可读的备用链路准备输出（``snnu`` 命令用）。"""
    lines: List[str] = []
    add = lines.append
    add("=" * 72)
    add("V0.3 P4 SNNU 备用链路与接口绑定认证（%s）"
        % ("受控认证 allow_auth=True" if result.allow_auth
           else "只读识别（不提交凭据）"))
    add("契约: %s    目标 SSID: %s" % (result.contract_version, result.target_ssid))
    add("=" * 72)
    add("")
    add("结论: %s%s" % ("成功" if result.ok else "未成功",
                       "" if not result.error else "（%s）" % result.error))
    add("阶段: %s" % result.stage.value)
    add("说明: %s" % (result.detail or "-"))
    add("是否提交凭据: %s" % ("是（受控认证路径）" if result.credentials_sent
                              else "否"))
    add("")
    link = result.link
    add("[无线关联] SSID=%s  状态=%s  已重连=%s"
        % (link.ssid or "未连接", link.state.value, link.reconnected))
    add("  接口=%s ifIndex=%s 源地址=%s IPv4就绪=%s"
        % (link.interface or "-", link.if_index, link.source_ip, link.has_ipv4))
    for entry in link.evidence:
        add("  - %s" % entry)
    add("")
    if result.egress is not None:
        add("[出口证明] %s（%s）" % (result.egress.proof.value,
                                    result.egress.detail))
    if result.portal is not None and result.portal.port is not None:
        add("[Portal] 端口=%s 来源=%s" % (result.portal.port,
                                          result.portal.source or "-"))
    if result.status is not None:
        add("[Portal 状态] %s" % result.status.value)
    if result.repair is not None:
        add("[认证结果] ok=%s network_ok=%s auth_ok=%s error_code=%s"
            % (result.repair.ok, result.repair.network_ok,
               result.repair.auth_ok, result.repair.error_code))
        add("  needs_user=%s already_authenticated=%s"
            % (result.repair.needs_user, result.repair.already_authenticated))
    add("")
    add("[证据链]")
    for entry in result.evidence:
        add("  - %s" % entry)
    add("=" * 72)
    return "\n".join(lines)
