# -*- coding: utf-8 -*-
"""V0.5-1 接口受限（path-scoped）真实上网验证。

需求（方案 §2.1/§3/§4）：任何网络「连接成功」之后都必须做**真实 Internet 验证**，
且验证必须绑定到**刚连接的接口**——

    verify(Ethernet) → Ethernet socket
    verify(WiFi)     → WiFi socket

绝不允许「验证 Wi-Fi 时实际走 Ethernet 默认出口」（否则 Wi-Fi 假死会被误判为在线）。

本文件覆盖方案 §26 的 T1–T12：

- T1/T2：Ethernet / Wi-Fi 各用**自己的** ifIndex 与源地址；
- T3：Wi-Fi 验证**不借用**系统最佳出口（Ethernet）；
- T4：源地址变化后**旧 transport 立即作废**；
- T5–T8：判据档位（闸门 + 交叉 ≥2 / 闸门失败 / 全失败）；
- T9：DNS + HTTP 双失败 ⇒ 不可信不了，但**不是** Internet 可用；
- T10：Portal 劫持 ⇒ ``CAPTIVE_PORTAL``；
- T11：验证请求**不能**经过环境代理（fail-closed，未证明就不发请求）；
- T12：接口约束失败 ⇒ ``PROBE_UNRELIABLE``，且不给出任何可触发切换/重启的结论。

约束：全程假 transport / 假 Session，零真实网络、零真实 socket。
"""
from __future__ import annotations

import requests

import campus_reconnect as cr
from tests.fakes import FakePathEngine, FakeResponse, FakeSession


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _title_page(spec) -> FakeResponse:
    """跟随重定向后的 200 页面（正文含该探测点的期望标题）。"""
    body = ("<HTML><HEAD><TITLE>%s</TITLE></HEAD><BODY>%s</BODY></HTML>"
            % (spec.expect, spec.expect))
    return FakeResponse(200, body, url=spec.url)


def _all_specs():
    return [cr.VERIFY_GATE] + list(cr.VERIFY_CROSS)


def _route_sites(session, specs) -> None:
    for spec in specs:
        session.route(spec.url, _title_page(spec))


def _eth(if_index: int = 12, source_ip: str = "10.0.0.5",
         dns_servers=("10.0.0.1",), **kwargs) -> cr.PathVerificationRequest:
    return cr.PathVerificationRequest(
        role=cr.PATH_ROLE_ETHERNET, interface_index=if_index, source_ip=source_ip,
        interface_alias="Ethernet", gateway="10.0.0.1",
        dns_servers=tuple(dns_servers), **kwargs)


def _wifi(if_index: int = 7, source_ip: str = "172.16.0.9",
          dns_servers=("172.16.0.1",), **kwargs) -> cr.PathVerificationRequest:
    return cr.PathVerificationRequest(
        role=cr.PATH_ROLE_WIFI, interface_index=if_index, source_ip=source_ip,
        interface_alias="WLAN", gateway="172.16.0.1", ssid="SNNU",
        dns_servers=tuple(dns_servers), **kwargs)


def _healthy_session() -> FakeSession:
    session = FakeSession()
    _route_sites(session, _all_specs())
    return session


# --------------------------------------------------------------------------- #
# T1 / T2：各走各的接口
# --------------------------------------------------------------------------- #
def test_t1_ethernet_verification_uses_ethernet_ifindex():
    """T1：Ethernet 验证使用 Ethernet 的 ifIndex / 源地址。"""
    session = _healthy_session()
    engine = FakePathEngine()

    result = cr.verify_interface_internet(_eth(), engine=engine,
                                          session_factory=lambda: session)

    assert engine.calls == [(12, "10.0.0.5", ("10.0.0.1",))], engine.calls
    assert result.interface_index == 12
    assert result.source_ip == "10.0.0.5"
    assert result.role == cr.PATH_ROLE_ETHERNET
    assert result.verified is True
    assert engine.transports[0].bound_sessions == [session], "未在指定接口上发请求"
    assert session.mounts, "会话未挂到接口受限适配器上（没有接口约束）"


def test_t2_wifi_verification_uses_wifi_ifindex():
    """T2：Wi-Fi 验证使用 Wi-Fi 的 ifIndex / 源地址。"""
    session = _healthy_session()
    engine = FakePathEngine()

    result = cr.verify_interface_internet(_wifi(), engine=engine,
                                          session_factory=lambda: session)

    assert engine.calls == [(7, "172.16.0.9", ("172.16.0.1",))], engine.calls
    assert result.interface_index == 7
    assert result.source_ip == "172.16.0.9"
    assert result.role == cr.PATH_ROLE_WIFI
    assert result.verified is True


