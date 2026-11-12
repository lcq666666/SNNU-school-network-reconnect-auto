"""P7.5-2: scoped evidence is independent of the system default route."""
from types import SimpleNamespace

import pytest

from campus_core import PortalStatus
from campus_network import HealthAxis, HealthState, InterfaceHealthChecker, evaluate
from campus_tray_observer import is_auto_executable_snapshot
from tests.fakes import FakeProbeEngine
from tests.unit.test_v03_p1_health import (
    ETH_ADAPTER, ETH_IP, WIFI_IP, ETH_ROUTE, WIFI_ROUTE, _report, _healthy_engine,
)


def _check(engine, **kwargs):
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    wifi = next(a for a in report.adapters if a.if_index == 5)
    return InterfaceHealthChecker(engine, **kwargs).check(
        HealthAxis.WIFI_HEALTH, wifi, report, engine.route_lookup)


def test_nondefault_wifi_is_independently_healthy():
    engine = _healthy_engine(FakeProbeEngine())
    engine.proof = {"default": True}
    engine.routes["default"] = SimpleNamespace(if_index=10)
    health = _check(engine)
    assert health.state is HealthState.HEALTHY
    assert health.egress_confirmed is True


def test_wifi_only_authorization_requires_successful_adapter_inventory():
    engine = _healthy_engine(FakeProbeEngine())
    report = _report(ips=[WIFI_IP], routes=[WIFI_ROUTE])
    report.adapters = [a for a in report.adapters if a.category != "physical-ethernet"]
    assert not is_auto_executable_snapshot(evaluate(report, engine))[0]
    report.commands_executed.append("adapters")
    assert is_auto_executable_snapshot(evaluate(report, engine))[0]
    report.errors.append({"command": "adapters", "error": "partial inventory"})
    assert not is_auto_executable_snapshot(evaluate(report, engine))[0]


@pytest.mark.parametrize("proof", [False, None])
def test_unbound_engine_fails_closed_before_any_probe(proof):
    engine = _healthy_engine(FakeProbeEngine())
    engine.proof = {"default": proof}
    health = _check(engine)
    assert health.state is HealthState.PROBE_UNRELIABLE
    assert not engine.calls["tcp"]
    assert not engine.calls["http"]
    assert not engine.calls["dns"]


def test_closed_gateway_ports_do_not_override_public_evidence():
    engine = _healthy_engine(FakeProbeEngine())
    for port in (80, 443):
        engine.tcp[("10.252.1.1", port)] = False
    assert _check(engine).state is HealthState.HEALTHY


def test_portal_evidence_precedes_public_tcp_failure():
    engine = _healthy_engine(FakeProbeEngine())
    engine.tcp["default"] = False
    for url in list(engine.http):
        engine.http[url] = {"status_code": 302, "redirected": True,
                            "final_url": "http://202.117.144.205:8601/snnuportal/login.jsp"}
    assert _check(engine).state is HealthState.CAPTIVE_PORTAL


def test_header_only_campus_redirect_is_detected_even_with_one_public_hit():
    engine = _healthy_engine(FakeProbeEngine())
    url = next(iter(engine.http))
    engine.http[url] = {"status_code": 302, "redirected": True, "final_url": url,
                        "redirect_url": "http://202.117.144.205:8601/snnuportal/login.jsp"}
    assert _check(engine).state is HealthState.CAPTIVE_PORTAL
    engine.http[url]["redirect_url"] = "http://202.117.144.205.example/snnuportal/login.jsp"
    assert _check(engine).state is HealthState.HEALTHY


@pytest.mark.parametrize(("status", "expected"), [
    (PortalStatus.AUTH_REQUIRED, HealthState.CAPTIVE_PORTAL),
    (PortalStatus.ONLINE, HealthState.INTERNET_UNREACHABLE),
    (None, HealthState.INTERNET_UNREACHABLE),
])
def test_associated_wifi_with_no_public_web_uses_read_only_portal_evidence(status, expected):
    engine = _healthy_engine(FakeProbeEngine())
    engine.http = {"default": {"status_code": 0, "error": "timeout"}}
    calls = []
    health = _check(engine, parallel_probes=True,
                    portal_probe=lambda alias, _scoped: calls.append(alias) or status)
    assert calls == ["WLAN"]
    assert health.state is expected


def test_dns_is_scoped_to_the_wifi_source():
    engine = _healthy_engine(FakeProbeEngine())
    engine.dns["default"] = True  # global/Ethernet DNS must not mask Wi-Fi failure
    for name in ("www.baidu.com", "www.qq.com"):
        engine.dns[(name, (WIFI_IP["IPAddress"], 0))] = False
    assert _check(engine).state is HealthState.DNS_FAILURE
    assert all(source == (WIFI_IP["IPAddress"], 0)
               for _, source in engine.calls["dns"])


def test_source_only_engine_is_not_authoritative():
    engine = _healthy_engine(FakeProbeEngine())
    engine.for_interface = None
    assert _check(engine).state is HealthState.PROBE_UNRELIABLE
    assert not engine.calls["tcp"]


def test_dns_config_collection_and_interface_selection():
    from campus_network import _interface_dns
    from tests.fakes import FakeCommandRunner
    from tools import netdiag
    runner = FakeCommandRunner({"dns_servers": '[{"InterfaceIndex":5,"ServerAddresses":["10.0.0.1"]},{"InterfaceIndex":10,"ServerAddresses":["192.168.1.1"]}]'}, default="[]")
    report = netdiag.collect(runner)
    assert _interface_dns(report, 5) == ["10.0.0.1"]
    assert report.to_dict()["dns_servers"] == report.dns_servers
    assert "dns_servers" in report.commands_executed


