"""V0.11 independent Task Scheduler guard and heartbeat contracts."""
from pathlib import Path

import campus_task_guard as guard


def test_heartbeat_is_atomic_and_stale_is_detected(tmp_path):
    path = tmp_path / "heartbeat.json"
    guard.write_heartbeat(path, active=True, pid=123, version="0.11.0", now=100.0)
    record = guard.read_heartbeat(path)
    assert record["active"] is True
    assert record["pid"] == 123
    assert record["boot_id"] == guard.current_boot_id()
    assert guard.heartbeat_age(record, now=140.0) == 40.0
    assert guard.heartbeat_age(record, now=146.0) == 46.0


def test_supervisor_launches_once_then_respects_window_limit(tmp_path):
    calls = []
    clock = [100.0]
    guard.write_heartbeat(tmp_path / guard.HEARTBEAT_FILE, active=True, now=0.0)
    supervisor = guard.GuardSupervisor(
        lambda: calls.append("launch"),
        heartbeat=tmp_path / guard.HEARTBEAT_FILE,
        clock=lambda: clock[0], stale_after=10, max_restarts=2,
        restart_window=60)
    assert supervisor.check_once().action == "launched"
    assert supervisor.check_once().action == "launched"
    assert supervisor.check_once().action == "restart_limited"
    assert calls == ["launch", "launch"]


def test_clean_stop_does_not_restart_user_quit(tmp_path):
    path = tmp_path / guard.HEARTBEAT_FILE
    guard.write_heartbeat(path, active=False, now=100.0)
    supervisor = guard.GuardSupervisor(lambda: (_ for _ in ()).throw(AssertionError()),
                                       heartbeat=path, clock=lambda: 1000.0)
    assert supervisor.check_once().action == "stopped"


def test_clean_stop_from_previous_boot_starts_on_next_login(tmp_path):
    path = tmp_path / guard.HEARTBEAT_FILE
    guard.write_heartbeat(path, active=False, now=100.0, boot_id=1)
    calls = []
    supervisor = guard.GuardSupervisor(lambda: calls.append("launch"),
                                       heartbeat=path, clock=lambda: 1000.0,
                                       boot_id=2)
    assert supervisor.check_once().action == "launched"
    assert calls == ["launch"]


def test_commands_are_shell_safe_and_use_guard_switch(tmp_path):
    executable = Path("C:/Program Files/CampusReconnect/CampusReconnect.exe")
    command = guard.build_guard_command(executable, frozen=True)
    assert command == [str(executable), "--guard"]
    main = guard.build_main_command(executable, frozen=True)
    assert main == [str(executable), "--background"]


def test_task_registration_is_per_user_and_reports_failure(monkeypatch):
    from xml.etree import ElementTree

    calls = []

    class Result:
        returncode = 0
        stdout = '"TEST\\user","S-1-5-21-42"'

    monkeypatch.setattr(guard.os, "name", "nt")

    def run(args):
        calls.append(args)
        if "/XML" in args:
            root = ElementTree.parse(args[args.index("/XML") + 1]).getroot()
            assert root.findtext(".//{*}RunLevel") == "LeastPrivilege"
            assert root.findtext(".//{*}LogonType") == "InteractiveToken"
            assert root.findtext(".//{*}ExecutionTimeLimit") == "PT0S"
            assert root.findtext(".//{*}LogonTrigger/{*}UserId") == "S-1-5-21-42"
            assert root.findtext(".//{*}Arguments") == "--guard"
        return Result()

    result = guard.register_task(
        Path("C:/CampusReconnect.exe"), run=run,
        frozen=True)
    assert result.ok and result.action == "registered"
    assert calls[0][0] == "whoami.exe"
    assert calls[1][:4] == ["schtasks.exe", "/Create", "/TN", guard.TASK_NAME]
    assert "/XML" in calls[1]
    assert calls[2][:4] == ["schtasks.exe", "/Query", "/TN", guard.TASK_NAME]


def test_stale_supervisor_waits_if_previous_instance_cannot_be_replaced(tmp_path):
    path = tmp_path / guard.HEARTBEAT_FILE
    calls = []
    guard.write_heartbeat(path, active=True, pid=123, now=1)
    supervisor = guard.GuardSupervisor(
        lambda: calls.append("launch"), heartbeat=path, clock=lambda: 100,
        retire=lambda record: calls.append(record["pid"]) or False)
    assert supervisor.check_once().action == "previous_instance_unconfirmed"
    assert calls == [123]
