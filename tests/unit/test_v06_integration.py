"""V0.6 集成测试：默认值 / 限幅 / 确认 / Clash 自愈接线 / UU 门禁与 Tier2。

零真实网络、零真实注册表、零真实凭据、零真实浏览器、零真实 sleep。
"""

import logging
import types

import pytest

import campus_clash_health as clash
import campus_core as core
import campus_reconnect_tray as tray
import campus_uu_recovery as uu
from campus_remote_app import RemoteAppStatus, discover
from tests.fakes import FakeRemoteAppOps, VirtualClock

LOG = logging.getLogger("campus")


def _app(**cfg):
    app = tray.TrayApp(LOG, config=dict(tray.DEFAULT_CONFIG))
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(cfg)
    return app


# ---------- 1. 默认值与限幅 ----------

def test_core_defaults_v06():
    cfg = core.Config()
    assert (cfg.interval, cfg.threshold, cfg.cooldown) == (5, 2, 60)


def test_tray_default_config_v06():
    cfg = tray.DEFAULT_CONFIG
    assert (cfg["interval"], cfg["threshold"], cfg["cooldown"]) == (5, 2, 60)
    assert cfg["v06_auto_prompted"] is False
    assert cfg["clash_heal_enabled"] is True
    assert cfg["uu_escalation_enabled"] is True


def test_read_v06_keys_invalid_falls_back():
    keys = tray._read_v06_keys('{"v06_auto_prompted": "yes", '
                               '"clash_heal_enabled": 1, '
                               '"uu_escalation_enabled": null}')
    assert keys == {"v06_auto_prompted": False, "clash_heal_enabled": True,
                    "uu_escalation_enabled": True}


def test_read_v06_keys_valid_kept():
    keys = tray._read_v06_keys('{"v06_auto_prompted": true, '
                               '"clash_heal_enabled": false, '
                               '"uu_escalation_enabled": false}')
    assert keys == {"v06_auto_prompted": True, "clash_heal_enabled": False,
                    "uu_escalation_enabled": False}


def test_interval_threshold_cooldown_clamped():
    app = _app(interval=1, threshold=0, cooldown=10)
    assert app._interval() == 5.0
    assert app._threshold() == 1
    assert app._cooldown() == 60.0
    app.config.update(interval=999, threshold=99, cooldown=99999)
    assert app._interval() == 120.0
    assert app._threshold() == 10
    assert app._cooldown() == 3600.0


# ---------- 2. 智能自动确认（纯函数） ----------

def test_should_prompt_only_once_in_monitor():
    base = {"v06_auto_prompted": False, "failover_mode": "MONITOR",
            "auto_switch_authorized": False}
    assert tray.should_prompt_auto_switch(base) is True
    assert tray.should_prompt_auto_switch(
        dict(base, v06_auto_prompted=True)) is False
    assert tray.should_prompt_auto_switch(
        dict(base, failover_mode="AUTO")) is False
    assert tray.should_prompt_auto_switch(
        dict(base, auto_switch_authorized=True)) is False


def test_apply_auto_switch_consent():
    agreed = tray.apply_auto_switch_consent(
        {"failover_mode": "MONITOR", "auto_switch_authorized": False}, True)
    assert agreed["failover_mode"] == "AUTO"
    assert agreed["auto_switch_authorized"] is True
    assert agreed["v06_auto_prompted"] is True
    refused = tray.apply_auto_switch_consent(
        {"failover_mode": "MONITOR", "auto_switch_authorized": False}, False)
    assert refused["failover_mode"] == "MONITOR"
    assert refused["auto_switch_authorized"] is False
    assert refused["v06_auto_prompted"] is True


# ---------- 3. Clash 自愈接线（假 ops / 假探针） ----------

class _FakeHealOps:
    def __init__(self):
        self.plans = []

    def execute(self, plan):
        self.plans.append(plan)
        return True


def _fake_dead_health():
    return clash.assess_dual_axis(
        True, clash.ProxyAxisResult(
            direct_ok=None, proxy_ok=False, proxy_error="timeout"))


def test_clash_heal_execute_uses_injected_ops():
    app = _app()
    ops = _FakeHealOps()
    app._clash_heal_ops = ops
    assert app._clash_heal_execute(
        clash.heal_plan(_fake_dead_health(), attempts=0)) is True
    assert ops.plans[0].actions == ["toggle_system_proxy"]
    assert app._clash_heal_execute(
        clash.heal_plan(_fake_dead_health(), attempts=1)) is True
    assert ops.plans[1].actions == ["restart_verge"]


