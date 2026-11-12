"""P2: overview signal console, topology and honest state mapping. Offline only."""
from __future__ import annotations

import pytest

import campus_desktop_ui as ui
from tests.unit.test_v080_ui_theme import _contrast


@pytest.fixture(autouse=True)
def _restore_light_theme():
    yield
    ui.set_theme("light")


def test_console_palette_matches_theme_key_set():
    assert set(ui.CONSOLE) == set(ui.THEMES["light"])
    assert set(ui.CONSOLE["TONES"]) == set(ui.THEMES["light"]["TONES"])


def test_console_palette_meets_wcag_aa_contrast():
    colors = ui.CONSOLE
    for foreground, background in (("TEXT", "BG"), ("MUTED", "BG"),
                                   ("TEXT", "SURFACE"), ("MUTED", "SURFACE")):
        assert _contrast(colors[foreground], colors[background]) >= 4.5, (
            foreground, background)
    assert _contrast(colors["ACCENT_TEXT"], colors["ACCENT"]) >= 4.5
    for name, color in colors["TONES"].items():
        assert _contrast(color, colors["BG"]) >= 4.5, name


def test_signal_words_are_verbatim_state_identifiers():
    from campus_ui_state import (OVERALL_DEGRADED, OVERALL_HEALTHY, OVERALL_OFFLINE,
                                 OVERALL_PAUSED, OVERALL_RECOVERING, OVERALL_UNKNOWN)
    expected = {OVERALL_HEALTHY, OVERALL_DEGRADED, OVERALL_RECOVERING,
                OVERALL_OFFLINE, OVERALL_PAUSED, OVERALL_UNKNOWN}
    assert set(ui.SIGNAL_WORDS) == expected
    assert ui.SIGNAL_WORDS[OVERALL_HEALTHY] == "ONLINE"
    assert ui.SIGNAL_WORDS[OVERALL_OFFLINE] == "OFFLINE"


def _open():
    from tests.unit.test_v06_ui_fusion import _open_app, _state
    return _open_app(), _state()


def test_overview_console_and_topology_survive_theme_switch():
    (app, root, refs), state = _open()
    try:
        app._render_dashboard_ctk(state)
        root.update_idletasks()
        hero = refs["gauges_frame"]
        assert hero.cget("fg_color") == ui.CONSOLE["SURFACE"]
        assert refs["overall"].cget("text") == "网络正常"
        assert refs["overall"].cget("font").cget("family") == ui.FONT
        assert refs["signal_word"].cget("text") == "ONLINE"
        assert refs["light"].cget("text_color") == ui.CONSOLE["TONES"]["ok"]
        topo = refs["topology"]
        assert topo.cget("bg") == ui.CONSOLE["SURFACE"]
        assert len(topo.find_all()) > 0
        data = topo._campus_topology
        assert set(data) == {"ethernet", "wifi", "uu"}
        assert data["ethernet"]["tone"] == "ok"
        assert data["ethernet"]["label"] == "已验证"
        assert data["ethernet"]["role"] in ("主用", "备用", "")
        assert data["uu"]["label"] == "就绪"
        assert refs["status_cards"]["ethernet"]["strip"].cget(
            "fg_color") == ui.CONSOLE["TONES"]["ok"]

        refs["set_theme"]("dark")
        root.update_idletasks()
        # The overview is a fixed dark console; other pages still follow the theme.
        assert hero.cget("fg_color") == ui.CONSOLE["SURFACE"]
        assert refs["overall"].cget("text_color") == ui.CONSOLE["TEXT"]
        assert refs["navigation"]["overview"].cget("fg_color") == ui.THEMES["dark"]["SURFACE"]
        assert topo.cget("bg") == ui.CONSOLE["SURFACE"]
        assert len(topo.find_all()) > 0
    finally:
        app._close_dashboard()
        root.destroy()


def test_offline_state_maps_to_real_signal_and_tones():
    (app, root, refs), _ = _open()
    try:
        from campus_ui_state import UuView
        from tests.unit.test_v06_ui_fusion import _card, _state
        state = _state(overall="OFFLINE", headline="双链路均不可用",
                       ethernet=_card("ethernet", verified=False, tone="bad", label="未连接"),
                       uu=UuView(enabled=True, label="等待验证", tone="idle", last_ok=None))
        app._render_dashboard_ctk(state)
        root.update_idletasks()
        assert refs["signal_word"].cget("text") == "OFFLINE"
        assert refs["light"].cget("text_color") == ui.CONSOLE["TONES"]["bad"]
        data = refs["topology"]._campus_topology
        assert data["ethernet"]["tone"] == "bad"
        assert data["uu"]["label"] == "等待验证"
    finally:
        app._close_dashboard()
        root.destroy()


def test_console_render_is_honest_and_read_only(monkeypatch):
    (app, root, refs), state = _open()
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("console render triggered a network or settings operation")
        for name in ("on_check_connection", "on_reconnect", "on_toggle_online_guard",
                     "on_open_log", "on_settings", "_open_proxy_compat"):
            monkeypatch.setattr(app, name, forbidden)
        app._render_dashboard_ctk(state)
        root.update_idletasks()
        monitor = refs["monitor"].cget("text")
        assert monitor.startswith("●")
        assert "守护时长" not in monitor
        assert "已持续" not in monitor
        assert state.monitor_label in monitor or "等待监测" in monitor
        assert len(refs["status_cards"]) == 3
        assert len(refs["buttons"]) == 3
        assert set(refs["pages"]) == {"overview", "network", "recovery",
                                     "activity", "settings"}
        assert refs["gauges_frame"] is not None
    finally:
        app._close_dashboard()
        root.destroy()