def test_t3_wifi_verification_never_borrows_ethernet_default_route():
    """T3：系统最佳出口是 Ethernet，也不能让 Wi-Fi 验证借用它。

    这是 V0.5 最危险的假设：``Wi-Fi Connected`` + ``Baidu PASS`` 可能其实是
    「请求走了 Ethernet」。故此处让 Wi-Fi 路径**站点全不可达**（假死），
    Ethernet 路径全通；结论必须是「Wi-Fi 未验证通过」，而不是「Wi-Fi 在线」。
    """
    eth_session = _healthy_session()
    wifi_session = FakeSession()
    for spec in _all_specs():
        wifi_session.route(spec.url, requests.ConnectTimeout("timed out"))
    sessions = iter([eth_session, wifi_session])
    engine = FakePathEngine()

    eth = cr.verify_interface_internet(_eth(), engine=engine,
                                       session_factory=lambda: next(sessions))
    wifi = cr.verify_interface_internet(_wifi(), engine=engine,
                                        session_factory=lambda: next(sessions))

    assert [call[0] for call in engine.calls] == [12, 7]
    assert eth.verified is True
    assert wifi.verified is False, "Wi-Fi 假死却判为在线（借用了 Ethernet 出口）"
    assert wifi.source_ip == "172.16.0.9"
    assert engine.transports[0].bound_sessions == [eth_session]
    assert engine.transports[1].bound_sessions == [wifi_session]
    assert eth_session not in engine.transports[1].bound_sessions
    assert wifi_session not in engine.transports[0].bound_sessions


# --------------------------------------------------------------------------- #
# T4：源地址变化 ⇒ 旧 transport 作废
# --------------------------------------------------------------------------- #
def test_t4_stale_transport_is_dropped_when_source_ip_changes():
    """T4：源地址变了，旧 transport 不允许继续被使用。"""
    sessions = [_healthy_session() for _ in range(3)]
    engine = FakePathEngine()
    verifier = cr.PathVerifier(engine=engine,
                               session_factory=iter(sessions).__next__)

    first = verifier.verify(_eth(source_ip="10.0.0.5"))
    second = verifier.verify(_eth(source_ip="10.0.0.9"))
    third = verifier.verify(_eth(source_ip="10.0.0.9"))

    assert [call[1] for call in engine.calls] == ["10.0.0.5", "10.0.0.9"], engine.calls
    assert len(engine.transports) == 2, "源地址变更未重建 transport（复用了旧绑定）"
    assert engine.transports[0] is not engine.transports[1]
    assert first.source_ip == "10.0.0.5"
    assert second.source_ip == "10.0.0.9" and third.source_ip == "10.0.0.9"
    assert engine.transports[1].bound_sessions == [sessions[1], sessions[2]]
    assert sessions[0] not in engine.transports[1].bound_sessions, "旧 transport 被复用"


# --------------------------------------------------------------------------- #
# T5–T8：判据档位
# --------------------------------------------------------------------------- #
def test_t5_gate_and_two_cross_pass_is_verified():
    """T5：Baidu PASS + 2 个交叉 PASS ⇒ VERIFIED。"""
    session = FakeSession()
    _route_sites(session, [cr.VERIFY_GATE] + list(cr.VERIFY_CROSS[:2]))
    for spec in cr.VERIFY_CROSS[2:]:
        session.route(spec.url, requests.ConnectionError("connection refused"))

    result = cr.verify_interface_internet(_eth(), engine=FakePathEngine(),
                                          session_factory=lambda: session)

    assert result.verified is True, result.detail
    assert result.reliability == cr.PATH_OK
    assert result.allows_recovery is True
    assert result.gate_result.ok is True
    assert "www.baidu.com" in result.detail, result.detail


def test_t6_gate_and_one_cross_pass_is_not_verified():
    """T6：Baidu PASS + 1 个交叉 PASS ⇒ NOT VERIFIED。"""
    session = FakeSession()
    _route_sites(session, [cr.VERIFY_GATE, cr.VERIFY_CROSS[0]])
    for spec in cr.VERIFY_CROSS[1:]:
        session.route(spec.url, requests.ConnectionError("connection refused"))

    result = cr.verify_interface_internet(_eth(), engine=FakePathEngine(),
                                          session_factory=lambda: session)

    assert result.verified is False
    assert result.allows_recovery is False
    assert "交叉命中不足" in result.reason, result.reason
    assert cr.VERIFY_MIN_CROSS == 2


