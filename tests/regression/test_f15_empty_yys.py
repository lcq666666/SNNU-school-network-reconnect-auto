# -*- coding: utf-8 -*-
"""F15 回归用例：空运营商被 mobile 覆盖（对应 T27）。

依据《审查方案》第 10 节离线复现：
``empty_yys_sent= 'mobile'`` —— 托盘 ``repair_once()`` 里
``cfg.get("yys") or "mobile"`` 把**合法空串**改写成 mobile。

**本文件在修复前必须失败**（阶段 3 修复后转绿）。
"""
from __future__ import annotations

import logging

import pytest

import campus_reconnect_tray as tray

pytestmark = pytest.mark.regression

LOG = logging.getLogger("campus")


def _make_app(monkeypatch, config: dict):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = config
    app.session = object()
    return app


def test_t27_empty_operator_is_preserved_end_to_end(monkeypatch):
    """T27：空运营商必须端到端保留为空串，不得改写为 mobile。"""
    captured = {}

    def fake_login(session, account, password, yys, port, log):
        captured["yys"] = yys
        return False, "stop-here"

    monkeypatch.setattr(tray, "wifi_state", lambda: ("SNNU", "已连接"))
    monkeypatch.setattr(tray, "resolve_port", lambda session, requested: (8601, "fake"))
    monkeypatch.setattr(tray, "portal_reachable", lambda session, port: True)
    monkeypatch.setattr(tray, "get_credentials", lambda: ("123456", "pw"))
    monkeypatch.setattr(tray, "portal_login", fake_login)

    app = _make_app(monkeypatch, {
        "ssid": "SNNU", "yys": "", "interval": 15, "threshold": 3,
        "cooldown": 180, "browser_fallback": False, "clash_auto_proxy": None,
    })
    app.repair_once()

    assert captured.get("yys") == "", \
        "空运营商被改写为 %r（应为空串）" % captured.get("yys")
