# -*- coding: utf-8 -*-
"""V0.6 Block 1：双轴探测 + Clash 假死自愈判定（纯逻辑测试）。

零真实网络 / 零真实注册表 / 零真实凭据 / 零真实浏览器 / 零真实 sleep：
会话与 TCP 探针一律用假件注入（``tests.fakes.FakeSession`` /
``FakeResponse`` / 匿名函数），``probe_via_proxy`` 默认不发请求
（空探测点时工厂一次也不调用），``check_endpoint`` 默认不做真实 socket。
"""
from __future__ import annotations

import requests
from campus_core import PROBE_EXACT, PROBE_HTML_TITLE, ProbeSpec
from tests.fakes import FakeResponse, FakeSession

import campus_clash_health as ch
from campus_clash_health import (
    ACTION_RESTART_VERGE, ACTION_TOGGLE_SYSTEM_PROXY, ClashHealth,
    ClashHealPlan, ProxyAxisResult, assess_dual_axis, check_endpoint,
    clamp_cooldown, clamp_interval, clamp_threshold, clamp_v06_defaults,
    classify, heal_plan, probe_via_proxy,
)

#: 代理轴探测点（与 V0.5 直连探测同源：严格匹配，任一命中即在线）。
PROBES = (
    ProbeSpec("http://detectportal.firefox.com/success.txt",
              "success", PROBE_EXACT),
    ProbeSpec("http://www.msftncsi.com/ncsi.txt",
              "Microsoft NCSI", PROBE_EXACT),
    ProbeSpec("http://captive.apple.com/hotspot-detect.html",
              "Success", PROBE_HTML_TITLE),
)


def _ok_session(url: str = PROBES[0].url, body: str = "success") -> FakeSession:
    """首个探测点严格匹配的假会话（代理轴通）。"""
    session = FakeSession()
    session.route(url, FakeResponse(200, body))
    return session


def _failing_session(error: Exception) -> FakeSession:
    """全部探测点都抛同一异常的假会话。"""
    session = FakeSession()
    for spec in PROBES:
        session.route(spec.url, error)
    return session


# --------------------------------------------------------------------------- #
# classify：双轴判定（每个分支）
# --------------------------------------------------------------------------- #
def test_classify_healthy_when_both_axes_ok():
    health = classify(True, True)
    assert health.verdict == "healthy"
    assert health.direct_ok is True and health.proxy_ok is True
    assert health.proxy_error is None


def test_classify_fake_dead_on_proxy_timeout():
    health = classify(True, False, "timeout")
    assert health.verdict == "clash_fake_dead"
    assert health.proxy_error == "timeout"


def test_classify_fake_dead_on_proxy_connection_error():
    health = classify(True, False, "connection")
    assert health.verdict == "clash_fake_dead"


def test_classify_fake_dead_error_name_is_case_insensitive():
    assert classify(True, False, " Timeout ").verdict == "clash_fake_dead"
    assert classify(True, False, "CONNECTION").verdict == "clash_fake_dead"


def test_classify_dns_error_is_not_fake_dead():
    health = classify(True, False, "dns")
    assert health.verdict == "unknown"
    assert health.proxy_error == "dns"


def test_classify_http_status_is_not_fake_dead():
    assert classify(True, False, "http_status").verdict == "unknown"


def test_classify_body_mismatch_is_not_fake_dead():
    assert classify(True, False, "mismatch").verdict == "unknown"


def test_classify_unknown_or_missing_error_is_not_fake_dead():
    assert classify(True, False, "unknown").verdict == "unknown"
    assert classify(True, False, None).verdict == "unknown"
    assert classify(True, False, "").verdict == "unknown"


def test_classify_offline_when_both_axes_fail():
    for error in ("timeout", "dns", "connection", None):
        health = classify(False, False, error)
        assert health.verdict == "offline", error


def test_classify_unknown_when_direct_fails_but_proxy_ok():
    health = classify(False, True)
    assert health.verdict == "unknown"


def test_classify_to_dict_round_trip():
    data = classify(True, False, "timeout").to_dict()
    assert data == {"direct_ok": True, "proxy_ok": False,
                    "verdict": "clash_fake_dead", "proxy_error": "timeout",
                    "reason": data["reason"]}
    assert data["reason"]


