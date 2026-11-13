"""Desktop presentation regression: real status, navigation and activity.

零真实网络、零真实注册表、零真实凭据、零真实浏览器、零真实 sleep。
纯函数部分不碰 Tk；渲染部分复用 V0.6 的 xvfb 真窗口模式，无显示时跳过。
"""

import logging

import pytest

import campus_reconnect_tray as tray
from campus_desktop_ui import CONSOLE, sleep_action_required
from campus_remote import ReadinessReport, RemoteReadinessItem
from campus_timeline import NetworkTimeline
from campus_ui_state import PathCard, StepView, UiState, UuView

LOG = logging.getLogger("campus")


def _card(role, *, verified, tone, label="正常"):
    return PathCard(
        role=role, title="以太网" if role == "ethernet" else "SNNU Wi-Fi",
        assignment="PRIMARY" if role == "ethernet" else "STANDBY",
        tone=tone, state_label=label, internet_tone=tone, verified=verified,
        fields=(("角色", "PRIMARY"), ("状态", label),
                ("源地址", "192.168.1.101" if role == "ethernet"
                 else "202.117.144.88"),
                ("网关", "192.168.1.1"), ("ifIndex", "12"),
                ("DNS", "202.117.144.1"), ("链路", "已连接"),
                ("Portal", "已通过"), ("Internet", "已验证"),
                ("最近验证", "10 秒前")))


def _state(overall="HEALTHY", headline="双链路正常", **over):
    steps = tuple(StepView(key, label, "done" if i < 4 else "pending")
                  for i, (key, label) in enumerate(
                      (("detect", "检测"), ("wifi_ready", "Wi-Fi Ready"),
                       ("portal", "Portal"),
                       ("internet", "Internet Verify"),
                       ("route", "Route Switch"), ("uu", "UU Restart"))))
    kwargs = dict(overall=overall, headline=headline,
                  ethernet=_card("ethernet", verified=True, tone="ok"),
                  wifi=_card("wifi", verified=True, tone="ok"),
                  pipeline=steps,
                  uu=UuView(enabled=True, label="就绪", tone="ok",
                            last_ok=True, detail="冷却 300 秒"))
    kwargs.update(over)
    return UiState(**kwargs)


def test_sleep_readiness_exposes_action_only_for_actionable_gap():
    action = RemoteReadinessItem("sleep", "睡眠/休眠", False,
                                 "睡眠超时已启用", status="ACTION")
    passed = RemoteReadinessItem("sleep", "睡眠/休眠", True,
                                 "已关闭", status="PASS")
    other_gap = RemoteReadinessItem("auto_logon", "自动登录", False,
                                    "未启用", status="ACTION")

    assert sleep_action_required(ReadinessReport(items=[action]))
    assert not sleep_action_required(ReadinessReport(items=[passed]))
    assert not sleep_action_required(ReadinessReport(items=[other_gap]))
    assert not sleep_action_required(None)


# Desktop rendering accepts immutable offline state fixtures.

def _open_app():
    import tkinter as tk
    from tests.fakes import desktop_test_root
    if tray.ctk is None:
        pytest.skip("本机无 CustomTkinter")
    try:
        root = desktop_test_root()
    except tk.TclError as error:
        if tray.sys.platform.startswith("win"):
            pytest.fail(str(error))
        pytest.skip("无显示环境")
    app = tray.TrayApp(LOG, config=dict(tray.DEFAULT_CONFIG))
    app.root = root
    app.config = dict(tray.DEFAULT_CONFIG)
    app._open_dashboard()
    app.dashboard_win.after_cancel(app._dash_refresh_id)
    app._dash_refresh_id = None
    refs = getattr(app, "_dash_ctk_widgets", None)
    assert refs is not None, "融合仪表盘应走 CTk 分支"
    app.dashboard_win.deiconify()
    root.update_idletasks()
    return app, root, refs


def test_fusion_healthy_big_status_and_gauges():
    app, root, refs = _open_app()
    try:
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert refs["overall"].cget("text") == "网络正常"
        assert refs["light"].cget("text_color") == CONSOLE["TONES"]["ok"]
        assert refs["status_cards"]["ethernet"]["value"].cget("text") == "已验证"
        assert refs["status_cards"]["wifi"]["value"].cget("text") == "已验证"
        assert refs["status_cards"]["uu"]["value"].cget("text") == "就绪"
        assert "192.168.1.101" in refs["status_cards"]["ethernet"]["fact"].cget("text")
        assert refs["headline"].cget("text") == "双链路正常"
    finally:
        app._close_dashboard()
        root.destroy()


