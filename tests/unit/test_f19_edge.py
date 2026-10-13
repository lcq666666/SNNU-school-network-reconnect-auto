# -*- coding: utf-8 -*-
"""F19 Edge 兜底限频与准确通知（对应 T31）。

要点：``edge_fallback`` 返回 ``BrowserLaunchResult``；只在需人工处理时触发；
同一故障周期只自动打开一次；通知如实反映是否真的打开。全程 fake launcher。
"""
from __future__ import annotations

import logging

import campus_reconnect as engine
import campus_reconnect_tray as tray
from campus_core import BrowserLaunchResult, PortalStatus, RepairResult
from tests.fakes import FakeLauncher

LOG = logging.getLogger("campus")


def test_t31_edge_fallback_reports_success(monkeypatch):
    launcher = FakeLauncher(which_result="C:/fake/msedge.exe").install(monkeypatch, engine)
    result = engine.edge_fallback(LOG)
    assert isinstance(result, BrowserLaunchResult)
    assert result.launched is True
    assert launcher.popen_calls, "未调用启动器"


def test_t31_edge_fallback_not_found_is_honest(monkeypatch):
    FakeLauncher(which_result=None).install(monkeypatch, engine)
    monkeypatch.setattr(engine.os, "environ", {})   # 避免命中真实安装路径
    result = engine.edge_fallback(LOG)
    assert result.launched is False
    assert result.error == "not_found"


def test_t31_edge_fallback_launch_failure_is_honest(monkeypatch):
    FakeLauncher(which_result="C:/fake/msedge.exe", popen_fail=True).install(monkeypatch, engine)
    result = engine.edge_fallback(LOG)
    assert result.launched is False
    assert result.error


def _app(monkeypatch, **overrides):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    return app


def test_t31_edge_opened_once_per_failure_cycle(monkeypatch):
    app = _app(monkeypatch, browser_fallback=True)
    calls = []
    monkeypatch.setattr(tray, "edge_fallback",
                        lambda log: (calls.append(1), BrowserLaunchResult(launched=True))[1])
    app._last_result = RepairResult(needs_user=True, status=PortalStatus.NEEDS_USER)
    cfg = {"browser_fallback": True}

    first = app._edge_note(cfg)
    second = app._edge_note(cfg)
    assert "已打开" in first
    assert "未重复打开" in second
    assert len(calls) == 1, "同一故障周期重复打开了 Edge"


def test_t31_edge_not_opened_when_not_needs_user(monkeypatch):
    app = _app(monkeypatch, browser_fallback=True)
    calls = []
    monkeypatch.setattr(tray, "edge_fallback",
                        lambda log: calls.append(1))
    app._last_result = RepairResult(needs_user=False, status=PortalStatus.SUSPECT)
    assert app._edge_note({"browser_fallback": True}) == ""
    assert calls == []


def test_t31_manual_reconnect_resets_edge_cycle(monkeypatch):
    app = _app(monkeypatch, browser_fallback=True)
    monkeypatch.setattr(app, "_enqueue_repair", lambda force=False: True)
    app._edge_opened_this_cycle = True
    app.on_reconnect(None)
    assert app._edge_opened_this_cycle is False
