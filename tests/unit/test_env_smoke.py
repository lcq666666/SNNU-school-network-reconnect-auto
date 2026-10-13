"""阶段 0 环境冒烟测试。

用途：验证隔离 venv 已就绪、运行/托盘/开发依赖可导入、依赖清单存在且可解析、
.gitignore 覆盖关键产物。

约束：本文件只做只读检查，不产生任何真实网络 / 注册表 / 凭据 / 浏览器副作用。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

RUNTIME_PACKAGES = ["requests", "keyring"]
TRAY_PACKAGES = ["pystray", "PIL"]
DEV_PACKAGES = ["pytest", "pytest_cov"]


def _missing_modules(names):
    """返回无法 import 的模块名列表。"""
    return [name for name in names if importlib.util.find_spec(name) is None]


def _parse_requirements(path: Path) -> set:
    """解析 requirements 文件，返回归一化后的包名集合（小写）。"""
    packages = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0].strip().lower()
        if name:
            packages.add(name)
    return packages


def test_runtime_dependencies_importable():
    """CLI 核心运行依赖可导入。"""
    assert _missing_modules(RUNTIME_PACKAGES) == []


def test_tray_dependencies_importable():
    """托盘附加依赖可导入。"""
    assert _missing_modules(TRAY_PACKAGES) == []


def test_dev_dependencies_importable():
    """开发/测试依赖可导入。"""
    assert _missing_modules(DEV_PACKAGES) == []


def test_ruff_cli_available():
    """ruff 可执行文件存在于当前解释器所在 Scripts 目录。"""
    scripts_dir = Path(sys.executable).parent
    exe = "ruff.exe" if os.name == "nt" else "ruff"
    assert (scripts_dir / exe).exists(), f"ruff 未安装于 {scripts_dir}"


def test_requirements_files_exist(project_root: Path):
    """运行与开发依赖清单文件均存在。"""
    for name in ("requirements.txt", "requirements-dev.txt"):
        assert (project_root / name).is_file(), f"缺少 {name}"


def test_requirements_declare_expected_packages(project_root: Path):
    """依赖清单声明了预期的包。"""
    runtime = _parse_requirements(project_root / "requirements.txt")
    assert {"requests", "keyring", "pystray", "pillow"} <= runtime
    dev = _parse_requirements(project_root / "requirements-dev.txt")
    assert {"pytest", "pytest-cov", "ruff", "pyinstaller"} <= dev


def test_gitignore_covers_env_and_caches(project_root: Path):
    """.gitignore 覆盖隔离环境与测试/静态检查缓存。"""
    content = (project_root / ".gitignore").read_text(encoding="utf-8")
    for entry in (".venv/", ".pytest_cache/", ".ruff_cache/"):
        assert entry in content, f".gitignore 缺少 {entry}"
