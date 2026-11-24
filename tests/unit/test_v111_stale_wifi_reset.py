from types import SimpleNamespace

import pytest

from campus_core import PortalStatus, RepairResult
from campus_session_reset import (has_confirmed_internet_failure,
                                  is_confirmed_degraded_auth)


def test_only_authenticated_degraded_results_qualify_for_session_reset():
    degraded = RepairResult(
        auth_ok=True, network_ok=False,
        status=PortalStatus.AUTHENTICATED_DEGRADED)
    assert is_confirmed_degraded_auth(degraded)

    for result in (
        RepairResult(auth_ok=False, network_ok=False,
                     status=PortalStatus.AUTH_REQUIRED),
        RepairResult(auth_ok=True, network_ok=None,
                     status=PortalStatus.AUTHENTICATED_DEGRADED),
        RepairResult(auth_ok=True, network_ok=True,
                     status=PortalStatus.ONLINE),
        RepairResult(auth_ok=True, network_ok=False,
                     status=PortalStatus.AUTH_REQUIRED),
    ):
        assert not is_confirmed_degraded_auth(result)


def test_unusable_wifi_requires_reliable_gate_and_two_failed_cross_targets():
    def probe(*, gate=False, cross=(False, False, False), reliability="ok",
              allows=False):
        return SimpleNamespace(
            reliability=reliability,
            allows_recovery=allows,
            gate_result=SimpleNamespace(ok=gate),
            cross_results=tuple(SimpleNamespace(ok=value) for value in cross),
        )

    assert has_confirmed_internet_failure(probe())
    assert has_confirmed_internet_failure(probe(cross=(True, False, False)))
    assert not has_confirmed_internet_failure(probe(gate=True))
    assert not has_confirmed_internet_failure(probe(cross=(False, True, True)))
    assert not has_confirmed_internet_failure(probe(cross=(False,)))
    assert not has_confirmed_internet_failure(
        probe(reliability="PROBE_UNRELIABLE"))
    assert not has_confirmed_internet_failure(probe(allows=True))


@pytest.mark.parametrize("enabled,qualifies,expected", [
    (True, True, "reset-result"),
    (False, True, "original"),
    (True, False, "original"),
])
def test_tray_only_uses_reset_for_opted_in_degraded_auth(
        enabled, qualifies, expected):
    import campus_reconnect_tray as tray

    original = RepairResult(
        auth_ok=True if qualifies else False,
        network_ok=False,
        status=(PortalStatus.AUTHENTICATED_DEGRADED if qualifies
                else PortalStatus.AUTH_REQUIRED),
        detail="ordinary recovery")
    replacement = SimpleNamespace(
        repair=RepairResult(auth_ok=True, network_ok=True,
                            status=PortalStatus.ONLINE, detail="reset-result"),
        ok=True,
    )
    calls = []
    app = SimpleNamespace(
        get_config=lambda: {"standby_session_reset_enabled": enabled},
        _cancelled=lambda: False,
        _reset_degraded_wifi_session=lambda result: calls.append(result) or replacement,
        log=SimpleNamespace(info=lambda *args: None),
    )

    result = tray.TrayApp._recover_authenticated_degraded_wifi(app, original)

    if expected == "original":
        assert result is original
    else:
        assert result.detail == expected
    assert calls == ([original] if enabled and qualifies else [])


def test_primary_repair_pipeline_passes_degraded_result_through_session_recovery(
        monkeypatch):
    import threading

    import campus_reconnect as engine
    import campus_reconnect_tray as tray

    degraded = RepairResult(auth_ok=True, network_ok=False,
                            status=PortalStatus.AUTHENTICATED_DEGRADED,
                            detail="ordinary recovery")
    recovered = RepairResult(auth_ok=True, network_ok=True,
                             status=PortalStatus.ONLINE, detail="reset-result")
    calls = []
    app = SimpleNamespace(
        get_config=lambda: {"ssid": "SNNU", "paused": False},
        stop_event=threading.Event(),
        _cancel=threading.Event(),
        _repair_override=False,
        session=object(),
        _repair_adapters=lambda: object(),
        _cancelled=lambda: False,
        _recover_authenticated_degraded_wifi=lambda result: calls.append(result) or recovered,
        log=SimpleNamespace(info=lambda *args: None),
        state={},
    )
    monkeypatch.setattr(engine, "wifi_state", lambda: ("SNNU", "connected"))
    monkeypatch.setattr(tray, "repair_service", lambda *args, **kwargs: degraded)

    ok, detail, _elapsed = tray.TrayApp.repair_once(app)

    assert ok and detail == "reset-result"
    assert calls == [degraded]
    assert app._last_result is recovered


