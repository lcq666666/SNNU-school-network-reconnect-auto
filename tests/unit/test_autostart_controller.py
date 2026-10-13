# -*- coding: utf-8 -*-
"""开机自启适配器单元测试（阶段 2，F12 前置 / 阶段 4 F21 的基础）。

覆盖 ``AutostartController``：读取命令内容（而非仅判断键是否存在）、
核对是否指向当前安装、启用/停用与失败反馈。

约束：全部使用 :class:`tests.fakes.FakeRegistry`，不写真实注册表。
"""
from __future__ import annotations

from campus_windows import AutostartController
from tests.fakes import FakeRegistry

RUN_VALUE = "CampusReconnectTray"
CURRENT_CMD = '"C:/app/CampusReconnect.exe"'


def _controller(registry, command=CURRENT_CMD):
    return AutostartController(registry, command_provider=lambda: command,
                               value_name=RUN_VALUE)


def test_disabled_when_value_absent():
    registry = FakeRegistry()
    controller = _controller(registry)
    assert controller.read_command() is None
    assert controller.is_enabled() is False
    assert controller.matches_current() is False


def test_reads_existing_command_content():
    registry = FakeRegistry(values={RUN_VALUE: '"C:/old/CampusReconnect.exe"'})
    controller = _controller(registry)
    assert controller.is_enabled() is True
    assert controller.matches_current() is False   # 指向旧安装，不算“已正确开启”


def test_matches_current_install():
    registry = FakeRegistry(values={RUN_VALUE: CURRENT_CMD})
    assert _controller(registry).matches_current() is True


def test_enable_writes_current_command():
    registry = FakeRegistry()
    assert _controller(registry).set_enabled(True) is True
    assert registry.get(RUN_VALUE) == CURRENT_CMD


def test_disable_removes_value():
    registry = FakeRegistry(values={RUN_VALUE: CURRENT_CMD})
    assert _controller(registry).set_enabled(False) is True
    assert registry.get(RUN_VALUE) is None
    assert registry.deletes == [RUN_VALUE]


def test_disable_when_absent_is_ok():
    registry = FakeRegistry()
    assert _controller(registry).set_enabled(False) is True
    assert registry.deletes == []


def test_write_failure_is_reported():
    registry = FakeRegistry(deny_set=True)
    assert _controller(registry).set_enabled(True) is False


def test_open_failure_is_reported():
    registry = FakeRegistry(deny_open=True)
    controller = _controller(registry)
    assert controller.read_command() is None
    assert controller.set_enabled(True) is False
