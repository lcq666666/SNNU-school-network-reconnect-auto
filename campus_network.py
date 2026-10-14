# -*- coding: utf-8 -*-
"""V0.3 P1 网络观察层与独立健康检测（campus_network）。

本模块只回答三个互不混淆的问题（方案 §3.2）：

- :attr:`HealthAxis.ETHERNET_HEALTH`：有线网络是否真正可用；
- :attr:`HealthAxis.WIFI_HEALTH`：SNNU 无线网络是否真正可用；
- :attr:`HealthAxis.SYSTEM_HEALTH`：Windows 当前默认网络是否可用。

职责边界（V0.3 专项规则 2）：
- 本模块**只读**：不切换路由、不发认证、不写系统，输出"如果允许切换，
  将选择哪条路径"的**决策建议**（:class:`Recommendation`），执行留给 P3；
- 复用 V0.2 既有能力：``tools/netdiag`` 只读取证管道（P0）、
  ``campus_core.probe_matches`` 严格内容匹配（F08/T18）、
  ``campus_reconnect.internet_ok`` 全局探测语义（由调用方注入）；
- 不做 Windows 出口臆断：每接口探测**绑定源地址**，且必须用
  Find-NetRoute 证明目标路由与被测接口一致；无法证明时判
  :attr:`HealthState.PROBE_UNRELIABLE`（不假定成功，方案 §3.2）；
- 多站点聚合：任何单一探测点失败都不构成"全网断"结论；
- 危险动作黑名单同样适用：本模块没有任何写操作入口。
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from campus_core import (PROBE_EXACT, PROBE_HTML_TITLE, ProbeSpec,
                         probe_matches)
from tools import netdiag

__all__ = [
    "CONTRACT_VERSION", "DEFAULT_TARGET_SSID", "DEFAULT_TCP_TARGETS",
    "DEFAULT_HTTP_TARGETS", "DEFAULT_DNS_TARGETS", "DEFAULT_GATEWAY_PORTS",
    "HealthState", "HealthAxis", "ProbeOutcome", "EgressInfo",
    "InterfaceHealth", "SystemHealth", "Recommendation",
    "NetworkHealthSnapshot", "NetworkObserver", "InterfaceHealthChecker",
    "WindowsProbeEngine", "bind_source_address", "resolve_roles",
    "recommend_path", "evaluate", "format_snapshot",
]

CONTRACT_VERSION = "v03-p1-health-1"
DEFAULT_TARGET_SSID = "SNNU"

#: 公网 TCP 探测点（多点；单点失败不判全网断）。
DEFAULT_TCP_TARGETS: Tuple[Tuple[str, int], ...] = (
    ("1.1.1.1", 443), ("8.8.8.8", 443))

#: HTTP/HTTPS 内容探测点（严格匹配，复用 V0.2 ProbeSpec/probe_matches）。
DEFAULT_HTTP_TARGETS: Tuple[ProbeSpec, ...] = (
    ProbeSpec("https://www.msftconnecttest.com/connecttest.txt",
              "Microsoft Connect Test", PROBE_EXACT),
    ProbeSpec("https://captive.apple.com/hotspot-detect.html",
              "Success", PROBE_HTML_TITLE),
)

#: DNS 探测名（任一成功即认为解析器工作）。
DEFAULT_DNS_TARGETS: Tuple[str, ...] = ("www.baidu.com", "www.qq.com")

#: 网关探测端口（连接被拒绝也算"主机可达"）。
DEFAULT_GATEWAY_PORTS: Sequence[int] = (443, 80)

#: Portal 劫持特征（命中重定向目标即判 CAPTIVE_PORTAL）。
PORTAL_HINTS = ("snnuportal/login.jsp", "202.117.144.205",
                "net.snnu.edu.cn")

#: APIPA（DHCP 失败自分配）前缀——有地址但不可用。
APIPA_PREFIX = "169.254."

#: 探测超时（秒）。
_DEFAULT_TIMEOUT = 3.0


class HealthState(Enum):
    """接口健康分类（方案 §3.3，九类）。"""

    HEALTHY = "HEALTHY"
    LINK_DOWN = "LINK_DOWN"
    NO_IP = "NO_IP"
    GATEWAY_UNREACHABLE = "GATEWAY_UNREACHABLE"
    DNS_FAILURE = "DNS_FAILURE"
    CAPTIVE_PORTAL = "CAPTIVE_PORTAL"
    INTERNET_UNREACHABLE = "INTERNET_UNREACHABLE"
    PROBE_UNRELIABLE = "PROBE_UNRELIABLE"
    UNKNOWN = "UNKNOWN"


class HealthAxis(Enum):
    """三个互不混淆的检测轴（方案 §3.2）。"""

    ETHERNET_HEALTH = "ETHERNET_HEALTH"
    WIFI_HEALTH = "WIFI_HEALTH"
    SYSTEM_HEALTH = "SYSTEM_HEALTH"


@dataclass(frozen=True)
class ProbeOutcome:
    """一层探测的结果记录（可追溯证据）。"""

    layer: str            # link/ip/gateway/route/tcp/http/dns/remote
    target: str
    ok: bool
    detail: str = ""
    kind: str = ""        # dns/timeout/connection/http_status/...

    def to_dict(self) -> Dict[str, Any]:
        return {"layer": self.layer, "target": self.target, "ok": self.ok,
                "detail": self.detail, "kind": self.kind}


@dataclass
class EgressInfo:
    """一次出口识别的结果（Find-NetRoute）。"""

    target: str = ""
    source_ip: Optional[str] = None
    if_index: Optional[int] = None
    interface_alias: Optional[str] = None
    interface_metric: Optional[int] = None
    error: Optional[str] = None

    @classmethod
    def from_netdiag(cls, data: Dict[str, Any]) -> "EgressInfo":
        return cls(target=str(data.get("target") or ""),
                   source_ip=data.get("source_ip"),
                   if_index=data.get("if_index"),
                   interface_alias=data.get("interface_alias"),
                   interface_metric=data.get("interface_metric"),
                   error=data.get("error"))


@dataclass
class InterfaceHealth:
    """一张网卡（主轴）的健康结论。"""

    axis: HealthAxis
    interface: str
    if_index: Optional[int]
    state: HealthState
    evidence: List[str] = field(default_factory=list)
    layers: List[ProbeOutcome] = field(default_factory=list)
    egress_confirmed: Optional[bool] = None   # True/False/None(无法证明)
    source_ip: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"axis": self.axis.value, "interface": self.interface,
                "if_index": self.if_index, "state": self.state.value,
                "evidence": list(self.evidence),
                "layers": [p.to_dict() for p in self.layers],
                "egress_confirmed": self.egress_confirmed,
                "source_ip": self.source_ip}


@dataclass
class SystemHealth:
    """系统轴：Windows 当前默认网络是否可用 + 当前出口。"""

    axis: HealthAxis = HealthAxis.SYSTEM_HEALTH
    online: Optional[bool] = None
    source: str = ""
    egress: Optional[EgressInfo] = None
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"axis": self.axis.value, "online": self.online,
                "source": self.source,
                "egress": self.egress.__dict__ if self.egress else None,
                "evidence": list(self.evidence)}


@dataclass
class Recommendation:
    """只读决策：如果允许切换，将选择哪条路径（P1 不执行）。"""

    preferred: Optional[str]        # "ethernet"/"wifi"/None
    reason: str
    would_switch: bool
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"preferred": self.preferred, "reason": self.reason,
                "would_switch": self.would_switch, "warnings": list(self.warnings)}


@dataclass
class NetworkHealthSnapshot:
    """一次三轴健康快照（P1 契约）。"""

    contract_version: str = CONTRACT_VERSION
    generated_at: str = ""
    host: str = ""
    ethernet: Optional[InterfaceHealth] = None
    wifi: Optional[InterfaceHealth] = None
    system: Optional[SystemHealth] = None
    recommendation: Optional[Recommendation] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "generated_at": self.generated_at,
            "host": self.host,
            "ethernet": self.ethernet.to_dict() if self.ethernet else None,
            "wifi": self.wifi.to_dict() if self.wifi else None,
            "system": self.system.to_dict() if self.system else None,
            "recommendation": (self.recommendation.to_dict()
                               if self.recommendation else None),
        }


# --------------------------------------------------------------------------- #
# 观察层（复用 P0 netdiag 只读管道）
# --------------------------------------------------------------------------- #
class NetworkObserver:
    """只读网络观察：把 ``netdiag`` 诊断报告与按需路由查询包成对象。"""

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    def observe(self, target_ssid: str = DEFAULT_TARGET_SSID
                ) -> "netdiag.DiagReport":
        """一次完整只读观察（P0 契约 v03-p0-diag-1 的报告）。"""
        return netdiag.collect(self._runner, target_ssid=target_ssid)

    def route_lookup(self, target: str) -> Optional[EgressInfo]:
        """按目标地址查询系统最佳路由（Find-NetRoute，不发包）。

        失败/无输出返回 ``None``（= 无法证明，调用方据此判 UNKNOWN 族）。
        """
        script = netdiag.COMMANDS["route_lookup"].replace("{target}", str(target))
        try:
            rc, out, err = self._runner.run("route_lookup", script)
        except Exception:
            return None
        data = netdiag.parse_egress((int(rc), str(out), str(err)),
                                    target=str(target))
        if data.get("error"):
            return None
        return EgressInfo.from_netdiag(data)


def resolve_roles(report: Any,
                  config: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Optional[netdiag.AdapterRecord]]:
    """角色解析：``adapters.json`` 配置优先，其次物理分类；多候选不指派。

    - 配置命中（GUID 或 PNPDeviceID 匹配）使用配置指定网卡；
    - 否则物理以太网/Wi-Fi 各**恰有 1 张**时自动指派；
    - 多于 1 张候选时保持 ``None``，由人工确认（不臆断首选）。
    """
    roles: Dict[str, Optional[netdiag.AdapterRecord]] = {"ethernet": None,
                                                         "wifi": None}
    eth = [a for a in report.adapters if a.category == "physical-ethernet"]
    wifi = [a for a in report.adapters if a.category == "physical-wifi"]
    if config:
        for role, pool in (("ethernet", eth), ("wifi", wifi)):
            entry = (config.get("adapters") or {}).get(role)
            if not entry:
                continue
            want_guid = (entry.get("guid") or "").lower()
            want_pnp = (entry.get("pnp_device_id") or "").lower()
            for adapter in pool:
                if ((want_guid and adapter.guid.lower() == want_guid)
                        or (want_pnp
                            and adapter.pnp_device_id.lower() == want_pnp)):
                    roles[role] = adapter
                    break
    if roles["ethernet"] is None and len(eth) == 1:
        roles["ethernet"] = eth[0]
    if roles["wifi"] is None and len(wifi) == 1:
        roles["wifi"] = wifi[0]
    return roles


# --------------------------------------------------------------------------- #
# 真实探测引擎（Windows only；离线测试一律注入假件）
# --------------------------------------------------------------------------- #
class _SourceAddressAdapter:
    """按源地址绑定出网连接的 HTTP 适配器（urllib3 source_address）。"""

    def __init__(self, source_address: Tuple[str, int], **kwargs: Any) -> None:
        self._source_address = source_address
        self._kwargs = kwargs

    def mount(self, session: Any) -> None:
        from requests.adapters import HTTPAdapter

        source_address = self._source_address

        class _Bound(HTTPAdapter):
            def init_poolmanager(self, connections, maxsize, block=False,
                                 **pool_kwargs):
                pool_kwargs["source_address"] = source_address
                super().init_poolmanager(connections, maxsize, block=block,
                                         **pool_kwargs)

        session.mount("http://", _Bound(**self._kwargs))
        session.mount("https://", _Bound(**self._kwargs))


def bind_source_address(session: Any, source_ip: Optional[str]) -> bool:
    """把会话出网连接绑定到指定源地址（复用 urllib3 ``source_address``）。

    返回是否成功绑定；``source_ip`` 为空时返回 ``False``。

    **重要**：在 Windows 上"绑定源地址"**不等于**"证明出口" —— 系统仍可能按目标
    地址把连接路由到其他接口。因此调用方必须另行用 ``Find-NetRoute`` 确认实际出口
    （方案 §3.2、AGENTS §3.6）；无法确认时**不得**发送真实凭据。

    V0.3 P4 的 SNNU 接口绑定认证（``campus_snnu``）复用本函数。
    """
    if not source_ip:
        return False
    _SourceAddressAdapter((str(source_ip), 0)).mount(session)
    return True


class WindowsProbeEngine:
    """真实探测引擎（仅 Windows 实机使用；零配置信任环境）。

    - TCP：``socket.create_connection`` + ``source_address`` 绑定；
      连接被拒绝视为"主机可达"（网关探测据此区分拒绝与超时）；
    - HTTP：``requests.Session(trust_env=False)`` + 源地址绑定适配器；
    - DNS：``getaddrinfo``；
    - 路由查询：由 :class:`NetworkObserver` 提供（不在本类）。
    """

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    def tcp_connect(self, host: str, port: int, timeout: Optional[float] = None,
                    source_address: Optional[Tuple[str, int]] = None) -> bool:
        try:
            socket.create_connection((host, port),
                                     timeout=self.timeout if timeout is None
                                     else timeout,
                                     source_address=source_address)
        except ConnectionRefusedError:
            return True          # 主机可达（端口拒绝连接）
        except OSError:
            return False         # 超时 / 网络不可达 / 绑定失败
        return True

    def http_probe(self, url: str, timeout: Optional[float] = None,
                   source_address: Optional[Tuple[str, int]] = None) -> Any:
        import requests
        from types import SimpleNamespace

        session = requests.Session()
        session.trust_env = False
        if source_address:
            _SourceAddressAdapter(source_address).mount(session)
        try:
            resp = session.get(url, allow_redirects=False,
                               timeout=(self.timeout,
                                        2 * self.timeout),
                               headers={"User-Agent":
                                        "CampusReconnect-netdiag/1.0"})
        except requests.RequestException as exc:
            return SimpleNamespace(status_code=0, redirected=False,
                                   final_url="", body="",
                                   error=type(exc).__name__)
        body = resp.text[:512] if resp.status_code == 200 else ""
        return SimpleNamespace(status_code=resp.status_code,
                               redirected=bool(resp.is_redirect or
                                               300 <= resp.status_code < 400
                                               and "Location" in resp.headers),
                               final_url=resp.url or "",
                               body=body, error="")

    def dns_resolve(self, name: str) -> bool:
        try:
            socket.getaddrinfo(name, None)
        except OSError:
            return False
        return True


# --------------------------------------------------------------------------- #
# 分层健康检测
# --------------------------------------------------------------------------- #
def _attr(value: Any, name: str, default: Any = None) -> Any:
    """对象属性 / 字典键双兼容读取（假件既可能是 SimpleNamespace 也可能是 dict）。"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _valid_ipv4(address: Optional[str]) -> bool:
    """可用 IPv4：非空、非 APIPA、非回环、非 0.0.0.0。"""
    if not address:
        return False
    text = str(address).strip()
    if text.startswith(APIPA_PREFIX) or text.startswith("127."):
        return False
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", text))