def test_proxy_axis_result_with_direct_and_dict():
    result = ProxyAxisResult(None, True, None, 0.12)
    assert result.direct_ok is None
    filled = result.with_direct(True)
    assert filled.direct_ok is True and filled.proxy_ok is True
    assert result.direct_ok is None  # 原记录不变
    assert filled.to_dict() == {"direct_ok": True, "proxy_ok": True,
                                "proxy_error": None, "elapsed": 0.12}


def test_assess_dual_axis_uses_proxy_record():
    proxy = ProxyAxisResult(None, False, "timeout", 0.5)
    assert assess_dual_axis(True, proxy).verdict == "clash_fake_dead"
    assert assess_dual_axis(False, proxy).verdict == "offline"
    proxy_ok = ProxyAxisResult(None, True, None, 0.1)
    assert assess_dual_axis(True, proxy_ok).verdict == "healthy"


# --------------------------------------------------------------------------- #
# probe_via_proxy：代理轴探测（注入会话）
# --------------------------------------------------------------------------- #
def test_probe_via_proxy_ok_on_first_strict_match():
    session = _ok_session()
    result = probe_via_proxy(lambda: session, PROBES, timeout=3.0)
    assert result.proxy_ok is True
    assert result.proxy_error is None
    assert result.direct_ok is None  # 本函数只测代理轴，直连轴未填
    assert result.elapsed >= 0.0
    assert len(session.calls) == 1
    assert session.calls[0]["kwargs"]["timeout"] == 3.0


def test_probe_via_proxy_ok_after_first_point_times_out():
    session = FakeSession()
    session.route(PROBES[0].url, requests.exceptions.ConnectTimeout("timed out"))
    session.route(PROBES[1].url, FakeResponse(200, "Microsoft NCSI"))
    result = probe_via_proxy(lambda: session, PROBES)
    assert result.proxy_ok is True
    assert result.proxy_error is None
    assert len(session.calls) == 2  # 单点失败不结束探测


def test_probe_via_proxy_all_timeout_reports_timeout():
    session = _failing_session(requests.exceptions.ReadTimeout("timed out"))
    result = probe_via_proxy(lambda: session, PROBES)
    assert result.proxy_ok is False
    assert result.proxy_error == "timeout"
    assert len(session.calls) == len(PROBES)


def test_probe_via_proxy_keeps_first_error_kind():
    session = FakeSession()
    session.route(PROBES[0].url,
                  requests.ConnectionError("getaddrinfo failed"))
    session.route(PROBES[1].url,
                  requests.exceptions.ConnectTimeout("timed out"))
    session.route(PROBES[2].url,
                  requests.ConnectionError("connection refused"))
    result = probe_via_proxy(lambda: session, PROBES)
    assert result.proxy_error == "dns"  # 首个错误种类，不被后者覆盖


def test_probe_via_proxy_dns_error():
    session = _failing_session(
        requests.ConnectionError("Name or service not known"))
    assert probe_via_proxy(lambda: session, PROBES).proxy_error == "dns"


def test_probe_via_proxy_connection_error():
    session = _failing_session(requests.ConnectionError("connection refused"))
    assert probe_via_proxy(lambda: session, PROBES).proxy_error == "connection"


def test_probe_via_proxy_unknown_exception_is_not_fake_dead_evidence():
    session = _failing_session(RuntimeError("boom"))
    result = probe_via_proxy(lambda: session, PROBES)
    assert result.proxy_error == "unknown"
    assert classify(True, result.proxy_ok, result.proxy_error).verdict == "unknown"


def test_probe_via_proxy_http_status_failure():
    session = FakeSession()
    for spec in PROBES:
        session.route(spec.url, FakeResponse(500, "error"))
    result = probe_via_proxy(lambda: session, PROBES)
    assert result.proxy_ok is False
    assert result.proxy_error == "http_status"


def test_probe_via_proxy_body_mismatch_failure():
    session = FakeSession()
    for spec in PROBES:
        session.route(spec.url, FakeResponse(200, "wrong body"))
    result = probe_via_proxy(lambda: session, PROBES)
    assert result.proxy_ok is False
    assert result.proxy_error == "mismatch"


def test_probe_via_proxy_redirect_does_not_match():
    session = FakeSession()
    for spec in PROBES:
        session.route(spec.url, FakeResponse(302, "success"))
    result = probe_via_proxy(lambda: session, PROBES)
    assert result.proxy_ok is False
    assert result.proxy_error == "mismatch"


def test_probe_via_proxy_empty_probes_makes_no_request():
    calls = []

    def factory():
        calls.append(1)
        return _ok_session()

    result = probe_via_proxy(factory, [])
    assert result.proxy_ok is False
    assert result.proxy_error == "no_probes"
    assert calls == []  # 默认不发请求：连工厂都不调用


