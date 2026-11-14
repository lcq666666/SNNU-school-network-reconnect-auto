"""Offline recovery, update and migration regressions for the reported failures."""
from pathlib import Path
import json

import pytest

from campus_resilience import RecoveryController, RecoveryOps


def make_controller(tmp_path, *, online=False, uu=False, repair_error=False):
    calls = []
    status = {"online": online, "uu": uu}

    def repair():
        calls.append("repair")
        if repair_error:
            raise RuntimeError("simulated failure")
        return False, "network unavailable"

    ops = RecoveryOps(
        network_ok=lambda: status["online"],
        uu_ok=lambda: status["uu"],
        restart_uu=lambda: calls.append("uu"),
        proxy_snapshot=lambda: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"},
        proxy_disable=lambda: calls.append("proxy-off"),
        proxy_restore=lambda value: calls.append(("restore", value)),
        repair=repair,
        reboot_ready=lambda: True,
        reboot=lambda: calls.append("reboot"),
        cancel_reboot=lambda: calls.append("cancel-reboot"),
    )
    controller = RecoveryController(tmp_path / "recovery.json", ops, clock=lambda: 1000)
    return controller, calls, status


def test_outage_advances_without_snapshot(tmp_path):
    controller, calls, _ = make_controller(tmp_path)
    controller.tick(1000)
    controller.tick(1031)
    assert calls == ["repair", "uu"]
    controller.tick(1092)
    assert calls[2:4] == ["proxy-off", "repair"]
    assert calls[4][0] == "restore"
    assert calls[5] == "uu"


def test_healthy_primary_still_maintains_standby_without_escalating(tmp_path):
    controller, calls, _ = make_controller(tmp_path, online=True, uu=True)
    controller.ops.standby_repair = lambda: calls.append("standby")
    controller.tick(1000, allow_reboot=True)
    assert calls == ["standby"]
    assert controller.state["outage_at"] is None
    assert controller.state["stage"] == "healthy"


def test_proxy_restored_when_repair_raises(tmp_path):
    controller, calls, _ = make_controller(tmp_path, repair_error=True)
    controller.tick(1000)
    controller.tick(1031)
    with pytest.raises(RuntimeError):
        controller.tick(1092)
    assert calls[-1][0] == "restore"
    assert controller.state.get("proxy_before") is None


def test_proxy_journal_recovered_after_crash(tmp_path):
    controller, calls, _ = make_controller(tmp_path)
    controller.state["proxy_before"] = {"ProxyEnable": 0}
    controller.save()
    recovered = RecoveryController(controller.path, controller.ops)
    recovered.recover_proxy()
    assert calls == [("restore", {"ProxyEnable": 0})]


def test_reboot_persisted_and_rate_limited(tmp_path):
    controller, calls, _ = make_controller(tmp_path)
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(1901, allow_reboot=True)
    assert calls[-1] == "reboot"
    restarted = RecoveryController(controller.path, controller.ops)
    restarted.tick(2000, allow_reboot=True)
    restarted.tick(3000, allow_reboot=True)
    assert calls.count("reboot") == 1


def test_healthy_network_with_uu_missing_recovers_uu(tmp_path):
    controller, calls, status = make_controller(tmp_path, online=True)
    controller.tick(1000)
    controller.tick(1031)
    assert calls == ["repair", "uu"]
    status["uu"] = True
    controller.tick(1040)
    assert controller.state["stage"] == "healthy"


def test_recovery_cancels_pending_reboot(tmp_path):
    controller, calls, status = make_controller(tmp_path)
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(1901, allow_reboot=True)
    status.update(online=True, uu=True)
    controller.tick(1910, allow_reboot=True)
    assert calls[-1] == "cancel-reboot"


def test_reboot_requires_enabled_and_startup_readiness(tmp_path):
    controller, calls, _ = make_controller(tmp_path)
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(2000)
    assert "reboot" not in calls
    controller.ops.reboot_ready = lambda: False
    repairs = calls.count("repair")
    controller.tick(2301, allow_reboot=True)
    assert "reboot" not in calls
    assert calls.count("repair") == repairs + 1


def test_first_layer_uses_network_only_action_when_supplied(tmp_path):
    calls = []
    controller, _, _ = make_controller(tmp_path)
    controller.ops.network_repair = lambda: calls.append("network-only") or (False, "wifi retry")
    controller.tick(1000)
    controller.tick(1031)
    assert calls == ["network-only"]


def test_migration_prefers_existing_stable_config(tmp_path):
    from campus_install import migrate_config
    source = tmp_path / "old.json"
    target = tmp_path / "data" / "tray_config.json"
    source.write_text('{"remote_restart_enabled":true}', encoding="utf-8")
    migrate_config(target, [source])
    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(source.read_text(encoding="utf-8"))
    source.write_text('{}', encoding="utf-8")
    migrate_config(target, [source])
    assert "remote_restart_enabled" in target.read_text(encoding="utf-8")


def test_download_rejects_wrong_digest(tmp_path):
    from campus_update import download_update, UpdateInfo

    class Response:
        status_code = 200
        headers = {}
        text = "0" * 64 + "  CampusReconnect.exe"
        def iter_content(self, chunk_size):
            yield b"MZ" + b"x" * 200
        def close(self):
            pass

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    info = UpdateInfo("v99.0.0", "test", "https://github.com/lcq666666/SNNU-school-network-reconnect-auto/releases/tag/v99.0.0")
    with pytest.raises(ValueError, match="SHA256"):
        download_update(Session(), info, tmp_path)
    assert not list(Path(tmp_path).glob("*.exe"))


def test_tray_recovery_continues_when_snapshot_is_untrusted(monkeypatch):
    import logging
    import campus_reconnect_tray as tray
    import campus_resilience_windows as native
    calls = []
    cfg = dict(tray.DEFAULT_CONFIG, lifeline_enabled=True)
    app = tray.TrayApp(logging.getLogger("campus"), config=cfg)
    monkeypatch.setattr(native, "set_awake", lambda required: calls.append(("awake", required)))
    app._observe_snapshot = lambda: calls.append("untrusted-snapshot")
    app._lifeline_tick = lambda: calls.append("lifeline")
    def forbidden(*args):
        raise AssertionError("The old probe must not block the independent recovery loop")
    monkeypatch.setattr(tray, "internet_ok", forbidden)
    app._probe_tick(0)
    assert calls == [("awake", True), "untrusted-snapshot", "lifeline"]
