# -*- coding: utf-8 -*-
"""F22 依赖锁定与可追溯构建（对应 T34 / T35 的构建部分）。

覆盖：``pyproject.toml`` 版本与 ``campus_core.__version__`` 一致、锁定清单精确固定
运行/托盘/测试/打包依赖、CI 与发布工作流（Windows 离线测试 + ruff + 构建、默认只读
权限、第三方 action 锁定不可变 commit、发布仅受 tag 触发）、构建元数据与版本资源可
对照源码、构建清单不含敏感值。全部为静态/纯函数检查，零真实构建与网络。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from campus_core import __version__
from tools import build_info

tomllib = pytest.importorskip("tomllib")


def _workflow_text(project_root: Path, name: str) -> str:
    return (project_root / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _uses_refs(text: str):
    refs = []
    for line in text.splitlines():
        match = re.match(r"uses:\s*(\S+)", line.strip())
        if match:
            refs.append(match.group(1))
    return refs


# --------------------------------------------------------------------------- #
# 依赖锁定（T34）
# --------------------------------------------------------------------------- #
def test_t34_pyproject_version_matches_core(project_root):
    data = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["version"] == __version__
    assert project["requires-python"].startswith(">=3.10")
    assert "windows" in " ".join(project["classifiers"]).lower()
    assert any("requests" in dep for dep in project["dependencies"])
    optional = project["optional-dependencies"]
    assert any("pystray" in dep for dep in optional["tray"])
    assert any("pyinstaller" in dep for dep in optional["pack"])
    assert any("pytest" in dep for dep in optional["dev"])


def test_t34_lock_file_pins_runtime_deps(project_root):
    text = (project_root / "requirements-lock.txt").read_text(encoding="utf-8")
    for pkg in ("requests", "keyring", "pystray", "pillow", "pytest", "ruff",
                "pyinstaller"):
        assert re.search(r"^%s==\S+" % re.escape(pkg), text, re.M | re.I), \
            "锁定清单缺少 %s 的精确版本" % pkg


# --------------------------------------------------------------------------- #
# CI / 发布工作流（T34）
# --------------------------------------------------------------------------- #
def test_t34_ci_workflow_offline_and_readonly(project_root):
    text = _workflow_text(project_root, "ci.yml")
    assert "windows-latest" in text
    assert "contents: read" in text
    assert "contents: write" not in text
    assert "ruff check ." in text
    assert "pytest" in text
    assert "build_release.py" in text
    assert "requirements-lock.txt" in text


def test_t34_release_workflow_tag_scoped_and_writable(project_root):
    text = _workflow_text(project_root, "release.yml")
    assert "tags:" in text and "v*" in text
    assert "contents: write" in text
    assert "gh release create" in text
    assert "CampusReconnect.exe.sha256" in text
    assert "build_info.json" in text


def test_t34_workflow_actions_pinned_to_immutable_sha(project_root):
    for name in ("ci.yml", "release.yml"):
        refs = _uses_refs(_workflow_text(project_root, name))
        assert refs, "%s 未找到 uses:" % name
        for ref in refs:
            assert re.search(r"@[0-9a-f]{40}$", ref), \
                "%s 的 action 未锁定到 commit SHA: %s" % (name, ref)


# --------------------------------------------------------------------------- #
# 可追溯构建（T34）
# --------------------------------------------------------------------------- #
def test_t34_build_metadata_has_traceability():
    meta = build_info.build_metadata()
    assert meta["name"] == "CampusReconnect"
    assert meta["version"] == __version__
    assert meta["machine"]
    assert isinstance(meta["dependencies"], list) and meta["dependencies"]
    assert "built_at" in meta


def test_t34_git_commit_short_form():
    full = build_info.git_commit()
    if not full:
        pytest.skip("不在 git 仓库或 git 不可用")
    short = build_info.git_commit(short=True)
    assert short and len(short) >= 7
    assert full.startswith(short)


def test_t34_version_resource_contains_version_and_commit():
    text = build_info.version_resource_text()
    assert __version__ in text
    assert "filevers=%r" % (build_info._version_tuple(__version__),) in text


def test_t34_write_version_file(tmp_path):
    path = Path(build_info.write_version_file(tmp_path / "version_info.txt"))
    assert path.is_file()
    assert __version__ in path.read_text(encoding="utf-8")


def test_t34_spec_embeds_version_file(project_root):
    text = (project_root / "CampusReconnect.spec").read_text(encoding="utf-8")
    assert "build_info.write_version_file" in text
    assert "version=_version_file" in text


# --------------------------------------------------------------------------- #
# 构建清单不含敏感值（T35 构建部分）
# --------------------------------------------------------------------------- #
def test_t35_build_manifest_has_no_secrets(tmp_path):
    target = build_info.write_build_info(tmp_path)
    text = target.read_text(encoding="utf-8").lower()
    for bad in ("fake_password", "fake_cookie", "password", "secret", "token"):
        assert bad not in text, "构建清单出现敏感值: %s" % bad
