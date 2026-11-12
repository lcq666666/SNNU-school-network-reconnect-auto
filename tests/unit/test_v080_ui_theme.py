"""P1: dual-theme tokens and repaintable controls. Presentation only, zero host access."""
from __future__ import annotations

import pytest

import campus_desktop_ui as ui


@pytest.fixture(autouse=True)
def _restore_light_theme():
    yield
    ui.set_theme("light")


def _luminance(value):
    channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground, background):
    first, second = _luminance(foreground), _luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


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


def test_palettes_share_identical_key_sets():
    light, dark = ui.THEMES["light"], ui.THEMES["dark"]
    assert set(light) == set(dark)
    assert set(light["TONES"]) == set(dark["TONES"])
    assert set(ui.TONES) == set(light["TONES"])


def test_light_palette_preserves_v070_baseline():
    light = ui.THEMES["light"]
    assert (light["BG"], light["SIDE"], light["SURFACE"], light["BORDER"]) == (
        "#F6F6F8", "#EEEEF2", "#FFFFFF", "#DFDFE7")
    assert (light["TEXT"], light["MUTED"], light["ACCENT"]) == (
        "#25252D", "#666673", "#6554C0")
    assert light["TONES"]["ok"] == "#237A50"
    assert light["TONES"]["bad"] == "#B43838"
    assert light["TONES"]["busy"] == "#6554C0"


def test_module_constants_stay_compatible_with_from_imports():
    assert (ui.BG, ui.SIDE, ui.SURFACE, ui.BORDER, ui.TEXT, ui.MUTED, ui.ACCENT) == (
        "#F6F6F8", "#EEEEF2", "#FFFFFF", "#DFDFE7", "#25252D", "#666673", "#6554C0")
    assert ui.TONES == ui.THEMES["light"]["TONES"]
    ui.set_theme("dark")
    assert ui.BG == "#F6F6F8"
    assert ui.TONES["ok"] == "#237A50"
    assert ui.tokens()["BG"] != ui.BG


def test_dark_palette_meets_wcag_aa_contrast():
    dark = ui.THEMES["dark"]
    pairs = (("TEXT", "BG"), ("MUTED", "BG"), ("TEXT", "SURFACE"),
             ("MUTED", "SURFACE"), ("TEXT", "SIDE"))
    for foreground, background in pairs:
        assert _contrast(dark[foreground], dark[background]) >= 4.5, (
            foreground, background)
    assert _contrast(dark["ACCENT_TEXT"], dark["ACCENT"]) >= 4.5
    for name, color in dark["TONES"].items():
        assert _contrast(color, dark["BG"]) >= 4.5, name


def test_set_theme_switches_palette_and_rejects_unknown():
    assert ui.theme_mode() == "light"
    ui.set_theme("dark")
    assert ui.theme_mode() == "dark"
    assert ui.tokens()["BG"] == ui.THEMES["dark"]["BG"]
    assert ui.tokens("light")["BG"] == "#F6F6F8"
    with pytest.raises(ValueError):
        ui.set_theme("solarized")
    assert ui.theme_mode() == "dark"


def test_apply_theme_ignores_destroyed_widgets():
    from tests.unit.test_v06_ui_fusion import _open_app
    app, root, refs = _open_app()
    try:
        widget = refs["overall"]
        widget.destroy()
        ui.set_theme("dark")
        ui.apply_theme(root, app.dashboard_win)
        assert ui.theme_mode() == "dark"
    finally:
        app._close_dashboard()
        root.destroy()


def test_theme_switch_repaints_controls_without_touching_app_state(monkeypatch):
    from tests.unit.test_v06_ui_fusion import _open_app, _state
    app, root, refs = _open_app()
    try:
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert set(refs["pages"]) == {"overview", "network", "recovery", "activity", "settings"}
        assert refs["light"].cget("text_color") == "#237A50"
        hero = refs["gauges_frame"]
        assert hero.cget("fg_color") == "#FFFFFF"
        refs["navigate"]("network")
        root.update_idletasks()

        def forbidden(*args, **kwargs):
            pytest.fail("theme switch triggered a network or settings operation")
        for name in ("on_check_connection", "on_reconnect", "on_toggle_online_guard",
                     "on_open_log", "on_settings", "_open_proxy_compat"):
            monkeypatch.setattr(app, name, forbidden)

        refs["set_theme"]("dark")
        root.update_idletasks()
        dark = ui.THEMES["dark"]
        assert ui.theme_mode() == "dark"
        assert hero.cget("fg_color") == dark["SURFACE"]
        assert refs["light"].cget("text_color") == dark["TONES"]["ok"]
        assert refs["overall"].cget("text_color") == dark["TEXT"]
        assert refs["navigation"]["network"].cget("fg_color") == dark["SURFACE"]
        assert refs["navigation"]["overview"].cget("fg_color") == "transparent"
        mode_buttons = _widgets_with_role(root, "mode")
        assert len(mode_buttons) == 2
        assert {button.cget("fg_color") for button in mode_buttons} == {
            dark["ACCENT"], dark["SURFACE"]}
        assert refs["progress"].cget("progress_color") == dark["ACCENT"]
        assert refs["status_cards"]["ethernet"]["value"].cget("text") == "已验证"
        assert refs["selected_page"] == "network"

        refs["set_theme"]("light")
        root.update_idletasks()
        assert refs["light"].cget("text_color") == "#237A50"
        assert hero.cget("fg_color") == "#FFFFFF"
        assert refs["navigation"]["network"].cget("fg_color") == "#FFFFFF"
    finally:
        app._close_dashboard()
        root.destroy()
