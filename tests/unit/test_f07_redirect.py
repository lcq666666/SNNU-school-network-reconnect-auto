# -*- coding: utf-8 -*-
"""F07 认证请求目标验证与重定向控制（对应 T14 / T15）。

要点：
- 认证 POST 禁止自动重定向（``allow_redirects=False``）；
- 302/303 以**不带凭据**的 GET 获取状态页；
- 307/308 默认拒绝（避免凭据重放）；跨 origin / 越界路径 / 循环跳转一律拒绝；
- 未允许目标收到的凭据请求数为 0。

全程假 Session，零真实网络。
"""
from __future__ import annotations

import logging

import requests

import campus_reconnect as engine
from campus_core import check_redirect_target
from tests.fakes import FakeResponse, FakeSession

LOG = logging.getLogger("campus")
PORT = 8601
BASE = "http://202.117.144.205:8601"
LOGIN_JSP = BASE + "/snnuportal/login.jsp"
LOGIN_ACTION = BASE + "/snnuportal/login"
USERSTATUS = BASE + "/snnuportal/userstatus.jsp"
STATUS_BODY = "当前登录账号：123456"


def _session(post_response: FakeResponse, status_route=None) -> FakeSession:
    """``status_route`` 可为响应、异常或列表（按调用顺序弹出）。

    预检 GET 与重定向后的 GET 共用同一 URL，因此通常传
    ``[ConnectionError, FakeResponse]`` 让预检失败、重定向后成功。
    """
    session = FakeSession()
    session.route(LOGIN_JSP, FakeResponse(200, "<html>login</html>"))
    session.route(LOGIN_ACTION, post_response)
    if status_route is not None:
        session.route(USERSTATUS, status_route)
    return session


def test_t14_302_follows_without_credentials():
    """T14：302 指向可信状态页时，用不带凭据的 GET 获取。"""
    session = _session(
        FakeResponse(302, "", headers={"Location": USERSTATUS}),
        [requests.ConnectionError("pre-check unavailable"),
         FakeResponse(200, STATUS_BODY, encoding="gb2312")])
    outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert outcome.ok is True
    assert session.posts(), "未提交认证请求"
    post = session.posts()[0]
    assert post["kwargs"].get("allow_redirects") is False, "认证 POST 未禁止自动重定向"
    gets = [c for c in session.calls if c["method"] == "GET" and c["url"] == USERSTATUS]
    assert gets, "未以 GET 获取状态页"
    for call in gets:
        assert "data" not in call["kwargs"], "状态页 GET 携带了表单凭据"


def test_t15_307_308_replay_is_rejected():
    """T15：307/308 默认拒绝重放凭据。"""
    for code in (307, 308):
        session = _session(FakeResponse(code, "", headers={"Location": USERSTATUS}))
        outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
        assert outcome.ok is False, "%d 被接受了" % code
        assert "重放" in outcome.detail or "重定向" in outcome.detail


def test_t15_cross_origin_redirect_is_rejected():
    """T15：跨 host/port 的重定向一律拒绝，且不向该目标发任何请求。"""
    evil = "http://evil.example.com:8601/snnuportal/userstatus.jsp"
    session = _session(FakeResponse(302, "", headers={"Location": evil}))
    outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert outcome.ok is False
    assert not [c for c in session.calls if "evil.example.com" in c["url"]], \
        "向未允许目标发起了请求"


def test_t15_out_of_scope_path_is_rejected():
    session = _session(FakeResponse(302, "", headers={"Location": BASE + "/other.jsp"}))
    outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert outcome.ok is False


def test_t15_redirect_loop_is_rejected():
    """状态页再次跳转 ⇒ 判定循环/非预期重定向。"""
    session = _session(
        FakeResponse(302, "", headers={"Location": USERSTATUS}),
        [requests.ConnectionError("pre-check unavailable"),
         FakeResponse(302, "", headers={"Location": USERSTATUS})])
    outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert outcome.ok is False


def test_check_redirect_target_allows_same_origin_relative():
    ok, target, _err = check_redirect_target("/snnuportal/userstatus.jsp", BASE,
                                             allowed_paths=("/snnuportal/userstatus.jsp",))
    assert ok is True
    assert target == USERSTATUS
