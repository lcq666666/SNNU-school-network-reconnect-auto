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


def test_power_guard_saves_old_values_and_restores(monkeypatch, tmp_path):
    import campus_windows_guard as guard
    monkeypatch.setattr(guard.os, "name", "nt")
    runner = Runner()
    backup = tmp_path / "power.json"
    result = configure_power(backup_path=backup, run=runner)
    assert result.power and result.changed
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert saved["values"] == {"standby_ac": 30, "standby_dc": 15,
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
