"""Offline acceptance for actionable summaries, navigation and DPI layout."""
from types import SimpleNamespace

import pytest

from campus_proxy_ui import assessment


@pytest.mark.parametrize(("report", "online", "expected"), [
    ("系统代理：已开启\n系统绕过：校园地址已配置", True, "网络正常，无需操作"),
    ("系统代理：已开启\n系统绕过：校园地址已配置\n校园规则不完整", True, "网络正常，当前无需修复"),
    ("系统代理：已开启\n系统绕过：缺少校园地址", False, "建议补充校园网绕过"),
    ("系统代理：未开启\n系统绕过：缺少校园地址", False, "未发现需要处理的代理配置"),
    ("系统代理：未开启\nPAC：已配置", False, "有兼容项需要核对"),
    ("系统绕过：校园地址已配置\nTUN：配置为开启", False, "有兼容项需要核对"),
    ("上次代理事务未完成", True, "需要处理：上次修复尚未完成"),
    ("读取失败", False, "检查结果暂未确认"),
])
def test_proxy_summary_does_not_confuse_configuration_with_connectivity(report, online, expected):
    title, explanation = assessment(report, online=online)
    assert title == expected
    assert explanation


def test_connected_without_verification_is_not_presented_as_internet_access():
    from dataclasses import replace
    from tests.unit.test_v06_ui_fusion import _card
    from campus_desktop_ui import path_summary
    card = _card("ethernet", verified=None, tone="ok")
    card = replace(card, fields=(("Internet", "未验证"),), internet_tone="unknown")
    assert path_summary(card) == ("未验证", "unknown")


def test_first_run_healthy_configuration_does_not_interrupt(tmp_path, monkeypatch):
    import campus_install
    import campus_proxy_ui as ui
    monkeypatch.setattr(campus_install, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(ui, "discover_roots", lambda: [])
    monkeypatch.setattr(ui, "windows_compatibility", lambda: SimpleNamespace(
        inspect=lambda roots: "系统代理：未开启\n系统绕过：缺少校园地址"))
    monkeypatch.setattr(ui, "runtime_summary", lambda: "未发现已知代理内核")
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(ui, "threading", SimpleNamespace(Thread=InlineThread))
    prompts = []
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *a, **k: prompts.append(a))
    app = SimpleNamespace(_ui_post=lambda f: f(), notify=lambda *a: prompts.append(a))
    ui.offer_onboarding(app, background=False)
    assert prompts == []
    assert (tmp_path / "proxy-compat-onboarding-v1.json").is_file()


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_desktop_pages_fit_at_supported_scaling(scale):
    from tests.unit.test_v06_ui_fusion import _open_app, _state
    from campus_desktop_ui import ctk, FONT
    app, root, refs = _open_app()
    try:
        dpi = app.dashboard_win._get_window_scaling()
        ctk.set_widget_scaling(scale / dpi)
        ctk.set_window_scaling(scale / dpi)
        app.dashboard_win._set_scaled_min_max()
        app.dashboard_win.geometry("760x560")
        app._render_dashboard_ctk(_state())
        for key in refs["pages"]:
            refs["navigate"](key)
            root.update_idletasks()
            assert refs["pages"][key]._parent_frame.winfo_manager() == "pack"
            for nav in refs["navigation"].values():
                assert nav.winfo_width() >= 140 * scale
        assert refs["overall"].cget("font").cget("family") == FONT
        assert refs["status_cards"]["uu"]["value"].cget("text") == "就绪"
        assert "gauges" not in refs
        app.config["lifeline_enabled"] = True
        app._render_dashboard_ctk(_state())
        assert refs["recovery_guard_button"].cget("text") == "关闭在线守护"
        refs["navigate"]("overview")
        root.update_idletasks()
        last_action = refs["buttons"][-1]
        assert last_action.winfo_rootx() + last_action.winfo_width() <= (
            app.dashboard_win.winfo_rootx() + app.dashboard_win.winfo_width())
    finally:
        ctk.set_widget_scaling(1)
        ctk.set_window_scaling(1)
        app._close_dashboard()
        root.destroy()


def test_navigation_and_render_do_not_trigger_operations(monkeypatch):
    from tests.unit.test_v06_ui_fusion import _open_app, _state
    app, root, refs = _open_app()
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("Read-only presentation attempted a network or settings operation")
        for name in ("on_detect_now", "on_reconnect", "on_restart_uu_remote", "_open_proxy_compat"):
            monkeypatch.setattr(app, name, forbidden)
        for key in refs["pages"]:
            refs["navigate"](key)
            app._render_dashboard_ctk(_state())
            root.update_idletasks()
    finally:
        app._close_dashboard()
        root.destroy()
