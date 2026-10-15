# -*- coding: utf-8 -*-
"""F08 探测与网络/认证状态分类（对应 T17 / T18）。

要点：
- 探测严格匹配响应结构：任意 200 正文含 Success **不**通过；
- 失败达阈值后区分非目标网络 / Wi-Fi 断开 / Portal 不可达 / 认证失效 /
  已认证但公网异常 / 探测不可信；
- 未知结构 ⇒ 需人工处理（不保证一定是改版）。

全程假 Session，零真实网络。
"""
from __future__ import annotations

import logging

import requests

import campus_reconnect as engine
from campus_core import (PROBE_EXACT, PROBE_HTML_TITLE, PortalStatus,
                         ProbeResult, ProbeSpec, StatusPageResult,
                         classify_network, probe_matches)

LOG = logging.getLogger("campus")
#: 默认探测点的首/末项（探测点集合属于契约，用例不写死具体 URL）。
FIRST_PROBE = engine.DEFAULT_PROBES[0].url
LAST_PROBE = engine.DEFAULT_PROBES[-1].url


def _matching_response(spec):
    """按探测点的匹配模式造一个**严格匹配**的假响应（供抗抖动用例复用）。"""
    from tests.fakes import FakeResponse
    if spec.mode == PROBE_HTML_TITLE:
        return FakeResponse(200, "<HTML><HEAD><TITLE>%s</TITLE></HEAD></HTML>"
                                 % spec.expect)
    return FakeResponse(200, spec.expect)


# --------------------------------------------------------------------------- #
# T18：严格匹配
# --------------------------------------------------------------------------- #
def test_t18_probe_matches_rejects_free_text_success():
    spec = ProbeSpec("http://probe.invalid/index.html", "Success", PROBE_HTML_TITLE)
    assert probe_matches(spec, 200, "yay Success here") is False
    assert probe_matches(spec, 200, "<title>Success</title>") is True


def test_t18_probe_matches_requires_200_and_no_redirect():
    spec = ProbeSpec("http://probe.invalid/exact.txt", "Microsoft Connect Test",
                     PROBE_EXACT)
    assert probe_matches(spec, 200, "Microsoft Connect Test") is True
    assert probe_matches(spec, 200, "Microsoft Connect Test ", redirected=True) is False
    assert probe_matches(spec, 302, "Microsoft Connect Test") is False


def test_t18_internet_ok_rejects_arbitrary_200_with_success(monkeypatch):
    from tests.fakes import FakeResponse, FakeSession
    session = FakeSession()
    for spec in engine.DEFAULT_PROBES:
        session.route(spec.url, FakeResponse(200, "totally unrelated Success page"))
    ok, _ = engine.internet_ok(session)
    assert ok is False, "任意 200 含 Success 被错误认可"


def test_t18_internet_ok_accepts_structured_response(monkeypatch):
    from tests.fakes import FakeSession
    spec = engine.DEFAULT_PROBES[0]
    session = FakeSession()
    session.route(spec.url, _matching_response(spec))
    ok, via = engine.internet_ok(session)
    assert ok is True
    assert via == spec.url


def test_probe_online_records_error_kinds():
    from tests.fakes import FakeSession
    session = FakeSession()
    session.route(FIRST_PROBE, requests.ConnectTimeout("timed out"))
    session.route(LAST_PROBE, requests.ConnectionError("getaddrinfo failed for host"))
    results = engine.probe_online(session)
    kinds = {r.error for r in results}
    assert "timeout" in kinds and "dns" in kinds


# --------------------------------------------------------------------------- #
# T17：分类
# --------------------------------------------------------------------------- #
def _failing():
    return [ProbeResult(False, FIRST_PROBE, error="connection"),
            ProbeResult(False, LAST_PROBE, error="connection")]


def test_t17_any_probe_ok_is_online():
    results = [ProbeResult(False, FIRST_PROBE, error="connection"),
               ProbeResult(True, LAST_PROBE, status_code=200)]
    assert classify_network("SNNU", "SNNU", results) is PortalStatus.ONLINE


def test_t17_portal_online_but_public_unreachable():
    portal = StatusPageResult(PortalStatus.ONLINE, account="123456")
    assert classify_network("SNNU", "SNNU", _failing(), portal) \
        is PortalStatus.AUTHENTICATED_DEGRADED


def test_t17_auth_required():
    portal = StatusPageResult(PortalStatus.AUTH_REQUIRED)
    assert classify_network("SNNU", "SNNU", _failing(), portal) \
        is PortalStatus.AUTH_REQUIRED


def test_t17_needs_user():
    portal = StatusPageResult(PortalStatus.NEEDS_USER)
    assert classify_network("SNNU", "SNNU", _failing(), portal) \
        is PortalStatus.NEEDS_USER


def test_t17_probe_unreliable_is_suspect():
    assert classify_network("SNNU", "SNNU", _failing()) is PortalStatus.SUSPECT


def test_t17_non_target_network():
    assert classify_network("SNNU-Other", "SNNU", _failing()) is PortalStatus.NON_TARGET