def test_v11_lifeline_auth_pipeline_also_uses_session_recovery(monkeypatch):
    from contextlib import contextmanager
    import threading
    import time

    import campus_resilience as resilience
    import campus_resilience_windows as native
    import campus_reconnect_tray as tray

    degraded = RepairResult(auth_ok=True, network_ok=False,
                            status=PortalStatus.AUTHENTICATED_DEGRADED,
                            detail="ordinary recovery")
    recovered = RepairResult(auth_ok=True, network_ok=True,
                             status=PortalStatus.ONLINE, detail="reset-result")
    config = {"lifeline_enabled": True, "remote_restart_enabled": False,
              "recovery_probe_interval": 5, "recovery_failure_threshold": 3,
              "auth_retry_cooldown": 300}
    repaired = []

    class Controller:
        def __init__(self, path, ops, **kwargs):
            self.ops = ops
            self.last_network = False
            self.last_uu = None
            self.detail = "test"

        def tick(self, allow_reboot=False):
            self.ops.repair()

    @contextmanager
    def direct_session():
        yield object()

    app = SimpleNamespace(
        _lifeline=None,
        _lifeline_next=0,
        _lifeline_busy=threading.Event(),
        _repair_busy=threading.Event(),
        _automatic_setup_armed=False,
        _cancelled=lambda: False,
        get_config=lambda: config,
        stop_event=threading.Event(),
        _repair_lifeline_wifi=lambda: degraded,
        _recover_authenticated_degraded_wifi=lambda result: repaired.append(result) or recovered,
        set_state=lambda *args: None,
        log=SimpleNamespace(info=lambda *args: None,
                            warning=lambda *args: None),
    )
    monkeypatch.setattr(resilience, "RecoveryController", Controller)
    monkeypatch.setattr(native, "uu_available", lambda: True)
    monkeypatch.setattr(native, "proxy_snapshot", lambda: {})
    monkeypatch.setattr(native, "proxy_disable", lambda: None)
    monkeypatch.setattr(native, "proxy_restore", lambda value: None)
    monkeypatch.setattr(native, "schedule_reboot", lambda: None)
    monkeypatch.setattr(native, "cancel_reboot", lambda: None)
    monkeypatch.setattr(tray, "direct_session", direct_session)
    monkeypatch.setattr(tray, "repair_service", lambda *args, **kwargs: degraded)

    tray.TrayApp._lifeline_tick(app)
    deadline = time.monotonic() + 2
    while app._repair_busy.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert repaired == [degraded]
    assert app._last_result is recovered
    assert not app._repair_busy.is_set()


