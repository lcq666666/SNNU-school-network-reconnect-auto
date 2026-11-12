"""No host I/O: automatic setup, rollback, restart and guardian integration."""
from copy import deepcopy
import logging
from types import SimpleNamespace

import pytest

from campus_auto_setup import AutomaticSetup, WindowsClientLifecycle
from tests.unit.test_v069_proxy_compat import fixture


class Lifecycle:
    def __init__(self):
        self.calls = []
        self.running = True
    def pause(self):
        self.calls.append("pause")
        self.running = False
        return ["original-client"]
    def resume(self, token):
        self.calls.append(("resume", token))
        self.running = True


def setup(tmp_path):
    compat, root, registry = fixture(tmp_path)
    lifecycle = Lifecycle()
    compat.client_running = lambda: lifecycle.running
    return AutomaticSetup(compat, lambda: [root], lifecycle), lifecycle, root, registry


def test_background_setup_stops_client_backs_up_then_restores_it(tmp_path):
    guard, lifecycle, root, registry = setup(tmp_path)
    old_registry = deepcopy(registry)
    result = guard.run()
    assert result.ready and result.changed
    assert lifecycle.calls == ["pause", ("resume", ["original-client"])]
    assert guard.compat.persistent_ready(root)
    assert registry["ProxyEnable"] == old_registry["ProxyEnable"]
    assert registry["ProxyServer"] == old_registry["ProxyServer"]
    assert list((guard.compat.directory / "proxy-backups").glob("*.json"))
    lifecycle.calls.clear()
    assert not guard.run().changed
    assert lifecycle.calls == []


def test_no_client_installation_only_adds_missing_system_entries(tmp_path):
    guard, lifecycle, _, registry = setup(tmp_path)
    guard.roots = lambda: []
    assert guard.run().ready
    assert lifecycle.calls == []
    assert "202.117.144.205" in registry["ProxyOverride"]["value"]


def test_multiple_configurations_preserve_user_selection(tmp_path):
    guard, lifecycle, root, _ = setup(tmp_path)
    guard.roots = lambda: [root, root / "another"]
    result = guard.run()
    assert not result.ready
    assert lifecycle.calls == []


def test_restart_after_write_failure_and_existing_rollback_still_applies(tmp_path, monkeypatch):
    guard, lifecycle, root, _ = setup(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    apply = guard.compat.apply
    def fail(target=None):
        if target is not None:
            raise OSError("simulated failure")
        return apply()
    monkeypatch.setattr(guard.compat, "apply", fail)
    with pytest.raises(OSError):
        guard.run()
    assert lifecycle.running
    assert lifecycle.calls[-1][0] == "resume"
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_cancellation_after_client_stop_still_restarts_client(tmp_path):
    guard, lifecycle, _, _ = setup(tmp_path)
    guard.cancelled = lambda: not lifecycle.running
    result = guard.run()
    assert not result.ready and lifecycle.running


def test_healthy_configuration_can_restart_a_failed_client_without_rewriting(tmp_path):
    guard, lifecycle, _, _ = setup(tmp_path)
    guard.run()
    before = list((guard.compat.directory / "proxy-backups").glob("*.json"))
    lifecycle.calls.clear()
    result = guard.run(restart_client=True)
    assert result.ready
    assert lifecycle.calls == ["pause", ("resume", ["original-client"])]
    assert list((guard.compat.directory / "proxy-backups").glob("*.json")) == before


def test_interrupted_system_only_transaction_does_not_stop_client(tmp_path):
    import json
    guard, lifecycle, _, _ = setup(tmp_path)
    guard.roots = lambda: []
    backup = guard.compat.apply()
    data = json.loads(backup.read_text(encoding="utf-8"))
    data["status"] = "applying"
    backup.write_text(json.dumps(data), encoding="utf-8")
    assert guard.run().ready
    assert lifecycle.calls == [("resume", [])]


def test_native_lifecycle_restores_original_executable_with_no_shell():
    running = [(123, "C:/Clash/clash-verge.exe")]
    calls = []
    def stop(args, **kwargs):
        calls.append(args)
        running.clear()
    native = WindowsClientLifecycle(clients=lambda: list(running), run=stop,
                                   launch=lambda args, **kwargs: calls.append(args), pause=lambda _: None)
    token = native.pause()
    native.resume(token)
    assert calls == [["taskkill.exe", "/PID", "123", "/F"], ["C:/Clash/clash-verge.exe"]]


def test_native_lifecycle_does_not_stop_multiple_clients():
    native = WindowsClientLifecycle(clients=lambda: [(1, "a"), (2, "b")],
                                   run=lambda *a, **k: pytest.fail("must not kill"))
    with pytest.raises(OSError):
        native.pause()


def test_setup_worker_is_unarmed_during_test_construction(monkeypatch):
    import campus_reconnect_tray as tray
    import campus_auto_setup as auto
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(auto, "windows_setup", lambda **kwargs: pytest.fail("host access"))
    app._automatic_setup_tick()
    assert not app._repair_busy.is_set()


def test_background_setup_serializes_with_recovery_and_backs_off(monkeypatch):
    import campus_reconnect_tray as tray
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG))
    calls = []
    app._automatic_setup_armed = True
    app._automatic_setup = SimpleNamespace(run=lambda: calls.append("setup") or SimpleNamespace(message="ready"))
    app._repair_busy.set()
    app._automatic_setup_tick()
    assert calls == []
    app._repair_busy.clear()
    app._automatic_setup_tick()
    app._automatic_setup_tick()
    assert calls == ["setup"]
    assert not app._repair_busy.is_set()


