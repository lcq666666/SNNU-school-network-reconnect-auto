# -*- coding: utf-8 -*-
"""F75-14 SNNU 绑定认证预检被「未绑定的 IP 级 TCP 证据」击穿（2026-10-16 P8 复验暴露）。

真机实证（手册 §7.12）：以太网健康时 ``snnu --auth`` 恒返回
``E_SNNU_AUTH_FAILED`` / ``network_ok=True`` / detail「公网已连通，无需认证」，永不重认证。
只读诊断脚本证实：在 **SNNU 绑定会话**上三个 HTTP 探测点全部 ``connection`` 失败，
但 ``internet_ok(会话)`` 仍返回 ``True``，证据 ``tcp://223.5.5.5:443``。

根因：F75-13② 新增的免 DNS IP 级证据使用模块级 ``IP_TCP_PROBE =
campus_windows.tcp_probe``（``socket.create_connection``，**无源地址绑定**）。它不认识
传入的绑定会话，于是即便本次预检针对 SNNU 接口，TCP 证据仍经**默认路由（以太网）**成功
握手 ⇒ 把「无线实际不通」误判为在线 ⇒ ``repair_service`` 在第 2 步短路返回
「公网已连通，无需认证」。

修复：``internet_ok(..., tcp_probe=...)`` 允许注入**接口级**探针；SNNU 预检传入
``campus_windows.tcp_probe_bound(..., source_ip=SNNU 源地址)``。OR 语义保持不变
（HTTP 结构匹配 **或** 接口级 TCP 可达 ⇒ 在线），但 TCP 证据**必须受接口约束**；
源地址不可得时 **fail-closed**（不产生 IP 级证据），绝不回退到默认路由证据。

约束：全程假 Session / 假探针，零真实网络、零真实 sleep。
"""
from __future__ import annotations

import logging
import types

import requests

import campus_reconnect as engine
import campus_windows
from campus_core import PROBE_HTML_TITLE, PortalStatus, RepairOptions
from tests.fakes import FakeResponse, FakeSession

SNNU_SOURCE_IP = "10.138.241.249"


def _matching_response(spec):
    """按探测点的匹配模式造一个**严格匹配**的假响应。"""
    if spec.mode == PROBE_HTML_TITLE:
        return FakeResponse(200, "<HTML><HEAD><TITLE>%s</TITLE></HEAD></HTML>"
                                 % spec.expect)
    return FakeResponse(200, spec.expect)


def _all_probes_fail(session):
    """让全部默认探测点失败（模拟真机：SNNU 接口无上行，连接直接被打回）。"""
    for spec in engine.DEFAULT_PROBES:
        session.route(spec.url,
                      requests.ConnectionError("Failed to establish a new connection"))


# --------------------------------------------------------------------------- #
# 核心回归：绑定上下文的 IP 级证据必须受接口约束
# --------------------------------------------------------------------------- #
def test_f7514_bound_context_ignores_default_route_tcp_success(monkeypatch):
    """F75-14 核心回归：默认路由的 TCP 成功**不得**为绑定上下文背书。

    模拟真机：以太网健康（未绑定探针必然成功）、SNNU 绑定会话三点 HTTP 全失败、
    SNNU 接口级探针失败 ⇒ 必须判**离线**。否则 ``repair_service`` 短路成
    「公网已连通，无需认证」，SNNU 永不重认证。
    """
    session = FakeSession()
    _all_probes_fail(session)
    unbound_calls = []
    monkeypatch.setattr(engine, "IP_TCP_PROBE",
                        lambda host, port, timeout: unbound_calls.append((host, port)) or True)

    ok, detail = engine.internet_ok(session, tcp_probe=lambda host, port, timeout: False)

    assert ok is False, (
        "绑定上下文仍采信默认路由的 TCP 证据 ⇒ SNNU 预检恒判在线、永不重认证（F75-14）")
    assert unbound_calls == [], (
        "绑定上下文调用了未绑定的 IP 级探针：%r" % (unbound_calls,))
    for host, port in engine.DEFAULT_TCP_TARGETS:
        assert host in detail, detail