def _pick_ipv4(report: Any, if_index: Optional[int]) -> Optional[str]:
    if if_index is None:
        return None
    for row in getattr(report, "ip_addresses", []):
        if row.get("if_index") == if_index and row.get("address_family") == "IPv4":
            address = row.get("ip_address")
            if address:
                return str(address)
    return None


def _default_route(report: Any, if_index: Optional[int]) -> Optional[Any]:
    if if_index is None:
        return None
    for row in getattr(report, "default_routes", []):
        if (row.get("if_index") == if_index
                and row.get("address_family") == "IPv4"):
            return row
    return None


def _portal_hit(redirected: bool, final_url: str, body: str) -> bool:
    if not redirected:
        return False
    text = "%s %s" % (final_url or "", body or "")
    return any(hint in text for hint in PORTAL_HINTS)


class InterfaceHealthChecker:
    """按方案 §3.2 的分层模型检测单个接口是否真正可用。

    层级：链路（Status）→ IP（有效 IPv4）→ 网关（路由 + 可达性）→
    出口确认（Find-NetRoute 证明）→ 公网 TCP → HTTP 内容 → DNS → 可选远控。
    """

    def __init__(self, engine: Any = None, *,
                 tcp_targets: Sequence[Tuple[str, int]] = DEFAULT_TCP_TARGETS,
                 http_targets: Sequence[ProbeSpec] = DEFAULT_HTTP_TARGETS,
                 dns_targets: Sequence[str] = DEFAULT_DNS_TARGETS,
                 gateway_ports: Sequence[int] = DEFAULT_GATEWAY_PORTS,
                 remote_targets: Sequence[Tuple[str, int]] = (),
                 timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.engine = engine
        self.tcp_targets = tuple(tcp_targets)
        self.http_targets = tuple(http_targets)
        self.dns_targets = tuple(dns_targets)
        self.gateway_ports = tuple(gateway_ports)
        self.remote_targets = tuple(remote_targets)
        self.timeout = timeout

    # ---- 单层 ---- #
    def _tcp(self, host: str, port: int, source: Tuple[str, int]) -> bool:
        return bool(self.engine.tcp_connect(host, port, timeout=self.timeout,
                                            source_address=source))

    def _http(self, spec: ProbeSpec, source: Tuple[str, int]) -> Any:
        return self.engine.http_probe(spec.url, timeout=self.timeout,
                                      source_address=source)

    def check(self, axis: HealthAxis, adapter: Any, report: Any,
              route_lookup: Optional[Callable[[str], Any]] = None
              ) -> InterfaceHealth:
        evidence: List[str] = []
        layers: List[ProbeOutcome] = []

        def _record(layer: str, target: str, ok: bool, detail: str = "",
                    kind: str = "") -> None:
            layers.append(ProbeOutcome(layer, target, ok, detail, kind))
            if not ok:
                evidence.append("%s 层探测失败：%s%s" %
                                (layer, target,
                                 ("（%s）" % detail) if detail else ""))

        # 1) 链路
        status = str(getattr(adapter, "status", "") or "").strip().lower()
        if status != "up":
            evidence.insert(0, "网卡状态非 Up：%s" % (adapter.status or "未知"))
            _record("link", str(getattr(adapter, "name", "")), False,
                    "Status=%s" % (adapter.status or "未知"))
            return InterfaceHealth(axis, getattr(adapter, "name", ""),
                                   getattr(adapter, "if_index", None),
                                   HealthState.LINK_DOWN, evidence, layers,
                                   None, None)
        _record("link", str(adapter.name), True, "Status=Up")

        # 2) IP
        ipv4 = _pick_ipv4(report, getattr(adapter, "if_index", None))
        if not _valid_ipv4(ipv4):
            _record("ip", str(ipv4 or "(无 IPv4 地址)"), False,
                    "APIPA/无有效地址" if ipv4 else "无 IPv4 地址")
            evidence.insert(0, "%s：无有效 IPv4（%s）" %
                            (adapter.name, ipv4 or "未获取"))
            return InterfaceHealth(axis, adapter.name, adapter.if_index,
                                   HealthState.NO_IP, evidence, layers,
                                   None, ipv4)
        source: Tuple[str, int] = (str(ipv4), 0)
        _record("ip", ipv4, True)

        # 3) 网关：默认路由 + 可达性
        route = _default_route(report, adapter.if_index)
        next_hop = str(route.get("next_hop") or "") if route else ""
        if route is None or not next_hop or next_hop == "0.0.0.0":
            _record("gateway", "(无默认路由)", False,
                    "该接口无有效 IPv4 默认路由")
            return InterfaceHealth(axis, adapter.name, adapter.if_index,
                                   HealthState.GATEWAY_UNREACHABLE, evidence,
                                   layers, None, ipv4)
        gateway_ok = False
        for port in self.gateway_ports:
            if self._tcp(next_hop, port, source):
                gateway_ok = True
                _record("gateway", "%s:%d" % (next_hop, port), True)
                break
            _record("gateway", "%s:%d" % (next_hop, port), False, "超时/不可达")
        if not gateway_ok:
            return InterfaceHealth(axis, adapter.name, adapter.if_index,
                                   HealthState.GATEWAY_UNREACHABLE, evidence,
                                   layers, None, ipv4)

        # 4) 出口确认（方案 §3.2：无法证明实际出口 → 不假定成功）
        confirmed: Optional[bool] = None
        if route_lookup is not None:
            target = self.tcp_targets[0][0]
            egress = route_lookup(target)
            if egress is None:
                _record("route", target, False, "路由查询失败，无法证明实际出口")
            else:
                egress_if = _attr(egress, "if_index")
                confirmed = (egress_if == adapter.if_index)
                _record("route", target, confirmed,
                        "系统最佳出口 ifIndex=%s（被测接口 ifIndex=%s）"
                        % (egress_if, adapter.if_index))
        else:
            _record("route", "(未提供路由查询)", False, "无出口确认手段")

        # 5) 公网 TCP（多点）
        tcp_ok = 0
        for host, port in self.tcp_targets:
            if self._tcp(host, port, source):
                tcp_ok += 1
                _record("tcp", "%s:%d" % (host, port), True)
            else:
                _record("tcp", "%s:%d" % (host, port), False, "连接失败/超时")

        # 6) HTTP 内容（多点，严格匹配；重定向到 Portal → CAPTIVE_PORTAL）
        http_ok = 0
        portal_hits = 0
        for spec in self.http_targets:
            probe = self._http(spec, source)
            status_code = _attr(probe, "status_code", 0) or 0
            redirected = bool(_attr(probe, "redirected", False))
            body = str(_attr(probe, "body", "") or "")
            error = str(_attr(probe, "error", "") or "")
            final_url = str(_attr(probe, "final_url", "") or "")
            if probe_matches(spec, status_code, body, redirected=redirected):
                http_ok += 1
                _record("http", spec.url, True)
            elif _portal_hit(redirected, final_url, body):
                portal_hits += 1
                _record("http", spec.url, False, "重定向至 Portal：%s"
                        % (final_url or "未知"), "captive")
            else:
                _record("http", spec.url, False,
                        error or ("status=%s" % status_code),
                        "http_status" if status_code else error or "unknown")

        # 7) DNS（任一成功即可）
        dns_ok = 0
        for name in self.dns_targets:
            if self.engine.dns_resolve(name):
                dns_ok += 1
                _record("dns", name, True)
            else:
                _record("dns", name, False, "解析失败", "dns")

        # 8) 可选：远控服务可达性（仅记录，不参与判定）
        for host, port in self.remote_targets:
            ok = self._tcp(host, port, source)
            _record("remote", "%s:%d" % (host, port), ok,
                    "" if ok else "不可达")

        # 分类（保守优先：不可证 > 不可达 > 内容层 > 健康）
        if confirmed is not True:
            state = HealthState.PROBE_UNRELIABLE
            evidence.append("无法证明探测经由本接口出口（源地址已绑定，但路由"
                            "确认缺失或指向其他接口）")
        elif tcp_ok == 0:
            state = HealthState.INTERNET_UNREACHABLE
        elif dns_ok == 0:
            state = HealthState.DNS_FAILURE
        elif portal_hits > 0 and http_ok == 0:
            state = HealthState.CAPTIVE_PORTAL
        elif http_ok > 0:
            state = HealthState.HEALTHY
            if tcp_ok < len(self.tcp_targets) or http_ok < len(self.http_targets):
                evidence.append("部分探测失败（多站点聚合后仍判健康）")
        else:
            state = HealthState.INTERNET_UNREACHABLE
            evidence.append("TCP 可达但 HTTP 内容层全部失败（矛盾，按不可用处理）")

        return InterfaceHealth(axis, adapter.name, adapter.if_index, state,
                               evidence, layers, confirmed, ipv4)


def _state_text(state: Optional[HealthState]) -> str:
    return state.value if state is not None else "缺失"


def recommend_path(ethernet: Optional[InterfaceHealth],
                   wifi: Optional[InterfaceHealth]) -> Recommendation:
    """只读决策：如果允许切换，将选择哪条路径（P1 不执行任何动作）。"""
    warnings: List[str] = []
    for name, health in (("以太网", ethernet), ("Wi-Fi", wifi)):
        if health is None:
            warnings.append("%s角色未解析，无法纳入决策" % name)
        elif health.state in (HealthState.UNKNOWN, HealthState.PROBE_UNRELIABLE):
            warnings.append("%s状态不可判定（%s），不纳入切换决策"
                            % (name, health.state.value))
    eth_state = ethernet.state if ethernet is not None else None
    wifi_state = wifi.state if wifi is not None else None
    unreliable = any(h is not None
                     and h.state in (HealthState.UNKNOWN, HealthState.PROBE_UNRELIABLE)
                     for h in (ethernet, wifi))

    if eth_state == HealthState.HEALTHY:
        return Recommendation("ethernet",
                              "以太网健康：保持当前默认出口，不触发任何切换或"
                              "认证动作", False, warnings)
    if unreliable:
        return Recommendation(None,
                              "存在不可判定/不可靠的健康检测（%s），按方案 §3.3 "
                              "不给出切换建议" % "；".join(warnings),
                              False, warnings)
    if wifi_state == HealthState.HEALTHY:
        return Recommendation(
            "wifi",
            "以太网不健康（%s）且 Wi-Fi 健康；若允许切换将选择 Wi-Fi"
            "（P1 仅给出决策，不执行）" % _state_text(eth_state),
            True, warnings)
    if wifi_state == HealthState.CAPTIVE_PORTAL:
        return Recommendation(
            None,
            "以太网不健康（%s），Wi-Fi 已连接但需要 Portal 认证；P1 只读监测"
            "不执行认证（认证属 P4 受控流程）" % _state_text(eth_state),
            False, warnings)
    if eth_state is None or wifi_state is None:
        return Recommendation(
            None,
            "角色缺失（以太网=%s，Wi-Fi=%s），无法给出切换建议"
            % (_state_text(eth_state), _state_text(wifi_state)),
            False, warnings)
    return Recommendation(
        None,
        "两条链路均不可用（以太网=%s，Wi-Fi=%s）；禁止在两张网卡之间来回切换"
        % (_state_text(eth_state), _state_text(wifi_state)),
        False, warnings)


def evaluate(report: Any, engine: Any = None, *,
             system_probe: Optional[Callable[[], Tuple[bool, str]]] = None,
             roles: Optional[Dict[str, Optional[Any]]] = None,
             config: Optional[Dict[str, Any]] = None,
             route_lookup: Optional[Callable[[str], Any]] = None,
             remote_targets: Sequence[Tuple[str, int]] = ()
             ) -> NetworkHealthSnapshot:
    """三轴健康评估入口（一次性快照，只读）。

    参数：
    - ``report``：P0 ``netdiag`` 诊断报告（观察层输入）；
    - ``engine``：探测引擎（离线测试注入 :class:`tests.fakes.FakeProbeEngine`，
      实机传 :class:`WindowsProbeEngine`）；
    - ``system_probe``：系统轴全局探测（实机由 CLI 注入
      ``internet_ok(session)``；离线注入假函数）；
    - ``roles``/``config``：角色解析覆盖/`adapters.json` 配置；
    - ``route_lookup``：出口确认函数（实机用 NetworkObserver.route_lookup）。
    """
    if roles is None:
        roles = resolve_roles(report, config)

    # 出口确认函数：显式传入优先；否则用探测引擎自带的 route_lookup
    # （FakeProbeEngine/未来引擎）；实机由 CLI 包 NetworkObserver.route_lookup。
    lookup = route_lookup
    if lookup is None and engine is not None and hasattr(engine, "route_lookup"):
        lookup = engine.route_lookup

    checker = InterfaceHealthChecker(engine,
                                     remote_targets=remote_targets)

    ethernet = None
    if roles.get("ethernet") is not None:
        ethernet = checker.check(HealthAxis.ETHERNET_HEALTH,
                                 roles["ethernet"], report, lookup)
    wifi = None
    if roles.get("wifi") is not None:
        wifi = checker.check(HealthAxis.WIFI_HEALTH, roles["wifi"], report,
                             lookup)

    system = SystemHealth(axis=HealthAxis.SYSTEM_HEALTH, online=None)
    if system_probe is not None:
        try:
            online, source = system_probe()
        except Exception:
            online, source = False, "system_probe 抛错"
        system.online = bool(online)
        system.source = str(source or "")
    else:
        system.evidence.append("未配置系统轴探测（system_probe=None）")
    egress_v4 = (getattr(report, "egress", {}) or {}).get("public_ipv4")
    if egress_v4 and not egress_v4.get("error"):
        system.egress = EgressInfo.from_netdiag(egress_v4)
        if egress_v4.get("if_index") is None:
            system.evidence.append("IPv4 出口未能识别")
    elif egress_v4 and egress_v4.get("error"):
        system.evidence.append("IPv4 出口识别失败：%s" % egress_v4["error"])

    recommendation = recommend_path(ethernet, wifi)
    return NetworkHealthSnapshot(
        generated_at=datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"),
        host=getattr(report, "host", "") or "",
        ethernet=ethernet, wifi=wifi, system=system,
        recommendation=recommendation)


def format_snapshot(snapshot: NetworkHealthSnapshot) -> str:
    """人类可读的三轴快照（netcheck 输出）。"""
    lines: List[str] = []
    add = lines.append
    add("=" * 72)
    add("V0.3 P1 三轴网络健康快照（只读：不切换、不认证）")
    add("契约: %s    时间: %s    主机: %s"
        % (snapshot.contract_version, snapshot.generated_at,
           snapshot.host or "-"))
    add("=" * 72)

    for label, health in (("ETHERNET_HEALTH", snapshot.ethernet),
                          ("WIFI_HEALTH", snapshot.wifi)):
        add("")
        add("[%s]" % label)
        if health is None:
            add("  角色未解析（未找到对应物理网卡）")
            continue
        add("  接口: %s (ifIndex=%s)" % (health.interface, health.if_index))
        add("  结论: %s" % health.state.value)
        if health.source_ip:
            add("  源地址: %s" % health.source_ip)
        add("  出口确认: %s" % {
            True: "已确认（系统路由经本接口）",
            False: "未确认（系统最佳路由不走本接口，不可假定成功）",
            None: "无法确认（路由查询失败/未提供）"}[health.egress_confirmed])
        for entry in health.evidence:
            add("  - %s" % entry)

    add("")
    add("[SYSTEM_HEALTH]")
    system = snapshot.system
    if system is None:
        add("  未评估")
    else:
        add("  当前默认网络: %s" % ("在线" if system.online else
                                   "离线" if system.online is False else
                                   "未探测"))
        if system.source:
            add("  探测来源: %s" % system.source)
        if system.egress is not None and system.egress.if_index is not None:
            add("  当前出口: ifIndex=%s（%s）源地址=%s"
                % (system.egress.if_index, system.egress.interface_alias,
                   system.egress.source_ip))
        for entry in system.evidence:
            add("  - %s" % entry)

    add("")
    add("[只读决策] 如果允许切换，将选择哪条路径：")
    rec = snapshot.recommendation
    if rec is not None:
        add("  选择: %s" % (rec.preferred or "（不建议切换）"))
        add("  是否切换: %s" % ("是" if rec.would_switch else "否"))
        add("  依据: %s" % rec.reason)
        for warning in rec.warnings:
            add("  警告: %s" % warning)
    add("=" * 72)
    return "\n".join(lines)
