# -*- coding: utf-8 -*-
"""F21 启动与自启入口锁定（对应 T33）。

覆盖：源码模式使用当前解释器（不优先相邻 exe）、打包模式仅当前 exe、
自启状态**核对命令内容**、外部旧路径先确认再接管、``set_autostart`` 写当前命令、
``run.bat`` / ``run_tray.bat`` 不再硬编码解释器路径并在启动前验证依赖。
全程假注册表，零真实注册表写入。
"""
from __future__ import annotations

from pathlib import Path

import campus_reconnect_tray as tray


# --------------------------------------------------------------------------- #
# 假件
# --------------------------------------------------------------------------- #
class _Key:
    def __init__(self, reg):
        self._reg = reg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, command=None):
        self.command = command
        self.deleted = 0

    def OpenKey(self, root, path, reserved=0, access=0):
        return _Key(self)

    def QueryValueEx(self, key, name):
        if self.command is None:
            raise FileNotFoundError(name)
        return self.command, self.REG_SZ

    def SetValueEx(self, key, name, reserved, type_, value):
        self.command = value

    def DeleteValue(self, key, name):
        self.command = None
        self.deleted += 1


class _FakeSys:
    def __init__(self, frozen, executable):
        self.frozen = frozen
        self.executable = executable


class _FakeApp:
    def __init__(self):
        self.icon = None
        self.notes = []

    def notify(self, title, body=""):
        self.notes.append((title, body))


# --------------------------------------------------------------------------- #
# launcher_command（T33）
# --------------------------------------------------------------------------- #
def test_t33_source_mode_uses_current_interpreter(monkeypatch):
    monkeypatch.setattr(tray, "sys", _FakeSys(False, "C:/proj/.venv/Scripts/python.exe"))
    monkeypatch.setattr(tray, "PYTHONW", "C:/proj/.venv/Scripts/pythonw.exe")
    monkeypatch.setattr(tray, "SCRIPT_DIR", Path("C:/proj"))
    cmd = tray.launcher_command()
    assert "pythonw.exe" in cmd
    assert "campus_reconnect_tray.py" in cmd
    assert "CampusReconnect.exe" not in cmd     # 源码模式不优先相邻 exe


def test_t33_frozen_mode_uses_current_exe(monkeypatch):
    exe = "C:/apps/CampusReconnect/CampusReconnect.exe"
    monkeypatch.setattr(tray, "sys", _FakeSys(True, exe))
    assert tray.launcher_command() == '"%s"' % Path(exe).resolve()


# --------------------------------------------------------------------------- #
# 自启状态核对命令内容（T33）
# --------------------------------------------------------------------------- #
def test_t33_autostart_matches_only_current(monkeypatch):
    reg = FakeWinreg(command='"C:/old/pythonw.exe" "C:/old/campus_reconnect_tray.py"')
    monkeypatch.setattr(tray, "winreg", reg)
    monkeypatch.setattr(tray, "launcher_command",
                        lambda: '"C:/new/pythonw.exe" "C:/new/campus_reconnect_tray.py"')
    assert tray.get_autostart() is False
    assert tray.autostart_foreign_command() == reg.command
    reg.command = tray.launcher_command()
    assert tray.get_autostart() is True
    assert tray.autostart_foreign_command() is None


def test_t33_autostart_missing_is_false(monkeypatch):
    monkeypatch.setattr(tray, "winreg", FakeWinreg(command=None))
    monkeypatch.setattr(tray, "launcher_command", lambda: "CURRENT")
    assert tray.get_autostart() is False
    assert tray.autostart_foreign_command() is None


def test_t33_autostart_normalizes_whitespace(monkeypatch):
    reg = FakeWinreg(command='"C:/a/pythonw.exe"   "C:/a/campus_reconnect_tray.py"')
    monkeypatch.setattr(tray, "winreg", reg)
    monkeypatch.setattr(tray, "launcher_command",
                        lambda: '"C:/a/pythonw.exe" "C:/a/campus_reconnect_tray.py"')
    assert tray.get_autostart() is True


def test_t33_set_autostart_writes_current_command(monkeypatch):
    reg = FakeWinreg()
    monkeypatch.setattr(tray, "winreg", reg)
    monkeypatch.setattr(tray, "launcher_command", lambda: "CURRENT")
    assert tray.set_autostart(True) is True
    assert reg.command == "CURRENT"
    assert tray.set_autostart(False) is True
    assert reg.command is None


def test_t33_toggle_autostart_foreign_requires_confirmation(monkeypatch):
    reg = FakeWinreg(command="OLD")
    monkeypatch.setattr(tray, "winreg", reg)
    monkeypatch.setattr(tray, "launcher_command", lambda: "CURRENT")
    app = _FakeApp()

    app._ui_ask = lambda *a, **k: False          # 用户拒绝接管
    tray.TrayApp.on_toggle_autostart(app)
    assert reg.command == "OLD", "未经确认就覆盖了外部自启项"

    app._ui_ask = lambda *a, **k: True           # 用户同意
    tray.TrayApp.on_toggle_autostart(app)
    assert reg.command == "CURRENT"


def test_t33_toggle_autostart_plain_toggle(monkeypatch):
    reg = FakeWinreg(command=None)
    monkeypatch.setattr(tray, "winreg", reg)
    monkeypatch.setattr(tray, "launcher_command", lambda: "CURRENT")
    app = _FakeApp()
    app._ui_ask = lambda *a, **k: True
    tray.TrayApp.on_toggle_autostart(app)
    assert reg.command == "CURRENT"


# --------------------------------------------------------------------------- #
# 启动脚本（T33）
# --------------------------------------------------------------------------- #
def test_t33_run_bat_uses_detected_interpreter(project_root):
    text = (project_root / "run.bat").read_text(encoding="utf-8")
    assert "Python314" not in text, "仍硬编码 Python314 路径"
    assert ".venv" in text
    assert "requests" in text          # 启动前验证依赖


def test_t33_run_tray_bat_no_hardcoded_python(project_root):
    text = (project_root / "run_tray.bat").read_text(encoding="utf-8")
    assert "Python314" not in text, "仍硬编码 Python314 路径"
    assert ".venv" in text
    assert "CampusReconnect.exe" in text
    assert "pystray" in text           # 托盘依赖校验
