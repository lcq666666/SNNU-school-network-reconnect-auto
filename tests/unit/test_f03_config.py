# -*- coding: utf-8 -*-
"""F03 回归用例：配置类型 / 授权值 / 未知键（对应 T05 / T06 / T07）。

依据《审查方案》第 10 节离线复现：
``invalid_config_survives= bad 'false' True`` —— 原 ``load_config()`` 原样接受
非法配置（``interval="bad"``、``clash_auto_proxy="false"``）。

**阶段 2 已修复**：``load_config()`` 改走 ``campus_core`` 验证器（类型 / 范围 /
授权三态 / 未知键隔离），本用例已并入默认离线集合。
"""
from __future__ import annotations

import json
import logging

import campus_reconnect_tray as tray

LOG = logging.getLogger("campus")


def _write(tmp_path, monkeypatch, text: str):
    path = tmp_path / "tray_config.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(tray, "CONFIG_FILE", path)
    return path


# --------------------------------------------------------------------------- #
# T05：未知键必须隔离（当前缺陷）
# --------------------------------------------------------------------------- #
def test_t05_unknown_key_must_not_leak_into_config(tmp_path, monkeypatch):
    """T05：未知键必须被隔离，不能原样并入配置（当前缺陷）。

    注：JSON null/list/截断在现有实现里已由 try/except 回退安全默认值，
    不属于可复现缺陷，其基线固化在 ``tests/unit/test_tray_config_baseline.py``。
    """
    _write(tmp_path, monkeypatch, json.dumps({"mystery": "SECRET-VALUE"}))
    cfg = tray.load_config()
    assert "mystery" not in cfg, "未知配置键被原样并入配置，未隔离"


def test_t05_unknown_key_value_is_never_logged(tmp_path, monkeypatch, caplog):
    """T05：未知键的**原始值**不得出现在日志里（脱敏告警）。"""
    _write(tmp_path, monkeypatch, json.dumps({"mystery": "SECRET-VALUE"}))
    with caplog.at_level(logging.WARNING, logger="campus"):
        cfg = tray.load_config()
    assert "mystery" not in cfg
    assert "SECRET-VALUE" not in caplog.text


# --------------------------------------------------------------------------- #
# T06：interval="bad"、bool 充当 int
# --------------------------------------------------------------------------- #
def test_t06_string_interval_is_rejected(tmp_path, monkeypatch):
    """T06：interval="bad" 必须被拒绝并回退默认整数（当前原样接受）。"""
    _write(tmp_path, monkeypatch, json.dumps({"interval": "bad"}))
    cfg = tray.load_config()
    assert isinstance(cfg["interval"], int) and not isinstance(cfg["interval"], bool)
    assert cfg["interval"] == tray.DEFAULT_CONFIG["interval"]


def test_t06_bool_as_int_is_rejected(tmp_path, monkeypatch):
    """T06：bool 充当 int 必须被拒绝（bool 是 int 子类）。"""
    _write(tmp_path, monkeypatch, json.dumps({"interval": True, "threshold": False}))
    cfg = tray.load_config()
    assert not isinstance(cfg["interval"], bool)
    assert not isinstance(cfg["threshold"], bool)
    assert cfg["interval"] == tray.DEFAULT_CONFIG["interval"]
    assert cfg["threshold"] == tray.DEFAULT_CONFIG["threshold"]


def test_t06_out_of_range_numbers_are_rejected(tmp_path, monkeypatch):
    """T06：负数 / 小数 / 超大值超出范围必须回退。"""
    _write(tmp_path, monkeypatch,
           json.dumps({"interval": -5, "threshold": 2.5, "cooldown": 10 ** 9}))
    cfg = tray.load_config()
    assert cfg["interval"] == tray.DEFAULT_CONFIG["interval"]
    assert cfg["threshold"] == tray.DEFAULT_CONFIG["threshold"]
    assert cfg["cooldown"] == tray.DEFAULT_CONFIG["cooldown"]


# --------------------------------------------------------------------------- #
# T07：授权字符串 "false" 不得被当作同意
# --------------------------------------------------------------------------- #
def test_t07_string_consent_is_not_truthy(tmp_path, monkeypatch):
    """T07：clash_auto_proxy="false"（字符串）不得被当作同意代理联动。"""
    _write(tmp_path, monkeypatch, json.dumps({"clash_auto_proxy": "false"}))
    cfg = tray.load_config()
    assert cfg["clash_auto_proxy"] in (None, True, False), \
        "授权值被接受为 %r，字符串被当作真值" % cfg["clash_auto_proxy"]
    assert cfg["clash_auto_proxy"] is not True


def test_t07_string_browser_fallback_is_not_truthy(tmp_path, monkeypatch):
    """T07：browser_fallback="false"（字符串）不得被当作开启。"""
    _write(tmp_path, monkeypatch, json.dumps({"browser_fallback": "false"}))
    cfg = tray.load_config()
    assert isinstance(cfg["browser_fallback"], bool)
