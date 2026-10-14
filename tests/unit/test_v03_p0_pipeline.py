# -*- coding: utf-8 -*-
"""V0.3 P0 离线集成测试（I-0-01 / I-0-02）。

被测对象：:func:`tools.netdiag.collect` 完整诊断管道 + 机器可读 JSON 契约。

- I-0-01：全部假输入跑完整管道，断言**零写入**（执行命令全部只读、
  全程无写模式文件打开、单项失败被记录而非崩溃）；
- I-0-02：JSON 契约可被机器解析，字段覆盖 P1（``campus_network.py``）
  所需的全部输入，且无自定义对象残留（冻结 P1 接口契约）。
"""
from __future__ import annotations

import builtins
import json

from tests.fakes import FakeCommandRunner
from tests.unit.test_v03_p0_netdiag import (ADAPTERS_JSON, ADVANCED_PROPS_BENIGN_JSON,
                                             CIM_ADAPTERS_JSON, CONNECTION_PROFILES_JSON,
                                             DEFAULT_ROUTES_JSON, EGRESS_V4_JSON,
                                             EGRESS_V6_JSON, IP_ADDRESSES_JSON,
                                             IP_INTERFACES_JSON, NETSH_INTERFACES_TEXT,
                                             POWERCFG_TEXT, POWER_MGMT_JSON,
                                             REMOTE_PROCESSES_JSON, RUN_ENTRIES_RAW,
                                             TCP_PORTS_JSON, WINLOGON_JSON)
from tools import netdiag

FULL_STUB = {
    "adapters": ADAPTERS_JSON,
    "cim_adapters": CIM_ADAPTERS_JSON,
    "ip_interfaces": IP_INTERFACES_JSON,
    "ip_addresses": IP_ADDRESSES_JSON,
    "default_routes": DEFAULT_ROUTES_JSON,
    "egress_v4": EGRESS_V4_JSON,
    "egress_v6": EGRESS_V6_JSON,
    "wifi_interfaces": NETSH_INTERFACES_TEXT,
    "connection_profiles": CONNECTION_PROFILES_JSON,
    "advanced_properties": ADVANCED_PROPS_BENIGN_JSON,
    "power_management": POWER_MGMT_JSON,
    "remote_processes": REMOTE_PROCESSES_JSON,
    "remote_tcp": TCP_PORTS_JSON,
    "sleep_states": POWERCFG_TEXT,
    "winlogon": WINLOGON_JSON,
    "run_entries": RUN_ENTRIES_RAW,
}

#: 命令表覆盖的全部诊断项（含按 pids 动态追加的 remote_tcp）。
EXPECTED_COMMANDS = set(netdiag.COMMANDS)


def test_i0_01_full_pipeline_has_zero_write_side_effects(monkeypatch):
    """I-0-01：完整管道零写入 —— 只读命令 + 无写模式文件打开 + 无异常逃逸。"""
    runner = FakeCommandRunner(responses=FULL_STUB)

    opened_modes = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        opened_modes.append(mode)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)

    report = netdiag.collect(runner)

    # 1) 全部执行过的命令都在命令表内且只读（假件记录 + 自检双保险）
    assert runner.calls, "未执行任何诊断命令"
    for name, script in runner.commands:
        assert name in EXPECTED_COMMANDS
        netdiag.assert_read_only(script)
    # 2) 全程没有任何写模式文件打开（collect 自身不落盘）
    assert not [m for m in opened_modes if any(c in m for c in "wax+")], \
        "collect 期间出现写模式文件打开：%r" % opened_modes
    # 3) 全桩成功时无采集错误
    assert report.errors == []
    assert set(report.commands_executed) == EXPECTED_COMMANDS


def test_i0_01_command_failure_is_recorded_not_fatal():
    """I-0-01 附加：单项失败只记录错误，不影响其余采集（fail-soft）。"""
    runner = FakeCommandRunner(responses=dict(FULL_STUB))
    runner.responses["default_routes"] = (1, "", "Access denied")
    runner.responses["egress_v4"] = OSError("no route")
    report = netdiag.collect(runner)
    failed = {e["command"] for e in report.errors}
    assert "default_routes" in failed
    assert "egress_v4" in failed
    # 其余项仍完整采集
    assert len(report.adapters) == 5
    assert report.wifi["available"] is True


def test_i0_02_json_contract_covers_p1_inputs():
    """I-0-02：JSON 契约完整、可解析、无自定义对象残留（P1 输入冻结）。"""
    runner = FakeCommandRunner(responses=FULL_STUB)
    report = netdiag.collect(runner)
    payload = report.to_dict()

    # 严格序列化：任何非 JSON 原生类型都会在这里暴露
    text = json.dumps(payload, ensure_ascii=False)
    data = json.loads(text)

    for key in ("contract_version", "generated_at", "host", "os_version",
                "adapters", "ip_interfaces", "ip_addresses", "default_routes",
                "egress", "wifi", "connection_profiles", "coexistence",
                "interface_summary", "remote_control", "errors",
                "commands_executed"):
        assert key in data, "契约缺少字段: %s" % key

    # P1 输入：网卡稳定标识与分类
    categories = {a["category"] for a in data["adapters"]}
    assert {"physical-ethernet", "physical-wifi", "virtual"} <= categories
    eth = [a for a in data["adapters"] if a["category"] == "physical-ethernet"][0]
    for field in ("name", "guid", "pnp_device_id", "mac", "if_index", "status"):
        assert eth[field], "网卡身份字段缺失: %s" % field

    # P1 输入：接口 Metric / 地址 / 默认路由（IPv4/IPv6 均覆盖）
    families = {r["address_family"] for r in data["ip_interfaces"]}
    assert {"IPv4", "IPv6"} <= families
    assert {r["address_family"] for r in data["default_routes"]} >= {"IPv4", "IPv6"}

    # P1 输入：公网出口（两个目标地址族均成功识别；失败分支见 U-0-05/I-0-01）
    v4 = data["egress"]["public_ipv4"]
    assert v4["if_index"] == 12 and v4["source_ip"] == "192.168.1.100"
    assert v4["error"] is None
    v6 = data["egress"]["public_ipv6"]
    assert v6["if_index"] == 12 and v6["source_ip"] == "2408:8207:1::10"
    assert v6["error"] is None

    # P1 输入：Wi-Fi 关联与 Profile、并存判定、连接配置文件
    assert data["wifi"]["interfaces"][0]["ssid"] == "SNNU"
    assert data["wifi"]["interfaces"][0]["profile"] == "SNNU"
    assert data["coexistence"]["verdict"] in ("allowed", "blocked", "undetermined")
    assert data["coexistence"]["evidence"]
    assert data["connection_profiles"]

    # 契约版本稳定（P1 按此版本消费）
    assert data["contract_version"] == netdiag.CONTRACT_VERSION
