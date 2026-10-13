# -*- coding: utf-8 -*-
"""F17 Wi-Fi 解析与网络选择（对应 T29 / T38 的离线部分）。

覆盖：中 / 英 netsh 输出、SSID 含 "BSSID" 字样、无冒号异常行、双网卡、
profile 别名、大小写差异、netsh 返回码、无无线接口（明确“不支持自动认证”）、
``ipconfig`` IPv4 就绪判定、连接后等待 IP、超时不误报。全程假件，零真实 netsh。
"""
from __future__ import annotations

import logging

import campus_reconnect as engine
import campus_windows as cw
from campus_core import PortalStatus, ProbeResult, classify_network

LOG = logging.getLogger("campus")

EN_OUTPUT = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 11111111-2222-3333-4444-555555555555
    Physical address       : aa:bb:cc:dd:ee:ff
    State                  : connected
    SSID                   : SNNU
    BSSID                  : aa:bb:cc:dd:ee:01
    Network type           : Infrastructure
    Radio type             : 802.11ax
    Profile                : SNNU
"""

CN_OUTPUT = """
系统上有 1 个接口:

    名称                   : WLAN
    描述                   : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 11111111-2222-3333-4444-555555555555
    物理地址               : aa:bb:cc:dd:ee:ff
    状态                   : 已连接
    SSID                   : SNNU
    BSSID                  : aa:bb:cc:dd:ee:01
    网络类型               : 结构
    无线电类型             : 802.11ax
    配置文件               : SNNU
