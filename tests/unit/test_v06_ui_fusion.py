"""V0.6.1 A+D+E 融合仪表盘测试：大状态 + 三环仪表 + 历史时间线。

零真实网络、零真实注册表、零真实凭据、零真实浏览器、零真实 sleep。
纯函数部分不碰 Tk；渲染部分复用 V0.6 的 xvfb 真窗口模式，无显示时跳过。
"""

import logging

import pytest

import campus_reconnect_tray as tray
from campus_timeline import NetworkTimeline
from campus_ui_state import CARD_ROWS, PathCard, StepView, UiState, UuView

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


# ---------- 1. 仪表映射（纯函数） ----------

def test_gauge_healthy_card_is_high():
    assert tray.gauge_from_card(_card("ethernet", verified=True,
                                      tone="ok")) == (0.95, "已验证")


def test_gauge_offline_card_is_low():
    assert tray.gauge_from_card(_card("ethernet", verified=False,
                                      tone="bad", label="未连接")) == (
        0.15, "未连接")


def test_gauge_degraded_card_is_mid():
    assert tray.gauge_from_card(_card("ethernet", verified=False,
                                      tone="warn",
                                      label="已连接（需要认证）")) == (
        0.5, "已连接（需要认证）")


def test_gauge_no_data_does_not_guess():
    assert tray.gauge_from_card(None) is None
    assert tray.gauge_from_card(_card("ethernet", verified=None,
                                      tone="unknown", label="未知")) is None


def test_gauge_uu_disabled_does_not_guess():
    assert tray.gauge_from_uu(None) is None
    assert tray.gauge_from_uu(UuView(enabled=False)) is None


def test_gauge_uu_states():
    assert tray.gauge_from_uu(
        UuView(enabled=True, last_ok=True)) == (0.95, "就绪")
    assert tray.gauge_from_uu(
        UuView(enabled=True, last_ok=False)) == (0.2, "异常")
    assert tray.gauge_from_uu(
        UuView(enabled=True, last_ok=None)) == (0.5, "待确认")


# ---------- 2. 真窗口渲染（xvfb；无显示跳过） ----------

def _open_app():
    import tkinter as tk

    if tray.ctk is None:
        pytest.skip("本机无 CustomTkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("无显示环境")
    root.withdraw()
    app = tray.TrayApp(LOG)
    app.root = root
    app.config = dict(tray.DEFAULT_CONFIG)
    app._open_dashboard()
    refs = getattr(app, "_dash_ctk_widgets", None)
    assert refs is not None, "融合仪表盘应走 CTk 分支"
    return app, root, refs


def _canvas_texts(canvas):
    return [canvas.itemcget(item, "text")
            for item in canvas.find_all()
            if canvas.type(item) == "text"]


def test_fusion_healthy_big_status_and_gauges():
    app, root, refs = _open_app()
    try:
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert refs["overall"].cget("text") == "网络正常"
        assert refs["light"].cget("text_color") == "#3fb950"
        assert "已验证" in _canvas_texts(refs["gauges"]["ethernet"]["canvas"])
        assert "已验证" in _canvas_texts(refs["gauges"]["wifi"]["canvas"])
        assert "就绪" in _canvas_texts(refs["gauges"]["uu"]["canvas"])
        assert "192.168.1.101" in refs["gauges"]["ethernet"]["fact"].cget("text")
        assert refs["headline"].cget("text") == "双链路正常"
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
        assert refs["light"].cget("text_color") == "#f85149"
        assert "未连接" in _canvas_texts(refs["gauges"]["ethernet"]["canvas"])
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_recovering_light_breathes():
    app, root, refs = _open_app()
    try:
        state = _state(overall="RECOVERING", headline="正在恢复")
        # 打开仪表盘时 _refresh_dashboard 已渲染一次（pulse 已消耗），
        # 这里只断言连续两次渲染灯色交替，不假设起始奇偶。
        app._render_dashboard_ctk(state)
        first = refs["light"].cget("text_color")
        app._render_dashboard_ctk(state)
        second = refs["light"].cget("text_color")
        assert {first, second} == {"#facc15", "#58a6ff"}
        assert first != second
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_details_collapsed_by_default_then_expand():
    app, root, refs = _open_app()
    try:
        assert refs["details_open"] is False
        assert refs["details_toggle"].cget("text") == "展开详情 ▾"
        assert not refs["details_frame"].winfo_ismapped()
        app._toggle_dashboard_details()
        root.update_idletasks()
        assert refs["details_open"] is True
        assert refs["details_toggle"].cget("text") == "收起详情 ▴"
        assert refs["details_frame"].winfo_ismapped()
        assert list(refs["ethernet"].keys()) == list(CARD_ROWS)
        assert len(refs["wifi"]) == 10
        app._toggle_dashboard_details()
        assert refs["details_open"] is False
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_pipeline_progress_and_buttons_unchanged():
    app, root, refs = _open_app()
    try:
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert refs["progress"].get() == pytest.approx(4 / 6)
        assert "Internet Verify" in refs["steps"]["internet"].cget("text")
        texts = [btn.cget("text") for btn in refs["buttons"]]
        assert texts == ["立即检测", "立即验证以太网", "立即验证 SNNU Wi-Fi",
                         "手动重连", "重启 UU Remote", "设置", "打开日志", "关闭"]
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
        assert refs["timeline"].cget("text").split("\n")[0] == lines[0]
    finally:
        app._close_dashboard()
        root.destroy()


def test_fusion_smoke_open_render_close():
    app, root, refs = _open_app()
    try:
        app._refresh_dashboard()
        root.update_idletasks()
        assert refs["overall"].cget("text")
        assert refs["gauges"]["uu"]["canvas"].find_all()
        assert len(refs["history"]) >= 1
        app._close_dashboard()
        assert app.dashboard_win is None
    finally:
        root.destroy()
