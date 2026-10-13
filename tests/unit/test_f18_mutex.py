# -*- coding: utf-8 -*-
"""F18 单实例互斥体 ABI 与生命周期（对应 T30）。

覆盖：大句柄（指针宽度）、``ERROR_ALREADY_EXISTS``、``NULL`` 句柄 /
``ACCESS_DENIED`` / DLL 异常 ⇒ **fail-closed**；重复实例路径及时 ``CloseHandle``；
不使用 ownership（不误调 ``ReleaseMutex``）；``once`` 认证遵守进程级排他。
全程使用假 kernel32，零真实系统副作用。
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

import campus_reconnect as engine
import campus_windows as cw
from campus_core import E_MUTEX_FAILED, SingleInstanceResult

LOG = logging.getLogger("campus")

BIG_HANDLE = 0x7FFF_FFFF_0000_1234     # 超出 32 位，验证 HANDLE 按指针宽度处理


class FakeKernel32:
    """最小 kernel32 假件：记录 CreateMutexW / CloseHandle 调用。"""

    def __init__(self, handle=BIG_HANDLE, last_error: int = 0,
                 raise_on_create: Exception | None = None,
                 close_result: bool = True) -> None:
        self.handle = handle
        self.last_error = last_error
        self.raise_on_create = raise_on_create
        self.close_result = close_result
        self.created: list = []
        self.closed: list = []

    def CreateMutexW(self, attrs, initial, name):
        if self.raise_on_create is not None:
            raise self.raise_on_create
        self.created.append(name)
        return self.handle

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return self.close_result


def _guard(fake, last_error=None):
    err = fake.last_error if last_error is None else last_error
    return cw.SingleInstanceGuard(kernel32=fake, get_last_error=lambda: err)


# --------------------------------------------------------------------------- #
# ABI / 生命周期
# --------------------------------------------------------------------------- #
def test_t30_big_handle_is_held():
    fake = FakeKernel32(handle=BIG_HANDLE, last_error=0)
    guard = _guard(fake)
    result = guard.acquire()
    assert result.ok is True
    assert result.already_running is False
    assert guard.held is True
    assert guard.handle == BIG_HANDLE      # 未被截断成 32 位
    assert fake.created == [cw.MUTEX_NAME]


def test_t30_already_exists_closes_new_handle():
    fake = FakeKernel32(handle=BIG_HANDLE, last_error=cw.ERROR_ALREADY_EXISTS)
    guard = _guard(fake)
    result = guard.acquire()
    assert result.already_running is True
    assert result.ok is False
    assert guard.held is False
    assert fake.closed == [BIG_HANDLE]     # 重复实例路径及时关闭，不泄漏


def test_t30_null_handle_fails_closed():
    fake = FakeKernel32(handle=0, last_error=0)
    guard = _guard(fake)
    result = guard.acquire()
    assert result.ok is False and result.already_running is False
    assert result.failed_closed is True
    assert result.error_code == E_MUTEX_FAILED
    assert guard.held is False


def test_t30_access_denied_fails_closed():
    fake = FakeKernel32(handle=0, last_error=cw.ERROR_ACCESS_DENIED)
    result = _guard(fake).acquire()
    assert result.failed_closed is True
    assert result.error_code == E_MUTEX_FAILED


def test_t30_create_exception_fails_closed():
    fake = FakeKernel32(raise_on_create=OSError("kernel32 加载失败"))
    result = _guard(fake).acquire()
    assert result.failed_closed is True
    assert result.error_code == E_MUTEX_FAILED


def test_t30_release_closes_handle_without_releasemutex():
    fake = FakeKernel32(handle=BIG_HANDLE, last_error=0)
    guard = _guard(fake)
    guard.acquire()
    guard.release()
    assert guard.held is False
    assert fake.closed == [BIG_HANDLE]
    # 假件未定义 ReleaseMutex：若被调用会 AttributeError，即验证未使用 ownership 语义
    assert not hasattr(fake, "ReleaseMutex")


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 需要校验 WinAPI ABI")
def test_t30_real_kernel32_declares_abi():
    from ctypes import wintypes
    kernel = cw.SingleInstanceGuard()._kernel()
    assert kernel.CreateMutexW.argtypes == [wintypes.LPVOID, wintypes.BOOL,
                                            wintypes.LPCWSTR]
    assert kernel.CreateMutexW.restype is wintypes.HANDLE
    assert kernel.CloseHandle.restype is wintypes.BOOL


# --------------------------------------------------------------------------- #
# 引擎 / CLI 集成（fail-closed）
# --------------------------------------------------------------------------- #
class _StubGuard:
    def __init__(self, result: SingleInstanceResult) -> None:
        self._result = result
        self.released = False

    def acquire(self) -> SingleInstanceResult:
        return self._result

    def release(self) -> None:
        self.released = True


def _install_guard(monkeypatch, result, made=None):
    class G(_StubGuard):
        def __init__(self, **kwargs):
            super().__init__(result)
            if made is not None:
                made["guard"] = self

    monkeypatch.setattr(engine.os, "name", "nt")
    monkeypatch.setattr(engine, "SingleInstanceGuard", G)


def test_t30_engine_success_holds_guard(monkeypatch):
    made = {}
    _install_guard(monkeypatch, SingleInstanceResult(ok=True), made)
    assert engine.ensure_single_instance(LOG) is True
    assert engine._SINGLE_INSTANCE_GUARD is made["guard"]


def test_t30_engine_already_running_refuses(monkeypatch):
    _install_guard(monkeypatch, SingleInstanceResult(already_running=True))
    assert engine.ensure_single_instance(LOG) is False


def test_t30_engine_create_failure_fails_closed(monkeypatch):
    _install_guard(monkeypatch, SingleInstanceResult(error_code=E_MUTEX_FAILED,
                                                     detail="boom"))
    assert engine.ensure_single_instance(LOG) is False


def test_t30_engine_non_windows_always_ok(monkeypatch):
    monkeypatch.setattr(engine.os, "name", "posix")
    assert engine.ensure_single_instance(LOG) is True


def test_t30_once_refuses_when_mutex_unavailable(monkeypatch):
    """once 认证与 watch/托盘共用互斥体：拿不到即拒绝，退出码 1。"""
    monkeypatch.setattr(engine, "setup_logging", lambda verbose: LOG)
    monkeypatch.setattr(sys, "argv", ["prog", "once"])
    monkeypatch.setattr(engine, "direct_session", lambda: object())
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: False)
    with pytest.raises(SystemExit) as exc:
        engine.main()
    assert exc.value.code == 1
