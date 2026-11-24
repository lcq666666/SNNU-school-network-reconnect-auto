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


def test_uu_discovery_rechecks_initial_absence(monkeypatch):
    from campus_reconnect_tray import TrayApp
    from types import SimpleNamespace

    results = iter((False, True))
    monkeypatch.setattr(native, "uu_available", lambda: next(results))
    monkeypatch.setattr(native, "uu_connected", lambda: False)
    app = SimpleNamespace()
    assert TrayApp._observe_lifeline_uu(app, now=100) is True
    assert TrayApp._observe_lifeline_uu(app, now=120) is True
    assert TrayApp._observe_lifeline_uu(app, now=160) is False
    assert app._uu_available is True