def test_probe_via_proxy_accepts_session_directly():
    result = probe_via_proxy(_ok_session(), PROBES)
    assert result.proxy_ok is True


def test_probe_via_proxy_factory_failure_is_proxy_failure():
    result = probe_via_proxy(
        lambda: (_ for _ in ()).throw(
            requests.exceptions.ConnectTimeout("timed out")), PROBES)
    assert result.proxy_ok is False
    assert result.proxy_error == "timeout"


def test_probe_via_proxy_none_session_is_connection_failure():
    result = probe_via_proxy(lambda: None, PROBES)
    assert result.proxy_ok is False
    assert result.proxy_error == "connection"


def test_probe_via_proxy_bare_url_only_checks_status_200():
    session = FakeSession()
    session.route("http://example.com/ping", FakeResponse(200, "anything"))
    result = probe_via_proxy(lambda: session, ["http://example.com/ping"])
    assert result.proxy_ok is True
    session2 = FakeSession()
    session2.route("http://example.com/ping", FakeResponse(404, "anything"))
    result2 = probe_via_proxy(lambda: session2, ["http://example.com/ping"])
    assert result2.proxy_ok is False
    assert result2.proxy_error == "http_status"


def test_probe_via_proxy_carries_direct_ok_when_given():
    result = probe_via_proxy(lambda: _ok_session(), PROBES, direct_ok=True)
    assert result.direct_ok is True
    assert assess_dual_axis(result.direct_ok, result).verdict == "healthy"


# --------------------------------------------------------------------------- #
# check_endpoint：Clash 端点 TCP 检查（注入探针，不做真实 socket）
# --------------------------------------------------------------------------- #
def test_check_endpoint_no_probe_is_fail_closed():
    assert check_endpoint("127.0.0.1", 7897, None) is False
    assert check_endpoint("127.0.0.1", 7897) is False


def test_check_endpoint_two_arg_probe():
    assert check_endpoint("127.0.0.1", 7897, lambda h, p: True) is True
    assert check_endpoint("127.0.0.1", 7897, lambda h, p: False) is False


def test_check_endpoint_three_arg_probe_receives_timeout():
    seen = {}

    def probe(host, port, timeout):
        seen.update({"host": host, "port": port, "timeout": timeout})
        return True

    assert check_endpoint("127.0.0.1", 7897, probe) is True
    assert seen == {"host": "127.0.0.1", "port": 7897, "timeout": 3.0}


def test_check_endpoint_default_host_port():
    seen = {}

    def probe(host, port):
        seen.update({"host": host, "port": port})
        return True

    assert check_endpoint(tcp_probe=probe) is True
    assert seen == {"host": "127.0.0.1", "port": 7897}


def test_check_endpoint_probe_exception_is_unreachable():
    def probe(host, port):
        raise OSError("refused")

    assert check_endpoint("127.0.0.1", 7897, probe) is False


def test_check_endpoint_invalid_host_or_port_never_calls_probe():
    calls = []

    def probe(host, port):
        calls.append((host, port))
        return True

    assert check_endpoint("", 7897, probe) is False
    assert check_endpoint("127.0.0.1", 0, probe) is False
    assert check_endpoint("127.0.0.1", 65536, probe) is False
    assert check_endpoint("127.0.0.1", "7897", probe) is False
    assert calls == []


# --------------------------------------------------------------------------- #
# heal_plan：自愈计划（只返回动作字符串，不执行）
# --------------------------------------------------------------------------- #
def _fake_dead() -> ClashHealth:
    return classify(True, False, "timeout")


def test_heal_plan_first_attempt_toggles_system_proxy():
    plan = heal_plan(_fake_dead(), endpoint_ok=True, attempts=0)
    assert plan.actions == [ACTION_TOGGLE_SYSTEM_PROXY]
    assert "系统代理" in plan.reason


def test_heal_plan_second_attempt_restarts_verge():
    plan = heal_plan(_fake_dead(), endpoint_ok=False, attempts=1)
    assert plan.actions == [ACTION_RESTART_VERGE]
    assert "Verge" in plan.reason


def test_heal_plan_stops_after_two_attempts_and_suggests_manual():
    for attempts in (2, 3, 10):
        plan = heal_plan(_fake_dead(), endpoint_ok=True, attempts=attempts)
        assert plan.actions == [], attempts
        assert "人工" in plan.reason, attempts


