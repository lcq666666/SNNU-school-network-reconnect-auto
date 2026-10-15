# -*- coding: utf-8 -*-
"""V0.3 P1 离线集成测试（I-1-01 / I-1-02）。

闭合剧本：以太网拔线 / SNNU 已连接未认证（CAPTIVE_PORTAL）/ 双断 /
有线恢复，逐帧断言三轴状态序列与副作用清单。

硬断言（对应 P1 出口条件）：**全程零认证 POST、零系统副作用**——
剧本中的系统轴探测复用 V0.2 真实 ``internet_ok`` + :class:`FakeSession`
（离线路由），断言 ``session.posts() == []``。
"""
from __future__ import annotations

from types import SimpleNamespace

import campus_reconnect as engine
from tests.fakes import (FakeProbeEngine, FakeResponse, FakeSession,
                         healthy_http_table, matching_http_response)
from tools import netdiag

from campus_network import (DEFAULT_HTTP_TARGETS, DEFAULT_TCP_TARGETS,
                            HealthState, evaluate)

ETH_ADAPTER = {"Name": "以太网",
               "InterfaceDescription": "Intel(R) Ethernet Connection (16) I219-V",
               "InterfaceGuid": "{ETH-GUID}", "ifIndex": 10, "Status": "Up",
               "MacAddress": "98-8F-E0-6B-B2-78", "MediaType": "802.3",
               "Virtual": False, "HardwareInterface": True}
WIFI_ADAPTER = {"Name": "WLAN",
                "InterfaceDescription": "MediaTek Wi-Fi 6 MT7921",
                "InterfaceGuid": "{WIFI-GUID}", "ifIndex": 5, "Status": "Up",
                "MacAddress": "10-B1-DF-EA-EB-B1", "MediaType": "Native 802.11",
                "Virtual": False, "HardwareInterface": True}
ETH_IP = {"ifIndex": 10, "AddressFamily": "IPv4", "IPAddress": "192.168.124.9",
          "PrefixLength": 24}
WIFI_IP = {"ifIndex": 5, "AddressFamily": "IPv4", "IPAddress": "10.252.37.88",
           "PrefixLength": 22}
ETH_ROUTE = {"ifIndex": 10, "DestinationPrefix": "0.0.0.0/0",
             "NextHop": "192.168.124.1", "RouteMetric": 0, "InterfaceMetric": 25}
WIFI_ROUTE = {"ifIndex": 5, "DestinationPrefix": "0.0.0.0/0",
              "NextHop": "10.252.1.1", "RouteMetric": 0, "InterfaceMetric": 35}

ONLINE_RESP = {"status_code": 200, "redirected": False, "body": ""}


def _session(online: bool) -> FakeSession:
    """离线全局探测 Session（V0.2 ``internet_ok`` 语义）。

    ``online=True``：按真实系统探测点表（``DEFAULT_PROBES``）逐个布点、逐个严格匹配；
    ``False``：无路由（FakeSession 抛 ConnectionError），等价于公网不可达。
    """
    session = FakeSession()
    if online:
        # P8/K-5：从 DEFAULT_PROBES 推导，不写死旧目标 URL。
        for spec in engine.DEFAULT_PROBES:
            session.route(spec.url,
                          FakeResponse(200, matching_http_response(spec)["body"]))
    return session


def _report(eth_status="Up", wifi_status="Up", *, wifi_ip=True,
            wifi_route=True):
    adapters = []
    if eth_status:
        adapters.append(ETH_ADAPTER if eth_status == "Up"
                        else dict(ETH_ADAPTER, Status=eth_status))
    if wifi_status:
        adapters.append(WIFI_ADAPTER if wifi_status == "Up"
                        else dict(WIFI_ADAPTER, Status=wifi_status))
    ips = [ETH_IP]
    if wifi_ip:
        ips.append(WIFI_IP)
    routes = [ETH_ROUTE]
    if wifi_route:
        routes.append(WIFI_ROUTE)
    return netdiag.DiagReport(
        adapters=netdiag.parse_adapters(
            __import__("json").dumps(adapters, ensure_ascii=False), "[]"),
        ip_addresses=netdiag.parse_ip_addresses(ips),
        ip_interfaces=netdiag.parse_ip_interfaces([]),
        default_routes=netdiag.parse_default_routes(routes),
        wifi=netdiag.parse_wifi("", target_ssid="SNNU"),
        generated_at="2026-10-14T00:00:00+08:00", host="test-host")


