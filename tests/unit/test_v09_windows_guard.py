"""Offline tests for reversible startup and power protection."""
import json

from campus_windows_guard import configure_power, configure_startup_guard, restore_power


POWER = """
Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44e7  (STANDBYIDLE)
  Current AC Power Setting Index: 0x0000001e
  Current DC Power Setting Index: 0x0000000f
Power Setting GUID: 9d7815a6-7ee4-497e-8888-515a05f02364  (HIBERNATEIDLE)
  Current AC Power Setting Index: 0x0000003c
  Current DC Power Setting Index: 0x0000001e
"""


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[1] == "/getactivescheme":
            return type("R", (), {"returncode": 0,
                                   "stdout": "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e"})()
        if args[1] == "/query":
            return type("R", (), {"returncode": 0, "stdout": POWER})()
        return type("R", (), {"returncode": 0, "stdout": ""})()


class StatefulRunner:
    def __init__(self, *, scheme="381b4222-f694-41f0-9685-ff5bb260df2e",
                 values=None, fail_change=None, fail_restore=False):
        self.scheme = scheme
        self.values = values or {"standby_ac": 30, "standby_dc": 15,
                                 "hibernate_ac": 60, "hibernate_dc": 30}
        self.fail_change = fail_change
        self.fail_restore = fail_restore
        self.calls = []

    @staticmethod
    def result(code=0, stdout=""):
        return type("R", (), {"returncode": code, "stdout": stdout})()

    def __call__(self, args):
        self.calls.append(args)
        if args[1] == "/getactivescheme":
            return self.result(stdout="Power Scheme GUID: " + self.scheme)
        if args[1] == "/query":
            standby = """Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44e7  (STANDBYIDLE)
  Current AC Power Setting Index: 0x%08x
  Current DC Power Setting Index: 0x%08x
""" % (self.values["standby_ac"], self.values["standby_dc"])
            hibernate = """Power Setting GUID: 9d7815a6-7ee4-497e-8888-515a05f02364  (HIBERNATEIDLE)
  Current AC Power Setting Index: 0x%08x
  Current DC Power Setting Index: 0x%08x
""" % (self.values["hibernate_ac"], self.values["hibernate_dc"])
            return self.result(stdout=standby + hibernate)
        if args[1] == "/change":
            option = args[2]
            if option == self.fail_change:
                return self.result(code=1)
            setting, _, scope = option.partition("-timeout-")
            self.values[setting + "_" + scope] = int(args[3])
        elif args[1] in ("/setacvalueindex", "/setdcvalueindex"):
            if self.fail_restore:
                return self.result(code=1)
            scope = "ac" if args[1] == "/setacvalueindex" else "dc"
            setting = "standby" if args[4] == "STANDBYIDLE" else "hibernate"
            self.values[setting + "_" + scope] = int(args[5])
        return self.result()


def test_power_guard_saves_old_values_and_restores(monkeypatch, tmp_path):
    import campus_windows_guard as guard
    monkeypatch.setattr(guard.os, "name", "nt")
    runner = Runner()
    backup = tmp_path / "power.json"
    result = configure_power(backup_path=backup, run=runner)
    assert result.power and result.changed
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert saved["plans"]["381b4222-f694-41f0-9685-ff5bb260df2e"] == {
        "standby_ac": 30, "standby_dc": 15,
        "hibernate_ac": 60, "hibernate_dc": 30}
    assert restore_power(backup_path=backup, run=runner)
    assert not backup.exists()
    assert any(call[1] == "/setactive" for call in runner.calls)


def test_startup_guard_is_idempotent_and_does_not_need_password(monkeypatch, tmp_path):
    import campus_windows_guard as guard
    monkeypatch.setattr(guard.os, "name", "nt")
    calls = []
    runner = Runner()
    result = configure_startup_guard(
        get_autostart=lambda: False,
        set_autostart=lambda value: calls.append(value) or True,
        backup_path=tmp_path / "power.json", run=runner)
    assert result.autostart and result.power
    assert calls == [True]
    assert all("password" not in str(call).lower() for call in runner.calls)


def test_existing_backup_does_not_hide_reenabled_sleep_timeouts(monkeypatch, tmp_path):
    import campus_windows_guard as guard
    monkeypatch.setattr(guard.os, "name", "nt")
    scheme = "381b4222-f694-41f0-9685-ff5bb260df2e"
    original = {"standby_ac": 30, "standby_dc": 15,
                "hibernate_ac": 60, "hibernate_dc": 30}
    backup = tmp_path / "power.json"
    backup.write_text(json.dumps({"scheme": scheme, "values": original}),
                      encoding="utf-8")
    runner = StatefulRunner()

    result = configure_power(backup_path=backup, run=runner)

    assert result.power and result.changed
    assert all(value == 0 for value in runner.values.values())
    assert json.loads(backup.read_text(encoding="utf-8"))["plans"][scheme] == original


def test_new_active_plan_is_snapshotted_and_reapplied_without_losing_user_choice(
        monkeypatch, tmp_path):
    import campus_windows_guard as guard
    monkeypatch.setattr(guard.os, "name", "nt")
    old_scheme = "381b4222-f694-41f0-9685-ff5bb260df2e"
    new_scheme = "a1841308-3541-4fab-bc81-f71556f20b4a"
    old_values = {"standby_ac": 30, "standby_dc": 15,
                  "hibernate_ac": 60, "hibernate_dc": 30}
    new_values = {"standby_ac": 45, "standby_dc": 20,
                  "hibernate_ac": 90, "hibernate_dc": 45}
    backup = tmp_path / "power.json"
    backup.write_text(json.dumps({"scheme": old_scheme, "values": old_values}),
                      encoding="utf-8")
    runner = StatefulRunner(scheme=new_scheme, values=dict(new_values))

    result = configure_power(backup_path=backup, run=runner)
    saved = json.loads(backup.read_text(encoding="utf-8"))

    assert result.power and result.changed
    assert saved["active_scheme"] == old_scheme
    assert saved["plans"] == {old_scheme: old_values, new_scheme: new_values}
    assert all(value == 0 for value in runner.values.values())
    assert restore_power(backup_path=backup, run=runner)
    assert runner.scheme == new_scheme
    assert runner.values == new_values
    assert not backup.exists()


def test_partial_power_change_failure_rolls_back_and_removes_new_backup(
        monkeypatch, tmp_path):
    import campus_windows_guard as guard
    monkeypatch.setattr(guard.os, "name", "nt")
    backup = tmp_path / "power.json"
    runner = StatefulRunner(fail_change="hibernate-timeout-ac")
    original = dict(runner.values)

    result = configure_power(backup_path=backup, run=runner)

    assert not result.power
    assert not result.changed
    assert "已回滚" in result.detail
    assert runner.values == original
    assert not backup.exists()
    assert any(call[1] == "/setacvalueindex" for call in runner.calls)


def test_failed_power_rollback_keeps_recovery_journal(monkeypatch, tmp_path):
    import campus_windows_guard as guard
    monkeypatch.setattr(guard.os, "name", "nt")
    backup = tmp_path / "power.json"
    runner = StatefulRunner(fail_change="hibernate-timeout-ac", fail_restore=True)

    result = configure_power(backup_path=backup, run=runner)

    assert not result.power and result.changed
    assert "恢复记录已保留" in result.detail
    journal = json.loads(backup.read_text(encoding="utf-8"))
    assert journal["plans"][runner.scheme] == {
        "standby_ac": 30, "standby_dc": 15,
        "hibernate_ac": 60, "hibernate_dc": 30}