def test_t7_gate_fail_is_not_verified_even_when_all_cross_pass():
    """T7：Baidu FAIL ⇒ NOT VERIFIED（交叉全通也不算）。"""
    session = FakeSession()
    _route_sites(session, cr.VERIFY_CROSS)
    session.route(cr.VERIFY_GATE.url, requests.ConnectTimeout("timed out"))

    result = cr.verify_interface_internet(_eth(), engine=FakePathEngine(),
                                          session_factory=lambda: session)

    assert result.verified is False
    assert result.allows_recovery is False
    assert result.gate_result.ok is False
    assert "闸门未通过" in result.reason, result.reason


def test_t8_all_sites_failed_is_not_verified():
    """T8：所有站点失败 ⇒ NOT VERIFIED（且逐站列出）。"""
    session = FakeSession()
    for spec in _all_specs():
        session.route(spec.url, requests.ConnectionError("connection refused"))

    result = cr.verify_interface_internet(_eth(), engine=FakePathEngine(),
                                          session_factory=lambda: session)

    assert result.verified is False
    assert result.allows_recovery is False
    assert result.successful_sites == ()
    assert len(result.failed_sites) == 1 + len(cr.VERIFY_CROSS)
    assert "www.baidu.com" in result.detail


# --------------------------------------------------------------------------- #
# T9：DNS + HTTP 双失败
# --------------------------------------------------------------------------- #
def test_t9_dns_and_http_failure_is_not_verified():
    """T9：DNS 失败 + HTTP 失败 ⇒ NOT VERIFIED。

    注意与 T12 的区别：这里**探测本身可信**（接口约束成立），只是真的上不了网，
    故 ``reliability == ok`` 而 ``verified is False`` —— 仍不允许触发恢复动作。
    """
    session = FakeSession()
    for spec in _all_specs():
        session.route(spec.url, requests.ConnectionError("getaddrinfo failed"))
    engine = FakePathEngine(dns_ok=False)

    result = cr.verify_interface_internet(_eth(), engine=engine,
                                          session_factory=lambda: session)

    assert result.verified is False
    assert result.reliability == cr.PATH_OK, result.reason
    assert result.allows_recovery is False
    assert "(dns)" in result.detail, result.detail
    assert engine.transports[0].dns_resolve("www.baidu.com") is False


# --------------------------------------------------------------------------- #
# T10：Portal 劫持
# --------------------------------------------------------------------------- #
def test_t10_portal_redirect_is_captive_portal():
    """T10：门户劫持 ⇒ 定性 ``CAPTIVE_PORTAL``（不是「Internet 通」）。"""
    session = FakeSession()
    session.route(cr.VERIFY_GATE.url, FakeResponse(
        200, "<HTML><TITLE>校园网认证</TITLE><A HREF='/snnuportal/login.jsp'>"
             "登录</A></HTML>",
        url="http://202.117.144.205/snnuportal/login.jsp"))
    for spec in cr.VERIFY_CROSS:
        session.route(spec.url, requests.ConnectionError("connection refused"))

    result = cr.verify_interface_internet(_eth(), engine=FakePathEngine(),
                                          session_factory=lambda: session)

    assert result.verified is False
    assert result.captive_portal is True
    assert cr.PATH_CAPTIVE_PORTAL in result.reason, result.reason
    assert result.allows_recovery is False


# --------------------------------------------------------------------------- #
# T11：不经环境代理
# --------------------------------------------------------------------------- #
def test_t11_verification_never_uses_environment_proxy():
    """T11(a)：正常绑定后，会话必须 ``trust_env is False`` 且已挂接口适配器。"""
    session = _healthy_session()
    engine = FakePathEngine()

    cr.verify_interface_internet(_eth(), engine=engine,
                                 session_factory=lambda: session)

    assert session.trust_env is False, "验证请求可能经过环境代理（Clash）"
    assert session.mounts, "未挂接口受限适配器（会走系统默认出口/代理）"


def test_t11_proxy_capable_session_is_rejected_without_any_request():
    """T11(b)：绑定未证明绕开环境代理 ⇒ 一个请求都不发，判 ``PROBE_UNRELIABLE``。"""
    session = _healthy_session()
    engine = FakePathEngine(set_trust_env=False)   # 缺陷实现：绑定"成功"但不改 trust_env

    result = cr.verify_interface_internet(_eth(), engine=engine,
                                          session_factory=lambda: session)

    assert result.reliability == cr.PATH_PROBE_UNRELIABLE, result.reason
    assert result.verified is False
    assert session.calls == [], "疑似经环境代理仍发出了请求"


