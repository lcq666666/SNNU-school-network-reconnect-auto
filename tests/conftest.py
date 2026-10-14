"""离线测试公共配置。

默认测试集合约定（全阶段有效）：
- 不访问真实校园网 Portal；
- 不读取真实凭据（Windows 凭据管理器）；
- 不写系统注册表；
- 不启动真实浏览器；
- 不干扰现有托盘进程；
- 不真实 ``sleep``（用 :class:`tests.fakes.VirtualClock`）。

阶段 1 提供的可注入假件见 ``tests/fakes.py``，并通过下列 fixture 暴露：
``fake_registry`` / ``fake_keyring`` / ``fake_session`` / ``fake_launcher`` / ``virtual_clock``。

标记说明：``regression`` 标记的用例记录《审查方案》第 10 节已复现缺陷，
修复前**必须失败**；默认集合通过 ``addopts = -m "not regression"`` 排除它们，
可用 ``pytest -m regression`` 单独运行。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.fakes import (FakeCommandRunner, FakeKeyring, FakeLauncher,
                         FakeRegistry, FakeSession, VirtualClock)

# tests/conftest.py -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def project_root() -> Path:
    """返回项目根目录（只读使用）。"""
    return PROJECT_ROOT


@pytest.fixture
def fake_registry() -> FakeRegistry:
    """假注册表（默认含 ``ProxyEnable=0``，无任何真实写入）。"""
    return FakeRegistry(values={"ProxyEnable": 0})


@pytest.fixture
def fake_keyring() -> FakeKeyring:
    """假凭据后端（默认空，不读取真实凭据）。"""
    return FakeKeyring()


@pytest.fixture
def fake_session() -> FakeSession:
    """假 HTTP Session（无真实连接）。"""
    return FakeSession()


@pytest.fixture
def fake_launcher() -> FakeLauncher:
    """假浏览器启动器（不真实启动进程）。"""
    return FakeLauncher()


@pytest.fixture
def virtual_clock() -> VirtualClock:
    """可推进的虚拟时钟（不真实 sleep）。"""
    return VirtualClock()


@pytest.fixture
def fake_runner() -> FakeCommandRunner:
    """假只读命令执行器（零真实子进程；V0.3 P0 netdiag 用）。"""
    return FakeCommandRunner()


@pytest.fixture(autouse=True)
def _isolate_campus_logger():
    """每个用例前后隔离 ``campus`` logger 的 handler，避免跨用例污染。"""
    logger = logging.getLogger("campus")
    saved = list(logger.handlers)
    logger.handlers.clear()
    yield
    logger.handlers.clear()
    logger.handlers.extend(saved)
