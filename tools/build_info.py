# -*- coding: utf-8 -*-
"""构建可追溯性工具（F22）。

把「版本 / 源码 commit / Python 与架构 / 依赖清单」写入构建产物旁边的
``build_info.json``，并生成 PyInstaller 的 Windows 版本资源文件。

安全约定：**不注入任何用户凭据，不收集环境变量值、日志或配置**。
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

__all__ = ["PROJECT_ROOT", "git_commit", "project_version", "runtime_dependencies",
           "build_metadata", "write_build_info", "version_resource_text",
           "write_version_file"]


def git_commit(short: bool = False) -> str:
    """返回当前源码 commit（取不到返回空串，绝不抛异常）。"""
    args = ["git", "rev-parse", "--short", "HEAD"] if short else \
        ["git", "rev-parse", "HEAD"]
    try:
        done = subprocess.run(args, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", "replace").strip()


def project_version() -> str:
    """读取应用版本（唯一来源 ``campus_core.__version__``）。"""
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from campus_core import __version__
    return __version__


def runtime_dependencies() -> List[str]:
    """解析 ``requirements.txt`` 中的运行依赖（忽略注释与 ``-r`` 行）。"""
    deps: List[str] = []
    try:
        lines = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return deps
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        deps.append(line)
    return deps


def build_metadata() -> Dict[str, object]:
    """构造构建元数据（**不含任何凭据 / 环境变量值**）。"""
    return {
        "name": "CampusReconnect",
        "version": project_version(),
        "commit": git_commit(),
        "commit_short": git_commit(short=True),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dependencies": runtime_dependencies(),
    }


def write_build_info(directory) -> Path:
    """把构建元数据写入 ``<directory>/build_info.json``，返回其路径。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "build_info.json"
    target.write_text(json.dumps(build_metadata(), ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


def _version_tuple(version: str) -> Tuple[int, int, int, int]:
    """把 ``"0.2.0"`` 转成 Windows 版本资源所需的 4 元组。"""
    parts: List[int] = []
    for chunk in str(version).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return (parts[0], parts[1], parts[2], parts[3])


def version_resource_text() -> str:
    """生成 PyInstaller ``--version-file`` 的 Windows 版本资源文本。"""
    version = project_version()
    quad = _version_tuple(version)
    commit = git_commit(short=True)
    description = "SNNU 校园网自动重连助手"
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'lcq666666'),
        StringStruct('FileDescription', '{description}'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'CampusReconnect'),
        StringStruct('OriginalFilename', 'CampusReconnect.exe'),
        StringStruct('ProductName', '{description}'),
        StringStruct('ProductVersion', '{version}'),
        StringStruct('Comments', 'commit {commit}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def write_version_file(path) -> str:
    """写出 PyInstaller 版本资源文件，返回其路径字符串。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version_resource_text(), encoding="utf-8")
    return str(path)
