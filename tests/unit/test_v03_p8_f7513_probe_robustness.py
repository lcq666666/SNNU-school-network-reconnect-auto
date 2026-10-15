# -*- coding: utf-8 -*-
"""F75-13 公网探测路径健壮性（P8-3 再观察暴露，2026-10-15 真机取证）。

背景（真机实测，见手册 §7.11）：
- 本机**公网 IPv6 是黑洞**：全局 IPv6 只在 WLAN，IPv6 默认路由仅 WLAN；
  `ping -6 2a04:4e42:7b::347` **100% 丢包**、`curl -6` 同一目标 **10s 超时**（HTTP=000），
  而 `curl -4` = 200。`detectportal.firefox.com` 的 DNS **会返回 AAAA** ⇒ 探测先撞黑洞
  再回退 IPv4：单点 3.25s；紧循环 18 轮中复现 `err=timeout` 5.12s。
- 三个 HTTP 探测点**全为域名**，任一次 DNS 抖动会让三点同时失败 ⇒ ``internet_ok`` 判离线
  ⇒ 达阈值进入修复/认证路径，**而 IPv4 的 IP 级连通性可能完好**（`ping 223.5.5.5` 4ms）。
- `watch` 只记录 `公网探测失败（n/3）`，**不含失败探测点与错误类型**，事后无法定位根因。

本文件锁定三条修复：
- ① 探测会话**只解析 IPv4**（规避 AAAA 黑洞）；
- ② 增加**免 DNS 的 IP 级 TCP 证据**（任一 HTTP 结构匹配 **或** 任一 IP 级 TCP 可达 ⇒ 在线）；
- ③ 失败时返回**失败探测点 + 错误类型**明细（供 ``watch`` 记入日志）。

约束：全程假 Session / 假 IP 级探测，零真实网络、零真实 sleep。
"""
from __future__ import annotations

import argparse
import logging
import socket
from urllib.parse import urlsplit

import requests

import campus_reconnect as engine
from campus_core import PROBE_HTML_TITLE
from tests.fakes import FakeResponse, FakeSession

LOG = logging.getLogger("campus")


def _matching_response(spec):
    """按探测点的匹配模式造一个**严格匹配**的假响应。"""
    if spec.mode == PROBE_HTML_TITLE:
        return FakeResponse(200, "<HTML><HEAD><TITLE>%s</TITLE></HEAD></HTML>"
                                 % spec.expect)
    return FakeResponse(200, spec.expect)


def _all_probes_fail(session, exc_factory=None):
    """让全部默认探测点失败（默认 DNS 解析失败）。"""
    for spec in engine.DEFAULT_PROBES:
        exc = (exc_factory or (lambda: requests.ConnectionError("getaddrinfo failed")))()
        session.route(spec.url, exc)


def _host(spec):
    return urlsplit(spec.url).hostname


# --------------------------------------------------------------------------- #
# ① 只解析 IPv4
# --------------------------------------------------------------------------- #
def test_f7513_probe_session_prefers_ipv4_only():
    """F75-13①：探测会话须只解析 IPv4，避免首选黑洞 IPv6 路径。"""
    from urllib3.util import connection as urllib3_connection

    engine.direct_session()
    assert urllib3_connection.allowed_gai_family() is socket.AF_INET, (
        "探测会话仍允许 IPv6 解析：DNS 返回 AAAA 时会先撞黑洞再回退（真机 3.25s/超时）")


# --------------------------------------------------------------------------- #
# ② 免 DNS 的 IP 级证据
# --------------------------------------------------------------------------- #
def test_f7513_http_structure_match_needs_no_ip_probe(monkeypatch):
    """HTTP 结构匹配已足够 ⇒ 不应再打 IP 级探测（保持原有快路径）。"""
    spec = engine.DEFAULT_PROBES[0]
    session = FakeSession()
    session.route(spec.url, _matching_response(spec))
    calls = []
    monkeypatch.setattr(engine, "IP_TCP_PROBE",
                        lambda host, port, timeout: calls.append((host, port)) or True)

    ok, via = engine.internet_ok(session)

    assert ok is True and via == spec.url
    assert calls == [], "HTTP 已判在线，仍打了 IP 级探测：%r" % (calls,)


def test_f7513_ip_evidence_keeps_online_when_all_http_probes_fail(monkeypatch):
    """F75-13②（核心回归）：三点 HTTP 因 DNS 抖动全失败时，免 DNS 的 IP 级证据应判在线。"""
    session = FakeSession()
    _all_probes_fail(session)
    first_target = engine.DEFAULT_TCP_TARGETS[0]
    monkeypatch.setattr(engine, "IP_TCP_PROBE",
                        lambda host, port, timeout: (host, port) == first_target)

    ok, via = engine.internet_ok(session)

    assert ok is True, (
        "DNS 抖动导致三点 HTTP 同时失败即判离线 ⇒ 有线正常时误判掉线并触发修复（F75-13 假掉线）")
    assert via == "tcp://%s:%d" % first_target, via