def test_dual_axis_check_heals_on_proxy_timeout(monkeypatch):
    app = _app(proxy_endpoint="127.0.0.1:10999", clash_auto_proxy=True)
    monkeypatch.setattr(tray.engine, "tcp_probe", lambda *args: False)
    executed = []
    monkeypatch.setattr(
        app, "_clash_heal_execute", lambda plan: executed.append(plan) or True)
    monkeypatch.setattr(
        tray, "probe_via_proxy",
        lambda session, probes: clash.ProxyAxisResult(
            direct_ok=None, proxy_ok=False, proxy_error="timeout"))
    monkeypatch.setattr(
        tray.engine, "direct_session",
        lambda: types.SimpleNamespace(proxies={}))
    app._dual_axis_clash_check(True)
    assert len(executed) == 1
    assert executed[0].actions == ["toggle_system_proxy"]


def test_dual_axis_check_healthy_resets_attempts(monkeypatch):
    app = _app(proxy_endpoint="127.0.0.1:10999")
    app._clash_heal_attempts = 2
    monkeypatch.setattr(
        tray, "probe_via_proxy",
        lambda session, probes: clash.ProxyAxisResult(
            direct_ok=None, proxy_ok=True, proxy_error=None))
    monkeypatch.setattr(
        tray.engine, "direct_session",
        lambda: types.SimpleNamespace(proxies={}))
    app._dual_axis_clash_check(True)
    assert app._clash_heal_attempts == 0


# ---------- 4. Wi-Fi 自启链 ----------

def test_wifi_bringup_returns_none_off_windows():
    import os

    if os.name == "nt":  # pragma: no cover - Windows 真机才跑
        pytest.skip("仅非 Windows 环境可断言返回 None")
    assert _app()._wifi_bringup("SNNU") is None


# ---------- 5. UU 门禁与 Tier2 ----------

class _Follow:
    def __init__(self, schedules=True, epoch=3):
        self.schedules_uu_restart = schedules
        self.uu_request = types.SimpleNamespace(network_epoch=epoch)


def test_uu_gate_blocks_without_outage(monkeypatch):
    app = _app()
    app._v06_had_outage = False
    app._v06_uu_was_running = True
    called = []
    monkeypatch.setattr(app, "_uu_recovery_async",
                        lambda *a: called.append(a))
    app._uu_recovery_after_verification(_Follow(), types.SimpleNamespace(allows_recovery=True))
    assert called == []


def test_uu_gate_blocks_when_not_running_before(monkeypatch):
    app = _app()
    app._v06_had_outage = True
    app._v06_uu_was_running = False
    called = []
    monkeypatch.setattr(app, "_uu_recovery_async",
                        lambda *a: called.append(a))
    app._uu_recovery_after_verification(_Follow(), types.SimpleNamespace(allows_recovery=True))
    assert called == []


def test_uu_gate_allows_when_all_conditions_hold(monkeypatch):
    app = _app()
    app._v06_had_outage = True
    app._v06_uu_was_running = True
    called = []
    monkeypatch.setattr(app, "_uu_recovery_async",
                        lambda *a: called.append(a))
    app._uu_recovery_after_verification(_Follow(epoch=7), types.SimpleNamespace(allows_recovery=True))
    assert len(called) == 1
    assert app._v06_had_outage is False  # 事件已消费


def _tier2_status():
    ops = FakeRemoteAppOps(
        processes=[
            {"pid": 101, "process_name": "GameViewer",
             "path": "C:\\GameViewer\\GameViewer.exe"},
            {"pid": 102, "process_name": "GameViewerServer",
             "path": "C:\\GameViewer\\GameViewerServer.exe"},
            {"pid": 103, "process_name": "GameViewerHealthd",
             "path": "C:\\GameViewer\\GameViewerHealthd.exe"},
        ],
        run_entries=[{"command":
                      "C:\\GameViewer\\GameViewer.exe --auto-run"}])
    return ops, discover(ops)


def test_uu_tier2_restart_kills_tier2_and_confirms_back(monkeypatch):
    # V0.6.1：Tier2 的 10 秒等待走 tray._REAL_SLEEP（导入时抓取），
    # 不再是模块级 time 的 sleep，测试里改打这个桩。
    monkeypatch.setattr(tray, "_REAL_SLEEP", lambda _s: None)
    app = _app()
    ops, status = _tier2_status()
    assert app._uu_tier2_restart(ops, status) is True
    assert ops.killed == [101, 102, 103]
    assert ops.launched  # 按自启项重启过


