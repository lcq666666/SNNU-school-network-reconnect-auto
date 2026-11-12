"""Offline regression cases found during the v0.8.1 implementation review."""
from types import SimpleNamespace

import pytest

import campus_dual_path as dp
from campus_core import RepairResult
from campus_core import PortalStatus
from campus_failover import FailoverMode, TickResult
from tests.unit.test_v03_p75_1a_supplement import _queued_source
from tests.unit.test_v05_ui_state import (
    D, H, ScriptedVerifier, _make_app, snapshot,
)
from tests.unit.test_tray_worker import wait_until


def test_rebuild_has_a_hard_limit_on_real_unfinished_tasks():
    source, clock, jobs = _queued_source(max_inflight=1)
    source.request()
    for _ in range(12):
        clock.advance(100)
        source.request()
    assert len(jobs) <= 2, "logical rebuild must not leak unlimited real threads"


def test_repeated_invalidation_retires_intervening_task():
    source, clock, jobs = _queued_source(max_inflight=1)
    source.request()
    jobs[0]()
    source.invalidate("network changed")
    clock.advance(1)
    source.request()
    source.invalidate("network changed")
    jobs[1]()
    assert not source.observe().trusted


def test_wifi_missing_facts_is_retried_when_dns_arrives():
    from tests.unit.test_v05_dual_path_integration import Rig
    rig = Rig()
    first = rig.step(D, H, None, dt=0, wifi_dns=())
    assert first.tick.verify_roles == ()
    later = rig.step(D, H, None, dt=31, wifi_dns=())
    assert later.tick.verify_roles == ()
    # The next complete Wi-Fi fact set must be independently verified.
    complete = rig.step(D, H, 14, dt=31)
    assert "wifi" in complete.tick.verify_roles or rig.verifier.calls[-1].role == "wifi"


def test_retired_completion_does_not_release_the_new_tasks_slot():
    source, clock, jobs = _queued_source(max_inflight=1)
    source.request()
    clock.advance(2)
    source.request()
    clock.advance(61)
    source.request()
    assert source._inflight_count == 1
    jobs[0]()
    assert source._inflight_count == 1
    jobs[1]()
    assert source.observe().trusted
    assert source._inflight_count == 0


def test_manual_wifi_uses_fresh_observation_in_online_guard_mode(monkeypatch):
    app = _make_app(monkeypatch, lifeline_enabled=True)
    fresh = snapshot(D, H)
    app._snapshot_source = SimpleNamespace(
        observe=lambda: SimpleNamespace(trusted=True, snapshot=fresh, age=0.0),
        collect_now=lambda: fresh)
    app._snapshot_collector = lambda: fresh
    app.snapshot_source().collect_now()
    assert app.dual_path.last_snapshot is None  # online guard does not tick failover
    verifier = app._path_verifier = ScriptedVerifier()
    app.on_verify_now("wifi")
    assert wait_until(lambda: bool(verifier.calls))
    assert verifier.calls[0].source_ip == fresh.wifi.source_ip


@pytest.mark.parametrize("index", [True, 0, -1, 2**32, "5"])
def test_manual_request_rejects_invalid_interface_index(index):
    snap = snapshot(D, H)
    snap.wifi.if_index = index
    assert dp.path_request_from_snapshot(snap, "wifi") is None


def test_manual_refresh_does_not_use_stale_collect_now_return(monkeypatch):
    app = _make_app(monkeypatch)
    app._snapshot_source = SimpleNamespace(
        collect_now=lambda: snapshot(D, H),
        observe=lambda: SimpleNamespace(trusted=False, snapshot=snapshot(D, H)))
    app._manual_verify_async = lambda *args: pytest.fail("stale snapshot authorized verification")
    notes = []
    app.notify = lambda *args: notes.append(args)
    app._refresh_then_verify_async("wifi")
    assert wait_until(lambda: not app._manual_verify_refresh_busy.is_set())
    assert notes


def test_background_setup_defers_persistent_writes_without_stopping_gui(tmp_path):
    from tests.unit.test_v070_auto_setup import setup
    guard, lifecycle, root, registry = setup(tmp_path)
    guard.allow_client_restart = False
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = guard.run()
    assert not result.ready and result.changed
    assert "202.117.144.205" in registry["ProxyOverride"]["value"]
    assert lifecycle.calls == []
    assert all(p.read_bytes() == raw for p, raw in before.items())
    lifecycle.running = False
    assert guard.run().ready
    assert guard.compat.persistent_ready(root)


@pytest.mark.parametrize("error_code", ["E_SNNU_AUTH_FAILED", "E_AUTH_REJECTED", "E_AUTH_CAPTCHA"])
def test_auth_failure_tells_user_to_finish_portal_login(monkeypatch, error_code):
    app = _make_app(monkeypatch, browser_fallback=False)
    app._last_result = RepairResult(error_code=error_code,
                                    status=PortalStatus.NEEDS_USER,
                                    needs_user=True,
                                    detail="认证失败")
    note = app._edge_note(app.get_config())
    assert "校园网认证失败" in note
    assert "202.117.144.205" in note


def test_online_guard_path_verification_does_not_restart_uu(monkeypatch):
    app = _make_app(monkeypatch, lifeline_enabled=True,
                    remote_restart_enabled=True)
    app._path_verifier = ScriptedVerifier()
    tick = TickResult(at=1.0, mode=FailoverMode.MONITOR,
                      state=app.failover.manager.state)
    app._dual_path_step(1.0, snapshot(D, H), trusted=True, result=tick)
    assert app.dual_path is not None
    assert app.dual_path.coordinator.last_uu_restart_ok is None


def test_theme_switch_paints_root_and_preserves_console_scrollbar(monkeypatch):
    import campus_desktop_ui as ui
    from tests.unit.test_v06_ui_fusion import _open_app
    monkeypatch.setattr(ui, "save_theme", lambda mode: True)
    app, root, refs = _open_app()
    try:
        ui.bind_theme(root, "tk_window")
        for mode in ("dark", "light", "dark"):
            refs["set_theme"](mode)
            root.update_idletasks()
            assert root.cget("background") == ui.tokens()["BG"]
            for name, page in refs["pages"].items():
                colors = ui.CONSOLE if name == "overview" else ui.tokens()
                assert page.cget("fg_color") == colors["BG"]
                assert page._parent_canvas.cget("background") == colors["BG"]
                assert page._scrollbar.cget("fg_color") == colors["SCROLL_TRACK"]
    finally:
        app._close_dashboard()
        root.destroy()
        ui.set_theme("light")