"""


class _Done:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


# --------------------------------------------------------------------------- #
# 解析（T29）
# --------------------------------------------------------------------------- #
def test_t29_parse_english_interface():
    interfaces = cw.parse_netsh_interfaces(EN_OUTPUT)
    assert len(interfaces) == 1
    iface = interfaces[0]
    assert iface.name == "Wi-Fi"
    assert iface.ssid == "SNNU"
    assert iface.bssid == "aa:bb:cc:dd:ee:01"
    assert iface.profile == "SNNU"
    assert iface.connected is True


def test_t29_parse_chinese_interface():
    interfaces = cw.parse_netsh_interfaces(CN_OUTPUT)
    assert len(interfaces) == 1
    iface = interfaces[0]
    assert iface.name == "WLAN"
    assert iface.ssid == "SNNU"
    assert iface.state == "已连接"
    assert iface.connected is True


def test_t29_ssid_containing_bssid_word_is_kept():
    """回归原缺陷：整行 ``"bssid" not in low`` 会误杀 SSID 含 BSSID 字样的行。"""
    text = ("    Name : Wi-Fi\n    State : connected\n"
            "    SSID : MyBSSIDNet\n    BSSID : aa:bb:cc:dd:ee:01\n")
    interfaces = cw.parse_netsh_interfaces(text)
    assert interfaces[0].ssid == "MyBSSIDNet"


def test_t29_lines_without_colon_are_ignored():
    text = ("    Name : Wi-Fi\n    State : connected\n    SSID : SNNU\n"
            "    garbage line without colon\n")
    interfaces = cw.parse_netsh_interfaces(text)
    assert interfaces[0].ssid == "SNNU"


def test_t29_unrecognized_output_yields_no_interface():
    assert cw.parse_netsh_interfaces("totally unrelated text") == []
    assert cw.parse_netsh_interfaces("") == []


def test_t29_dual_nic_selects_connected():
    text = ("    Name : Wi-Fi\n    State : disconnected\n"
            "\n"
            "    Name : Wi-Fi 2\n    State : connected\n    SSID : SNNU\n")
    ssid, state = cw.wifi_state_from_text(text)
    assert ssid == "SNNU"
    assert state == "connected"


def test_t29_dual_nic_can_select_by_interface_name():
    text = ("    Name : Wi-Fi\n    State : disconnected\n"
            "\n"
            "    Name : Wi-Fi 2\n    State : connected\n    SSID : SNNU\n")
    interfaces = cw.parse_netsh_interfaces(text)
    iface = cw.select_wifi_interface(interfaces, interface="Wi-Fi")
    assert iface is not None and iface.name == "Wi-Fi"
    assert iface.ssid is None            # 断开的接口不返回 SSID


def test_t29_profile_alias_differs_from_ssid():
    text = ("    Name : Wi-Fi\n    State : connected\n    SSID : SNNU\n"
            "    Profile : SNNU-Open\n")
    interfaces = cw.parse_netsh_interfaces(text)
    assert interfaces[0].profile == "SNNU-Open"
    assert interfaces[0].ssid == "SNNU"


def test_t29_select_prefers_exact_target():
    text = ("    Name : Wi-Fi\n    State : connected\n    SSID : Other\n"
            "\n"
            "    Name : Wi-Fi 2\n    State : connected\n    SSID : SNNU\n")
    ssid, _ = cw.wifi_state_from_text(text, ssid="SNNU")
    assert ssid == "SNNU"


def test_t29_case_difference_is_not_target():
    """大小写不同不得视为同一目标（回归 casefold 缺陷）。"""
    text = "    Name : Wi-Fi\n    State : connected\n    SSID : snnu\n"
    interfaces = cw.parse_netsh_interfaces(text)
    assert interfaces[0].matches_ssid("SNNU") is False
    ssid, _ = cw.wifi_state_from_text(text)
    results = [ProbeResult(False, "http://probe", error="connection")]
    assert classify_network(ssid, "SNNU", results) is PortalStatus.NON_TARGET


def test_t29_no_wireless_interface_is_explicit():
    assert cw.wifi_state_from_text("no interfaces here") == (None, "no-wireless")


# --------------------------------------------------------------------------- #
# 返回码 / 网卡消失（T38）
# --------------------------------------------------------------------------- #
def test_t38_wifi_state_checks_returncode(monkeypatch):
    monkeypatch.setattr(engine, "_run_console", lambda args, timeout=5.0: (1, "boom"))
    assert engine.wifi_state() == (None, "error")


def test_t38_wifi_state_command_failure(monkeypatch):
    monkeypatch.setattr(engine, "_run_console", lambda args, timeout=5.0: (None, ""))
    assert engine.wifi_state() == (None, "error")


def test_t38_nic_disappears_after_sleep(monkeypatch):
    """睡眠唤醒后网卡消失 ⇒ 空输出 ⇒ no-wireless，不误判为已连接。"""
    monkeypatch.setattr(engine, "_run_console", lambda args, timeout=5.0: (0, ""))
    assert engine.wifi_state() == (None, "no-wireless")


def test_t38_wifi_interfaces_parses_console(monkeypatch):
    monkeypatch.setattr(engine, "_run_console", lambda args, timeout=5.0: (0, EN_OUTPUT))
    interfaces = engine.wifi_interfaces()
    assert len(interfaces) == 1 and interfaces[0].ssid == "SNNU"


# --------------------------------------------------------------------------- #
# ipconfig IPv4 就绪判定（T38）
# --------------------------------------------------------------------------- #
def test_t38_ipv4_ready_parsing():
    good = ("\nWindows IP 配置\n\n以太网适配器 以太网:\n\n"
            "   IPv4 地址 . . . . . . . . . . . . : 192.168.1.10\n")
    assert cw.parse_ipconfig_ipv4(good) is True


def test_t38_ipv4_rejects_link_local_and_zero():
    assert cw.parse_ipconfig_ipv4(
        "   IPv4 地址 . . . . . . . . . . . . : 169.254.10.1\n") is False
    assert cw.parse_ipconfig_ipv4(
        "   IPv4 Address. . . . . . . . . . . : 0.0.0.0\n") is False
    assert cw.parse_ipconfig_ipv4("") is False


def test_t38_ipv4_respects_interface_filter():
    text = ("以太网适配器 以太网:\n"
            "   IPv4 地址 . . . . . . . . . . . . : 192.168.1.10\n"
            "\n"
            "无线局域网适配器 WLAN:\n"
            "   IPv4 地址 . . . . . . . . . . . . : 10.0.0.5\n")
    assert cw.parse_ipconfig_ipv4(text, interface="WLAN") is True
    assert cw.parse_ipconfig_ipv4(text, interface="不存在") is False


# --------------------------------------------------------------------------- #
# 连接后等待 IP（T29）
# --------------------------------------------------------------------------- #
def test_t29_reconnect_uses_profile_and_waits(monkeypatch):
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        return _Done(0)

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    waited = {"n": 0}

    def wait():
        waited["n"] += 1
        return True

    assert engine.wifi_reconnect("SNNU", LOG, profile="SNNU-Open", wait=wait) is True
    assert "name=SNNU-Open" in calls["args"]
    assert waited["n"] == 1


def test_t29_reconnect_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(engine.subprocess, "run", lambda args, **kwargs: _Done(1))
    assert engine.wifi_reconnect("SNNU", LOG, wait=lambda: True) is False


def test_t29_reconnect_timeout_without_real_sleep(monkeypatch):
    monkeypatch.setattr(engine.subprocess, "run", lambda args, **kwargs: _Done(0))
    # timeout=0 ⇒ 轮询循环不执行，也不真实 sleep
    assert engine.wifi_reconnect("SNNU", LOG, wait=lambda: False, timeout=0) is False


def test_t29_reconnect_empty_name_is_rejected():
    assert engine.wifi_reconnect("", LOG) is False
