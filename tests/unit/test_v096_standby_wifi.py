"""Hot standby maintenance must preserve a working primary and rate-limit login."""
from types import SimpleNamespace

import pytest

from campus_resilience import StandbyWifiGuard
from tests.unit.test_v05_dual_path_integration import H, IU, C, D, PU, snapshot


def test_successful_auth_with_failed_internet_is_not_reported_as_bad_login():
    from campus_snnu import SnnuBackupLink, BackupLinkResult
    repair = SimpleNamespace(ok=False, auth_ok=True, detail="公网仍不可达")
    link = SnnuBackupLink(ops=SimpleNamespace(repair=lambda *args: repair), session=None)
    result = link._authenticate(BackupLinkResult())
    assert not result.ok
    assert result.error == "E_SNNU_INTERNET_UNREACHABLE"


@pytest.mark.parametrize("wifi", [IU, C, D])
def test_standby_failure_requires_confirmation_and_limits_retries(wifi):
    calls = []
    guard = StandbyWifiGuard(lambda link: calls.append(link))
    snap = snapshot(H, wifi)
    for moment in (0, 15):
        guard.tick(snap, trusted=True, now=moment)
    assert calls == []
    for moment in range(30, 600, 15):
        guard.tick(snap, trusted=True, now=moment)
    assert len(calls) == 2
    assert all(link is snap.wifi for link in calls)
    guard.tick(snap, trusted=True, now=630)
    assert len(calls) == 3


@pytest.mark.parametrize("trusted,eth,wifi,egress", [
    (False, H, IU, 12), (True, IU, IU, 12),
    (True, H, PU, 12), (True, H, H, 12), (True, H, C, 14),
])
def test_standby_preserves_trust_and_primary_egress_gates(trusted, eth, wifi, egress):
    calls = []
    guard = StandbyWifiGuard(lambda link: calls.append(link))
    for moment in range(0, 900, 15):
        guard.tick(snapshot(eth, wifi, egress), trusted=trusted, now=moment)
    assert calls == []


def test_standby_uses_bound_snnu_pipeline_and_refreshes_observer(monkeypatch):
    import campus_reconnect_tray as tray
    import campus_snnu

    captured = {}
    invalidated = []
    def ops(*args, **kwargs):
        captured.update(kwargs)
        return object()

    class Link:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def run(self):
            return SimpleNamespace(ok=True, detail="verified")

    monkeypatch.setattr(tray.engine, "_snnu_ops", ops)
    monkeypatch.setattr(campus_snnu, "SnnuBackupLink", Link)
    app = SimpleNamespace(
        get_config=lambda: {"ssid": "SNNU"}, log=SimpleNamespace(info=lambda *a: None),
        stop_event=SimpleNamespace(wait=lambda seconds: False), _cancelled=lambda: False,
        snapshot_source=lambda: SimpleNamespace(invalidate=invalidated.append))
    wifi = snapshot(H, IU).wifi
    result = tray.TrayApp._repair_standby_wifi(app, wifi)
    assert result.ok and invalidated
    assert captured["require_web"] and captured["force_login"]
    assert captured["allow_auth"] and captured["if_index"] == wifi.if_index
    assert captured["interface"] == wifi.interface
    assert captured["source_ip"] == wifi.source_ip
