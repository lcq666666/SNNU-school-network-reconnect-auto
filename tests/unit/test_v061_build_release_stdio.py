# -*- coding: utf-8 -*-
"""V0.6.1：构建脚本在 Windows 控制台（cp1252）上也能正常输出中文。

约束：不跑 pytest / PyInstaller（只测 stdio 重配本身），零真实网络。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import build_release

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _cp1252_stream():
    """返回 (原始字节缓冲, cp1252 文本流)，模拟 Windows 控制台 stdio。"""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)
    return raw, stream


def test_ensure_utf8_stdio_reconfigures_cp1252_stream():
    """cp1252 流重配后，中文按 UTF-8 输出，不报 UnicodeEncodeError。"""
    raw, stream = _cp1252_stream()
    old = sys.stdout
    sys.stdout = stream
    try:
        build_release._ensure_utf8_stdio()
        assert stream.encoding == "utf-8"
        print("测试中文")
    finally:
        sys.stdout = old
    assert raw.getvalue().decode("utf-8") == "测试中文\n"


def test_ensure_utf8_stdio_ignores_streams_without_reconfigure():
    """没有 reconfigure 的流（如 StringIO）直接跳过，不报错。"""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        build_release._ensure_utf8_stdio()
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def test_build_release_main_help_prints_chinese_under_cp1252(monkeypatch):
    """main() 开篇重配后，--help 的中文说明在 cp1252 流上正常输出。"""
    raw, stream = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    with pytest.raises(SystemExit) as exc:
        build_release.main(["--help"])
    assert exc.value.code == 0
    assert "受控构建" in raw.getvalue().decode("utf-8")


def test_build_release_stdio_real_interpreter_under_cp1252():
    """真实解释器 stdio 为 cp1252 时，重配后中文输出仍是 UTF-8。"""
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    code = ("from tools import build_release;"
            "build_release._ensure_utf8_stdio();"
            "print('测试中文')")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT_ROOT),
                          capture_output=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert done.stdout.decode("utf-8") == "测试中文\n"
