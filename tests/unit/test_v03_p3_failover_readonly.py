# -*- coding: utf-8 -*-
"""V0.3 P3 离线集成闭合回路与只读 CLI 接线测试（I-3-01 / I-3-02 / U-3-30..32）。

闭合剧本用 **P1 真实管道**（``campus_network.evaluate`` + ``netdiag`` 假命令输出
+ :class:`tests.fakes.FakeProbeEngine` + :class:`tests.fakes.FakeSession`）逐帧喂给
P3 状态机，断言状态序列与**副作用清单**：

- 全程零认证 POST、零路由/Metric 写入、零注册表写入、零执行器调用；
- 只读监测模式下任何执行态都不可达（仅有预测状态）；
- 有线正常时不得误判 SNNU 故障、不得执行多余认证（主方案 §12 P1 出口条件）。
"""
from __future__ import annotations

import argparse
import json
import logging
from types import SimpleNamespace

import pytest

import campus_reconnect as engine
from campus_failover import (FailoverMode, FailoverPolicy, FailoverState,
                             NetworkFailoverManager)
from campus_network import (CONTRACT_VERSION as HEALTH_CONTRACT,
                            DEFAULT_HTTP_TARGETS, DEFAULT_TCP_TARGETS, evaluate)
from tests.fakes import (FakeProbeEngine, FakeResponse, FakeSession,
                         healthy_http_table, matching_http_response)
from tools import netdiag


@pytest.fixture(autouse=True)
def _no_real_ip_probe(monkeypatch):
    """F75-13②：离线剧本里 IP 级证据恒为不可达，保持零真实网络。"""
    monkeypatch.setattr(engine, "IP_TCP_PROBE", lambda host, port, timeout: False)

ETH_ADAPTER = {"Name": "以太网",
               "InterfaceDescription": "Intel(R) Ethernet Connection (16) I219-V",
               "InterfaceGuid": "{ETH-GUID}", "ifIndex": 10, "Status": "Up",
               "MacAddress": "98-8F-E0-6B-B2-78", "MediaType": "802.3",
               "Virtual": False, "HardwareInterface": True}
WIFI_ADAPTER = {"Name": "WLAN",
                "InterfaceDescription": "MediaTek Wi-Fi 6 MT7921",
                "InterfaceGuid": "{WIFI-GUID}", "ifIndex": 5, "Status": "Up",
                "MacAddress": "10-B1-DF-EA-EB-B1", "MediaType": "Native 802.11",
                "Virtual": False, "HardwareInterface": True}
ETH_IP = {"ifIndex": 10, "AddressFamily": "IPv4", "IPAddress": "192.168.124.9",
          "PrefixLength": 24}
WIFI_IP = {"ifIndex": 5, "AddressFamily": "IPv4", "IPAddress": "10.252.37.88",
           "PrefixLength": 22}
ETH_ROUTE = {"ifIndex": 10, "DestinationPrefix": "0.0.0.0/0",
             "NextHop": "192.168.124.1", "RouteMetric": 0, "InterfaceMetric": 25}
WIFI_ROUTE = {"ifIndex": 5, "DestinationPrefix": "0.0.0.0/0",
              "NextHop": "10.252.1.1", "RouteMetric": 0, "InterfaceMetric": 35}

STEP = 15.0


# --------------------------------------------------------------------------- #
# P1 管道假件（与 test_v03_p1_failover_readonly.py 同构）
# --------------------------------------------------------------------------- #
def _session(online: bool) -> FakeSession:
    session = FakeSession()
    if online:
        # P8/K-5：按真实系统探测点表（DEFAULT_PROBES）逐个布点，不写死旧目标。
        for spec in engine.DEFAULT_PROBES:
            session.route(spec.url,
                          FakeResponse(200, matching_http_response(spec)["body"]))
    return session


def _report(eth_status="Up", wifi_status="Up", *, wifi_ip=True,
            wifi_route=True) -> netdiag.DiagReport:
    adapters = []
    if eth_status:
        adapters.append(ETH_ADAPTER if eth_status == "Up"
                        else dict(ETH_ADAPTER, Status=eth_status))
    if wifi_status:
        adapters.append(WIFI_ADAPTER if wifi_status == "Up"
                        else dict(WIFI_ADAPTER, Status=wifi_status))
    ips = [ETH_IP]
    if wifi_ip:
        ips.append(WIFI_IP)
    routes = [ETH_ROUTE]
    if wifi_route:
        routes.append(WIFI_ROUTE)
    return netdiag.DiagReport(
        adapters=netdiag.parse_adapters(
            json.dumps(adapters, ensure_ascii=False), "[]"),
        ip_addresses=netdiag.parse_ip_addresses(ips),
        ip_interfaces=netdiag.parse_ip_interfaces([]),
        default_routes=netdiag.parse_default_routes(routes),
        wifi=netdiag.parse_wifi("", target_ssid="SNNU"),
        generated_at="2026-10-14T00:00:00+08:00", host="test-host")


