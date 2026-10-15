# -*- coding: utf-8 -*-
"""V0.3 P4 snnu CLI 接线测试（U-4-30..34）。

验证 ``snnu`` 子命令：
- 默认**只读识别**：不提交凭据、不产生 POST，但确实绑定 SNNU 接口；
- ``--auth`` 必须取得单实例互斥体（fail-closed），并传递 ``allow_auth=True``；
- JSON 输出符合 ``BackupLinkResult`` 契约。

全程零真实网络 / 零真实凭据 / 零真实注册表 / 零真实 sleep。
"""
from __future__ import annotations

import argparse
import json
import logging
from types import SimpleNamespace

import campus_reconnect as engine
from campus_core import PortalStatus
from campus_snnu import BackupLinkResult
from tests.fakes import FakeProbeEngine, FakeSession
from tools import netdiag

LOG = logging.getLogger("campus")

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


def _engine() -> FakeProbeEngine:
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
    eng.routes.update({"default": SimpleNamespace(if_index=5,
                                                  source_ip="10.252.37.88")})
    return eng


def _wire(monkeypatch, *, status=PortalStatus.AUTH_REQUIRED, reachable=True,
          resolve=(8601, "测试端口")) -> None:
    report = _report()
    probe_engine = _engine()

    class _Observer:
        def observe(self, target_ssid: str = "SNNU"):
            return report

        def route_lookup(self, target: str):
            return SimpleNamespace(if_index=5, source_ip="10.252.37.88")

    monkeypatch.setattr(engine, "NetworkObserver", lambda runner: _Observer())
    class _Scoped:
        interface_bound = True
        def bind_session(self, session):
            session.trust_env = False
            session.mount("http://", object())
            session.mount("https://", object())
            return True
        def tcp_connect(self, host, port, timeout=None):
            return True
        def http_probe(self, url, timeout=None):
            return SimpleNamespace(status_code=200, redirected=False,
                                   final_url=url, body="Microsoft Connect Test", error="")
        def dns_resolve(self, name):
            return True
    probe_engine.for_interface = lambda if_index, source_ip, dns=(): _Scoped()
    monkeypatch.setattr(engine, "WindowsProbeEngine", lambda: probe_engine)
    monkeypatch.setattr(engine, "internet_ok",
                        lambda session: (True, "msftconnecttest"))
    monkeypatch.setattr(engine, "wifi_state",
                        lambda interface=None: ("SNNU", "connected"))
    monkeypatch.setattr(engine, "wifi_reconnect",
                        lambda ssid, log=None, interface=None: True)
    monkeypatch.setattr(engine, "_ipv4_ready", lambda interface=None: True)
    monkeypatch.setattr(engine, "resolve_port",
                        lambda session, requested: resolve)
    monkeypatch.setattr(engine, "portal_reachable",
                        lambda session, port: reachable)
    monkeypatch.setattr(engine, "read_status_page",
                        lambda session, port, log=None:
                        SimpleNamespace(status=status))


def _args(**kw) -> argparse.Namespace:
    base = dict(ssid="SNNU", port=None, yys="mobile", account_format="plain",
                no_reconnect=False, auth=False, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# U-4-30 子命令注册
# --------------------------------------------------------------------------- #
def test_u4_30_parser_registers_snnu():
    args = engine.build_parser().parse_args(["snnu"])
    assert args.command == "snnu"
    assert args.ssid == "SNNU"
    assert args.auth is False
    assert args.no_reconnect is False
    assert args.json is False
    parsed = engine.build_parser().parse_args(
        ["snnu", "--auth", "--no-reconnect", "--json", "--port", "8602"])
    assert parsed.auth is True and parsed.no_reconnect is True
    assert parsed.json is True and parsed.port == 8602


# --------------------------------------------------------------------------- #
# U-4-31 默认只读识别：零凭据、零 POST、已绑定
# --------------------------------------------------------------------------- #
def test_u4_31_snnu_cli_is_readonly(monkeypatch, capsys):
    _wire(monkeypatch)
    session = FakeSession()
    rc = engine.snnu(session, _args(), LOG)
    out = capsys.readouterr().out
    assert rc == 0
    assert "V0.3 P4 SNNU 备用链路与接口绑定认证" in out
    assert "只读识别（不提交凭据）" in out
    assert "是否提交凭据: 否" in out
    assert "AUTH_REQUIRED" in out
    assert session.posts() == []
    assert len(session.mounts) == 2          # 已绑定 SNNU 接口


def test_u4_31b_snnu_cli_blocks_when_portal_unreachable(monkeypatch, capsys):
    _wire(monkeypatch, reachable=False)
    session = FakeSession()
    rc = engine.snnu(session, _args(), LOG)
    out = capsys.readouterr().out
    assert rc == 1
    assert "E_SNNU_PORTAL_UNREACHABLE" in out
    assert session.posts() == []


# --------------------------------------------------------------------------- #
# U-4-32 JSON 契约
# --------------------------------------------------------------------------- #
def test_u4_32_snnu_cli_json_contract(monkeypatch, capsys):
    _wire(monkeypatch, status=PortalStatus.ONLINE)
    session = FakeSession()
    engine.snnu(session, _args(json=True), LOG)
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["contract_version"] == "v03-p4-snnu-1"
    assert payload["allow_auth"] is False
    assert payload["credentials_sent"] is False
    assert payload["stage"] == "DONE"
    assert payload["status"] == "ONLINE"
    assert payload["egress"]["proof"] == "CONFIRMED"


# --------------------------------------------------------------------------- #
# U-4-33 --auth 必须先取得单实例互斥体（fail-closed）
# --------------------------------------------------------------------------- #
def test_u4_33_snnu_cli_auth_requires_single_instance(monkeypatch, capsys):
    _wire(monkeypatch)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: False)
    session = FakeSession()
    rc = engine.snnu(session, _args(auth=True), LOG)
    out = capsys.readouterr().out
    assert rc == 1
    assert out == ""                      # 未进入流程，未做任何观察/请求
    assert session.calls == []


def test_u4_33b_snnu_cli_auth_passes_allow_auth(monkeypatch, capsys):
    _wire(monkeypatch)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    captured = {}

    class _Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self) -> BackupLinkResult:
            return BackupLinkResult(ok=True, allow_auth=True,
                                    detail="stub（测试替身）")

    monkeypatch.setattr(engine, "SnnuBackupLink", _Recorder)
    rc = engine.snnu(FakeSession(), _args(auth=True), LOG)
    capsys.readouterr()
    assert rc == 0
    assert captured["allow_auth"] is True
    assert captured["target_ssid"] == "SNNU"
    assert captured["if_index"] == 5
    assert captured["source_ip"] == "10.252.37.88"
