"""V0.11 recovery timing and config budget contracts."""
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from campus_resilience import RecoveryController, RecoveryOps, RecoveryPolicy
import campus_reconnect_tray as tray
from campus_core import PortalStatus, RepairResult


def make_controller(tmp_path, *, online=False, uu=False):
    calls = []
    status = {"online": online, "uu": uu}

    def repair():
        calls.append("repair")
        return False, "network unavailable"

    ops = RecoveryOps(
        network_ok=lambda: status["online"],
        uu_ok=lambda: status["uu"],
        restart_uu=lambda: calls.append("uu") or True,
        proxy_snapshot=lambda: {"ProxyEnable": 1},
        proxy_disable=lambda: calls.append("proxy-off"),
        proxy_restore=lambda value: calls.append(("restore", value)),
        repair=repair,
        reboot_ready=lambda: True,
        reboot=lambda: calls.append("reboot"),
        cancel_reboot=lambda: calls.append("cancel-reboot"),
    )
    controller = RecoveryController(tmp_path / "recovery.json", ops,
                                    clock=lambda: 1000)
    return controller, calls, status


def test_portal_result_blocks_computer_reboot_until_authentication_is_resolved():
    for result in (
        RepairResult(auth_ok=True, network_ok=False,
                     status=PortalStatus.AUTHENTICATED_DEGRADED),
        RepairResult(auth_ok=False, network_ok=False,
                     status=PortalStatus.NEEDS_USER,
                     error_code="E_AUTH_CAPTCHA"),
        RepairResult(auth_ok=False, network_ok=False,
                     status=PortalStatus.NEEDS_USER,
                     error_code="E_AUTH_REJECTED"),
    ):
        assert tray._portal_result_blocks_computer_reboot(result)

    assert not tray._portal_result_blocks_computer_reboot(
        RepairResult(auth_ok=False, network_ok=False,
                     status=PortalStatus.AUTH_REQUIRED,
                     error_code="E_PORTAL_UNREACHABLE"))


def test_production_policy_starts_auth_after_confirmed_failure(tmp_path):
    calls = []
    status = {"network": False, "uu": False}

    def repair():
        calls.append("auth")
        status["network"] = True
        return True, "authenticated"

    ops = RecoveryOps(
        network_ok=lambda: status["network"], uu_ok=lambda: status["uu"],
        restart_uu=lambda: calls.append("uu") or True,
        proxy_snapshot=lambda: {}, proxy_disable=lambda: None,
        proxy_restore=lambda value: None, repair=repair,
        reboot_ready=lambda: False, reboot=lambda: None,
        cancel_reboot=lambda: None,
        network_repair=lambda: calls.append("wifi") or (False, "still offline"),
    )
    policy = RecoveryPolicy(confirmation_delay=30, network_repair_delay=15,
                            repair_retry_delay=300, uu_observe_delay=10,
                            uu_restart_delay=15, uu_confirmation_delay=30)
    controller = RecoveryController(tmp_path / "recovery.json", ops,
                                    clock=lambda: 1000, policy=policy)
    controller.tick(1000)
    controller.tick(1029)
    assert calls == []
    controller.tick(1030)
    assert calls == ["wifi"]
    controller.tick(1045)
    assert calls == ["wifi", "auth"]


def test_final_portal_repair_precedes_computer_restart_decision(tmp_path):
    controller, calls, _status = make_controller(tmp_path)

    def ready():
        assert "repair" in calls
        return True

    controller.ops.reboot_ready = ready
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(1901, allow_reboot=True)

    assert calls[-1] == "reboot"


def test_computer_restart_waits_when_portal_requires_user_action(tmp_path):
    controller, calls, _status = make_controller(tmp_path)
    controller.ops.reboot_ready = lambda: False
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(1901, allow_reboot=True)

    assert calls[-1][0] == "restore"
    assert "reboot" not in calls
    assert controller.state["reboot_pending"] is False


def test_final_portal_repair_recovery_cancels_computer_restart(tmp_path):
    controller, calls, status = make_controller(tmp_path)
    normal_repair = controller.ops.repair

    def recover_on_final_attempt():
        result = normal_repair()
        if calls.count("repair") == 2:
            status["online"] = True
        return result

    controller.ops.repair = recover_on_final_attempt
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(1901, allow_reboot=True)

    assert status["online"] is True
    assert "reboot" not in calls
    assert controller.state["reboot_pending"] is False


def test_computer_restart_waits_when_final_network_probe_is_unreliable(tmp_path):
    controller, calls, _status = make_controller(tmp_path)
    original_probe = controller.ops.network_ok
    probe_count = 0

    def probe():
        nonlocal probe_count
        probe_count += 1
        if probe_count == 5:
            return None
        return original_probe()

    controller.ops.network_ok = probe
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(1901, allow_reboot=True)

    assert "reboot" not in calls
    assert controller.state["reboot_pending"] is False


def test_network_recovery_cancels_pending_restart_while_uu_is_offline(tmp_path):
    controller, calls, status = make_controller(tmp_path)
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(1901, allow_reboot=True)
    assert controller.state["reboot_pending"] is True

    status.update(online=True, uu=False)
    controller.tick(1902, allow_reboot=True)

    assert calls[-1] == "cancel-reboot"
    assert controller.state["reboot_pending"] is False


def test_unreliable_network_probe_cancels_pending_computer_restart(tmp_path):
    controller, calls, status = make_controller(tmp_path)
    controller.tick(1000)
    controller.tick(1031)
    controller.tick(1092)
    controller.tick(1901, allow_reboot=True)
    assert controller.state["reboot_pending"] is True

    status["online"] = None
    controller.tick(1902, allow_reboot=True)

    assert calls[-1] == "cancel-reboot"
    assert controller.state["reboot_pending"] is False