def test_wifi_is_evaluated_when_ethernet_is_down():
    """有线断开不得短路 Wi-Fi 轴；Wi-Fi 仍使用自己的接口证据。"""
    engine = _healthy_engine(FakeProbeEngine())
    report = _report(
        eth=dict(ETH_ADAPTER, Status="Disconnected"),
        ips=[WIFI_IP], routes=[WIFI_ROUTE])
    report.dns_servers = [{"if_index": 5, "server_addresses": ["10.0.0.1"]}]
    snapshot = evaluate(report, engine)
    assert snapshot.ethernet.state is HealthState.LINK_DOWN
    assert snapshot.wifi.state is HealthState.HEALTHY
    assert snapshot.recommendation.preferred == "wifi"
    assert engine.calls["tcp"], "Wi-Fi 轴未独立发起探测"


def test_wifi_missing_interface_facts_remains_fail_closed_with_reason():
    from campus_dual_path import path_request_failure_reason, path_request_from_snapshot
    report = _report(ips=[ETH_IP, WIFI_IP], routes=[ETH_ROUTE, WIFI_ROUTE])
    report.dns_servers = []
    snapshot = evaluate(report, _healthy_engine(FakeProbeEngine()))
    assert path_request_from_snapshot(snapshot, "wifi") is None
    assert path_request_failure_reason(snapshot, "wifi") == "缺少该接口 DNS"


def test_plain_http_portal_probe_is_available_without_disabling_tls():
    from campus_network import DEFAULT_HTTP_TARGETS
    assert any(spec.url.startswith("http://") for spec in DEFAULT_HTTP_TARGETS)
    assert any(spec.url.startswith("https://") for spec in DEFAULT_HTTP_TARGETS)


def test_p8_k5_axis_http_targets_avoid_unreachable_host_and_keep_diversity():
    """P8/K-5（2026-10-15 真机取证）：``www.msftconnecttest.com`` 在本校园网 **3/3 不可达**
    （DNS/连接失败），不得再留在分轴探测点表里——否则每个轴恒有一条失败证据、每次多付
    一次连接失败。同时必须保留 ``http://`` 与 ``https://`` 两种 scheme 且目标互不重复
    （"任一 http 命中即健康"，单点失败不得判全网断）。
    """
    from campus_network import DEFAULT_HTTP_TARGETS
    urls = [spec.url for spec in DEFAULT_HTTP_TARGETS]
    assert len(urls) >= 3, urls
    assert len(set(urls)) == len(urls), urls
    assert all("msftconnecttest.com" not in url for url in urls), urls
    assert any(url.startswith("http://") for url in urls), urls
    assert any(url.startswith("https://") for url in urls), urls


def test_p8_k6_axis_tcp_targets_avoid_blocked_hosts_and_keep_diversity():
    """P8/K-6（2026-10-15 真机取证）：``1.1.1.1:443`` 与 ``8.8.8.8:443`` 在本校园网
    **2/2 超时不可达**（各 4s 超时），不得再留在分轴 TCP 探测点表里——否则每个轴恒有
    一条失败证据、每轮多付两次连接超时。换为实测可达（~15ms）且分属两家不同供应商的
    公共 DoH 端点（裸 IP，不依赖 DNS），且必须保留双点聚合（单点失败不得判全网断）。
    """
    from campus_network import DEFAULT_TCP_TARGETS
    hosts = [host for host, _port in DEFAULT_TCP_TARGETS]
    assert len(DEFAULT_TCP_TARGETS) >= 2, DEFAULT_TCP_TARGETS
    assert len(set(hosts)) == len(hosts), "TCP 探测点重复：%r" % (DEFAULT_TCP_TARGETS,)
    for blocked in ("1.1.1.1", "8.8.8.8"):
        assert blocked not in hosts, \
            "真机实测不可达的 TCP 探测点仍在列表中：%r" % (DEFAULT_TCP_TARGETS,)
    assert set(hosts) == {"223.5.5.5", "120.53.53.53"}, \
        "TCP 探测点不是真机实测可达的两家不同供应商端点：%r" % (DEFAULT_TCP_TARGETS,)
    assert all(port == 443 for _host, port in DEFAULT_TCP_TARGETS), DEFAULT_TCP_TARGETS


def test_p75_3_binding_refresh_replaces_dhcp_source_before_auth():
    from campus_snnu import InterfaceBinding, SnnuBackupLink, SnnuOps
    from tests.fakes import FakeInterfaceBinder, FakeSnnuLink
    fake = FakeSnnuLink()
    refreshed = InterfaceBinding("WLAN", 5, "10.8.0.9")
    binder = FakeInterfaceBinder()
    ops = SnnuOps(
        wifi_state=fake.wifi_state, wifi_reconnect=fake.wifi_reconnect,
        ip_ready=fake.ip_ready, route_lookup=lambda target: SimpleNamespace(if_index=5),
        resolve_port=lambda session, requested: (8601, "test"),
        portal_reachable=lambda session, port: True,
        read_status=lambda session, port: None,
        bind_interface=binder, refresh_binding=lambda: refreshed)
    link = SnnuBackupLink(ops=ops, session=SimpleNamespace(),
                          if_index=5, source_ip="10.8.0.5", reconnect_wifi=False)
    result = link.run()
    assert result.error is not None
    assert binder.calls and binder.calls[0].source_ip == "10.8.0.9"
    assert result.binding.source_ip == "10.8.0.9"
