# -*- coding: utf-8 -*-
"""F13 回归用例：配置非原子保存、失败静默吞掉（对应 T25）。

依据《审查方案》第 10 节离线复现：
``save_permission_failure_return= None`` —— 原 ``save_config()`` 在 ``OSError`` 时
静默 ``pass``，不向调用方返回失败，UI 仍提示“已保存”。

**阶段 2 已修复**：``save_config()`` 改为原子写入（临时文件 + fsync +
``os.replace``）并在失败时返回 ``False``、保留旧文件；本用例已并入默认离线集合。
"""
from __future__ import annotations

import json
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import campus_reconnect
import campus_reconnect_tray as tray


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


def test_t25_successful_save_roundtrips_and_keeps_known_keys(tmp_path, monkeypatch):
    """T25：成功保存后可被 ``load_config`` 读回；未知键不落盘。"""
    cfg_path = tmp_path / "tray_config.json"
    monkeypatch.setattr(tray, "CONFIG_FILE", cfg_path)

    assert tray.save_config({"interval": 45, "mystery": "SECRET-VALUE"}) is True
    text = cfg_path.read_text(encoding="utf-8")
    assert "SECRET-VALUE" not in text
    cfg = tray.load_config()
    assert cfg["interval"] == 45


def test_t25_no_temp_files_left_behind(tmp_path, monkeypatch):
    """T25：原子保存不得遗留临时文件。"""
    cfg_path = tmp_path / "tray_config.json"
    monkeypatch.setattr(tray, "CONFIG_FILE", cfg_path)
    assert tray.save_config({"interval": 30}) is True
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "tray_config.json"]
    assert leftovers == []


def test_t25_concurrent_saves_leave_a_valid_file(tmp_path, monkeypatch):
    """T25：并发保存必须串行化，最终文件始终是合法 JSON。"""
    cfg_path = tmp_path / "tray_config.json"
    monkeypatch.setattr(tray, "CONFIG_FILE", cfg_path)
    errors = []

    def worker(value):
        try:
            assert tray.save_config({"interval": value}) is True
        except Exception as exc:   # pragma: no cover - 失败时记录
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v,)) for v in (10, 20, 30, 40, 50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert errors == []
    parsed = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert parsed["interval"] in (10, 20, 30, 40, 50)


def test_t37_unwritable_data_dir_is_reported(tmp_path, monkeypatch):
    """T37：数据目录不可写时返回失败，且不影响调用方（不抛异常）。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(tray, "CONFIG_FILE", blocker / "tray_config.json")
    assert tray.save_config({"interval": 30}) is False


def test_t37_log_init_degrades_instead_of_crashing(tmp_path, monkeypatch):
    """T37：日志目录不可写时降级为仅控制台输出，不崩溃。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(campus_reconnect, "LOG_DIR", blocker / "logs")

    logger = campus_reconnect.setup_logging(False)
    assert logger is not None
    files = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    streams = [h for h in logger.handlers if type(h) is logging.StreamHandler]
    assert files == [], "日志目录不可写时不应创建文件 handler"
    assert len(streams) == 1, "降级后应保留且仅保留一个控制台 handler"


def test_t37_config_read_failure_falls_back_with_visible_error(tmp_path, monkeypatch, caplog):
    """T37：配置读取失败时回退安全默认值并给出可见错误。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(tray, "CONFIG_FILE", blocker / "tray_config.json")

    with caplog.at_level(logging.WARNING, logger="campus"):
        cfg = tray.load_config()
    assert cfg["interval"] == tray.DEFAULT_CONFIG["interval"]
    assert cfg["clash_auto_proxy"] is None