def test_heal_plan_negative_or_invalid_attempts_count_as_zero():
    assert heal_plan(_fake_dead(), attempts=-1).actions == [
        ACTION_TOGGLE_SYSTEM_PROXY]
    assert heal_plan(_fake_dead(), attempts=None).actions == [
        ACTION_TOGGLE_SYSTEM_PROXY]
    assert heal_plan(_fake_dead(), attempts="bad").actions == [
        ACTION_TOGGLE_SYSTEM_PROXY]


def test_heal_plan_no_action_when_not_fake_dead():
    assert heal_plan(classify(True, True), attempts=0).actions == []
    assert heal_plan(classify(False, False, "timeout"), attempts=0).actions == []
    assert heal_plan(classify(True, False, "dns"), attempts=0).actions == []
    assert heal_plan(classify(False, True), attempts=0).actions == []


def test_heal_plan_accepts_verdict_string():
    assert heal_plan("clash_fake_dead", attempts=0).actions == [
        ACTION_TOGGLE_SYSTEM_PROXY]
    assert heal_plan("healthy", attempts=0).actions == []
    assert heal_plan("offline", attempts=0).actions == []
    assert heal_plan("unknown", attempts=0).actions == []


def test_heal_plan_accepts_proxy_axis_result():
    proxy = ProxyAxisResult(True, False, "connection", 0.3)
    assert heal_plan(proxy, attempts=1).actions == [ACTION_RESTART_VERGE]


def test_heal_plan_endpoint_ok_only_changes_reason_not_actions():
    plan_ok = heal_plan(_fake_dead(), endpoint_ok=True, attempts=0)
    plan_bad = heal_plan(_fake_dead(), endpoint_ok=False, attempts=0)
    assert plan_ok.actions == plan_bad.actions == [ACTION_TOGGLE_SYSTEM_PROXY]
    assert "正常" in plan_ok.reason
    assert "异常" in plan_bad.reason


def test_heal_plan_to_dict_copies_actions():
    plan = heal_plan(_fake_dead(), attempts=0)
    data = plan.to_dict()
    assert data["actions"] == [ACTION_TOGGLE_SYSTEM_PROXY]
    data["actions"].append("x")
    assert plan.actions == [ACTION_TOGGLE_SYSTEM_PROXY]


def test_heal_plan_default_actions_empty():
    plan = ClashHealPlan()
    assert plan.actions == []
    assert plan.reason == ""


# --------------------------------------------------------------------------- #
# 限幅工具：V0.6 新默认值 interval=5, threshold=2, cooldown=60（旧值 15/3/180）
# --------------------------------------------------------------------------- #
def test_clamp_interval_defaults_and_bounds():
    assert clamp_interval() == 5
    assert clamp_interval(None) == 5
    assert clamp_interval("bad") == 5
    assert clamp_interval(True) == 5
    assert clamp_interval(1) == 5
    assert clamp_interval(5) == 5
    assert clamp_interval(30) == 30
    assert clamp_interval(120) == 120
    assert clamp_interval(121) == 120
    assert clamp_interval("7") == 7
    assert clamp_interval(5.9) == 5


def test_clamp_threshold_defaults_and_bounds():
    assert clamp_threshold() == 2
    assert clamp_threshold(None) == 2
    assert clamp_threshold("bad") == 2
    assert clamp_threshold(0) == 1
    assert clamp_threshold(1) == 1
    assert clamp_threshold(2) == 2
    assert clamp_threshold(10) == 10
    assert clamp_threshold(11) == 10


def test_clamp_cooldown_defaults_and_bounds():
    assert clamp_cooldown() == 60
    assert clamp_cooldown(None) == 60
    assert clamp_cooldown("bad") == 60
    assert clamp_cooldown(10) == 60
    assert clamp_cooldown(60) == 60
    assert clamp_cooldown(300) == 300
    assert clamp_cooldown(3600) == 3600
    assert clamp_cooldown(3601) == 3600


def test_clamp_v06_defaults_match_new_defaults_not_old():
    assert clamp_v06_defaults() == {"interval": 5, "threshold": 2,
                                    "cooldown": 60}
    assert ch.V06_DEFAULTS == {"interval": 5, "threshold": 2, "cooldown": 60}
    assert ch.V05_DEFAULTS == {"interval": 15, "threshold": 3,
                               "cooldown": 180}
    assert clamp_v06_defaults(1, 99, 10) == {"interval": 5, "threshold": 10,
                                             "cooldown": 60}