def _engine(wifi_state: str, *, best_route_if_index: int = 10) -> FakeProbeEngine:
    eng = FakeProbeEngine()
    eng.tcp.update({"default": True})
    # P8/K-5：命中响应由真实探测点表推导（不再写死旧目标正文）。
    eng.http.update(healthy_http_table())
    if wifi_state == "captive":
        wifi_src = ("10.252.37.88", 0)
        for spec in DEFAULT_HTTP_TARGETS:
            eng.http[(spec.url, wifi_src)] = matching_http_response(
                spec, status_code=302, redirected=True,
                final_url="http://202.117.144.205:8601/snnuportal/"
                          "login.jsp")
    if wifi_state == "down":
        wifi_src = ("10.252.37.88", 0)
        for host, port in DEFAULT_TCP_TARGETS:   # P8/K-6：跟随真实探测点表
            eng.tcp[(host, port, wifi_src)] = False
    eng.dns.update({"default": True})
    eng.routes.update({
        "1.1.1.1": SimpleNamespace(if_index=best_route_if_index,
                                   source_ip="192.168.124.9"),
        "2606:4700:4700::1111": SimpleNamespace(if_index=best_route_if_index,
                                                source_ip="192.168.124.9"),
    })
    return eng


def _snapshot(frame):
    eth_status, wifi_status, wifi_state, wifi_ip, wifi_route, best = frame
    session = _session(online=True)
    return evaluate(_report(eth_status, wifi_status, wifi_ip=wifi_ip,
                            wifi_route=wifi_route),
                    engine=_engine(wifi_state, best_route_if_index=best),
                    system_probe=lambda: engine.internet_ok(session),
                    route_lookup=None), session


# --------------------------------------------------------------------------- #
# I-3-01 闭合剧本：拔线 → Portal 未认证 → 双断 → 有线恢复
# --------------------------------------------------------------------------- #
def test_i3_01_closed_loop_states_and_zero_side_effects():
    manager = NetworkFailoverManager(
        policy=FailoverPolicy(prepare_timeout=30.0),
        mode=FailoverMode.MONITOR)
    script = [
        ("Up", "Up", "healthy", True, True, 10),               # 双网正常
        ("Disconnected", "Up", "captive", True, True, 5),      # 拔线 + SNNU 未认证
        ("Disconnected", "Up", "captive", True, True, 5),
        ("Disconnected", "Up", "captive", True, True, 5),      # 疑似异常
        ("Disconnected", "Up", "captive", True, True, 5),      # 准备备用链路
        ("Disconnected", "Up", "captive", True, True, 5),      # 需认证（只读：仅预测）
        ("Disconnected", "Up", "healthy", True, True, 5),      # 认证后无线可用
        ("Disconnected", "Disconnected", "down", False, False, 5),
        ("Disconnected", "Disconnected", "down", False, False, 5),
        ("Up", "Disconnected", "down", False, False, 10),      # 有线恢复
        ("Up", "Up", "healthy", True, True, 10),
    ]
    results = []
    sessions = []
    t = 0.0
    for frame in script:
        snap, session = _snapshot(frame)
        sessions.append(session)
        results.append(manager.tick(t, snap))
        t += STEP

    states = [r.state for r in results]
    assert states[0] is FailoverState.ETHERNET_PRIMARY
    assert states[1] is FailoverState.ETHERNET_PRIMARY      # 滞回：1/3 不动作
    assert states[3] is FailoverState.ETHERNET_SUSPECT
    assert states[4] is FailoverState.WIFI_PREPARING        # 准备备用链路
    assert states[7] is FailoverState.BOTH_OFFLINE          # 备用准备超时
    assert states[9] is FailoverState.ETHERNET_PRIMARY      # 有线恢复
    assert states[-1] is FailoverState.ETHERNET_PRIMARY

    # 只读监测：需要执行才能进入的状态一律不可达，仅上报预测
    assert results[5].predicted_state is FailoverState.WIFI_AUTHENTICATING
    assert [p.kind.value for p in results[5].plan] == ["portal_post"]
    assert results[6].predicted_state is FailoverState.WIFI_SWITCHING
    assert results[6].would_switch is True
    assert results[6].preferred == "wifi"
    assert all(not r.state.is_execution for r in results)

    # 副作用清单：零执行、零认证 POST、全部探测均为 GET
    for result in results:
        assert result.executed == [], result.executed
        assert result.readonly is True
        assert len(result.plan) <= 1
    for session in sessions:
        assert session.posts() == [], session.posts()
        assert all(call["method"] == "GET" for call in session.calls)
    # 只读监测下不登记切换冷却（未执行任何路由变更）
    assert manager.timers.switch_ready_at == 0.0
    assert manager.timers.auth_ready_at == 0.0
    # 网络检测计时器持续运行：恒为 now + interval（AGENTS §3.4）
    assert manager.timers.next_detection_at == (len(script) - 1) * STEP \
        + manager.policy.detection_interval


