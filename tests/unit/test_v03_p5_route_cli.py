# -*- coding: utf-8 -*-
"""V0.3 P5 ``route`` 子命令离线测试（U-5-30..36）。

覆盖：子命令注册与默认值；默认**只读快照**（零写入）；JSON 契约；受控切换的
``--yes`` 授权门禁（未授权直接拒绝、零写入）；``--apply --yes`` 的事务式切换；
``--recover`` 无未完成事务；``--auto --yes`` 接入真实组合执行器并执行切换。

全部用例**零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实 sleep**。
"""
from __future__ import annotations

import argparse
import json
import logging
from types import SimpleNamespace

import campus_reconnect as engine
from campus_failover import FailoverState
from campus_network import (EgressInfo, HealthAxis, HealthState, InterfaceHealth,
                            NetworkHealthSnapshot, SystemHealth)
from campus_route_guard import CONTRACT_VERSION, RouteJournal
from tests.fakes import FakeRouteOps, FakeSession

ETH_IF, WIFI_IF = 10, 5

_ETH_ROW = {"alias": "以太网", "ipv4_metric": 10, "ipv6_metric": 10,
            "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"}
_WIFI_ROW = {"alias": "WLAN", "ipv4_metric": 50, "ipv6_metric": 50,
             "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"}


def _ops(**kwargs) -> FakeRouteOps:
    metrics = {ETH_IF: dict(_ETH_ROW), WIFI_IF: dict(_WIFI_ROW)}
    egress = {"8.8.8.8": ETH_IF, "2606:4700:4700::1111": ETH_IF}
    return FakeRouteOps(metrics=metrics, egress=egress, **kwargs)


