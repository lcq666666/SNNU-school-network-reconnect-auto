# -*- coding: utf-8 -*-
"""V0.3 P1 只读 CLI 入口 ``netcheck`` 的离线接线测试（U-1-07）。

验证：
- ``build_parser`` 注册了 ``netcheck`` 子命令（含 --ssid/--json）；
- ``netcheck`` 复用观察层 + 三轴评估并输出人类可读快照与可选 JSON；
- 全程**零副作用**：不发认证 POST、不写系统（仅 GET 探测）。
"""
from __future__ import annotations

import argparse
import json
import logging
from types import SimpleNamespace

import campus_reconnect
from campus_network import CONTRACT_VERSION
from tests.fakes import FakeProbeEngine, FakeSession
from tools import netdiag

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


def _report() -> netdiag.DiagReport:
    return netdiag.DiagReport(
        adapters=netdiag.parse_adapters(
            json.dumps([ETH_ADAPTER, WIFI_ADAPTER], ensure_ascii=False), "[]"),
        ip_addresses=netdiag.parse_ip_addresses([ETH_IP, WIFI_IP]),
        ip_interfaces=netdiag.parse_ip_interfaces([]),
        default_routes=netdiag.parse_default_routes([ETH_ROUTE, WIFI_ROUTE]),
        wifi=netdiag.parse_wifi("", target_ssid="SNNU"),
        generated_at="2026-10-14T00:00:00+08:00", host="test-host")


class _FakeObserver:
    """假观察层：固定报告 + 出口查询（系统最佳路由经以太网 ifIndex=10）。"""

    def __init__(self, report: netdiag.DiagReport) -> None:
        self._report = report
        self.observed_ssid = None

    def observe(self, target_ssid: str = "SNNU") -> netdiag.DiagReport:
        self.observed_ssid = target_ssid
        return self._report

    def route_lookup(self, target: str):
        return SimpleNamespace(if_index=10, source_ip="192.168.124.9")


def _healthy_engine() -> FakeProbeEngine:
    eng = FakeProbeEngine()
    eng.tcp.update({"default": True})
    eng.http.update({
        "https://www.msftconnecttest.com/connecttest.txt":
            {"status_code": 200, "redirected": False,
             "body": "Microsoft Connect Test"},
        "https://captive.apple.com/hotspot-detect.html":
            {"status_code": 200, "redirected": False,
             "body": "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success"
                     "</BODY></HTML>"},
    })
    eng.dns.update({"default": True})
    return eng


def _wire(monkeypatch) -> FakeSession:
    """把 netcheck 的观察层/探测引擎/系统轴探测替换为假件（零真实网络）。"""
    observer = _FakeObserver(_report())
    monkeypatch.setattr(campus_reconnect, "NetworkObserver",
                        lambda runner: observer)
    monkeypatch.setattr(campus_reconnect, "WindowsProbeEngine",
                        lambda: _healthy_engine())
    monkeypatch.setattr(campus_reconnect, "internet_ok",
                        lambda session: (True, "msftconnecttest"))
    return FakeSession()


def test_u1_07_parser_registers_netcheck():
    args = campus_reconnect.build_parser().parse_args(["netcheck"])
    assert args.command == "netcheck"
    assert args.ssid == "SNNU"
    assert args.json is False


def test_u1_07_netcheck_prints_three_axes_and_readonly_decision(
        monkeypatch, capsys):
    session = _wire(monkeypatch)
    args = argparse.Namespace(ssid="SNNU", json=False)
    rc = campus_reconnect.netcheck(session, args, logging.getLogger("test"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "ETHERNET_HEALTH" in out and "WIFI_HEALTH" in out
    assert "SYSTEM_HEALTH" in out
    assert "只读决策" in out
    assert "ethernet" in out          # 以太网健康 → 建议保持以太网
    assert session.posts() == []      # 零认证 POST
    assert all(c["method"] == "GET" for c in session.calls)


def test_u1_07_netcheck_json_contract(monkeypatch, capsys):
    session = _wire(monkeypatch)
    args = argparse.Namespace(ssid="SNNU", json=True)
    campus_reconnect.netcheck(session, args, logging.getLogger("test"))
    out = capsys.readouterr().out
    # 人类可读快照之后追加一段可解析的 JSON 契约
    payload = out[out.index("{"):].strip()
    data = json.loads(payload)
    assert data["contract_version"] == CONTRACT_VERSION
    assert data["recommendation"]["preferred"] == "ethernet"
    assert data["recommendation"]["would_switch"] is False
    assert session.posts() == []
