"""A stale wireless session may be reset only after primary and identity checks."""
from types import SimpleNamespace

import pytest

import campus_reconnect as engine
from tests.fakes import FakeSession, FakeResponse


def setup_reset(monkeypatch, tmp_path):
    from campus_session_reset import reset_wireless_session
    session = FakeSession()
    session.trust_env = False
    transport = SimpleNamespace(interface_bound=True, if_index=5, source_ip="10.0.0.2")
    session.get_adapter = lambda url: SimpleNamespace(transport=transport)
    base = "http://202.117.144.205:8602/snnuportal/"
    session.route(base + "userstatus.jsp", FakeResponse(200,
        '当前登录账号：123456@mobile 当前IP：10.0.0.2'
        '<iframe src="http://202.117.144.205/zili/auth_my_state.php?test=1"></iframe>'))
    session.route("http://202.117.144.205/zili/auth_my_state.php?test=1", FakeResponse(200,
        '<table><tr><td>10.0.0.2</td><td>10:11:12:13:14:15</td></tr></table>'))
    session.route(base + "logoff", FakeResponse(200, "login"))
    monkeypatch.setattr(engine, "resolve_port", lambda *args: (8602, "root"))
    calls = []
    args = dict(session=session, wifi=SimpleNamespace(if_index=5, source_ip="10.0.0.2"),
                mac="10-11-12-13-14-15", account="123456", enabled=True,
                journal=tmp_path / "reset.json", primary_ready=lambda: True,
                relogin=lambda: calls.append("login") or "restored", clock=lambda: 1000)
    return reset_wireless_session, session, args, calls


def test_reset_relogs_in_and_persists_six_hour_cooldown(monkeypatch, tmp_path):
    reset, session, args, calls = setup_reset(monkeypatch, tmp_path)
    assert reset(**args) == "restored"
    assert calls == ["login"]
    assert args["journal"].exists()
    assert reset(**args) is None
    assert calls == ["login"]
    args["clock"] = lambda: 22600
    assert reset(**args) == "restored"
    logouts = [c for c in session.calls if c["url"].endswith("logoff")]
    assert len(logouts) == 2
    assert all(c["kwargs"]["allow_redirects"] is False for c in logouts)


@pytest.mark.parametrize("change", ["disabled", "primary", "mac", "source", "account", "proxy", "cancel"])
def test_reset_refuses_without_full_proof(monkeypatch, tmp_path, change):
    reset, session, args, calls = setup_reset(monkeypatch, tmp_path)
    if change == "disabled":
        args["enabled"] = False
    if change == "primary":
        args["primary_ready"] = lambda: False
    if change == "mac":
        args["mac"] = "AA:BB:CC:DD:EE:FF"
    if change == "source":
        args["wifi"].source_ip = "10.0.0.3"
    if change == "account":
        args["account"] = "654321"
    if change == "proxy":
        session.trust_env = True
    if change == "cancel":
        args["cancelled"] = lambda: True
    assert reset(**args) is None
    assert not calls
    assert not any(c["url"].endswith("logoff") for c in session.calls)


def test_ambiguous_logout_timeout_still_restores_login(monkeypatch, tmp_path):
    import requests
    reset, session, args, calls = setup_reset(monkeypatch, tmp_path)
    session.route("http://202.117.144.205:8602/snnuportal/logoff", requests.Timeout())
    assert reset(**args) == "restored"
    assert calls == ["login"]
    assert reset(**args) is None


def test_corrupt_journal_refuses_logout(monkeypatch, tmp_path):
    reset, session, args, calls = setup_reset(monkeypatch, tmp_path)
    args["journal"].write_text("broken", encoding="utf-8")
    assert reset(**args) is None
    assert not calls


@pytest.mark.parametrize("backend", [
    '<tr><td>10.0.0.2</td><td>aa:bb:cc:dd:ee:ff</td></tr>'
    '<tr><td>10.0.0.3</td><td>10:11:12:13:14:15</td></tr>',
    '<tr><td>110.0.0.20</td><td>10:11:12:13:14:15</td></tr>',
])
def test_ip_and_mac_must_match_the_same_exact_device_row(monkeypatch, tmp_path, backend):
    reset, session, args, calls = setup_reset(monkeypatch, tmp_path)
    session.route("http://202.117.144.205/zili/auth_my_state.php?test=1", FakeResponse(200, backend))
    assert reset(**args) is None
    assert not calls


def test_foreign_frame_is_not_requested(monkeypatch, tmp_path):
    reset, session, args, calls = setup_reset(monkeypatch, tmp_path)
    session.route("http://202.117.144.205:8602/snnuportal/userstatus.jsp", FakeResponse(200,
        '当前登录账号：123456 当前IP：10.0.0.2<iframe src="http://other.invalid/zili/auth_my_state.php">'))
    assert reset(**args) is None
    assert all("other.invalid" not in item["url"] for item in session.calls)


def test_pause_during_primary_verification_prevents_logout(monkeypatch, tmp_path):
    reset, session, args, calls = setup_reset(monkeypatch, tmp_path)
    paused = [False]
    def primary_ready():
        paused[0] = True
        return True
    args.update(primary_ready=primary_ready, cancelled=lambda: paused[0])
    assert reset(**args) is None
    assert not args["journal"].exists()