@pytest.mark.parametrize("route_kind,expected", [
    ("wifi", True), ("tun", True), ("wrong_portal", False), ("other_physical", False),
])
def test_active_wifi_reset_requires_two_bound_failure_rounds_and_exact_identity(
        monkeypatch, tmp_path, route_kind, expected):
    from contextlib import contextmanager

    import campus_network as network
    import campus_reconnect as engine
    import campus_reconnect_tray as tray
    from tests.fakes import FakeResponse, FakeSession
    from tests.unit.test_v05_dual_path_integration import WIFI_IF, snapshot
    from tools import netdiag

    snap = snapshot(eth=network.HealthState.INTERNET_UNREACHABLE,
                    wifi=network.HealthState.INTERNET_UNREACHABLE,
                    egress_if=WIFI_IF if route_kind == "wifi" else 99)
    session = FakeSession()
    session.trust_env = False
    session.route("http://202.117.144.205:8602/snnuportal/userstatus.jsp",
                  FakeResponse(200,
                      f"当前登录账号：123456 当前IP：{snap.wifi.source_ip}"
                      '<iframe src="http://202.117.144.205/zili/auth_my_state.php?test=1"></iframe>'))
    session.route("http://202.117.144.205/zili/auth_my_state.php?test=1",
                  FakeResponse(200,
                      f"<tr><td>{snap.wifi.source_ip}</td>"
                      "<td>10:11:12:13:14:15</td></tr>"))
    session.route("http://202.117.144.205:8602/snnuportal/logoff",
                  FakeResponse(200, "下线成功"))

    @contextmanager
    def direct_session():
        yield session

    class ScopedTransport:
        interface_bound = True
        if_index = WIFI_IF
        source_ip = snap.wifi.source_ip

        def bind_session(self, bound):
            bound.trust_env = False
            bound.get_adapter = lambda url: SimpleNamespace(transport=self)
            return True

    class ProbeEngine:
        def __init__(self, **kwargs):
            pass

        def for_interface(self, index, source, dns):
            assert index == WIFI_IF and source == snap.wifi.source_ip
            return ScopedTransport()

    adapter = SimpleNamespace(if_index=WIFI_IF, name=snap.wifi.interface,
                              media="wifi", virtual=False,
                              mac="10:11:12:13:14:15")
    report = SimpleNamespace(adapters=[adapter, SimpleNamespace(
        if_index=99, virtual=route_kind in ("tun", "wrong_portal"))])
    monkeypatch.setattr(network.NetworkObserver, "route_lookup",
                        lambda self, target: SimpleNamespace(
                            if_index=99 if route_kind == "wrong_portal" else WIFI_IF))

    class FailedVerifier:
        def __init__(self):
            self.calls = []

        def verify(self, request):
            self.calls.append(request)
            return SimpleNamespace(
                reliability="ok", allows_recovery=False,
                gate_result=SimpleNamespace(ok=False),
                cross_results=tuple(SimpleNamespace(ok=value)
                                    for value in (False, True, False)))

    verifier = FailedVerifier()
    repairs = []
    app = SimpleNamespace(
        get_config=lambda: {"standby_session_reset_enabled": True, "ssid": "SNNU"},
        _cancelled=lambda: False,
        snapshot_source=lambda: SimpleNamespace(
            observe=lambda now: SimpleNamespace(trusted=True, snapshot=snap)),
        _build_path_verifier=lambda: verifier,
        _repair_active_wifi=lambda wifi: repairs.append(wifi) or SimpleNamespace(
            ok=True,
            repair=RepairResult(auth_ok=True, network_ok=True,
                                status=PortalStatus.ONLINE, detail="强制重新登录成功")),
        log=SimpleNamespace(info=lambda message: None,
                            warning=lambda message: None),
    )
    monkeypatch.setattr(tray, "direct_session", direct_session)
    monkeypatch.setattr(tray, "get_credentials", lambda: ("123456", "test-password"))
    monkeypatch.setattr(tray, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(engine, "resolve_port", lambda *args: (8602, "root"))
    monkeypatch.setattr(engine, "_wifi_source_ip", lambda *args: snap.wifi.source_ip)
    monkeypatch.setattr(engine, "wifi_state", lambda *args: ("SNNU", "connected"))
    monkeypatch.setattr(network, "_interface_dns", lambda *args: snap.wifi.dns_servers)
    monkeypatch.setattr(network, "WindowsProbeEngine", ProbeEngine)
    monkeypatch.setattr(netdiag, "collect_health", lambda runner: report)

    result = tray.TrayApp._reset_degraded_wifi_session(
        app, RepairResult(auth_ok=True, network_ok=False,
                          status=PortalStatus.AUTHENTICATED_DEGRADED))

    if not expected:
        assert result is None
        assert not repairs
        assert not any(call["url"].endswith("/logoff") for call in session.calls)
        return
    assert result.ok
    assert len(verifier.calls) == 2
    assert all(request.role == "wifi" for request in verifier.calls)
    assert repairs == [snap.wifi]
    assert sum(call["url"].endswith("/logoff") for call in session.calls) == 1
    assert not any("logoffall" in call["url"] for call in session.calls)


def test_active_wifi_reset_fails_closed_on_unreliable_probe(monkeypatch, tmp_path):
    import campus_network as network
    import campus_reconnect as engine
    import campus_reconnect_tray as tray
    from tests.unit.test_v05_dual_path_integration import WIFI_IF, snapshot
    from tools import netdiag

    snap = snapshot(eth=network.HealthState.INTERNET_UNREACHABLE,
                    wifi=network.HealthState.INTERNET_UNREACHABLE,
                    egress_if=WIFI_IF)
    report = SimpleNamespace(adapters=[SimpleNamespace(
        if_index=WIFI_IF, name=snap.wifi.interface, media="wifi",
        virtual=False, mac="10:11:12:13:14:15")])
    app = SimpleNamespace(
        get_config=lambda: {"standby_session_reset_enabled": True, "ssid": "SNNU"},
        _cancelled=lambda: False,
        snapshot_source=lambda: SimpleNamespace(
            observe=lambda now: SimpleNamespace(trusted=True, snapshot=snap)),
        _build_path_verifier=lambda: SimpleNamespace(verify=lambda request: SimpleNamespace(
            reliability="PROBE_UNRELIABLE", allows_recovery=False,
            gate_result=None, cross_results=())),
        log=SimpleNamespace(info=lambda message: None,
                            warning=lambda message: None),
    )
    monkeypatch.setattr(tray, "get_credentials",
                        lambda: pytest.fail("credentials must not be read"))
    monkeypatch.setattr(netdiag, "collect_health", lambda runner: report)
    monkeypatch.setattr(network.NetworkObserver, "route_lookup",
                        lambda self, target: SimpleNamespace(if_index=WIFI_IF))
    monkeypatch.setattr(engine, "wifi_state", lambda *args: ("SNNU", "connected"))

    result = tray.TrayApp._reset_degraded_wifi_session(
        app, RepairResult(auth_ok=True, network_ok=False,
                          status=PortalStatus.AUTHENTICATED_DEGRADED))

    assert result is None
