# -*- coding: utf-8 -*-
"""托盘工作线程、取消令牌与配置快照测试（阶段 2，任务 2.3 / 2.4）。

覆盖：

- T01：手动 / 自动请求共用**单网络工作线程**，重复请求合并且最多一个认证任务在途；
- T02：初始化（Session 未建立）前手动重连不抛异常、不访问 ``None`` Session；
- T03：任务进行中修改运营商不影响本次请求；过期结果不覆盖新状态；
- T04：重连 / 探测异常被转换为可识别状态，线程不静默失效且后续命令仍可处理；
      线程意外退出可被检测并重启；
- T21：暂停 / 退出后不得启动新的 POST / Edge / 代理写入；退出不遗留工作线程。

约束：全部外部副作用（Wi-Fi / 端口 / Portal / 凭据 / 浏览器 / 代理 / 注册表）
均替换为假件；零真实网络、零真实注册表、零真实凭据、零真实浏览器启动。
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

import campus_reconnect_tray as tray
from campus_core import ProxyApplyResult
from tests.fakes import FakeSession

LOG = logging.getLogger("campus")


def wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _make_app(monkeypatch, **overrides):
    """构造托盘应用，并把全部外部副作用替换为假件。"""
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app._test_proxy_calls = []

    monkeypatch.setattr(tray, "wifi_state", lambda: ("SNNU", "已连接"))
    monkeypatch.setattr(tray, "resolve_port", lambda session, requested: (8601, "fake"))
    monkeypatch.setattr(tray, "portal_reachable", lambda session, port: True)
    monkeypatch.setattr(tray, "get_credentials", lambda: ("123456", "pw"))
    monkeypatch.setattr(tray, "internet_ok", lambda session, probes=None: (False, ""))
    monkeypatch.setattr(tray, "portal_login", lambda *a, **k: (False, "stop"))
    monkeypatch.setattr(tray, "edge_fallback", lambda log: None)
    monkeypatch.setattr(tray, "get_autostart", lambda: False)
    monkeypatch.setattr(tray, "set_autostart", lambda enabled: True)
    monkeypatch.setattr(tray, "apply_system_proxy",
                        lambda *a, **k: app._test_proxy_calls.append(a)
                        or ProxyApplyResult(ok=False, detail="测试环境未启用"))
    return app


# --------------------------------------------------------------------------- #
# T01 / T02 / T03：排他、快照与初始化
# --------------------------------------------------------------------------- #
def test_t01_only_one_network_worker_thread(monkeypatch):
    """10 次手动重连只允许新增一个网络工作线程。"""
    app = _make_app(monkeypatch)
    app.session = object()
    gate = threading.Event()
    entered = threading.Event()

    def fake_repair():
        entered.set()
        gate.wait(2.0)
        return False, "x", 0.0

    monkeypatch.setattr(app, "repair_once", fake_repair)
    before = {t.ident for t in threading.enumerate() if t.name == "campus-net"}
    for _ in range(10):
        app.on_reconnect(None)

    assert entered.wait(2.0), "重连任务未启动"
    after = {t.ident for t in threading.enumerate() if t.name == "campus-net"}
    assert len(after - before) == 1, \
        "创建了 %d 个网络工作线程（应仅 1 个）" % len(after - before)
    gate.set()
    app.on_quit()


def test_t02_manual_reconnect_before_session_init_does_not_raise(monkeypatch):
    """Session 未建立时手动重连不得抛异常，也不得访问 None Session。"""
    app = _make_app(monkeypatch)
    app.session = None
    seen = []

    def fake_login(session, account, password, yys, port, log):
        seen.append(session)
        return False, "stop"

    monkeypatch.setattr(tray, "portal_login", fake_login)
    app.on_reconnect(None)

    assert wait_until(lambda: bool(seen)), "重连任务未执行"
    assert app.session is not None
    assert seen[0] is app.session
    assert app.state["mode"] != "error"
    app.on_quit()


def test_t03_config_snapshot_isolated_from_mid_task_change(monkeypatch):
    """任务进行中修改运营商不得改变本次请求。"""
    app = _make_app(monkeypatch)
    app.config["yys"] = "mobile"
    app.session = object()
    captured = {}

    def fake_login(session, account, password, yys, port, log):
        captured["yys"] = yys
        app.config["yys"] = "unicom"   # 任务进行中改运营商
        return False, "stop"

    monkeypatch.setattr(tray, "portal_login", fake_login)
    app.repair_once()
    assert captured["yys"] == "mobile", "本次请求被中途修改影响：%r" % captured["yys"]


def test_t03_stale_result_does_not_override_new_state(monkeypatch):
    """配置版本变化后，过期重连结果不得覆盖新状态。"""
    app = _make_app(monkeypatch)
    app.state["mode"] = "reconnecting"
    stale_version = app.config_version()
    app.update_config({"interval": 30})          # 任务进行中配置变更 → 版本提升

    app._after_repair(True, "过期结果", 1.0, config_version=stale_version)
    assert app.state["mode"] == "reconnecting"


# --------------------------------------------------------------------------- #
# T04：工作单元异常边界与线程监督
# --------------------------------------------------------------------------- #
def test_t04_repair_exception_keeps_worker_alive_and_recoverable(monkeypatch):
    """重连任务抛异常：转成可识别状态，线程存活，后续合法命令仍可处理。"""
    app = _make_app(monkeypatch)
    app.session = object()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("port resolve exploded")

    monkeypatch.setattr(app, "repair_once", boom)
    app.on_reconnect(None)

    assert wait_until(lambda: app.state["mode"] == "error"), app.state
    assert calls["n"] == 1
    status = app.worker_status()
    assert status["alive"] is True, "工作线程静默失效"
    assert status["last_error"], "未记录具体故障"

    monkeypatch.setattr(app, "repair_once", lambda: (True, "ok", 0.1))
    app.on_reconnect(None)
    assert wait_until(lambda: app.state["mode"] == "online"), app.state
    app.on_quit()


def test_t04_probe_exception_is_contained(monkeypatch):
    """探测异常按“探测失败”处理，不当作内部错误、不抛给调用方。"""
    app = _make_app(monkeypatch)
    app.session = object()

    def boom(session, probes=None):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(tray, "internet_ok", boom)
    result = app._probe_tick(0.0)
    assert isinstance(result, float)
    assert app.state["mode"] == "offline"


def test_t04_worker_death_is_detected_and_restartable(monkeypatch):
    """线程意外退出时 UI 可见“监测已停止”，且提供重启动作。"""
    app = _make_app(monkeypatch)

    def exiting_loop():
        return   # 模拟工作线程意外退出（立即结束，不留下存活线程）

    monkeypatch.setattr(app, "_worker_loop", exiting_loop)
    app._ensure_worker()
    assert wait_until(lambda: not app.worker_status()["alive"])
    assert "监测已停止" in app.worker_status()["label"]

    monkeypatch.setattr(app, "_worker_loop", lambda: app.stop_event.wait(2.0))
    app.restart_monitor()
    assert app.worker_status()["alive"] is True
    app.on_quit()


# --------------------------------------------------------------------------- #
# T21：取消令牌
# --------------------------------------------------------------------------- #
def test_t21_paused_repair_has_no_side_effects(monkeypatch):
    """暂停状态下不得发起 POST / Edge / 代理写入。"""
    app = _make_app(monkeypatch, paused=True)
    session = FakeSession()
    app.session = session
    calls = {"login": 0, "edge": 0}

    def fake_login(*a, **k):
        calls["login"] += 1
        return True, "ok"

    monkeypatch.setattr(tray, "portal_login", fake_login)
    monkeypatch.setattr(tray, "edge_fallback",
                        lambda log: calls.__setitem__("edge", calls["edge"] + 1))

    ok, detail, _elapsed = app.repair_once()
    assert ok is False
    assert "暂停" in detail
    assert calls == {"login": 0, "edge": 0}
    assert session.posts() == []
    assert app._test_proxy_calls == []


def test_t21_pause_during_login_blocks_edge_and_proxy(monkeypatch):
    """认证过程中暂停：不得继续打开 Edge 或写系统代理。"""
    app = _make_app(monkeypatch, browser_fallback=True, clash_auto_proxy=True)
    session = FakeSession()
    app.session = session
    calls = {"edge": 0}

    def fake_login(session_, account, password, yys, port, log):
        app.config["paused"] = True    # 认证过程中用户暂停
        return True, "ok"

    monkeypatch.setattr(tray, "portal_login", fake_login)
    monkeypatch.setattr(tray, "edge_fallback",
                        lambda log: calls.__setitem__("edge", calls["edge"] + 1))

    app._run_repair()
    assert calls["edge"] == 0, "暂停后仍打开了 Edge"
    assert app._test_proxy_calls == [], "暂停后仍写入了系统代理"
    assert app.state["mode"] == "offline"


def test_t21_quit_blocks_repair_and_stops_worker(monkeypatch):
    """退出后不得发起认证，且不遗留可执行副作用的工作线程。"""
    app = _make_app(monkeypatch)
    session = FakeSession()
    app.session = session
    app._ensure_worker()
    assert app.worker_status()["alive"] is True

    app.on_quit()
    assert wait_until(lambda: not app.worker_status()["alive"]), "工作线程未退出"

    ok, detail, _elapsed = app.repair_once()
    assert ok is False
    assert session.posts() == []
    assert app._test_proxy_calls == []


# --------------------------------------------------------------------------- #
# 授权版本（F12 的并发语义）
# --------------------------------------------------------------------------- #
def test_consent_version_invalidates_stale_answer(monkeypatch):
    """授权版本提升后，过期的询问结果不再被应用。"""
    app = _make_app(monkeypatch)
    app.config["clash_auto_proxy"] = None
    token = app._consent_version
    app._apply_consent(False, save=False)      # 用户显式拒绝 → 版本提升
    assert app._consent_version != token
    assert app.config["clash_auto_proxy"] is False


@pytest.mark.parametrize("value", [True, False, None])
def test_consent_roundtrip_keeps_three_states(monkeypatch, value):
    app = _make_app(monkeypatch)
    app._apply_consent(value, save=False)
    assert app.config["clash_auto_proxy"] is value


# --------------------------------------------------------------------------- #
# T10（托盘层）：网络结果与代理联动是两项独立结果（F05）
# --------------------------------------------------------------------------- #
def test_t10_proxy_failure_does_not_fake_network_result(monkeypatch):
    """代理联动失败不得把网络恢复成功谎报成失败，反之亦然。"""
    app = _make_app(monkeypatch, clash_auto_proxy=True,
                    proxy_endpoint="127.0.0.1:7897")
    app.session = object()
    app._after_repair(True, "Portal 确认在线，公网已恢复", 3.0)

    assert app.state["mode"] == "online", "网络恢复成功被代理失败带偏"
    assert app.state["detail"] == "Portal 确认在线，公网已恢复"
    assert app._test_proxy_calls, "同意联动后未尝试写系统代理"


def test_t10_proxy_success_note_matches_actual_result(monkeypatch):
    """代理确实开启时，通知文案才可以说“已开启”。"""
    app = _make_app(monkeypatch, clash_auto_proxy=True,
                    proxy_endpoint="127.0.0.1:7897")
    app.session = object()
    notes = []
    monkeypatch.setattr(app, "notify", lambda title, body: notes.append((title, body)))
    monkeypatch.setattr(tray, "apply_system_proxy",
                        lambda *a, **k: ProxyApplyResult(ok=True, endpoint="127.0.0.1:7897"))

    app._after_repair(True, "detail", 1.0)
    assert notes and notes[0][0] == "重连成功"
    assert "已自动开启系统代理" in notes[0][1]


def test_t10_proxy_skip_note_is_honest(monkeypatch):
    """代理未开启时通知必须如实说明，不得声称成功。"""
    app = _make_app(monkeypatch, clash_auto_proxy=True,
                    proxy_endpoint="127.0.0.1:7897")
    app.session = object()
    notes = []
    monkeypatch.setattr(app, "notify", lambda title, body: notes.append((title, body)))
    monkeypatch.setattr(tray, "apply_system_proxy",
                        lambda *a, **k: ProxyApplyResult(
                            ok=False, detail="代理端点未在监听（127.0.0.1:7897）"))

    app._after_repair(True, "detail", 1.0)
    assert "已自动开启系统代理" not in notes[0][1]
    assert "系统代理未开启" in notes[0][1]



# --------------------------------------------------------------------------- #
# T36：UI 事件队列（设置面板实时状态的数据通路）
# --------------------------------------------------------------------------- #
class _FakeRoot:
    """最小 Tk root 替身：记录 ``after`` 重排定时器，不创建真实窗口。"""

    def __init__(self):
        self.after_calls = []

    def after(self, ms, func):
        self.after_calls.append((ms, func))
        return "after-id"


def test_t36_ui_actions_are_consumed_on_pump(monkeypatch):
    """``_ui_post`` 只入队；``_pump_ui``（Tk 主线程）按序消费，网络线程不直接调 Tk。"""
    app = _make_app(monkeypatch)
    app.root = _FakeRoot()
    seen = []
    app._ui_post(lambda: seen.append("a"))
    app._ui_post(lambda: seen.append("b"))
    app._pump_ui()
    assert seen == ["a", "b"]
    app._pump_ui()               # 空队列不应抛异常
    assert seen == ["a", "b"]


def test_t36_pump_survives_action_exception(monkeypatch):
    """单个 UI 动作抛异常不得中断后续动作的消费。"""
    app = _make_app(monkeypatch)
    app.root = _FakeRoot()
    seen = []

    def boom():
        raise RuntimeError("ui action failed")

    app._ui_post(boom)
    app._ui_post(lambda: seen.append("after"))
    app._pump_ui()
    assert seen == ["after"]


def test_t36_ui_post_is_thread_safe_enqueue(monkeypatch):
    """网络线程调用 ``_ui_post`` 只入队（不触碰 Tk）。"""
    app = _make_app(monkeypatch)
    app.root = None
    app._ui_post(lambda: None)
    assert app._ui_actions.qsize() == 1
