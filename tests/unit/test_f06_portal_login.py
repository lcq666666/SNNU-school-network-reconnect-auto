# -*- coding: utf-8 -*-
"""F06 登录成功结构化校验（对应 T11 / T12 / T13 / T16）。

阶段 3 修复后由回归集合并入默认集合。修复要点：
- 解析状态页账号字段并**完整比较**（不是子串）；
- 校验允许的状态码与页面结构；
- 区分「已有会话」与本次认证成功；
- 验证码页 / 未知结构 ⇒ 需人工处理；
- GB2312 状态页正确解码。

全程使用假 Session，零真实网络。
"""
from __future__ import annotations

import logging

import campus_reconnect as engine
from campus_core import (E_AUTH_CAPTCHA, E_AUTH_REJECTED,
                         E_PORTAL_STRUCTURE_UNKNOWN, PortalStatus,
                         extract_error_hint, parse_status_page)
from tests.fakes import FakeResponse, FakeSession

LOG = logging.getLogger("campus")
PORT = 8601
LOGIN_JSP = "http://202.117.144.205:8601/snnuportal/login.jsp"
LOGIN_ACTION = "http://202.117.144.205:8601/snnuportal/login"
USERSTATUS = "http://202.117.144.205:8601/snnuportal/userstatus.jsp"


def _session_with_login_response(resp: FakeResponse) -> FakeSession:
    session = FakeSession()
    session.route(LOGIN_JSP, FakeResponse(200, "<html>login</html>"))
    session.route(LOGIN_ACTION, resp)
    return session


# --------------------------------------------------------------------------- #
# T11 / T12：误判修复
# --------------------------------------------------------------------------- #
def test_t11_http500_with_online_marker_must_not_be_success():
    """T11：HTTP 500 + 正文含在线标记，不得判定为成功。"""
    body = "当前登录账号 123456"
    session = _session_with_login_response(FakeResponse(500, body, encoding="utf-8"))
    ok, detail = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert ok is False, "HTTP 500 被判定为登录成功: %r" % detail


def test_t12_account_prefix_must_not_match():
    """T12：提交账号 123456 不得被状态页里的 1234567 子串误匹配。"""
    body = "当前登录账号 1234567"
    session = _session_with_login_response(FakeResponse(200, body, encoding="utf-8"))
    ok, detail = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert ok is False, "账号子串误匹配被判为成功: %r" % detail


def test_t12_form_echo_must_not_be_treated_as_login_success():
    """T12：登录表单回显了提交账号，不得据此判成功。"""
    body = ("<html><body>当前登录账号 999999"
            "<input name='account' value='123456'></body></html>")
    session = _session_with_login_response(FakeResponse(200, body, encoding="utf-8"))
    ok, detail = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert ok is False, "表单回显账号被判为登录成功: %r" % detail


# --------------------------------------------------------------------------- #
# T13：已有会话 + 假新密码
# --------------------------------------------------------------------------- #
def test_t13_existing_session_does_not_validate_new_password():
    """T13：已有会话时不得 POST，也不得声称验证了新密码。"""
    session = FakeSession()
    session.route(LOGIN_JSP, FakeResponse(200, "<html>login</html>"))
    session.route(USERSTATUS, FakeResponse(200, "当前登录账号：123456", encoding="gb2312"))
    outcome = engine.portal_login(session, "123456", "brand-new-pw", "mobile", PORT, LOG)
    ok, detail = outcome
    assert ok is True
    assert outcome.already_authenticated is True
    assert "未验证新凭据" in detail
    assert session.posts() == [], "已有会话仍提交了认证请求"


def test_t13_no_online_marker_does_post():
    """T13 对照：没有在线标记时才提交认证。"""
    body = "当前登录账号 123456"
    session = _session_with_login_response(FakeResponse(200, body, encoding="utf-8"))
    outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert outcome.ok is True
    assert outcome.already_authenticated is False
    assert len(session.posts()) == 1


# --------------------------------------------------------------------------- #
# T16：GB2312 / UTF-8 / 未知结构 / 验证码
# --------------------------------------------------------------------------- #
def test_t16_gb2312_status_page_is_decoded():
    body = "当前登录账号：123456"
    session = _session_with_login_response(FakeResponse(200, body, encoding="gb2312"))
    ok, _detail = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert ok is True, "GB2312 状态页未被正确解码"


def test_t16_captcha_page_needs_user():
    session = _session_with_login_response(
        FakeResponse(200, "请输入验证码后继续", encoding="utf-8"))
    outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert outcome.ok is False
    assert outcome.status is PortalStatus.NEEDS_USER
    assert outcome.error_code == E_AUTH_CAPTCHA