# --------------------------------------------------------------------------- #
# T12：接口约束失败 ⇒ PROBE_UNRELIABLE（fail-closed）
# --------------------------------------------------------------------------- #
def test_t12_unconfirmed_interface_binding_is_unreliable():
    """T12(a)：IP_UNICAST_IF 未回读确认 ⇒ PROBE_UNRELIABLE，且不发请求。"""
    created = []
    engine = FakePathEngine(interface_bound=False)

    result = cr.verify_interface_internet(
        _eth(), engine=engine, session_factory=lambda: created.append(1) or FakeSession())

    assert result.reliability == cr.PATH_PROBE_UNRELIABLE
    assert result.verified is False
    assert result.allows_recovery is False, "接口约束失败却给出可动作结论"
    assert created == [], "接口约束未证实却仍创建会话发请求"


def test_t12_binding_factory_failure_is_unreliable():
    """T12(b)：接口约束工厂本身抛错 ⇒ PROBE_UNRELIABLE（不炸、不误判）。"""
    engine = FakePathEngine(error=OSError("interface binding requires Windows"))

    result = cr.verify_interface_internet(_eth(), engine=engine,
                                          session_factory=FakeSession)

    assert result.reliability == cr.PATH_PROBE_UNRELIABLE
    assert result.verified is False
    assert result.allows_recovery is False
    assert "OSError" in result.reason, result.reason


def test_t12_invalid_path_never_reaches_the_engine():
    """T12(c)：路径描述本身无效（APIPA 源地址）⇒ 直接不可信，且不构造 transport。"""
    engine = FakePathEngine()

    result = cr.verify_interface_internet(_eth(source_ip="169.254.9.9"), engine=engine,
                                          session_factory=FakeSession)

    assert result.reliability == cr.PATH_PROBE_UNRELIABLE
    assert result.verified is False
    assert result.allows_recovery is False
    assert engine.calls == [], "无效路径仍去构造了接口 transport"


def test_t12_unreliable_also_covered_for_unknown_role_and_bad_index():
    """T12(d)：未知角色 / 非法 ifIndex 同样 fail-closed。"""
    engine = FakePathEngine()
    bad_role = cr.PathVerificationRequest(role="system", interface_index=3,
                                          source_ip="10.0.0.5",
                                          dns_servers=("10.0.0.1",))
    bad_index = cr.PathVerificationRequest(role=cr.PATH_ROLE_WIFI, interface_index=0,
                                           source_ip="10.0.0.5",
                                           dns_servers=("10.0.0.1",))

    for path in (bad_role, bad_index):
        result = cr.verify_interface_internet(path, engine=engine,
                                              session_factory=FakeSession)
        assert result.reliability == cr.PATH_PROBE_UNRELIABLE, path
        assert result.allows_recovery is False
    assert engine.calls == []


# --------------------------------------------------------------------------- #
# 结果自描述（供 V0.5-2/4/5 复用）
# --------------------------------------------------------------------------- #
def test_result_is_serializable_and_carries_evidence():
    """结果须可序列化（日志/UI 用），并带逐站证据。"""
    session = _healthy_session()
    result = cr.verify_interface_internet(_eth(), engine=FakePathEngine(),
                                          session_factory=lambda: session)

    payload = result.to_dict()

    assert payload["verified"] is True
    assert payload["interface_index"] == 12
    assert payload["source_ip"] == "10.0.0.5"
    assert payload["allows_recovery"] is True
    assert len(payload["successful_sites"]) == 1 + len(cr.VERIFY_CROSS)
    assert result.gate_result.source == cr.VERIFY_GATE.url
    assert [r.source for r in result.cross_results] == [s.url for s in cr.VERIFY_CROSS]
    assert result.elapsed >= 0.0


def test_request_snapshot_is_redaction_safe():
    """请求快照只含接口/地址类信息，不含凭据。"""
    payload = _wifi().to_dict()

    assert payload["ssid"] == "SNNU"
    assert payload["role"] == cr.PATH_ROLE_WIFI
    assert set(payload) == {"role", "interface_index", "interface_alias", "source_ip",
                            "gateway", "ssid", "network_generation", "dns_servers"}