def test_proxy_failures_require_three_samples_and_restart_cooldown(monkeypatch):
    import campus_reconnect_tray as tray
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG))
    app._observed_proxy_endpoint = lambda: "127.0.0.1:8118"
    calls = []
    app._automatic_setup = SimpleNamespace(run=lambda **kwargs: calls.append(kwargs))
    class Session:
        proxies = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(tray, "direct_session", Session)
    monkeypatch.setattr(tray, "probe_via_proxy", lambda *a: SimpleNamespace(proxy_ok=False))
    app._guard_proxy_health()
    app._guard_proxy_health()
    assert calls == []
    app._guard_proxy_health()
    assert calls == [{"restart_client": True}]
    for _ in range(5):
        app._guard_proxy_health()
    assert len(calls) == 1
    monkeypatch.setattr(tray, "probe_via_proxy", lambda *a: SimpleNamespace(proxy_ok=True))
    assert app._guard_proxy_health()
    assert app._proxy_failure_count == 0


def test_online_guard_settings_save_existing_values(monkeypatch):
    import campus_reconnect_tray as tray
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG, proxy_endpoint="127.0.0.1:8118"))
    saved = []
    monkeypatch.setattr(tray, "save_config", lambda cfg: saved.append(cfg) or True)
    app.notify = lambda *args: None
    app.on_toggle_online_guard()
    assert app.config["lifeline_enabled"] and app.config["automatic_proxy_enabled"]
    assert saved[0]["proxy_endpoint"] == "127.0.0.1:8118"
    assert not saved[0]["lifeline_reboot_enabled"]


def test_new_install_initialization_enables_guard_and_autostart(monkeypatch):
    import campus_reconnect_tray as tray
    calls = []
    monkeypatch.setattr(tray, "save_config", lambda cfg: True)
    monkeypatch.setattr(tray, "get_autostart", lambda: False)
    monkeypatch.setattr(tray, "set_autostart", lambda value: calls.append(value) or True)
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG))
    app.enable_automatic_setup(first_install=True)
    assert app.config["lifeline_enabled"]
    assert calls == [True]
    assert not app.config["lifeline_reboot_enabled"]


def test_client_restart_journal_recovers_after_helper_crash(tmp_path):
    from campus_install import atomic_json
    executable = tmp_path / "clash-verge.exe"
    executable.write_bytes(b"test fixture only")
    journal = tmp_path / "restart.json"
    atomic_json(journal, [{"executable": str(executable)}])
    running = []
    def launch(args, **kwargs):
        running.append((23, args[0]))
    native = WindowsClientLifecycle(journal=journal, clients=lambda: running,
                                   launch=launch, pause=lambda _: None)
    native.recover()
    assert running == [(23, str(executable))]
    assert not journal.exists()


def test_client_launch_failure_keeps_recovery_journal(tmp_path):
    from campus_install import atomic_json
    executable = tmp_path / "clash-verge.exe"
    executable.write_bytes(b"test fixture only")
    journal = tmp_path / "restart.json"
    atomic_json(journal, [{"executable": str(executable)}])
    native = WindowsClientLifecycle(journal=journal, clients=lambda: [],
                                   launch=lambda *a, **k: None, pause=lambda _: None)
    with pytest.raises(OSError):
        native.recover()
    assert journal.exists()


def test_different_running_client_does_not_discard_original_restart_record(tmp_path):
    from campus_install import atomic_json
    executable = tmp_path / "clash-verge.exe"
    executable.write_bytes(b"test fixture only")
    journal = tmp_path / "restart.json"
    atomic_json(journal, [{"executable": str(executable)}])
    native = WindowsClientLifecycle(journal=journal,
                                   clients=lambda: [(12, str(tmp_path / "other" / "clash-verge.exe"))])
    with pytest.raises(OSError):
        native.recover()
    assert journal.exists()


def test_live_guard_status_does_not_claim_interface_specific_verification():
    import campus_reconnect_tray as tray
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG, lifeline_enabled=True))
    app._lifeline = SimpleNamespace(last_network=True, last_uu=True)
    app._lifeline_observed_at = 100
    app._uu_available = True
    state = app.ui_state(now=101)
    assert state.overall == "HEALTHY"
    assert state.ethernet.verified is not True
    assert state.wifi.verified is not True
    assert state.uu.label == "已建立连接"
    assert state.mode_label == "在线守护"
    app._lifeline.last_uu = None
    assert app.ui_state(now=101).uu.label == "状态待确认"
    assert app.ui_state(now=160).overall != "HEALTHY"