def test_t17_wifi_disconnected():
    assert classify_network(None, "SNNU", _failing()) is PortalStatus.WIFI_DISCONNECTED


# --------------------------------------------------------------------------- #
# F75-10：只读状态页对"跳转到登录页"的判定（真机实测：302 → login.jsp）
# --------------------------------------------------------------------------- #
def _status_url(port=8601):
    return engine.portal_base(port) + engine.USERSTATUS_JSP


def test_f75_10_status_page_redirect_to_login_means_auth_required():
    """未认证链路上 userstatus.jsp 会 302 到 login.jsp ⇒ 应判「需要认证」。"""
    from tests.fakes import FakeResponse, FakeSession
    session = FakeSession()
    session.route(_status_url(), FakeResponse(
        302, "", headers={"Location": engine.portal_base(8601) + engine.LOGIN_JSP}))
    page = engine.read_status_page(session, 8601)
    assert page is not None, "跳转到登录页被当成「不可解析」，丢失了需要认证的信号"
    assert page.status is PortalStatus.AUTH_REQUIRED
    assert page.marker_found is False


def test_f75_10_redirect_path_sends_no_credentials():
    from tests.fakes import FakeResponse, FakeSession
    session = FakeSession()
    session.route(_status_url(), FakeResponse(
        302, "", headers={"Location": engine.portal_base(8601) + engine.LOGIN_JSP}))
    engine.read_status_page(session, 8601)
    assert session.posts() == [], "只读识别绝不应发出 POST 凭据"
    assert len(session.calls) == 1, "判明跳转后不应再跟随请求"


def test_f75_10_untrusted_redirect_stays_unparsed():
    """非同源 / 非 Portal 流程内的跳转不得被臆断为需要认证（fail-closed）。"""
    from tests.fakes import FakeResponse, FakeSession
    for location in ("http://evil.example/snnuportal/login.jsp",
                     engine.portal_base(8601) + "/snnuportal/other.jsp"):
        session = FakeSession()
        session.route(_status_url(), FakeResponse(302, "", headers={"Location": location}))
        assert engine.read_status_page(session, 8601) is None, location


def test_f75_10_plain_200_without_marker_is_auth_required():
    from tests.fakes import FakeResponse, FakeSession
    session = FakeSession()
    session.route(_status_url(), FakeResponse(200, "<html>hello</html>"))
    page = engine.read_status_page(session, 8601)
    assert page is not None and page.status is PortalStatus.AUTH_REQUIRED


def test_diagnose_network_uses_probe_and_status_page(monkeypatch):
    from tests.fakes import FakeResponse, FakeSession
    monkeypatch.setattr(engine, "wifi_state", lambda: ("SNNU", "已连接"))
    monkeypatch.setattr(engine, "portal_reachable", lambda session, port: True)
    monkeypatch.setattr(engine, "read_status_page",
                        lambda session, port, log=None:
                        StatusPageResult(PortalStatus.AUTH_REQUIRED))
    session = FakeSession()
    for spec in engine.DEFAULT_PROBES:
        session.route(spec.url, FakeResponse(200, "nope"))
    verdict, results, portal = engine.diagnose_network(session, "SNNU", port=8601)
    assert verdict is PortalStatus.AUTH_REQUIRED
    assert len(results) == len(engine.DEFAULT_PROBES)
    assert portal is not None


# --------------------------------------------------------------------------- #
# P8 / K-4：默认探测点必须在本校园网**可达**且具备冗余
# 真机实测（2026-10-15）：www.msftconnecttest.com 3/3 不可达（DNS/连接失败），
# 导致「所有探测点同时失败 ⇒ 假掉线」。改为 3 个实测可达的独立目标。
# --------------------------------------------------------------------------- #
def test_p8_k4_default_probes_are_reachable_and_redundant():
    urls = [spec.url for spec in engine.DEFAULT_PROBES]
    assert len(engine.DEFAULT_PROBES) >= 3, urls
    assert all("msftconnecttest.com" not in url for url in urls), \
        "真机实测不可达的探测点仍在列表中：%r" % urls
    assert "http://detectportal.firefox.com/success.txt" in urls, urls
    assert len(set(urls)) == len(urls), "探测点重复：%r" % urls


def test_p8_k4_internet_ok_survives_other_endpoints_flaking():
    """任一探测点可用即在线：其余探测点全部抖动/失败时仍须判在线。"""
    from tests.fakes import FakeSession
    specs = engine.DEFAULT_PROBES
    assert len(specs) >= 3, "探测点不足以容忍单点抖动：%r" % [s.url for s in specs]
    session = FakeSession()
    for spec in specs[:-1]:
        session.route(spec.url, requests.ConnectionError("getaddrinfo failed (flaky)"))
    last = specs[-1]
    session.route(last.url, _matching_response(last))
    ok, via = engine.internet_ok(session)
    assert ok is True, "仅最后一个探测点可用就被判离线（K-4 假掉线）"
    assert via == last.url
