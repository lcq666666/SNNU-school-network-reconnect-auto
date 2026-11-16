"""Regression coverage for UU process-health probing."""
import json

import campus_resilience_windows as native
from tools import netdiag


def test_uu_probe_treats_missing_user_process_as_disconnected(monkeypatch):
    calls = []

    class Runner:
        def __init__(self, **kwargs):
            pass

        def run(self, name, script):
            calls.append((name, script))
            return 0, "", ""

    monkeypatch.setattr(netdiag, "PowerShellRunner", Runner)

    assert native.uu_connected() is False
    name, script = calls[0]
    assert name == "uu_connections"
    assert "Get-Process -ErrorAction SilentlyContinue" in script
    assert ("Where-Object { $_.ProcessName -in @('GameViewer', 'GameViewerLauncher') }"
            in script)
    assert "-Name GameViewer,GameViewerServer" not in script


def test_uu_probe_requires_external_connection_from_user_process(monkeypatch):
    class Runner:
        def __init__(self, **kwargs):
            pass

        def run(self, name, script):
            return 0, json.dumps([{"RemoteAddress": "203.0.113.8"}]), ""

    monkeypatch.setattr(netdiag, "PowerShellRunner", Runner)
    assert native.uu_connected() is True


def test_uu_probe_keeps_command_failure_unknown(monkeypatch):
    class Runner:
        def __init__(self, **kwargs):
            pass

        def run(self, name, script):
            return 1, "", "PowerShell unavailable"

    monkeypatch.setattr(netdiag, "PowerShellRunner", Runner)
    assert native.uu_connected() is None
