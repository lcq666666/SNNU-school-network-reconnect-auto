# -*- coding: utf-8 -*-
"""F01 回归用例：自动 / 手动重连并发（对应 T01）。

依据《审查方案》F01：``on_reconnect()`` 每次点击都新建线程，且与监测线程可同时
调用 ``repair_once()``，没有 repair 锁 —— 掉线时连续点击会产生多个并行认证任务。

**本文件在修复前必须失败**（阶段 2 修复后转绿）；不真实联网，不真实 sleep。
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

import campus_reconnect_tray as tray

pytestmark = pytest.mark.regression

LOG = logging.getLogger("campus")


def test_t01_manual_reconnects_are_serialized(monkeypatch):
    """T01：连续 10 次手动重连，最多只允许 1 个认证任务在途。"""
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "edge_fallback", lambda log: None)
    monkeypatch.setattr(tray, "internet_ok", lambda session: (False, ""))

    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config["browser_fallback"] = False
    app.session = object()

    lock = threading.Lock()
    state = {"active": 0, "max": 0}
    gate = threading.Event()
    entered = threading.Event()

    def fake_repair():
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            entered.set()
        gate.wait(3.0)
        with lock:
            state["active"] -= 1
        return False, "x", 0.0

    monkeypatch.setattr(app, "repair_once", fake_repair)

    for _ in range(10):
        app.on_reconnect(None)

    assert entered.wait(3.0), "手动重连线程未启动"
    time.sleep(0.1)
    gate.set()
    time.sleep(0.3)

    assert state["max"] <= 1, \
        "并发手动重连产生了 %d 个并行认证任务（应最多 1 个）" % state["max"]
