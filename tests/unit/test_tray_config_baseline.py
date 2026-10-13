# -*- coding: utf-8 -*-
"""托盘配置加载的「已安全」基线（非缺陷，阶段 1 任务 1.4 的对照项）。

《审查方案》第 10 节复现的是**可解析但类型非法**的配置被原样接受（见
``tests/regression/test_f03_config.py``）；而 JSON ``null`` / ``list`` / 截断
在现有 ``load_config()`` 里已由 ``try/except`` 回退安全默认值，**不是**可复现缺陷。
此处固化该基线，防止后续改造时退化。

约束：不读真实 ``tray_config.json``，全部走 ``tmp_path``。
"""
from __future__ import annotations

import pytest

import campus_reconnect_tray as tray


@pytest.mark.parametrize("text", ["null", "[1, 2]", '{"interval": 15', "", "not json"])
def test_malformed_config_falls_back_safely(tmp_path, monkeypatch, text):
    """JSON null/list/截断/损坏：回退安全默认值且不启用代理。"""
    path = tmp_path / "tray_config.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(tray, "CONFIG_FILE", path)

    cfg = tray.load_config()
    assert cfg["interval"] == tray.DEFAULT_CONFIG["interval"]
    assert cfg["threshold"] == tray.DEFAULT_CONFIG["threshold"]
    assert cfg["cooldown"] == tray.DEFAULT_CONFIG["cooldown"]
    assert cfg["clash_auto_proxy"] is None


def test_missing_config_file_uses_defaults(tmp_path, monkeypatch):
    """配置文件不存在时使用默认值。"""
    monkeypatch.setattr(tray, "CONFIG_FILE", tmp_path / "nope.json")
    cfg = tray.load_config()
    assert cfg["ssid"] == tray.DEFAULT_CONFIG["ssid"]
    assert cfg["clash_auto_proxy"] is None