def test_f7514_bound_context_online_when_interface_tcp_reachable(monkeypatch):
    """OR 语义保留：接口级探针可达（HTTP 全失败）⇒ 仍判在线，并报接口级证据。"""
    session = FakeSession()
    _all_probes_fail(session)
    first_target = engine.DEFAULT_TCP_TARGETS[0]
    monkeypatch.setattr(engine, "IP_TCP_PROBE", lambda host, port, timeout: False)

    ok, via = engine.internet_ok(
        session, tcp_probe=lambda host, port, timeout: (host, port) == first_target)

    assert ok is True and via == "tcp://%s:%d" % first_target, via


def test_f7514_http_structure_match_skips_tcp_probe(monkeypatch):
    """HTTP 结构匹配已足够 ⇒ 绑定探针也不该被打（保持原有快路径）。"""
    spec = engine.DEFAULT_PROBES[0]
    session = FakeSession()
    session.route(spec.url, _matching_response(spec))
    calls = []

    ok, via = engine.internet_ok(
        session, tcp_probe=lambda host, port, timeout: calls.append(host) or True)

    assert ok is True and via == spec.url
    assert calls == [], calls


def test_f7514_bound_probe_exception_is_unreachable(monkeypatch):
    """接口级探针自身抛异常 ⇒ 视为不可达，绝不因探测把监测循环炸掉。"""
    session = FakeSession()
    _all_probes_fail(session)

    def boom(host, port, timeout):
        raise OSError(10049, "The requested address is not valid in its context")

    ok, detail = engine.internet_ok(session, tcp_probe=boom)

    assert ok is False
    assert "OSError" in detail, detail


# --------------------------------------------------------------------------- #
# 无回归：默认（未绑定）路径行为不变
# --------------------------------------------------------------------------- #
def test_f7514_default_path_still_uses_module_ip_probe(monkeypatch):
    """未传 ``tcp_probe`` 时沿用模块级 ``IP_TCP_PROBE``（主链路语义，F75-13 不回归）。"""
    session = FakeSession()
    _all_probes_fail(session)
    first_target = engine.DEFAULT_TCP_TARGETS[0]
    monkeypatch.setattr(engine, "IP_TCP_PROBE",
                        lambda host, port, timeout: (host, port) == first_target)

    ok, via = engine.internet_ok(session)

    assert ok is True and via == "tcp://%s:%d" % first_target, via


# --------------------------------------------------------------------------- #
# 绑定探针本体
# --------------------------------------------------------------------------- #
class _FakeSocket:
    def __init__(self):
        self.actions = []

    def settimeout(self, timeout):
        self.actions.append(("settimeout", timeout))

    def bind(self, address):
        self.actions.append(("bind", address))

    def connect(self, address):
        self.actions.append(("connect", address))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_socket_module(monkeypatch, factory):
    """把 ``campus_windows.socket`` 换成只支持本次用到的 API 的假模块。"""
    import socket as real_socket
    monkeypatch.setattr(campus_windows, "socket", types.SimpleNamespace(
        socket=factory, AF_INET=real_socket.AF_INET,
        SOCK_STREAM=real_socket.SOCK_STREAM))


def test_f7514_tcp_probe_bound_binds_source_before_connect(monkeypatch):
    """``tcp_probe_bound`` 必须先 ``bind((源地址, 0))`` 再 ``connect``（接口级证据）。"""
    created = []

    def factory(*_args, **_kwargs):
        sock = _FakeSocket()
        created.append(sock)
        return sock

    _install_fake_socket_module(monkeypatch, factory)

    assert campus_windows.tcp_probe_bound("223.5.5.5", 443, 0.5, SNNU_SOURCE_IP) is True

    actions = created[0].actions
    names = [name for name, _value in actions]
    assert ("bind", (SNNU_SOURCE_IP, 0)) in actions, actions
    assert ("connect", ("223.5.5.5", 443)) in actions, actions
    assert names.index("bind") < names.index("connect"), actions
    assert ("settimeout", 0.5) in actions, actions


def test_f7514_tcp_probe_bound_without_source_delegates(monkeypatch):
    """未给源地址 ⇒ 退化为普通 ``tcp_probe``（默认路由语义）。"""
    calls = []
    monkeypatch.setattr(campus_windows, "tcp_probe",
                        lambda host, port, timeout: calls.append((host, port, timeout)) or True)

    assert campus_windows.tcp_probe_bound("223.5.5.5", 443, 0.5, None) is True
    assert calls == [("223.5.5.5", 443, 0.5)], calls


