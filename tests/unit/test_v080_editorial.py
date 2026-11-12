"""P3: editorial pages, de-duplication and persisted appearance. Offline only."""
from __future__ import annotations

import pytest

import campus_desktop_ui as ui
import campus_ui_prefs as prefs


@pytest.fixture(autouse=True)
def _restore_theme(monkeypatch):
    monkeypatch.setattr(ui, "save_theme", lambda mode: True)
    yield
    ui.set_theme("light")


def _widgets_with_role(root, role):
    found = []

    def visit(widget):
        for child in widget.winfo_children():
            mark = getattr(child, "_campus_theme", None)
            if mark and mark[0] == role:
                found.append(child)
            visit(child)

    visit(root)
    return found


def _open():
    from tests.unit.test_v06_ui_fusion import _open_app, _state
    return _open_app(), _state()


def test_theme_preference_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "data_directory", lambda: tmp_path)
    assert prefs.load_theme() == "light"
    assert prefs.save_theme("dark") is True
    assert prefs.load_theme() == "dark"
    assert (tmp_path / prefs.THEME_FILE).is_file()


def test_theme_preference_rejects_invalid_modes_and_bad_files(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "data_directory", lambda: tmp_path)
    assert prefs.save_theme("solarized") is False
    path = tmp_path / prefs.THEME_FILE
    path.write_text('{"theme": "neon"}', encoding="utf-8")
    assert prefs.load_theme() == "light"
    path.write_text("not json", encoding="utf-8")
    assert prefs.load_theme() == "light"


def test_switch_theme_persists_choice(monkeypatch):
    saved = []
    monkeypatch.setattr(ui, "save_theme", lambda mode: saved.append(mode) or True)
    ui.switch_theme("dark")
    assert saved == ["dark"]
    assert ui.theme_mode() == "dark"


def test_editorial_pages_drop_cards_and_keep_contract():
    (app, root, refs), state = _open()
    try:
        app._render_dashboard_ctk(state)
        root.update_idletasks()
        assert set(refs["pages"]) == {"overview", "network", "recovery",
                                     "activity", "settings"}
        assert list(refs["ethernet"].keys()) == ["状态", "Internet", "最近验证"]
        assert set(refs["steps"]) == {"detect", "internet", "uu"}
        assert len(refs["buttons"]) == 3
        assert refs["guard_button"] is not None
        assert refs["autostart_button"] is not None
        for key in ("network", "recovery", "activity", "settings"):
            panels = [w for w in ui._walk(refs["pages"][key])
                      if getattr(w, "_campus_theme", (None,))[0] == "panel"]
            assert panels == [], key
        rules = _widgets_with_role(root, "rule")
        assert len(rules) >= 8
        fonts = set()
        for widget in ui._walk(root):
            try:
                face = widget.cget("font")
            except Exception:
                continue
            if hasattr(face, "cget"):
                fonts.add(face.cget("family"))
        assert ui.FONT_DISPLAY in fonts
        assert ui.FONT in fonts
        # Guard state is stated once, on the settings page.
        assert "在线守护：" not in refs["protection"].cget("text")
        assert "电脑重启恢复" in refs["protection"].cget("text")
        assert refs["gauges_frame"].cget("fg_color") == ui.CONSOLE["SURFACE"]

        rule = rules[0]
        ui.switch_theme("dark")
        root.update_idletasks()
        assert rule.cget("fg_color") == ui.THEMES["dark"]["BORDER"]
        assert refs["gauges_frame"].cget("fg_color") == ui.CONSOLE["SURFACE"]
        assert refs["navigation"]["overview"].cget("fg_color") == ui.THEMES["dark"]["SURFACE"]
    finally:
        app._close_dashboard()
        root.destroy()


def test_advanced_toggle_still_works_and_is_read_only(monkeypatch):
    (app, root, refs), state = _open()
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("editorial render triggered a network or settings operation")
        for name in ("on_check_connection", "on_reconnect", "on_toggle_online_guard",
                     "on_settings", "_open_proxy_compat"):
            monkeypatch.setattr(app, name, forbidden)
        app._render_dashboard_ctk(state)
        toggle = refs["advanced_toggle"]
        toggle.invoke()
        root.update_idletasks()
        assert refs["advanced_section"].winfo_manager() == "pack"
        toggle.invoke()
        root.update_idletasks()
        assert not refs["advanced_section"].winfo_manager()
    finally:
        app._close_dashboard()
        root.destroy()
