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


def test_updater_waits_for_all_target_processes_and_preflights_copied_file():
    wait_all = UPDATE_SCRIPT.index("Get-TargetProcessIds")
    copy_incoming = UPDATE_SCRIPT.index("Copy-Item -LiteralPath $staged -Destination $incoming -Force")
    copied_hash = UPDATE_SCRIPT.index("Copied update SHA256 mismatch")
    copied_preflight = UPDATE_SCRIPT.index("Preflighting the copied executable before replacement.")
    replace_exe = UPDATE_SCRIPT.index("[IO.File]::Replace($incoming, $target, $backup)")
    target_hash = UPDATE_SCRIPT.index("Replaced executable SHA256 mismatch")
    assert wait_all < copy_incoming < copied_hash < copied_preflight < replace_exe < target_hash
    assert "Old application processes did not exit" in UPDATE_SCRIPT
    assert "Updated application exited before startup receipt" in UPDATE_SCRIPT
    assert "Update failed:" in UPDATE_SCRIPT


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
