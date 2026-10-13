# -*- coding: utf-8 -*-
"""F20 CLI 参数边界与退出码（对应 T32）。

要点：``--force`` 只属于 ``once``（``watch --force`` 拒绝）；``--yys`` 限定合法
集合；端口范围与动态识别能力一致；退出码 0=成功/无需操作、1=运行失败、
2=参数/配置错误；无效参数在读取凭据/联网/写注册表前失败；``--dry-run`` 不 POST
且账号脱敏。
"""
from __future__ import annotations

import argparse
import logging
import sys

import pytest

import campus_reconnect as engine
from campus_core import RepairResult

LOG = logging.getLogger("campus")


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def test_t32_watch_force_is_rejected():
    with pytest.raises(SystemExit) as exc:
        engine.build_parser().parse_args(["watch", "--force"])
    assert exc.value.code == 2


def test_t32_once_force_is_accepted():
    args = engine.build_parser().parse_args(["once", "--force"])
    assert args.force is True


def test_t32_invalid_yys_is_rejected():
    with pytest.raises(SystemExit) as exc:
        engine.build_parser().parse_args(["once", "--yys", "bad"])
    assert exc.value.code == 2


def test_t32_empty_yys_is_accepted():
    args = engine.build_parser().parse_args(["once", "--yys", ""])
    assert args.yys == ""


def test_t32_out_of_range_port_is_rejected():
    with pytest.raises(SystemExit) as exc:
        engine.build_parser().parse_args(["once", "--port", "70000"])
    assert exc.value.code == 2


def test_t32_dynamic_port_is_accepted():
    args = engine.build_parser().parse_args(["once", "--port", "8612"])
    assert args.port == 8612


def test_t32_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        engine.build_parser().parse_args(["--help"])
    assert exc.value.code == 0


# --------------------------------------------------------------------------- #
# 退出码
# --------------------------------------------------------------------------- #
def _stub_logging(monkeypatch):
    monkeypatch.setattr(engine, "setup_logging", lambda verbose: LOG)
    # F18：once 认证会获取进程级互斥体；测试中替换为直接放行，避免真实系统副作用。
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)


def test_t32_fix_bypass_failure_exits_nonzero(monkeypatch):
    _stub_logging(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["prog", "fix-bypass"])
    monkeypatch.setattr(engine, "direct_session", lambda: object())
    monkeypatch.setattr(engine, "fix_bypass", lambda log: False)
    with pytest.raises(SystemExit) as exc:
        engine.main()
    assert exc.value.code == 1


def test_t32_once_failure_exits_1(monkeypatch):
    _stub_logging(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["prog", "once"])
    monkeypatch.setattr(engine, "direct_session", lambda: object())
    monkeypatch.setattr(engine, "internet_ok", lambda session, probes=None: (False, ""))
    monkeypatch.setattr(engine, "attempt_repair",
                        lambda session, args, log: RepairResult(network_ok=False))
    with pytest.raises(SystemExit) as exc:
        engine.main()
    assert exc.value.code == 1


def test_t32_once_online_is_noop(monkeypatch):
    _stub_logging(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["prog", "once"])
    monkeypatch.setattr(engine, "direct_session", lambda: object())
    monkeypatch.setattr(engine, "internet_ok", lambda session, probes=None: (True, "url"))
    called = []
    monkeypatch.setattr(engine, "attempt_repair", lambda *a: called.append(1))
    engine.main()          # 不应抛 SystemExit
    assert called == [], "在线时仍尝试认证"


def test_t32_invalid_watch_params_fail_before_side_effects(monkeypatch):
    _stub_logging(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["prog", "watch", "--interval", "1"])
    side = []
    monkeypatch.setattr(engine, "direct_session", lambda: side.append("session"))
    with pytest.raises(SystemExit) as exc:
        engine.main()
    assert exc.value.code == 2
    assert side == [], "无效参数在建立会话前未失败"


# --------------------------------------------------------------------------- #
# dry-run 不 POST 且账号脱敏
# --------------------------------------------------------------------------- #
def test_t32_dry_run_never_posts_and_masks_account(monkeypatch, capsys):
    monkeypatch.setattr(engine, "wifi_state", lambda: ("SNNU", "已连接"))
    monkeypatch.setattr(engine, "resolve_port", lambda session, requested: (8601, "fake"))
    monkeypatch.setattr(engine, "portal_reachable", lambda session, port: True)
    monkeypatch.setattr(engine, "get_credentials", lambda: ("123456789", "pw"))

    class _Session:
        def post(self, *a, **k):        # pragma: no cover - 不应被调用
            raise AssertionError("dry-run 不应发送 POST")

    args = argparse.Namespace(port=None, yys="mobile", account_format="plain")
    engine.dry_run(_Session(), args, LOG)
    out = capsys.readouterr().out
    assert "123456789" not in out, "dry-run 报告泄露了完整账号"
    assert "***已隐藏***" in out
    assert "未发送任何认证请求" in out