@pytest.mark.parametrize("auth,network,should_reset", [
    (True, False, True), (False, False, False), (None, False, False),
    (True, None, False), (True, True, False),
])
def test_guard_only_resets_after_confirmed_degraded_auth(auth, network, should_reset):
    from campus_resilience import StandbyWifiGuard
    from tests.unit.test_v05_dual_path_integration import snapshot, H, IU
    calls = []
    result = SimpleNamespace(ok=False, repair=SimpleNamespace(auth_ok=auth, network_ok=network))
    restored = SimpleNamespace(ok=True)
    guard = StandbyWifiGuard(lambda wifi: result,
                             reset=lambda wifi: calls.append("reset") or restored)
    snap = snapshot(H, IU)
    for moment in (0, 15):
        guard.tick(snap, trusted=True, now=moment)
    outcome = guard.tick(snap, trusted=True, now=30)
    assert bool(calls) == should_reset
    assert outcome is (restored if should_reset else result)


def test_uu_probe_failure_does_not_block_standby_maintenance(tmp_path):
    from tests.unit.test_v067_resilience import make_controller
    controller, calls, _ = make_controller(tmp_path, online=True, uu=None)
    controller.ops.standby_repair = lambda: calls.append("standby")
    controller.tick(1000)
    assert calls == ["standby"]


def test_tray_entry_uses_fresh_primary_and_bound_wireless_identity(monkeypatch, tmp_path):
    from contextlib import contextmanager
    import campus_reconnect_tray as tray
    import campus_network as network
    from tools import netdiag
    from tests.unit.test_v05_dual_path_integration import snapshot, H, IU

    _, session, _, _ = setup_reset(monkeypatch, tmp_path)
    snap = snapshot(H, IU)
    wifi = snap.wifi
    session.route("http://202.117.144.205:8602/snnuportal/userstatus.jsp", FakeResponse(200,
        f'当前登录账号：123456 当前IP：{wifi.source_ip}'
        '<iframe src="http://202.117.144.205/zili/auth_my_state.php?test=1">'))
    session.route("http://202.117.144.205/zili/auth_my_state.php?test=1", FakeResponse(200,
        f'<tr><td>{wifi.source_ip}</td><td>10:11:12:13:14:15</td></tr>'))
    @contextmanager
    def direct_session():
        yield session
    def for_interface(self, index, source, dns):
        transport = SimpleNamespace(interface_bound=True, if_index=index, source_ip=source)
        def bind(bound):
            bound.trust_env = False
            bound.get_adapter = lambda url: SimpleNamespace(transport=transport)
            return True
        transport.bind_session = bind
        return transport
    report = SimpleNamespace(adapters=[SimpleNamespace(if_index=wifi.if_index,
        name=wifi.interface, media="wifi", virtual=False, mac="10:11:12:13:14:15")])
    checked = []
    def verify(request):
        checked.append(request.role)
        return SimpleNamespace(allows_recovery=True)
    monkeypatch.setattr(tray, "direct_session", direct_session)
    monkeypatch.setattr(tray, "get_credentials", lambda: ("123456", "test-only-password"))
    monkeypatch.setattr(tray, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(netdiag, "collect_health", lambda runner: report)
    monkeypatch.setattr(engine, "_wifi_source_ip", lambda *args: wifi.source_ip)
    monkeypatch.setattr(network, "_interface_dns", lambda *args: wifi.dns_servers)
    monkeypatch.setattr(network.WindowsProbeEngine, "for_interface", for_interface)
    monkeypatch.setattr(engine, "verify_internet", lambda *args, **kwargs: (False, "offline"))
    monkeypatch.setattr(engine, "wifi_state", lambda *args: ("SNNU", "connected"))
    relogins = []
    app = SimpleNamespace(
        get_config=lambda: {"lifeline_enabled": True, "standby_session_reset_enabled": True},
        _cancelled=lambda: False, log=SimpleNamespace(info=lambda *args: None),
        snapshot_source=lambda: SimpleNamespace(observe=lambda now: SimpleNamespace(trusted=True, snapshot=snap)),
        _build_path_verifier=lambda: SimpleNamespace(verify=verify),
        _repair_standby_wifi=lambda link: relogins.append(link) or SimpleNamespace(ok=True))
    result = tray.TrayApp._reset_standby_wifi(app, wifi)
    assert result.ok and relogins == [wifi]
    assert checked == ["ethernet"]
    assert sum(c["url"].endswith("logoff") for c in session.calls) == 1


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", False)])
def test_session_reset_consent_survives_config_round_trip(monkeypatch, tmp_path, value, expected):
    import campus_reconnect_tray as tray
    path = tmp_path / "tray_config.json"
    monkeypatch.setattr(tray, "CONFIG_FILE", path)
    cfg = dict(tray.DEFAULT_CONFIG, standby_session_reset_enabled=value)
    assert tray.save_config(cfg)
    assert tray.load_config()["standby_session_reset_enabled"] is expected


def test_journal_write_failure_cannot_disconnect(monkeypatch, tmp_path):
    import campus_session_reset as reset_module
    reset, session, args, calls = setup_reset(monkeypatch, tmp_path)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(reset_module, "atomic_json", fail)
    with pytest.raises(OSError):
        reset(**args)
    assert not calls
    assert not any(c["url"].endswith("logoff") for c in session.calls)
