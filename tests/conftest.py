"""离线测试公共配置（阶段 0 最小骨架）。

默认测试集合约定（全阶段有效）：
- 不访问真实校园网 Portal；
- 不读取真实凭据（Windows 凭据管理器）；
- 不写系统注册表；
- 不启动真实浏览器；
- 不干扰现有托盘进程。

阶段 1 将在此扩展 FakeRegistry / FakeKeyring / FakeSession / FakeLauncher /
VirtualClock 等可注入假件。
"""
from __future__ import annotations

from pathlib import Path

import pytest

# tests/conftest.py -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def project_root() -> Path:
    """返回项目根目录（只读使用）。"""
    return PROJECT_ROOT
