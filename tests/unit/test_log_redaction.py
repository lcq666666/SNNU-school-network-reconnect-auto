# -*- coding: utf-8 -*-
"""日志脱敏与初始化幂等测试（阶段 1，任务 1.5，对应 T35 / T36 部分）。

- T35：假凭据值（``FAKE_PASSWORD`` / ``FAKE_COOKIE``）不得出现在日志记录、
  ``repr``、配置序列化结果中；
- T36：``setup_logging`` 幂等，重复调用不堆叠 handler。

约束：使用 ``tmp_path`` 作为日志目录，不写项目真实 logs/，不产生网络/注册表副作用。
"""
from __future__ import annotations

import dataclasses
import json
import logging
from logging.handlers import RotatingFileHandler

import campus_reconnect
from campus_core import Config, Secret

FAKE_PASSWORD = "FAKE_PASSWORD"
FAKE_COOKIE = "FAKE_COOKIE"


def test_secret_never_appears_in_logs(caplog, tmp_path, monkeypatch):
    """把秘密容器写进日志，输出与日志文件都不得含明文。"""
    monkeypatch.setattr(campus_reconnect, "LOG_DIR", tmp_path)
    logger = campus_reconnect.setup_logging(False)
    with caplog.at_level(logging.DEBUG, logger="campus"):
        logger.info("凭据容器: %s", Secret(FAKE_PASSWORD))
        logger.debug("cookie 容器: %s", Secret(FAKE_COOKIE))
        logger.warning("字符串化: %s", "%s" % Secret(FAKE_PASSWORD))
    assert FAKE_PASSWORD not in caplog.text
    assert FAKE_COOKIE not in caplog.text
    logfile = tmp_path / "campus_reconnect.log"
    assert logfile.exists()
    content = logfile.read_text(encoding="utf-8")
    assert FAKE_PASSWORD not in content
    assert FAKE_COOKIE not in content


def test_config_repr_and_serialization_have_no_secret():
    """Config 的 repr 与 JSON 序列化结果均不含任何密码/凭据字段。"""
    cfg = Config()
    assert "password" not in repr(cfg).lower()
    assert "cookie" not in repr(cfg).lower()
    serialized = json.dumps(dataclasses.asdict(cfg), ensure_ascii=False)
    assert "password" not in serialized.lower()
    assert FAKE_PASSWORD not in serialized


def test_setup_logging_is_idempotent(tmp_path, monkeypatch):
    """重复调用 setup_logging 不堆叠 handler，且保持 1 个文件 + 1 个流 handler。"""
    monkeypatch.setattr(campus_reconnect, "LOG_DIR", tmp_path)
    logger = campus_reconnect.setup_logging(False)
    first = len(logger.handlers)
    campus_reconnect.setup_logging(False)
    second = len(logger.handlers)
    campus_reconnect.setup_logging(True)
    third = len(logger.handlers)

    assert first == second == third
    files = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    streams = [h for h in logger.handlers if type(h) is logging.StreamHandler]
    assert len(files) == 1
    assert len(streams) == 1
