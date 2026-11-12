"""Regression tests for updater preflight and tray default-click behavior."""
from pathlib import Path

import subprocess

import pytest

import campus_update_install as installer
from campus_update_install import UPDATE_SCRIPT
from campus_reconnect_tray import _load_check


def test_updater_preflights_staged_exe_before_replacing_installed_exe():
    startup_check = UPDATE_SCRIPT.index("--load-check")
    replace_exe = UPDATE_SCRIPT.index("[IO.File]::Replace($incoming, $target, $backup)")
    assert startup_check < replace_exe
    assert "Staged update startup check failed" in UPDATE_SCRIPT
    assert "$startupReport" in UPDATE_SCRIPT


def test_updater_reports_preflight_failure_without_replacing_target():
    startup_check = UPDATE_SCRIPT.index("--load-check")
    replace_exe = UPDATE_SCRIPT.index("[IO.File]::Replace($incoming, $target, $backup)")
    preflight = UPDATE_SCRIPT[startup_check:replace_exe]
    assert "if ($check.ExitCode -ne 0 -or" in preflight
    assert "throw 'Staged update startup check failed'" in preflight


def test_staged_executable_preflight_accepts_loadable_binary(tmp_path, monkeypatch):
    def run(command, **kwargs):
        assert command[1] == "--load-check"
        assert kwargs["timeout"] == 30
        Path(command[2]).write_text("ok\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.preflight_staged_executable(tmp_path / "update.exe", tmp_path)
    assert not (tmp_path / "update-preflight.txt").exists()


def test_staged_executable_preflight_rejects_failed_binary(tmp_path, monkeypatch):
    def run(command, **kwargs):
        Path(command[2]).write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="启动自检失败"):
        installer.preflight_staged_executable(tmp_path / "broken.exe", tmp_path)


def test_load_check_writes_success_receipt_without_opening_dashboard(tmp_path):
    report = tmp_path / "load-check.txt"
    assert _load_check(report) == 0
    assert report.read_text(encoding="utf-8") == "ok\n"