def _engine(wifi_state: str, *, best_route_if_index: int = 10) -> FakeProbeEngine:
    """按剧本编排假探测引擎。

    ``wifi_state``：``healthy`` / ``captive`` / ``down``（TCP 全失败）。
    ``best_route_if_index``：系统对公网目标的最佳路由出口（模拟 Find-NetRoute）。
    以太网在线时最佳出口为以太网（10），以太网断开后系统改走 Wi-Fi（5）
    ——P1 只把"最佳路由与被测接口一致"当作出口证明（方案 §3.2：无法证明
    实际出口时判 PROBE_UNRELIABLE，不假定成功）。
    """
    eng = FakeProbeEngine()
    eng.tcp.update({"default": True})
    # P8/K-5：命中响应由真实探测点表推导（不再写死旧目标正文）。
    eng.http.update(healthy_http_table())
    if wifi_state == "captive":
        # Portal 只劫持 Wi-Fi 源地址路径（以太网路径不受影响）
        wifi_src = ("10.252.37.88", 0)
        for spec in DEFAULT_HTTP_TARGETS:
            eng.http[(spec.url, wifi_src)] = matching_http_response(
                spec, status_code=302, redirected=True,
                final_url="http://202.117.144.205:8601/snnuportal/"
                          "login.jsp")
    if wifi_state == "down":
        # 仅 Wi-Fi 源地址的公网探测失败（以太网路径不受影响）
        wifi_src = ("10.252.37.88", 0)
        for host, port in DEFAULT_TCP_TARGETS:   # P8/K-6：跟随真实探测点表
            eng.tcp[(host, port, wifi_src)] = False
    eng.dns.update({"default": True})
    # 出口证明：系统最佳路由（以太网在线时为 10，断开后为 5）
    eng.routes.update({
        "1.1.1.1": SimpleNamespace(if_index=best_route_if_index,
                                   source_ip="192.168.124.9"),
        "2606:4700:4700::1111": SimpleNamespace(if_index=best_route_if_index,
                                                source_ip="192.168.124.9"),
    })
    return eng


