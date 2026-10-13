# -*- coding: utf-8 -*-
"""F16 端口解析与 Portal 身份验证/预算（对应 T28）。

要点：解析 Location 与 JS 模式并验证为学校 Portal；支持动态有效端口；
全失败返回失败（不假装已选端口）；失败路径无重复回退请求。
"""
from __future__ import annotations

import campus_reconnect as engine
from campus_core import E_PORTAL_UNREACHABLE
from tests.fakes import FakeResponse, FakeSession

ROOT = engine.PORTAL_ROOT


def test_t28_location_302_resolves_port():
    session = FakeSession()
    session.route(ROOT, FakeResponse(302, "", headers={
        "Location": "http://202.117.144.205:8603/snnuportal/login.jsp"}))
    resolution = engine.resolve_portal(session)
    assert resolution.port == 8603
    assert "Location" in resolution.source


def test_t28_js_jump_resolves_known_port():
    session = FakeSession()
    session.route(ROOT, FakeResponse(
        200, "<script>location='http://202.117.144.205:8605/snnuportal/login.jsp';</script>"))
    resolution = engine.resolve_portal(session)
    assert resolution.port == 8605
    assert "JS" in resolution.source


def test_t28_dynamic_new_port_is_supported():
    """新端口（不在历史列表内）也应被接受。"""
    session = FakeSession()
    session.route(ROOT, FakeResponse(
        200, "<script>location=':8612/snnuportal/login.jsp';</script>"))
    resolution = engine.resolve_portal(session)
    assert resolution.port == 8612


def test_t28_all_fail_returns_failure_without_fake_port():
    session = FakeSession()
    session.route(ROOT, FakeResponse(200, "<html>no portal pattern here</html>"))
    resolution = engine.resolve_portal(session)
    assert resolution.port is None
    assert resolution.ok is False
    assert resolution.error == E_PORTAL_UNREACHABLE


def test_t28_root_page_is_fetched_once():
    """回退不重复同一无效请求（根页面只取一次）。"""
    session = FakeSession()
    session.route(ROOT, FakeResponse(200, "<html>nothing</html>"))
    engine.resolve_portal(session)
    root_gets = [c for c in session.calls if c["url"] == ROOT]
    assert len(root_gets) == 1


def test_t28_requested_port_out_of_range_is_rejected():
    session = FakeSession()
    resolution = engine.resolve_portal(session, requested=70000)
    assert resolution.port is None
    assert resolution.error == E_PORTAL_UNREACHABLE


def test_t28_requested_reachable_port_is_used():
    session = FakeSession()
    session.route(engine.portal_base(8612) + engine.LOGIN_JSP, FakeResponse(200, "<html>login</html>"))
    resolution = engine.resolve_portal(session, requested=8612)
    assert resolution.port == 8612
    assert "手动指定" in resolution.source


def test_t28_resolve_port_wrapper_returns_none_on_failure():
    session = FakeSession()
    session.route(ROOT, FakeResponse(200, "<html>nothing</html>"))
    port, reason = engine.resolve_port(session, None)
    assert port is None
    assert reason


def test_t28_response_body_size_is_limited():
    session = FakeSession()
    huge = "<html>" + ("x" * 5000) + ":8613/snnuportal/login.jsp"
    session.route(ROOT, FakeResponse(200, huge))
    body = engine.decode_body(FakeResponse(200, huge), limit=64)
    assert len(body) <= 64
