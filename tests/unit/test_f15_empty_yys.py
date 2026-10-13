# -*- coding: utf-8 -*-
"""F15 空运营商端到端保留（对应 T27）。

阶段 3 修复后由回归集合并入默认集合。要点：仅缺失键使用默认值；空串端到端
保留；托盘 ``repair_once`` 经共享服务把 ``yys=""`` 原样传给 ``portal_login``。
"""
from __future__ import annotations

import logging

import campus_reconnect_tray as tray

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


def test_t27_missing_operator_falls_back_to_default():
    """仅**缺失**键使用默认值（mobile）。"""
    from campus_core import RepairOptions
    options = RepairOptions.from_mapping({"ssid": "SNNU"})
    assert options.operator == "mobile"


def test_t27_none_operator_falls_back_to_default():
    """无效 None 按校验规则处理为默认值。"""
    from campus_core import RepairOptions
    options = RepairOptions.from_mapping({"yys": None})
    assert options.operator == "mobile"