def _args(**overrides) -> argparse.Namespace:
    base = dict(ssid="SNNU", port=None, yys="mobile", account_format="plain",
                apply=None, auto=False, recover=False, yes=False, frames=1,
                interval=15, threshold=3, auth_cooldown=180,
                switch_min_interval=300, json=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _wire(monkeypatch, ops: FakeRouteOps) -> None:
    class _Observer:
        def observe(self, target_ssid: str = "SNNU") -> object:
            return SimpleNamespace(
                host="test-host",
                ip_addresses=[{"if_index": WIFI_IF, "address_family": "IPv4",
                               "ip_address": "10.252.37.88"},
                              {"if_index": ETH_IF, "address_family": "IPv4",
                               "ip_address": "192.168.124.9"}])

        def route_lookup(self, target: str) -> object:
            return SimpleNamespace(if_index=ops.egress.get(target),
                                   source_ip=None)

    eth = SimpleNamespace(if_index=ETH_IF, interface="以太网", name="以太网")
    wifi = SimpleNamespace(if_index=WIFI_IF, interface="WLAN", name="WLAN")
    monkeypatch.setattr(engine, "NetworkObserver", lambda runner: _Observer())
    monkeypatch.setattr(engine, "resolve_roles",
                        lambda report, config: {"ethernet": eth, "wifi": wifi})
    monkeypatch.setattr(engine, "build_windows_route_ops",
                        lambda observer, runner, **kw: ops)
    # 离线：变更日志一律用内存日志，不落盘 logs/
    monkeypatch.setattr(engine, "RouteJournal", lambda path=None: RouteJournal())


def _snap(eth: HealthState, wifi: HealthState, egress_if: int
          ) -> NetworkHealthSnapshot:
    ethernet = InterfaceHealth(HealthAxis.ETHERNET_HEALTH, "以太网", ETH_IF, eth)
    wireless = InterfaceHealth(HealthAxis.WIFI_HEALTH, "WLAN", WIFI_IF, wifi)
    system = SystemHealth(axis=HealthAxis.SYSTEM_HEALTH, online=True,
                          egress=EgressInfo(target="8.8.8.8",
                                            if_index=egress_if))
    return NetworkHealthSnapshot(ethernet=ethernet, wifi=wireless, system=system)


# --------------------------------------------------------------------------- #
# U-5-30 子命令注册与默认值
# --------------------------------------------------------------------------- #
def test_u5_30_parser_registers_route():
    args = engine.build_parser().parse_args(["route"])
    assert args.command == "route"
    assert args.ssid == "SNNU"
    assert args.apply is None and args.auto is False and args.recover is False
    assert args.yes is False
    assert (args.frames, args.interval, args.threshold) == (1, 15, 3)
    assert (args.auth_cooldown, args.switch_min_interval) == (180, 300)
    assert args.json is False
    parsed = engine.build_parser().parse_args(
        ["route", "--apply", "wifi", "--yes", "--json"])
    assert (parsed.apply, parsed.yes, parsed.json) == ("wifi", True, True)


# --------------------------------------------------------------------------- #
# U-5-31 默认只读快照：零写入
# --------------------------------------------------------------------------- #
def test_u5_31_route_default_is_readonly_snapshot(monkeypatch, capsys):
    ops = _ops()
    _wire(monkeypatch, ops)
    rc = engine.route(FakeSession(), _args(), logging.getLogger("test"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "V0.3 P5 路由卫士快照" in out
    assert "只读" in out
    assert "ETHERNET" not in out            # 不涉及状态机
    assert ops.writes == [] and ops.restores == []
    assert ops.reads >= 1


# --------------------------------------------------------------------------- #
# U-5-32 JSON 契约
# --------------------------------------------------------------------------- #
def test_u5_32_route_json_contract(monkeypatch, capsys):
    ops = _ops()
    _wire(monkeypatch, ops)
    engine.route(FakeSession(), _args(json=True), logging.getLogger("test"))
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["snapshot"]["contract_version"] == CONTRACT_VERSION
    assert payload["snapshot"]["primary_egress_if_index"] == ETH_IF
    assert payload["snapshot"]["interface_ids"] == {"ethernet": ETH_IF,
                                                    "wifi": WIFI_IF}
    assert payload["pending"] is None


# --------------------------------------------------------------------------- #
# U-5-33 受控切换的授权门禁
# --------------------------------------------------------------------------- #
def test_u5_33_apply_without_yes_is_refused(monkeypatch, capsys):
    ops = _ops()
    _wire(monkeypatch, ops)
    rc = engine.route(FakeSession(), _args(apply="wifi"), logging.getLogger("test"))
    assert rc == 2
    assert ops.writes == [] and ops.restores == []
    assert capsys.readouterr().out == ""


def test_u5_33b_auto_without_yes_is_refused(monkeypatch):
    ops = _ops()
    _wire(monkeypatch, ops)
    rc = engine.route(FakeSession(), _args(auto=True, yes=False),
                      logging.getLogger("test"))
    assert rc == 2
    assert ops.writes == []


def test_u5_33c_apply_with_yes_performs_transaction(monkeypatch, capsys):
    ops = _ops()
    _wire(monkeypatch, ops)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    rc = engine.route(FakeSession(), _args(apply="wifi", yes=True, json=True),
                      logging.getLogger("test"))
    out = capsys.readouterr().out
    assert rc == 0
    assert ops.writes == [(ETH_IF, "IPv4", 150), (ETH_IF, "IPv6", 150)]
    payload = json.loads(out[out.index("{"):])
    assert payload["status"] == "COMPLETED"
    assert payload["ok"] is True
    assert payload["target"] == "wifi"


def test_u5_33d_apply_blocked_when_another_instance_runs(monkeypatch, capsys):
    ops = _ops()
    _wire(monkeypatch, ops)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: False)
    rc = engine.route(FakeSession(), _args(apply="wifi", yes=True),
                      logging.getLogger("test"))
    assert rc == 1
    assert ops.writes == []


# --------------------------------------------------------------------------- #
# U-5-34 崩溃恢复：无未完成事务
# --------------------------------------------------------------------------- #
def test_u5_34_recover_without_pending(monkeypatch, capsys):
    ops = _ops()
    _wire(monkeypatch, ops)
    rc = engine.route(FakeSession(), _args(recover=True, json=True),
                      logging.getLogger("test"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "未发现未完成的路由变更事务" in out
    assert ops.writes == [] and ops.restores == []


# --------------------------------------------------------------------------- #
# U-5-36 --auto --yes 接入真实组合执行器并执行切换
# --------------------------------------------------------------------------- #
def test_u5_36_auto_with_yes_runs_state_machine(monkeypatch, capsys):
    ops = _ops()
    _wire(monkeypatch, ops)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    frames = [(HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF),
              (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),
              (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),
              (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),
              (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),
              (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF)]
    queue = list(frames)

    def _evaluate(report, **kwargs):
        eth, wifi, egress = queue.pop(0)
        return _snap(eth, wifi, egress)

    monkeypatch.setattr(engine, "evaluate", _evaluate)
    monkeypatch.setattr(engine, "WindowsProbeEngine", lambda: object())
    monkeypatch.setattr(engine, "internet_ok", lambda session: (True, "test"))

    rc = engine.route(FakeSession(), _args(auto=True, yes=True, frames=6,
                                          interval=0),
                      logging.getLogger("test"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "AUTO 帧 1/6" in out
    assert "AUTO 帧 6/6" in out
    assert ops.writes == [(ETH_IF, "IPv4", 150), (ETH_IF, "IPv6", 150)]


def test_u5_36b_auto_stays_primary_when_ethernet_healthy(monkeypatch, capsys):
    ops = _ops()
    _wire(monkeypatch, ops)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)

    def _evaluate(report, **kwargs):
        return _snap(HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF)

    monkeypatch.setattr(engine, "evaluate", _evaluate)
    monkeypatch.setattr(engine, "WindowsProbeEngine", lambda: object())
    monkeypatch.setattr(engine, "internet_ok", lambda session: (True, "test"))

    rc = engine.route(FakeSession(), _args(auto=True, yes=True, frames=2,
                                          interval=0),
                      logging.getLogger("test"))
    out = capsys.readouterr().out
    assert rc == 0
    assert FailoverState.ETHERNET_PRIMARY.value in out
    assert ops.writes == []