def test_f7513_offline_requires_http_and_ip_all_fail(monkeypatch):
    """只有 HTTP 三点与 IP 级探测**全部**失败才判离线。"""
    session = FakeSession()
    _all_probes_fail(session)
    monkeypatch.setattr(engine, "IP_TCP_PROBE", lambda host, port, timeout: False)

    ok, detail = engine.internet_ok(session)

    assert ok is False
    for spec in engine.DEFAULT_PROBES:              # 明细含每个失败探测点
        assert _host(spec) in detail, detail
    for host, port in engine.DEFAULT_TCP_TARGETS:   # 明细含每个失败的 IP 级点
        assert host in detail, detail


def test_f7513_ip_probe_exception_is_treated_as_unreachable(monkeypatch):
    """IP 级探测自身抛异常 ⇒ 视为不可达，绝不因探测把监测循环炸掉。"""
    session = FakeSession()
    _all_probes_fail(session)

    def boom(host, port, timeout):
        raise OSError("socket unavailable")

    monkeypatch.setattr(engine, "IP_TCP_PROBE", boom)

    ok, detail = engine.internet_ok(session)

    assert ok is False
    assert "OSError" in detail, detail


def test_f7513_ip_probe_timeout_is_bounded(monkeypatch):
    """IP 级探测超时必须受控（不得拖慢 15s 检测节奏）。"""
    session = FakeSession()
    _all_probes_fail(session)
    seen = []
    monkeypatch.setattr(engine, "IP_TCP_PROBE",
                        lambda host, port, timeout: seen.append(timeout) or False)

    engine.internet_ok(session)

    assert seen, "未执行 IP 级探测"
    assert all(0 < t <= 2.0 for t in seen), seen
    assert len(seen) == len(engine.DEFAULT_TCP_TARGETS), seen


# --------------------------------------------------------------------------- #
# ③ 失败明细（探测点 + 错误类型）
# --------------------------------------------------------------------------- #
def test_f7513_detail_carries_probe_point_and_error_kind(monkeypatch):
    """F75-13③：明细须含**失败探测点 + 错误类型**（dns/timeout/connection）。"""
    session = FakeSession()
    specs = engine.DEFAULT_PROBES
    session.route(specs[0].url, requests.ConnectTimeout("timed out"))
    session.route(specs[1].url, requests.ConnectionError("getaddrinfo failed for host"))
    session.route(specs[2].url, requests.ConnectionError("connection refused"))
    monkeypatch.setattr(engine, "IP_TCP_PROBE", lambda host, port, timeout: False)

    ok, detail = engine.internet_ok(session)

    assert ok is False
    assert "%s(timeout)" % _host(specs[0]) in detail, detail
    assert "%s(dns)" % _host(specs[1]) in detail, detail
    assert "%s(connection)" % _host(specs[2]) in detail, detail


# --------------------------------------------------------------------------- #
# watch 日志：失败时记录明细
# --------------------------------------------------------------------------- #
def _args(threshold=3, interval=15, cooldown=180):
    return argparse.Namespace(ssid="SNNU", yys="mobile", interval=interval,
                              threshold=threshold, cooldown=cooldown, force=False)


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_f7513_watch_logs_probe_failure_detail(monkeypatch, virtual_clock):
    """F75-13③：watch 的探测失败日志应带明细（失败探测点 + 错误类型），且阈值未达不触发修复。"""
    detail = "%s(timeout)；tcp://223.5.5.5:443(unreachable)" % _host(engine.DEFAULT_PROBES[0])
    virtual_clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    calls = {"n": 0}

    def fake_internet_ok(session, probes_=None):
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt
        return False, detail

    repairs: list = []
    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair",
                        lambda session, args, log: repairs.append(1))
    handler = _CapturingHandler()
    previous_level = LOG.level
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)          # 测试环境的 campus logger 默认未放行 INFO
    try:
        engine.watch(object(), _args(), LOG)
    finally:
        LOG.removeHandler(handler)
        LOG.setLevel(previous_level)

    text = " ".join(handler.messages)
    assert "公网探测失败" in text, text
    assert detail in text, "失败日志缺少探测点/错误类型明细：%s" % text
    assert repairs == [], "阈值未达（1/3）却触发了修复路径"