def test_recovery_config_is_bounded_and_defaults_to_three_captcha_attempts():
    values = tray._read_recovery_keys('{"recovery_probe_interval":1,'
                                      '"recovery_failure_threshold":99,'
                                      '"captcha_max_attempts":99,'
                                      '"watchdog_stale_after":5}')
    assert values["recovery_probe_interval"] == 5
    assert values["recovery_failure_threshold"] == 10
    assert values["captcha_max_attempts"] == 3
    assert values["watchdog_stale_after"] == 30


def test_unlock_cancels_pending_reboot_before_slow_network_probe(tmp_path):
    controller, calls, _ = make_controller(tmp_path)
    controller.state["reboot_pending"] = True
    controller.ops.reboot_ready = lambda: False

    def probe():
        assert calls == ["cancel-reboot"]
        return None

    controller.ops.network_ok = probe
    controller.tick(1000, allow_reboot=True)
    assert not controller.state["reboot_pending"]


def test_successful_repair_disagrees_with_probe_and_blocks_reboot(tmp_path):
    controller, calls, _ = make_controller(tmp_path)
    controller.state.update(stage="network_repair", outage_at=1000)
    controller.ops.repair = lambda: (True, "公网已连通")
    controller.tick(1901, allow_reboot=True)
    assert not controller.state["reboot_pending"]
    assert "reboot" not in calls


@pytest.mark.parametrize("wifi_online,system_online,expected", [
    (True, True, True), (True, False, None), (None, False, None),
])
def test_physical_wifi_success_alone_does_not_prove_system(
        wifi_online, system_online, expected, monkeypatch):
    from campus_reconnect import PathVerificationResult

    ethernet = SimpleNamespace(role="ethernet")
    wifi = SimpleNamespace(role="wifi") if wifi_online else None
    results = [PathVerificationResult(False, reliability="PROBE_UNRELIABLE")]
    if wifi is not None:
        results.append(PathVerificationResult(True))
    monkeypatch.setattr(tray, "direct_session", lambda: nullcontext(object()))
    monkeypatch.setattr(tray, "verify_internet", lambda *args, **kwargs: (system_online, "test"))
    app = SimpleNamespace(
        _build_path_verifier=lambda: SimpleNamespace(verify=lambda request: results.pop(0)),
        _automatic_setup_armed=False,
        log=SimpleNamespace(info=lambda *args: None),
    )

    assert tray.TrayApp._verify_lifeline_paths(app, ethernet, wifi) is expected
    assert not results


def test_working_system_route_can_monitor_uu_without_authorizing_wifi_repair(monkeypatch):
    from campus_reconnect import PathVerificationResult

    monkeypatch.setattr(tray, "direct_session", lambda: nullcontext(object()))
    monkeypatch.setattr(tray, "verify_internet", lambda *args, **kwargs: (True, "system route"))
    app = SimpleNamespace(
        _build_path_verifier=lambda: SimpleNamespace(
            verify=lambda request: PathVerificationResult(False)),
        _automatic_setup_armed=False,
        log=SimpleNamespace(info=lambda *args: None),
    )
    assert tray.TrayApp._verify_lifeline_paths(app, SimpleNamespace(role="wifi"), None) is True


@pytest.mark.parametrize("online,expected", [(True, True), (False, None)])
def test_system_route_is_checked_even_without_physical_requests(monkeypatch, online, expected):
    monkeypatch.setattr(tray, "direct_session", lambda: nullcontext(object()))
    monkeypatch.setattr(tray, "verify_internet", lambda *a, **kw: (online, "system"))
    app = SimpleNamespace(_automatic_setup_armed=False)
    assert tray.TrayApp._verify_lifeline_paths(app, None, None) is expected


def test_first_network_failure_after_uu_only_fault_waits_for_confirmation(tmp_path):
    controller, calls, status = make_controller(tmp_path, online=True, uu=False)
    controller.tick(1000)
    status["online"] = False
    controller.tick(2000)
    assert controller.state["outage_at"] == 2000
    assert calls == []


def test_unknown_observation_restarts_network_confirmation(tmp_path):
    controller, calls, status = make_controller(tmp_path)
    controller.tick(1000)
    status["online"] = None
    controller.tick(1009)
    status["online"] = False
    controller.tick(1030)
    assert controller.state["outage_at"] == 1030
    assert calls == []


def test_direct_portal_repair_leaves_system_proxy_untouched(tmp_path):
    controller, calls, _ = make_controller(tmp_path)
    controller.policy = replace(controller.policy, manage_system_proxy=False)
    controller.state.update(stage="network_repair", outage_at=1000)
    controller.tick(1100)
    assert calls == ["repair"]


@pytest.mark.parametrize("unlock,network,expected", [
    (True, False, False), (False, True, False), (False, None, False), (False, False, True),
])
def test_reboot_countdown_stays_in_app_until_fresh_final_check(
        tmp_path, unlock, network, expected):
    controller, calls, status = make_controller(tmp_path)
    controller.policy = replace(controller.policy, reboot_countdown=60)
    controller.state.update(stage="network_repair", outage_at=1000)
    controller.tick(1901, allow_reboot=True)
    controller.tick(1959, allow_reboot=True)
    assert "reboot" not in calls
    assert controller.state["reboot_pending"] is False

    def final_probe():
        if unlock:
            controller.ops.reboot_ready = lambda: False
        return network

    controller.ops.network_ok = final_probe
    status["uu"] = True
    controller.tick(1961, allow_reboot=True)
    assert ("reboot" in calls) is expected
    assert "cancel-reboot" not in calls