def test_t16_unknown_structure_needs_user():
    """出现登录标记但无账号字段 ⇒ 结构未知 ⇒ 需人工处理，不误报成功。"""
    session = _session_with_login_response(FakeResponse(200, "当前登录账号", encoding="utf-8"))
    outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert outcome.ok is False
    assert outcome.status is PortalStatus.NEEDS_USER
    assert outcome.error_code == E_PORTAL_STRUCTURE_UNKNOWN


def test_t16_navigation_script_words_do_not_trigger_error_classification():
    """导航脚本里的“失败/绑定”等词不得触发错误分类（只在错误区域提取）。"""
    nav_only = "<script>var tip='失败';</script><a>绑定账号</a>"
    assert extract_error_hint(nav_only) is None


def test_t16_real_error_region_is_classified():
    """已识别的错误区域（“错误：密码错误”）应被分类为需人工处理。"""
    body = "<script>var tip='失败';</script><div>错误：密码错误</div>"
    session = _session_with_login_response(FakeResponse(200, body, encoding="utf-8"))
    outcome = engine.portal_login(session, "123456", "pw", "mobile", PORT, LOG)
    assert outcome.ok is False
    assert outcome.status is PortalStatus.NEEDS_USER
    assert outcome.error_code == E_AUTH_REJECTED


# --------------------------------------------------------------------------- #
# F75-12：真机状态页「账号被 <span> 包裹 + 带运营商后缀」导致的验证假阴性
# （P8-4 实测：受控认证**已真正成功**——SNNU 出口 3/3 探测通过、状态页有登录
#  账号——但工具报 E_AUTH_REJECTED/E_SNNU_AUTH_FAILED，needs_user=True）
# --------------------------------------------------------------------------- #
#: 真机 `userstatus.jsp` 片段（2026-10-15 实测抓取，仅账号数字被替换）。
REAL_STATUS_PAGE = (
    '<td width="30%" class="zhengwen">当前登录账号：'
    '<span class="zhengwen1">20210001@mobile</span></td>'
)


def test_f75_12_span_wrapped_account_must_be_parsed():
    """真机形态：账号被 <span> 包裹、且带 @mobile 后缀，必须解析出纯学号。"""
    result = parse_status_page(REAL_STATUS_PAGE, expected_account="20210001")
    assert result.account == "20210001", "未解析出真机账号字段：%r" % result.account
    assert result.status is PortalStatus.ONLINE
    assert result.marker_found is True


def test_f75_12_operator_suffix_is_the_same_account():
    """`学号@运营商`（页面）与 `学号`（plain 提交）指向同一账号，不得判为不一致。"""
    result = parse_status_page(REAL_STATUS_PAGE, expected_account="20210001@mobile")
    assert result.status is PortalStatus.ONLINE


def test_f75_12_another_account_is_still_rejected():
    """安全属性不得后退：页面显示**另一个学号**时仍必须拒绝。"""
    page = REAL_STATUS_PAGE.replace("20210001", "20219999")
    result = parse_status_page(page, expected_account="20210001")
    assert result.status is PortalStatus.NEEDS_USER
    assert result.error_code == E_AUTH_REJECTED


def test_f75_12_marker_without_account_is_structure_unknown_not_other_account():
    """标记后解析不出账号 ⇒ 结构未知；**不得**因截到非账号片段而误判「另一账号」。"""
    page = '<td class="zhengwen">当前登录账号：<span class="zhengwen1"></span></td>'
    result = parse_status_page(page, expected_account="20210001")
    assert result.status is PortalStatus.NEEDS_USER
    assert result.error_code == E_PORTAL_STRUCTURE_UNKNOWN, (
        "把非账号片段当成「另一账号」：account=%r" % result.account)


def test_f75_12_real_page_login_is_reported_success():
    """真机形态下 POST 后的状态页必须被判为成功（P8-4 假阴性的直接回归）。"""
    session = _session_with_login_response(
        FakeResponse(200, REAL_STATUS_PAGE, encoding="utf-8"))
    outcome = engine.portal_login(session, "20210001", "pw", "mobile", PORT, LOG)
    assert outcome.ok is True, "真机状态页被判为失败：%s（%s）" % (
        outcome.detail, outcome.error_code)
    assert outcome.status is PortalStatus.ONLINE


def test_f75_12_real_page_existing_session_is_reused_without_post():
    """真机形态下已有会话必须被识别为同账号在线 ⇒ 不重复提交凭据。"""
    session = FakeSession()
    session.route(LOGIN_JSP, FakeResponse(200, "<html>login</html>"))
    session.route(USERSTATUS, FakeResponse(200, REAL_STATUS_PAGE, encoding="utf-8"))
    outcome = engine.portal_login(session, "20210001", "pw", "mobile", PORT, LOG)
    assert outcome.ok is True
    assert outcome.already_authenticated is True
    assert session.posts() == [], "已有会话仍重复提交了认证请求"