def test_uu_tier2_restart_aborts_without_launch_command(monkeypatch):
    monkeypatch.setattr(tray, "_REAL_SLEEP", lambda _s: None)
    app = _app()
    ops = FakeRemoteAppOps(
        processes=[{"pid": 101, "process_name": "GameViewer", "path": ""}])
    status = discover(ops)
    assert status.user_processes
    assert app._uu_tier2_restart(ops, status) is False
    assert ops.killed == []  # 未找到重启命令：不做任何改动


def test_should_escalate_timeout_semantics():
    assert uu.should_escalate(False, 59.0, False).escalate is False
    assert uu.should_escalate(False, 60.0, False).escalate is True
    assert uu.should_escalate(False, 61.0, False).escalate is True


# ---------- V0.6.1：托盘等待不得推进虚拟时钟（i2_01 回归） ----------

def test_v061_tier2_sleep_does_not_advance_virtual_clock(monkeypatch):
    """UU Tier2 的 10 秒确认等待不得推进虚拟时钟。

    根因（Windows CI 上 i2_01 漂移）：V0.6 把等待写进托盘并走模块级
    time，计时器测试装上虚拟时钟后，残留的后台线程恰好在此时调用它，
    虚拟时间被多推进几秒，探测时刻从 [0, 15, 30, 45] 漂到
    [0, 15, 30, 50]。托盘的等待一律走导入时抓取的 _REAL_SLEEP。
    """
    clock = VirtualClock()
    clock.install(monkeypatch, tray)  # tray.time 换成虚拟时钟
    slept = []
    monkeypatch.setattr(tray, "_REAL_SLEEP", lambda s: slept.append(s))
    app = _app()
    ops, status = _tier2_status()
    assert app._uu_tier2_restart(ops, status) is True
    assert slept == [10.0]
    assert clock.now == 0.0, "Tier2 等待推进了虚拟时钟：%s" % clock.now
    assert clock.sleeps == []


def test_v061_tray_has_no_patchable_sleep_calls():
    """托盘源码里不得再出现可被虚拟时钟替换的等待调用。

    守卫上面的回归：以后任何人在托盘里新加等待，都必须走 _REAL_SLEEP，
    否则计时器测试的虚拟时钟会被后台线程推进，i2_01 这类时序断言
    会在 CI 上偶发漂移。
    """
    from pathlib import Path

    text = Path(tray.__file__).read_text(encoding="utf-8")
    assert "time.sleep(" not in text
    assert "_REAL_SLEEP(" in text
    assert uu.should_escalate(True, 60.0, False).escalate is False
    assert uu.should_escalate(False, 60.0, True).escalate is False


def test_schedule_tier2_check_escalates_after_timeout(monkeypatch):
    app = _app()
    timers = []

    class _FakeTimer:
        def __init__(self, delay, fn):
            self.delay = delay
            self.fn = fn
            self.daemon = False

        def start(self):
            timers.append(self)

    monkeypatch.setattr(tray.threading, "Timer", _FakeTimer)
    monkeypatch.setattr(tray.engine, "internet_ok", lambda _s: (False, "x"))
    ops, status = _tier2_status()
    escalated = []
    monkeypatch.setattr(app, "_uu_tier2_restart",
                        lambda o, s: escalated.append((o, s)) or True)
    monkeypatch.setattr(
        "campus_remote_app.discover",
        lambda _ops: RemoteAppStatus(processes=[]))
    app._schedule_uu_tier2_check(ops, status)
    assert len(timers) == 1
    assert timers[0].delay == 60.0
    timers[0].fn()  # 60 秒后：仍未确认回来 → 升 Tier2
    assert len(escalated) == 1


# ---------- 6. UI 后端回退 ----------

def test_ui_backend_falls_back_without_customtkinter(monkeypatch):
    monkeypatch.setattr(tray, "ctk", None)
    assert tray.ui_backend() == "ttk"


def test_ui_backend_customtkinter_when_available():
    if tray.ctk is None:
        pytest.skip("本机无 CustomTkinter")
    assert tray.ui_backend() == "customtkinter"


def test_ctk_dashboard_opens_and_renders():
    """CustomTkinter 仪表盘真窗口冒烟：无显示时跳过（不伪造通过）。"""
    import tkinter as tk

    if tray.ctk is None:
        pytest.skip("本机无 CustomTkinter")
    try:
        from tests.fakes import desktop_test_root
        root = desktop_test_root()
    except tk.TclError:
        pytest.skip("无显示环境")
    try:
        app = _app()
        app.root = root
        app._open_dashboard()
        refs = getattr(app, "_dash_ctk_widgets", None)
        assert refs is not None
        app._refresh_dashboard()
        root.update_idletasks()
        assert refs["overall"].cget("text")
        assert len(refs["status_cards"]) == 3
        app._close_dashboard()
        assert app.dashboard_win is None
    finally:
        root.destroy()
