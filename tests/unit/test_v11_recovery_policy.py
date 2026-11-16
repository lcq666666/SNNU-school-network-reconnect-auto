"""V0.11 recovery timing and config budget contracts."""
from campus_resilience import RecoveryController, RecoveryOps, RecoveryPolicy
import campus_reconnect_tray as tray


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


def test_recovery_config_is_bounded_and_defaults_to_three_captcha_attempts():
    values = tray._read_recovery_keys('{"recovery_probe_interval":1,'
                                      '"recovery_failure_threshold":99,'
                                      '"captcha_max_attempts":99,'
                                      '"watchdog_stale_after":5}')
    assert values["recovery_probe_interval"] == 5
    assert values["recovery_failure_threshold"] == 10
    assert values["captcha_max_attempts"] == 3
    assert values["watchdog_stale_after"] == 30
