# -*- coding: utf-8 -*-
"""F06 回归用例：登录成功判定过宽（对应 T11 / T12）。

依据《审查方案》第 10 节离线复现：
``login_http500_account_substring= (True, 'Portal 确认已在线（账号出现在状态页）')``
—— 原 ``portal_login()`` 仅判断 ``ONLINE_MARKER in body and account in body``，
既不校验 HTTP 状态，也不做账号完整比较。

**本文件在修复前必须失败**（阶段 3 修复后转绿）。
"""
from __future__ import annotations

import logging

import pytest

import campus_reconnect as engine
from tests.fakes import FakeResponse, FakeSession

pytestmark = pytest.mark.regression

LOG = logging.getLogger("campus")
PORT = 8601
LOGIN_JSP = "http://202.117.144.205:8601/snnuportal/login.jsp"
LOGIN_ACTION = "http://202.117.144.205:8601/snnuportal/login"


def _session_with_login_response(resp: FakeResponse) -> FakeSession:
    session = FakeSession()
    session.route(LOGIN_JSP, FakeResponse(200, "<html>login</html>"))
    session.route(LOGIN_ACTION, resp)
    return session


def test_t11_http500_with_online_marker_must_not_be_success():
    """T11：HTTP 500 + 正文含在线标记，不得判定为成功（当前误报成功）。"""
    body = "当前登录账号 123456"
    session = _session_with_login_response(FakeResponse(500, body, encoding="utf-8"))
    ok, detail = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert ok is False, "HTTP 500 被判定为登录成功: %r" % detail


def test_t12_account_prefix_must_not_match():
    """T12：提交账号 123456 不得被状态页里的 1234567 子串误匹配（当前通过）。"""
    body = "当前登录账号 1234567"
    session = _session_with_login_response(FakeResponse(200, body, encoding="utf-8"))
    ok, detail = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert ok is False, "账号子串误匹配被判为成功: %r" % detail


def test_t12_form_echo_must_not_be_treated_as_login_success():
    """T12：登录表单回显了提交账号，不得据此判成功（当前误报成功）。"""
    body = ("<html><body>当前登录账号 999999"
            "<input name='account' value='123456'></body></html>")
    session = _session_with_login_response(FakeResponse(200, body, encoding="utf-8"))
    ok, detail = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert ok is False, "表单回显账号被判为登录成功: %r" % detail
