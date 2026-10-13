# -*- coding: utf-8 -*-
"""F04 回归用例：代理注册表权限与部分写入（对应 T08）。

依据《审查方案》第 10 节离线复现：
``proxy_failure_return= False proxy_enable_after= 1`` —— 原 ``set_system_proxy()``
仅用 ``KEY_SET_VALUE`` 打开句柄却调用 ``QueryValueEx``；读取权限失败返回 False，
但前面的 ``ProxyEnable=1`` 已经写入（部分写入）。

**本文件在修复前必须失败**（阶段 2 修复后转绿）。
"""
from __future__ import annotations

import pytest

import campus_reconnect_tray as tray

pytestmark = pytest.mark.regression


def test_t08_query_denied_must_not_modify_proxy_enable(monkeypatch, fake_registry):
    """T08：查询权限被拒绝时，不得发生任何部分写入（ProxyEnable 必须保持不变）。"""
    monkeypatch.setattr(tray, "winreg", fake_registry)
    result = tray.set_system_proxy(True)

    assert fake_registry.get("ProxyEnable") == 0, \
        "查询被拒绝，但 ProxyEnable 已被改成 %r（部分写入）" % fake_registry.get("ProxyEnable")
    assert result is False


def test_t08_handle_must_be_opened_with_query_rights(monkeypatch, fake_registry):
    """T08：读快照所用句柄必须具备查询权限，否则无法在写之前校验。"""
    monkeypatch.setattr(tray, "winreg", fake_registry)
    tray.set_system_proxy(True)
    assert fake_registry.open_access, "未记录任何 OpenKey 调用"
    assert all(access & fake_registry.KEY_QUERY_VALUE for access in fake_registry.open_access), \
        "打开句柄时未申请 KEY_QUERY_VALUE，无法在写入前读取快照"
