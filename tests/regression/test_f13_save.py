# -*- coding: utf-8 -*-
"""F13 回归用例：配置非原子保存、失败静默吞掉（对应 T25）。

依据《审查方案》第 10 节离线复现：
``save_permission_failure_return= None`` —— 原 ``save_config()`` 在 ``OSError`` 时
静默 ``pass``，不向调用方返回失败，UI 仍提示“已保存”。

**本文件在修复前必须失败**（阶段 2 修复后转绿）；不写真实磁盘。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import campus_reconnect_tray as tray

pytestmark = pytest.mark.regression


def test_t25_save_failure_must_be_reported(monkeypatch, tmp_path):
    """T25：写失败必须显式返回失败（False）或抛出，不能静默吞掉；
    同时旧文件内容必须保持完整（不得被截断）。"""
    cfg_path = tmp_path / "tray_config.json"
    original = '{"interval": 15}'
    cfg_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(tray, "CONFIG_FILE", cfg_path)

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)

    try:
        result = tray.save_config({"interval": 30})
    except OSError:
        result = False

    assert result is False, "保存失败未向调用方报告（返回 %r）" % result
    assert cfg_path.read_text(encoding="utf-8") == original, "保存失败却破坏了旧文件"
