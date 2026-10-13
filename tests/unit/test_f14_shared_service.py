# -*- coding: utf-8 -*-
"""F14 CLI/托盘共用重连服务（对应 T26）。

要点：CLI（Namespace）与托盘（dict/Config）转换为同一 ``RepairOptions``；
plain/isp × 三运营商 × 空运营商在同一输入下产生**相同**请求载荷与结果。
"""
from __future__ import annotations

import argparse

import pytest

from campus_core import (Config, RepairAdapters, RepairOptions, repair_service)

OPERATORS = ("mobile", "unicom", "telecom", "")


def _run_service(options, captured, login_result=(False, "stop")):
    adapters = RepairAdapters(
        wifi_state=lambda: ("SNNU", "已连接"),
        wifi_reconnect=lambda ssid: True,
        resolve_port=lambda session, requested: (8601, "fake"),
        portal_reachable=lambda session, port: True,
        probe_online=lambda session: (False, ""),
        login=lambda *a, **k: (captured.append(a), login_result)[1],
    )
    return repair_service(object(), options, lambda: 0.0, adapters,
                          lambda: ("123456", "pw"))


@pytest.mark.parametrize("operator", OPERATORS)
@pytest.mark.parametrize("fmt", ("plain", "isp"))
def test_t26_cli_and_tray_options_are_equivalent(operator, fmt):
    ns = argparse.Namespace(ssid="SNNU", yys=operator, account_format=fmt, port=None,
                            reconnect_wifi=False, browser_fallback=False, force=False)
    from_ns = RepairOptions.from_namespace(ns)
    from_map = RepairOptions.from_mapping(
        {"ssid": "SNNU", "yys": operator, "account_format": fmt,
         "browser_fallback": False})
    assert from_ns == from_map


@pytest.mark.parametrize("operator", OPERATORS)
@pytest.mark.parametrize("fmt", ("plain", "isp"))
def test_t26_two_entries_produce_same_payload(operator, fmt):
    ns = argparse.Namespace(ssid="SNNU", yys=operator, account_format=fmt, port=None,
                            reconnect_wifi=False, browser_fallback=False, force=False)
    cli_calls, tray_calls = [], []
    _run_service(RepairOptions.from_namespace(ns), cli_calls)
    _run_service(RepairOptions.from_mapping(
        {"ssid": "SNNU", "yys": operator, "account_format": fmt}), tray_calls)

    cli_account = cli_calls[0][1]
    tray_account = tray_calls[0][1]
    assert cli_account == tray_account, "两入口提交账号不一致"
    assert cli_calls[0][3] == tray_calls[0][3], "两入口运营商不一致"


def test_t26_isp_plain_account_format():
    ns_plain = argparse.Namespace(ssid="SNNU", yys="mobile", account_format="plain")
    calls = []
    _run_service(RepairOptions.from_namespace(ns_plain), calls)
    assert calls[0][1] == "123456"

    ns_isp = argparse.Namespace(ssid="SNNU", yys="mobile", account_format="isp")
    calls = []
    _run_service(RepairOptions.from_namespace(ns_isp), calls)
    assert calls[0][1] == "123456@mobile"


def test_t26_isp_empty_operator_has_no_trailing_at():
    ns = argparse.Namespace(ssid="SNNU", yys="", account_format="isp")
    calls = []
    _run_service(RepairOptions.from_namespace(ns), calls)
    assert calls[0][1] == "123456", "空运营商 isp 形成了尾部 @"


def test_from_config_matches_mapping():
    config = Config(ssid="SNNU", operator="", account_format="isp")
    assert RepairOptions.from_config(config) == RepairOptions.from_mapping(
        {"ssid": "SNNU", "yys": "", "account_format": "isp"})
