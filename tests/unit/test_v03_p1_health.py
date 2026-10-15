# -*- coding: utf-8 -*-
"""V0.3 P1 独立健康检测单元测试（U-1-01..U-1-06）。

被测对象：``campus_network.py`` 的分层健康分类、三轴汇总与只读决策。

全部输入为假件（FakeProbeEngine / netdiag.DiagReport），零真实网络、
零真实网卡、零系统副作用。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tools import netdiag

from campus_network import (CONTRACT_VERSION, DEFAULT_HTTP_TARGETS,
                            DEFAULT_TCP_TARGETS, HealthAxis,
                            HealthState, evaluate, recommend_path, resolve_roles)
from tests.fakes import healthy_http_table, matching_http_response

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


def _report(eth=None, wifi=None, ips=(), routes=()):
    adapters = [a for a in (eth if eth is not None else ETH_ADAPTER,
                            wifi if wifi is not None else WIFI_ADAPTER) if a]
    return netdiag.DiagReport(
        adapters=netdiag.parse_adapters(json.dumps(adapters, ensure_ascii=False),
                                        "[]"),
        ip_addresses=netdiag.parse_ip_addresses(list(ips)),
        ip_interfaces=netdiag.parse_ip_interfaces([]),
        default_routes=netdiag.parse_default_routes(list(routes)),
        wifi=netdiag.parse_wifi("", target_ssid="SNNU"),
        generated_at="2026-10-14T00:00:00+08:00", host="test-host")


def _healthy_engine(engine):
    """把 engine 编排成"Wi-Fi 侧全通"：网关可达、TCP/HTTP/DNS 成功、
    出口确认经被测接口（ifIndex=5）。先清空，避免跨场景响应串味。"""
    engine.tcp.clear()
    engine.http.clear()
    engine.dns.clear()
    engine.routes.clear()
    engine.tcp.update({"default": True})
    # P8/K-5：命中响应由真实探测点表推导，不再写死旧目标的正文（表变则夹具跟随）。
    engine.http.update(healthy_http_table())
    engine.http["default"] = {"status_code": 200, "redirected": False,
                              "body": "success"}
    engine.dns.update({"default": True})
    engine.routes.update({"default": SimpleNamespace(if_index=5,
                                                     source_ip="10.252.37.88")})
    return engine


# --------------------------------------------------------------------------- #
# U-1-01 分层分类：九种状态全部分支
# --------------------------------------------------------------------------- #
def test_u1_01_healthy_layers_all_pass(fake_engine):
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    _healthy_engine(fake_engine)
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.HEALTHY
    assert snapshot.wifi.egress_confirmed is True
    layers = {p.layer for p in snapshot.wifi.layers}
    assert {"link", "ip", "gateway", "route", "tcp", "http", "dns"} <= layers


def test_u1_01_link_down(fake_engine):
    wifi_down = dict(WIFI_ADAPTER, Status="Disconnected")
    report = _report(wifi=wifi_down, ips=[ETH_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.LINK_DOWN
    wifi_source = ("10.252.37.88", 0)
    assert not [c for c in fake_engine.calls["tcp"]
                if c[2] == wifi_source], "Wi-Fi 链路断开不应再发公网探测"


def test_u1_01_no_ip_apipa_only(fake_engine):
    """R-1 移交项：接口 Up 但只有 APIPA（DHCP 失败）。"""
    apipa = dict(WIFI_IP, IPAddress="169.254.196.157")
    report = _report(ips=[ETH_IP, apipa], routes=[ETH_ROUTE, WIFI_ROUTE])
    _healthy_engine(fake_engine)
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.NO_IP


def test_u1_01_no_default_route_via_interface(fake_engine):
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE])
    _healthy_engine(fake_engine)
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.GATEWAY_UNREACHABLE


def test_u1_01_gateway_probe_timeout(fake_engine):
    _healthy_engine(fake_engine)
    fake_engine.tcp[("10.252.1.1", 443)] = False   # 网关探测超时
    fake_engine.tcp[("10.252.1.1", 80)] = False
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    # P7.5-2：公网已证实可用时，网关端口关闭不能判全网故障。
    assert snapshot.wifi.state == HealthState.HEALTHY


def test_u1_01_internet_unreachable(fake_engine):
    _healthy_engine(fake_engine)
    # 网关可达，但全部公网 TCP 失败
    fake_engine.tcp[("10.252.1.1", 443)] = True
    fake_engine.tcp.update({"default": False})
    fake_engine.http = {"default": {"status_code": 0, "error": "timeout"}}
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.INTERNET_UNREACHABLE


def test_u1_01_captive_portal_redirect(fake_engine):
    _healthy_engine(fake_engine)
    for spec in DEFAULT_HTTP_TARGETS:
        fake_engine.http[spec.url] = matching_http_response(
            spec, status_code=302, redirected=True,
            final_url="http://202.117.144.205:8601/snnuportal/login.jsp")
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.CAPTIVE_PORTAL


def test_u1_01_dns_failure(fake_engine):
    _healthy_engine(fake_engine)
    fake_engine.dns.update({"default": False})
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.DNS_FAILURE


# --------------------------------------------------------------------------- #
# U-1-02 出口无法确认 → 不得假定成功（方案 §3.2 硬规则）
# --------------------------------------------------------------------------- #
def test_u1_02_unconfirmed_egress_is_never_healthy(fake_engine):
    _healthy_engine(fake_engine)
    fake_engine.routes.update({"default": None})   # 路由查询失败
    fake_engine.proof = {"default": None}  # 无接口约束证据，不依赖系统路由
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.PROBE_UNRELIABLE
    assert snapshot.wifi.egress_confirmed is None


def test_u1_02_egress_via_other_interface_is_unreliable(fake_engine):
    _healthy_engine(fake_engine)
    # 系统最佳出口是以太极（ifIndex=10），不能把成功归因于 Wi-Fi
    fake_engine.routes.update({"default": SimpleNamespace(if_index=10,
                                                          source_ip="192.168.124.9")})
    fake_engine.proof = {"default": False}
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.PROBE_UNRELIABLE
    assert snapshot.wifi.egress_confirmed is False


def test_u1_02_probes_use_source_address_binding(fake_engine):
    """方案 §3.2：每张网卡的探测必须明确绑定源地址。"""
    _healthy_engine(fake_engine)
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    evaluate(report, engine=fake_engine)
    assert fake_engine.calls["tcp"], "未执行 TCP 探测"
    wifi_source = ("10.252.37.88", 0)
    eth_source = ("192.168.124.9", 0)
    # P8/K-6：探测点表可能调整，断言用真实表推导，避免改表即挂。
    tcp_host, tcp_port = DEFAULT_TCP_TARGETS[0]
    # Wi-Fi 轴探测绑定 Wi-Fi 源地址，以太网轴绑定以太网源地址
    assert any(c == (tcp_host, tcp_port, wifi_source)
               for c in fake_engine.calls["tcp"])
    assert any(c == (tcp_host, tcp_port, eth_source)
               for c in fake_engine.calls["tcp"])
    assert any(c[0].startswith("https://") and c[1] == wifi_source
               for c in fake_engine.calls["http"])
    assert all(c[2] in (wifi_source, eth_source)
               for c in fake_engine.calls["tcp"]), "存在未绑定源地址的探测"


# --------------------------------------------------------------------------- #
# U-1-03 多站点聚合：单站点失败不判全网断
# --------------------------------------------------------------------------- #
def test_u1_03_single_site_failure_keeps_healthy(fake_engine):
    _healthy_engine(fake_engine)
    fake_engine.tcp[DEFAULT_TCP_TARGETS[0]] = False                # 一个 TCP 点失败
    fake_engine.http["https://captive.apple.com/hotspot-detect.html"] = \
        {"status_code": 0, "redirected": False, "body": "",
         "error": "timeout"}                                       # 一个 HTTP 点失败
    fake_engine.dns["www.qq.com"] = False                          # 一个 DNS 名失败
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.HEALTHY
    assert any("部分探测失败" in e or "失败" in e for e in snapshot.wifi.evidence)


def test_u1_03_all_sites_fail_is_internet_unreachable(fake_engine):
    _healthy_engine(fake_engine)
    fake_engine.tcp[("10.252.1.1", 443)] = True   # 网关可达
    fake_engine.tcp.update({"default": False})
    fake_engine.http = {"default": {"status_code": 0, "error": "timeout"}}
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.INTERNET_UNREACHABLE


# --------------------------------------------------------------------------- #
# U-1-04 陈旧默认路由（R-1 移交项）：有路由 ≠ 接口可用
# --------------------------------------------------------------------------- #
def test_u1_04_stale_route_with_apipa_is_not_healthy(fake_engine):
    """WLAN Up 但只有 APIPA，且历史默认路由仍在 → NO_IP（绝不允许 HEALTHY）。"""
    apipa = dict(WIFI_IP, IPAddress="169.254.196.157")
    stale_route = dict(WIFI_ROUTE, NextHop="10.138.0.1")
    report = _report(ips=[ETH_IP, apipa], routes=[ETH_ROUTE, stale_route])
    _healthy_engine(fake_engine)
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.wifi.state == HealthState.NO_IP

    disconnected = dict(WIFI_ADAPTER, Status="Disconnected")
    report2 = _report(wifi=disconnected, ips=[ETH_IP, apipa],
                      routes=[ETH_ROUTE, stale_route])
    snapshot2 = evaluate(report2, engine=fake_engine)
    assert snapshot2.wifi.state == HealthState.LINK_DOWN


# --------------------------------------------------------------------------- #
# U-1-05 三轴汇总与只读决策
# --------------------------------------------------------------------------- #
def test_u1_05_three_axes_present(fake_engine):
    _healthy_engine(fake_engine)
    fake_engine.routes.update({"default": SimpleNamespace(if_index=10,
                                                          source_ip="192.168.124.9")})
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    system_probe = lambda: (True, "msftconnecttest")  # noqa: E731
    snapshot = evaluate(report, engine=fake_engine, system_probe=system_probe)
    assert snapshot.ethernet.axis == HealthAxis.ETHERNET_HEALTH
    assert snapshot.wifi.axis == HealthAxis.WIFI_HEALTH
    assert snapshot.system.axis == HealthAxis.SYSTEM_HEALTH
    assert snapshot.system.online is True
    assert snapshot.ethernet.state == HealthState.HEALTHY
    data = snapshot.to_dict()
    assert data["contract_version"] == CONTRACT_VERSION
    json.dumps(data, ensure_ascii=False)   # 契约可 JSON 序列化


def test_u1_05_recommendation_branches(fake_engine):
    wifi_src = ("10.252.37.88", 0)

    def _snap(eth_state, wifi_state):
        """按目标主轴状态编排 report + engine。"""
        eth = ETH_ADAPTER if eth_state != "link_down" else \
            dict(ETH_ADAPTER, Status="Disconnected")
        wifi = WIFI_ADAPTER if wifi_state != "link_down" else \
            dict(WIFI_ADAPTER, Status="Disconnected")
        ips = []
        if eth_state != "no_ip":
            ips.append(ETH_IP)
        if wifi_state not in ("link_down", "no_ip"):
            ips.append(WIFI_IP)
        routes = []
        if eth_state not in ("link_down", "no_ip", "no_route"):
            routes.append(ETH_ROUTE)
        if wifi_state not in ("link_down", "no_ip", "no_route"):
            routes.append(WIFI_ROUTE)
        _healthy_engine(fake_engine)
        # 系统最佳路由：以太网在线走以太网，断开后走 Wi-Fi
        fake_engine.routes.update({"default": SimpleNamespace(
            if_index=10 if eth_state != "link_down" else 5,
            source_ip="192.168.124.9")})
        if wifi_state == "captive":
            # Portal 只劫持 Wi-Fi 源地址路径（双网卡并存时的真实行为）
            for spec in DEFAULT_HTTP_TARGETS:
                fake_engine.http[(spec.url, wifi_src)] = matching_http_response(
                    spec, status_code=302, redirected=True,
                    final_url="http://202.117.144.205:8601/snnuportal/"
                              "login.jsp")
        if wifi_state == "unreachable":
            fake_engine.tcp[("10.252.1.1", 443)] = True   # 网关可达
            fake_engine.tcp.update({"default": False})
            fake_engine.http = {"default": {"status_code": 0, "error": "timeout"}}
        report = _report(eth=eth, wifi=wifi, ips=ips, routes=routes)
        return evaluate(report, engine=fake_engine)

    # 以太网健康 → 保持以太网，不切换
    snap = _snap("ok", "captive")
    assert snap.recommendation.preferred == "ethernet"
    assert snap.recommendation.would_switch is False
    assert "无需" in snap.recommendation.reason or "保持" in snap.recommendation.reason

    # 以太网断 + Wi-Fi 健康 → 建议切 Wi-Fi（仅决策，不执行）
    snap = _snap("link_down", "ok")
    assert snap.recommendation.preferred == "wifi"
    assert snap.recommendation.would_switch is True

    # 以太网断 + Wi-Fi 需认证 → 不建议切换（P1 不执行认证）
    snap = _snap("link_down", "captive")
    assert snap.recommendation.preferred is None
    assert snap.recommendation.would_switch is False
    assert "认证" in snap.recommendation.reason

    # 双断 → 不建议切换（避免来回切换）
    snap = _snap("link_down", "unreachable")
    assert snap.recommendation.preferred is None
    assert snap.recommendation.would_switch is False


def test_u1_05_unknown_axis_blocks_recommendation(fake_engine):
    """任一主轴 UNKNOWN/PROBE_UNRELIABLE 时不得给出切换建议。"""
    _healthy_engine(fake_engine)
    fake_engine.routes.update({"default": None})
    fake_engine.proof = {"default": None}
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    snapshot = evaluate(report, engine=fake_engine)
    assert snapshot.ethernet.state == HealthState.PROBE_UNRELIABLE
    assert snapshot.recommendation.preferred is None
    assert snapshot.recommendation.would_switch is False
    assert snapshot.recommendation.warnings


# --------------------------------------------------------------------------- #
# U-1-06 角色解析与纯函数 recommend_path
# --------------------------------------------------------------------------- #
def test_u1_06_resolve_roles_single_candidates(fake_engine):
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    roles = resolve_roles(report)
    assert roles["ethernet"].if_index == 10
    assert roles["wifi"].if_index == 5


def test_u1_06_resolve_roles_multiple_ethernet_not_assigned():
    second = dict(ETH_ADAPTER, Name="以太网 2", InterfaceGuid="{ETH2}",
                  ifIndex=15)
    report = _report(eth=None, wifi=WIFI_ADAPTER, ips=[], routes=[])
    report.adapters = netdiag.parse_adapters(
        json.dumps([ETH_ADAPTER, second, WIFI_ADAPTER], ensure_ascii=False), "[]")
    roles = resolve_roles(report)
    assert roles["ethernet"] is None, "多候选时不得臆断指派"
    assert roles["wifi"].if_index == 5


def test_u1_06_recommend_path_pure_function():
    from campus_network import InterfaceHealth, Recommendation
    eth = InterfaceHealth(axis=HealthAxis.ETHERNET_HEALTH, interface="以太网",
                          if_index=10, state=HealthState.HEALTHY, evidence=[],
                          layers=[], egress_confirmed=True, source_ip="192.168.124.9")
    wifi = InterfaceHealth(axis=HealthAxis.WIFI_HEALTH, interface="WLAN",
                           if_index=5, state=HealthState.CAPTIVE_PORTAL,
                           evidence=[], layers=[], egress_confirmed=True,
                           source_ip="10.252.37.88")
    rec = recommend_path(eth, wifi)
    assert isinstance(rec, Recommendation)
    assert rec.preferred == "ethernet"
    assert rec.would_switch is False