def test_f7514_tcp_probe_bound_connect_failure_is_false(monkeypatch):
    """绑定后连接失败（含源地址不可用）⇒ 返回 False，不抛异常。"""
    class _Boom(_FakeSocket):
        def connect(self, address):
            raise OSError(10049, "The requested address is not valid in its context")

    _install_fake_socket_module(monkeypatch, lambda *_a, **_k: _Boom())

    assert campus_windows.tcp_probe_bound("223.5.5.5", 443, 0.5, SNNU_SOURCE_IP) is False


# --------------------------------------------------------------------------- #
# 管道：SNNU 预检确实把接口级探针接进了 probe_online
# --------------------------------------------------------------------------- #
def _fake_snnu_observer():
    report = types.SimpleNamespace(
        adapters=[types.SimpleNamespace(name="WLAN", if_index=5)],
        ip_addresses=[{"if_index": 5, "address_family": "IPv4",
                       "ip_address": SNNU_SOURCE_IP}],
    )
    return types.SimpleNamespace(observe=lambda target_ssid=None: report,
                                 route_lookup=lambda target: None)


def test_f7514_snnu_repair_adapter_uses_interface_bound_probe(monkeypatch):
    """`_snnu_ops._repair` 必须把 SNNU 源地址接进接口级探针，且不再用未绑定探针。"""
    session = FakeSession()
    _all_probes_fail(session)
    ops = engine._snnu_ops(session, _fake_snnu_observer(),
                           logging.getLogger("campus"), "WLAN")

    unbound, bound = [], []
    monkeypatch.setattr(engine, "IP_TCP_PROBE",
                        lambda host, port, timeout: unbound.append((host, port)) or True)
    monkeypatch.setattr(
        engine, "IP_TCP_PROBE_BOUND",
        lambda host, port, timeout, source:
            bound.append((host, port, source)) or False)
    monkeypatch.setattr(engine, "wifi_state", lambda interface=None: ("SNNU", "connected"))
    monkeypatch.setattr(engine, "resolve_port",
                        lambda sess, requested=None: (8601, "测试"))
    monkeypatch.setattr(engine, "portal_reachable", lambda sess, port: False)

    result = ops.repair(session, RepairOptions(ssid="SNNU", operator="mobile"))

    assert result.detail != "公网已连通，无需认证", (
        "SNNU 预检仍采信默认路由的 TCP 成功 ⇒ 恒判在线、永不重认证（F75-14）")
    assert result.status is not PortalStatus.ONLINE, result.status
    assert unbound == [], "SNNU 预检调用了未绑定的 IP 级探针：%r" % (unbound,)
    assert bound, "SNNU 预检未使用接口级 IP 探针"
    assert all(source == SNNU_SOURCE_IP for _h, _p, source in bound), bound


def test_f7514_snnu_repair_fails_closed_without_source_ip(monkeypatch):
    """源地址不可得 ⇒ **不产生** IP 级证据（fail-closed），绝不用默认路由兜底。"""
    session = FakeSession()
    _all_probes_fail(session)
    report = types.SimpleNamespace(
        adapters=[types.SimpleNamespace(name="WLAN", if_index=5)],
        ip_addresses=[],                       # 无线接口尚无 IPv4 源地址
    )
    observer = types.SimpleNamespace(observe=lambda target_ssid=None: report,
                                     route_lookup=lambda target: None)
    ops = engine._snnu_ops(session, observer, logging.getLogger("campus"), "WLAN")

    unbound, bound = [], []
    monkeypatch.setattr(engine, "IP_TCP_PROBE",
                        lambda host, port, timeout: unbound.append((host, port)) or True)
    monkeypatch.setattr(
        engine, "IP_TCP_PROBE_BOUND",
        lambda host, port, timeout, source:
            bound.append((host, port, source)) or True)
    monkeypatch.setattr(engine, "wifi_state", lambda interface=None: ("SNNU", "connected"))
    monkeypatch.setattr(engine, "resolve_port", lambda sess, requested=None: (8601, "测试"))
    monkeypatch.setattr(engine, "portal_reachable", lambda sess, port: False)

    result = ops.repair(session, RepairOptions(ssid="SNNU", operator="mobile"))

    assert result.detail != "公网已连通，无需认证", result.detail
    assert unbound == [] and bound == [], (
        "源地址不可得时不应做任何 IP 级证据：unbound=%r bound=%r" % (unbound, bound))
