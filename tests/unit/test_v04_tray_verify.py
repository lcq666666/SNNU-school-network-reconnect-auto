# -*- coding: utf-8 -*-
"""V0.4-1 托盘侧触发点：**每个「在线段」异步做一次真实上网校验**。

背景：托盘是全自动路径的常态入口（``_probe_tick`` 由网络工作线程驱动）。需求要求
「连接上之后做一次网络校验」，因此：
- 进入 online 态时触发一次；
- 掉线（判为离线）后**重置标记** ⇒ 下次连上重新校验；
- **重连成功**（``_after_repair(ok=True)``）也触发 —— 该路径直接置 online，不经过
  ``_probe_tick`` 的状态跃迁，若只在跃迁处触发就会漏掉「重连成功」这一核心场景。

校验必须**异步**（独立短命线程）且**用专用 Session**，绝不阻塞监测/重连循环，
也不与工作线程共享 Session。

约束：零真实网络、零真实 sleep（只等线程收尾）。
"""
from __future__ import annotations

import logging
import time

import campus_reconnect_tray as tray

LOG = logging.getLogger("campus")


def wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _make_app(monkeypatch, online_series=None, **overrides):
    """构造托盘应用：全部外部副作用替换为假件（与 test_tray_worker 同构）。

    ``online_series``：``internet_ok`` 的按序应答；用尽后保持最后一个值。
    """
    replies = list(online_series or [True])
    state = {"i": 0}

    def fake_internet_ok(session, probes=None):
        index = min(state["i"], len(replies) - 1)
        state["i"] += 1
        return bool(replies[index]), "via"

    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    monkeypatch.setattr(tray, "wifi_state", lambda: ("SNNU", "已连接"))
    monkeypatch.setattr(tray, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(app, "notify", lambda *a, **k: None)
    return app


def _wait_idle(app):
    """等待校验线程收尾（``_verify_busy`` 释放）。"""
    def done():
        busy = getattr(app, "_verify_busy", None)
        return busy is None or not busy.is_set()

    return wait_until(done)


def _record_verify(monkeypatch):
    calls = []
    monkeypatch.setattr(tray, "verify_internet",
                        lambda session: calls.append(session) or (True, "ok"))
    return calls


# --------------------------------------------------------------------------- #
# 探测跃迁
# --------------------------------------------------------------------------- #
def test_v04_tray_verifies_once_per_online_segment(monkeypatch):
    """在线段内只校验一次；掉线后再连上**重新**校验。"""
    app = _make_app(monkeypatch, online_series=[True, True, False, True])
    app.session = object()
    calls = _record_verify(monkeypatch)

    app._probe_tick(0.0)                      # 上线 → 校验 #1
    assert wait_until(lambda: len(calls) == 1), "上线未触发连接后校验"
    assert _wait_idle(app)

    app._probe_tick(0.0)                      # 仍在线 → 不重复
    time.sleep(0.05)
    assert len(calls) == 1, "在线期间重复校验：%d 次" % len(calls)

    app._probe_tick(0.0)                      # 离线 → 结束本在线段
    app._probe_tick(0.0)                      # 重新连上 → 校验 #2
    assert wait_until(lambda: len(calls) == 2), "掉线恢复未重新校验"


def test_v04_tray_verify_uses_dedicated_session(monkeypatch):
    """校验用**专用** Session（不与工作线程共享，避免并发复用连接池）。"""
    app = _make_app(monkeypatch)
    worker_session = object()
    app.session = worker_session
    seen = []
    monkeypatch.setattr(tray, "direct_session", lambda: object())
    monkeypatch.setattr(tray, "verify_internet",
                        lambda session: seen.append(session) or (True, "ok"))

    app._probe_tick(0.0)

    assert wait_until(lambda: seen), "未触发校验"
    assert seen[0] is not worker_session, "校验复用了工作线程的 Session"


def test_v04_tray_reconnect_success_triggers_verify(monkeypatch):
    """**重连成功**（``_after_repair``）必须触发校验（需求1 核心场景）。"""
    app = _make_app(monkeypatch, clash_auto_proxy=False, browser_fallback=False)
    calls = _record_verify(monkeypatch)

    app._after_repair(True, "重连成功", 3.0)

    assert app.state["mode"] == "online"
    assert wait_until(lambda: calls), "重连成功未触发连接后校验"


def test_v04_tray_reconnect_success_does_not_double_verify(monkeypatch):
    """重连成功已校验 ⇒ 紧随其后的在线探测不得重复校验。"""
    app = _make_app(monkeypatch, clash_auto_proxy=False, online_series=[True])
    calls = _record_verify(monkeypatch)

    app._after_repair(True, "重连成功", 3.0)
    assert wait_until(lambda: len(calls) == 1)
    assert _wait_idle(app)

    app._probe_tick(0.0)
    time.sleep(0.05)
    assert len(calls) == 1, "重连成功后重复校验：%d 次" % len(calls)


# --------------------------------------------------------------------------- #
# 失败/异常不得影响主流程
# --------------------------------------------------------------------------- #
def test_v04_tray_verify_failure_notifies_and_does_not_change_state(monkeypatch):
    """校验失败 ⇒ 气泡提示 + 只记日志；**不**改动状态机、不触发重连。"""
    app = _make_app(monkeypatch)
    app.session = object()
    notices = []
    monkeypatch.setattr(app, "notify", lambda *a, **k: notices.append(a))
    monkeypatch.setattr(tray, "verify_internet",
                        lambda session: (False, "闸门 www.baidu.com(timeout)"))

    app._probe_tick(0.0)

    assert wait_until(lambda: notices), "校验失败未提示"
    assert app.state["mode"] == "online", "校验失败不应改变在线状态"


def test_v04_tray_verify_exception_is_swallowed(monkeypatch):
    """校验自身异常不得影响工作线程（不得把监测循环炸掉）。"""
    app = _make_app(monkeypatch)
    app.session = object()

    def boom(session):
        raise OSError("socket unavailable")

    monkeypatch.setattr(tray, "verify_internet", boom)

    app._probe_tick(0.0)
    time.sleep(0.05)

    assert app.state["mode"] == "online"
    assert _wait_idle(app), "校验线程未正常收尾（异常路径未释放 busy）"