def test_recovery_page_offers_power_fix_and_settings_only_when_needed(monkeypatch):
    app, root, refs = _open_app()
    opened = []
    monkeypatch.setattr(app, "_update_opener",
                        lambda uri: opened.append(uri) or True)
    try:
        app.readiness_report = ReadinessReport(items=[RemoteReadinessItem(
            "sleep", "睡眠/休眠", False, "睡眠超时已启用", status="ACTION")])
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        action = refs["sleep_action"]
        assert action.winfo_manager() == "pack"
        assert action.cget("text") == "一键关闭睡眠/休眠"
        settings_action = refs["sleep_settings_action"]
        assert settings_action.winfo_manager() == "pack"
        settings_action.invoke()
        assert opened == ["ms-settings:powersleep"]

        app.readiness_report = ReadinessReport(items=[RemoteReadinessItem(
            "sleep", "睡眠/休眠", True, "已关闭", status="PASS")])
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert action.winfo_manager() == ""
        assert settings_action.winfo_manager() == ""
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_offline_big_status_and_gauge():
    app, root, refs = _open_app()
    try:
        state = _state(
            overall="OFFLINE", headline="双链路均不可用",
            ethernet=_card("ethernet", verified=False, tone="bad",
                           label="未连接"),
            wifi=_card("wifi", verified=False, tone="bad", label="未连接"),
            uu=UuView(enabled=True, label="等待验证", tone="idle",
                      last_ok=None, detail=""))
        app._render_dashboard_ctk(state)
        root.update_idletasks()
        assert refs["overall"].cget("text") == "离线"
        assert refs["light"].cget("text_color") == CONSOLE["TONES"]["bad"]
        assert refs["status_cards"]["ethernet"]["value"].cget("text") == "未连接"
    finally:
        app._close_dashboard()
        root.destroy()


def test_recovering_status_color_is_stable():
    app, root, refs = _open_app()
    try:
        state = _state(overall="RECOVERING", headline="正在恢复")
        app._render_dashboard_ctk(state)
        first = refs["light"].cget("text_color")
        app._render_dashboard_ctk(state)
        second = refs["light"].cget("text_color")
        assert first == second == CONSOLE["TONES"]["busy"]
    finally:
        app._close_dashboard()
        root.destroy()


def test_navigation_separates_network_details_from_overview():
    app, root, refs = _open_app()
    try:
        root.update_idletasks()
        assert refs["selected_page"] == "overview"
        assert not bool(refs["pages"]["network"]._parent_frame.winfo_manager())
        refs["navigation"]["network"].invoke()
        root.update_idletasks()
        assert refs["selected_page"] == "network"
        assert bool(refs["pages"]["network"]._parent_frame.winfo_manager())
        assert not bool(refs["pages"]["overview"]._parent_frame.winfo_manager())
        assert list(refs["ethernet"].keys()) == ["状态", "Internet", "最近验证"]
        assert len(refs["wifi"]) == 3
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_pipeline_progress_and_buttons_unchanged():
    app, root, refs = _open_app()
    try:
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert refs["progress"].get() == pytest.approx(4 / 6)
        assert refs["steps"]["internet"].cget("text") == "完成"
        texts = [btn.cget("text") for btn in refs["buttons"]]
        assert texts == ["立即检查", "尝试恢复", "开启在线守护"]
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_timeline_empty_shows_placeholder():
    app, root, refs = _open_app()
    try:
        app.timeline = NetworkTimeline()
        app._render_dashboard_ctk(_state())
        assert refs["timeline"].cget("text") == "尚无网络故障事件记录"
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_timeline_newest_first_max_five():
    app, root, refs = _open_app()
    try:
        clock = [1000.0]

        def _tick():
            clock[0] += 10.0
            return clock[0]

        timeline = NetworkTimeline(clock=_tick,
                                   wall_clock=lambda: 1762830000.0)
        timeline.observe(now=1000.0, offline=True)
        timeline.note_uu_started(active_path="ethernet", network_epoch=1)
        timeline.note_uu_outcome(restarted=True, ok=True,
                                 action="restarted", detail="已重启")
        timeline.observe(now=1060.0, offline=False)
        timeline.note_reconnect(True, detail="Portal 认证完成")
        timeline.observe(now=1120.0, offline=True)
        timeline.observe(now=1180.0, offline=False)
        app.timeline = timeline
        lines = app._dashboard_timeline_lines(limit=5)
        assert len(lines) == 5
        assert lines == [event.line()
                         for event in reversed(timeline.recent(5))]
        assert "RECOVERY_COMPLETE" in lines[0]
        app._render_dashboard_ctk(_state())
        from campus_desktop_ui import translated_event
        assert refs["timeline"].cget("text").split("\n")[0] == translated_event(lines[0])
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_smoke_open_render_close():
    app, root, refs = _open_app()
    try:
        app._refresh_dashboard()
        root.update_idletasks()
        assert refs["overall"].cget("text")
        assert refs["status_cards"]["uu"]["value"].cget("text")
        assert len(refs["pages"]) == 5
        app._close_dashboard()
        assert app.dashboard_win is None
    finally:
        root.destroy()
