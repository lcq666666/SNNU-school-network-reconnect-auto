"""启动路径回归：互斥体冲突时不接触凭据，手动启动与自启分开。"""
from unittest.mock import Mock, call

import pytest

import campus_reconnect_tray as tray
from campus_core import E_MUTEX_FAILED, SingleInstanceResult


@pytest.mark.parametrize(("result", "message"), [
    (SingleInstanceResult(already_running=True), "先退出旧版"),
    (SingleInstanceResult(error_code=E_MUTEX_FAILED), "无法启用单实例保护"),
])
def test_manual_start_explains_why_it_was_blocked(monkeypatch, result, message):
    monkeypatch.setattr(tray, "setup_logging", Mock())
    monkeypatch.setattr(tray.engine, "claim_single_instance", Mock(return_value=result))
    dialog = Mock()
    credentials = Mock()
    monkeypatch.setattr(tray, "_show_startup_error", dialog)
    monkeypatch.setattr(tray, "load_credentials", credentials)

    assert tray.main([]) == 1
    assert message in dialog.call_args.args[0]
    credentials.assert_not_called()


def test_background_duplicate_exits_without_dialog(monkeypatch):
    monkeypatch.setattr(tray, "setup_logging", Mock())
    monkeypatch.setattr(tray.engine, "claim_single_instance", Mock(
        return_value=SingleInstanceResult(already_running=True)))
    dialog = Mock()
    monkeypatch.setattr(tray, "_show_startup_error", dialog)

    assert tray.main(["--background"]) == 1
    dialog.assert_not_called()


@pytest.mark.parametrize(("args", "open_dashboard"), [([], True), (["--background"], False)])
@pytest.mark.parametrize("packaged", [False, True])
def test_manual_start_queues_dashboard_after_starting_tray(monkeypatch, args, open_dashboard, packaged):
    import campus_resilience_windows as native
    import campus_proxy_ui
    onboarding = Mock()
    monkeypatch.setattr(campus_proxy_ui, "offer_onboarding", onboarding)
    monkeypatch.setattr(tray.sys, "frozen", packaged, raising=False)
    monkeypatch.setattr(tray, "get_autostart_command", lambda: '"C:/old/CampusReconnect.exe" --background')
    monkeypatch.setattr(tray, "autostart_matches_current", lambda: False)
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "migrate_config", Mock())
    monkeypatch.setattr(tray, "set_autostart", Mock())
    monkeypatch.setattr(native, "recover_pending_proxy", Mock())
    monkeypatch.setattr(tray, "setup_logging", Mock())
    monkeypatch.setattr(tray.engine, "claim_single_instance", Mock(
        return_value=SingleInstanceResult(ok=True)))
    monkeypatch.setattr(tray, "load_credentials", Mock(return_value=Mock(error_code=None)))
    monkeypatch.setattr(tray, "get_autostart", lambda: False)
    monkeypatch.setattr(tray.tk, "Tk", Mock())
    monkeypatch.setattr(tray.threading, "Thread", Mock())
    app = Mock(config={"remote_guard": False})
    monkeypatch.setattr(tray, "TrayApp", Mock(return_value=app))

    assert tray.main(args) == 0
    assert tray.migrate_config.call_count == int(packaged)
    assert tray.set_autostart.call_count == int(packaged)
    app.start.assert_called_once_with()
    assert app.on_dashboard.call_count == int(open_dashboard)
    assert onboarding.call_count == int(packaged)
    if open_dashboard:
        assert app.method_calls.index(call.on_dashboard()) > app.method_calls.index(call.start())