def test_i1_01_script_states_and_zero_side_effects():
    """I-1-01：闭合剧本逐帧断言状态序列，全程零认证 POST。

    P7.5-2：指定接口的约束探测独立于系统最佳路由；双网并存时均可证明健康。
    """
    session = _session(online=True)
    system_probe = lambda: engine.internet_ok(session)  # noqa: E731

    # 帧 1：双网正常，系统默认有线；Wi-Fi 的接口受限探测也正常。
    eng = _engine("healthy", best_route_if_index=10)
    snap = evaluate(_report(), engine=eng, system_probe=system_probe)
    assert snap.ethernet.state == HealthState.HEALTHY
    assert snap.wifi.state == HealthState.HEALTHY
    assert snap.wifi.egress_confirmed is True
    assert snap.system.online is True
    assert snap.recommendation.preferred == "ethernet"
    assert snap.recommendation.would_switch is False

    # 帧 2：以太网拔线（链路断），最佳路由改走 Wi-Fi；Wi-Fi 已连接但未认证
    eng = _engine("captive", best_route_if_index=5)
    snap = evaluate(_report(eth_status="Disconnected", wifi_ip=True,
                            wifi_route=True), engine=eng,
                    system_probe=system_probe)
    assert snap.ethernet.state == HealthState.LINK_DOWN
    assert snap.wifi.state == HealthState.CAPTIVE_PORTAL
    assert snap.wifi.egress_confirmed is True
    assert snap.recommendation.preferred is None
    assert snap.recommendation.would_switch is False
    assert "认证" in snap.recommendation.reason

    # 帧 3：Wi-Fi 完成认证后健康（剧本接入，不真的发认证），最佳路由为 Wi-Fi
    session_online = _session(online=True)
    system_probe = lambda: engine.internet_ok(session_online)  # noqa: E731
    eng = _engine("healthy", best_route_if_index=5)
    snap = evaluate(_report(eth_status="Disconnected"), engine=eng,
                    system_probe=system_probe)
    assert snap.ethernet.state == HealthState.LINK_DOWN
    assert snap.wifi.state == HealthState.HEALTHY
    assert snap.recommendation.preferred == "wifi"
    assert snap.recommendation.would_switch is True

    # 帧 4：双断
    session_offline = _session(online=False)
    system_probe = lambda: engine.internet_ok(session_offline)  # noqa: E731
    eng = _engine("down", best_route_if_index=5)
    snap = evaluate(_report(eth_status="Disconnected", wifi_status="Disconnected",
                            wifi_ip=False, wifi_route=False), engine=eng,
                    system_probe=system_probe)
    assert snap.ethernet.state == HealthState.LINK_DOWN
    assert snap.wifi.state == HealthState.LINK_DOWN
    assert snap.system.online is False
    assert snap.recommendation.preferred is None
    assert snap.recommendation.would_switch is False

    # 帧 5：有线恢复（双网卡并存，最佳路由回以太网）
    session_back = _session(online=True)
    system_probe = lambda: engine.internet_ok(session_back)  # noqa: E731
    eng = _engine("healthy", best_route_if_index=10)
    snap = evaluate(_report(), engine=eng, system_probe=system_probe)
    assert snap.ethernet.state == HealthState.HEALTHY
    assert snap.wifi.state == HealthState.HEALTHY
    assert snap.recommendation.preferred == "ethernet"
    assert snap.recommendation.would_switch is False

    # 全程副作用清单：零认证 POST（V0.2 认证引擎未被触碰）
    for s in (session, session_online, session_offline, session_back):
        assert s.posts() == [], "P1 剧本中出现认证 POST：%r" % s.posts()
    # 全部探测均为 GET（只读）
    for s in (session, session_online, session_offline, session_back):
        assert s.calls and all(c["method"] == "GET" for c in s.calls)


def test_i1_02_ethernet_healthy_never_misjudges_wifi_or_authenticates():
    """I-1-02（出口条件）：以太网健康时，不误判 SNNU 故障、不执行多余认证。"""
    session = _session(online=True)
    system_probe = lambda: engine.internet_ok(session)  # noqa: E731

    # Wi-Fi 侧各种非健康形态（未认证 / 断开 / 无 IP / 陈旧路由）都不影响
    # 以太网判定与系统结论，也不触发任何认证。
    for wifi_state in ("captive", "down"):
        for wifi_status in ("Up", "Disconnected"):
            eng = _engine(wifi_state)
            snap = evaluate(_report(wifi_status=wifi_status, wifi_ip=True),
                            engine=eng, system_probe=system_probe)
            assert snap.ethernet.state == HealthState.HEALTHY
            assert snap.system.online is True
            assert snap.recommendation.preferred == "ethernet"
            assert snap.recommendation.would_switch is False
            if wifi_status == "Disconnected":
                assert snap.wifi.state == HealthState.LINK_DOWN
            elif wifi_state == "captive":
                # 以太网在线时 Wi-Fi 公网路径不可证：观察到 Portal 重定向但
                # 出口无法归因，保守判 PROBE_UNRELIABLE；两者都不是"误判健康"。
                assert snap.wifi.state in (HealthState.CAPTIVE_PORTAL,
                                           HealthState.PROBE_UNRELIABLE)
    assert session.posts() == []
    assert all(c["method"] == "GET" for c in session.calls)