def test_i3_01b_no_executor_invocation_across_closed_loop():
    """即便显式注入执行器，只读监测下也不得被调用（单一副作用门生效）。"""
    from tests.fakes import FakeOpExecutor

    executor = FakeOpExecutor()
    manager = NetworkFailoverManager(
        policy=FailoverPolicy(prepare_timeout=30.0),
        mode=FailoverMode.MONITOR, executor=executor)
    t = 0.0
    for frame in [("Up", "Up", "healthy", True, True, 10),
                  ("Disconnected", "Up", "captive", True, True, 5)] * 4 + \
            [("Disconnected", "Up", "healthy", True, True, 5)] * 2:
        snap, _ = _snapshot(frame)
        manager.tick(t, snap)
        t += STEP
    assert executor.calls == []


# --------------------------------------------------------------------------- #
# I-3-02 有线正常：不误判 SNNU 故障、不多余认证、不切换
# --------------------------------------------------------------------------- #
def test_i3_02_ethernet_healthy_never_misjudges_or_authenticates():
    from tests.fakes import FakeOpExecutor

    executor = FakeOpExecutor()
    manager = NetworkFailoverManager(mode=FailoverMode.MONITOR,
                                     executor=executor)
    t = 0.0
    for wifi_status, wifi_state in (("Up", "captive"), ("Up", "down"),
                                    ("Disconnected", "down"), ("Up", "healthy")):
        for _ in range(3):
            snap, session = _snapshot(("Up", wifi_status, wifi_state, True,
                                       True, 10))
            result = manager.tick(t, snap)
            t += STEP
            assert result.state is FailoverState.ETHERNET_PRIMARY
            assert result.plan == [] and result.executed == []
            assert result.would_switch is False
            assert session.posts() == []
    assert executor.calls == []
    assert manager.eth_failure_streak == 0


# --------------------------------------------------------------------------- #
# U-3-30 CLI：failover 子命令注册
# --------------------------------------------------------------------------- #
def test_u3_30_parser_registers_failover():
    args = engine.build_parser().parse_args(["failover"])
    assert args.command == "failover"
    assert args.ssid == "SNNU"
    assert args.frames == 1
    assert args.interval == 15
    assert args.threshold == 3
    assert args.auth_cooldown == 180
    assert args.switch_min_interval == 300
    assert args.json is False
    parsed = engine.build_parser().parse_args(
        ["failover", "--frames", "5", "--interval", "10", "--json"])
    assert (parsed.frames, parsed.interval, parsed.json) == (5, 10, True)


def _wire_cli(monkeypatch, report, engine_fake) -> None:
    class _Observer:
        def observe(self, target_ssid: str = "SNNU"):
            return report

        def route_lookup(self, target: str):
            return SimpleNamespace(if_index=10, source_ip="192.168.124.9")

    monkeypatch.setattr(engine, "NetworkObserver", lambda runner: _Observer())
    monkeypatch.setattr(engine, "WindowsProbeEngine", lambda: engine_fake)
    monkeypatch.setattr(engine, "internet_ok",
                        lambda session: (True, "msftconnecttest"))


# --------------------------------------------------------------------------- #
# U-3-31 CLI：只读监测输出（零副作用）
# --------------------------------------------------------------------------- #
def test_u3_31_failover_cli_is_readonly(monkeypatch, capsys):
    report = _report()
    _wire_cli(monkeypatch, report, _engine("healthy"))
    session = FakeSession()
    args = argparse.Namespace(ssid="SNNU", frames=2, interval=0, threshold=3,
                              auth_cooldown=180, switch_min_interval=300,
                              json=False)
    rc = engine.failover(session, args, logging.getLogger("test"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "V0.3 P3 故障切换监测" in out
    assert "只读监测" in out
    assert "ETHERNET_PRIMARY" in out
    assert "不执行任何动作" in out
    assert "帧 2/2" in out
    assert session.posts() == []
    assert all(call["method"] == "GET" for call in session.calls)


def test_u3_31b_failover_cli_json_contract(monkeypatch, capsys):
    _wire_cli(monkeypatch, _report(), _engine("healthy"))
    args = argparse.Namespace(ssid="SNNU", frames=1, interval=0, threshold=3,
                              auth_cooldown=180, switch_min_interval=300,
                              json=True)
    engine.failover(FakeSession(), args, logging.getLogger("test"))
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["contract_version"] == "v03-p3-failover-1"
    assert payload["mode"] == "MONITOR"
    assert payload["state"] == "ETHERNET_PRIMARY"
    assert payload["executed"] == []
    assert payload["counters"]["failure_threshold"] == 3
    # P1 三轴契约仍在（CLI 复用 P1 评估管道）
    assert HEALTH_CONTRACT.startswith("v03-p1-health")


def test_u3_31c_failover_cli_warns_on_out_of_range_policy(monkeypatch, capsys):
    _wire_cli(monkeypatch, _report(), _engine("healthy"))
    args = argparse.Namespace(ssid="SNNU", frames=1, interval=1, threshold=0,
                              auth_cooldown=10, switch_min_interval=30,
                              json=False)
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("test-policy")
    logger.addHandler(_Handler())
    logger.setLevel(logging.WARNING)
    assert engine.failover(FakeSession(), args, logger) == 0
    assert any("策略参数告警" in message for message in records)
