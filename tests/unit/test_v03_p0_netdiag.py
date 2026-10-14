# -*- coding: utf-8 -*-
"""V0.3 P0 只读诊断 netdiag 单元测试（U-0-01..U-0-07 及解析边角）。

被测对象：``tools/netdiag.py``（纯解析函数 + :func:`collect` 汇总管道）。
全部输入为**假 PowerShell 输出文本**（ mimics ConvertTo-Json -Compress），
零真实子进程、零真实网络、零真实注册表（I-0-* 在 pipeline 测试中断言零写入）。
"""
from __future__ import annotations

import json

import pytest

from tools import netdiag


def _json(payload) -> str:
    """模拟 ``ConvertTo-Json -Compress`` 的单行 JSON 输出。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# 假 PowerShell 输出（字段名/枚举值对齐真实 NetTCPIP cmdlet 的英文属性名）
# --------------------------------------------------------------------------- #
ADAPTERS_JSON = _json([
    {"Name": "以太网", "InterfaceDescription": "Intel(R) Ethernet Connection (10) I219-V",
     "InterfaceGuid": "{ETH-GUID}", "ifIndex": 12, "Status": "Up",
     "MacAddress": "00-11-22-33-44-55", "LinkSpeed": "1 Gbps",
     "MediaType": "802.3", "PhysicalMediaType": "Ethernet",
     "Virtual": False, "HardwareInterface": True, "ConnectorPresent": True},
    {"Name": "WLAN", "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201 160MHz",
     "InterfaceGuid": "{WIFI-GUID}", "ifIndex": 7, "Status": "Up",
     "MacAddress": "66-77-88-99-AA-BB", "LinkSpeed": "400 Mbps",
     "MediaType": "Native 802.11", "PhysicalMediaType": "Wireless80211",
     "Virtual": False, "HardwareInterface": True, "ConnectorPresent": True},
    {"Name": "ClashTUN", "InterfaceDescription": "TAP-Windows Adapter V9",
     "InterfaceGuid": "{TAP-GUID}", "ifIndex": 25, "Status": "Up",
     "MediaType": "802.3", "Virtual": True, "HardwareInterface": False},
    {"Name": "vEthernet (WSL)", "InterfaceDescription":
     "Hyper-V Virtual Ethernet Adapter #2", "InterfaceGuid": "{WSL-GUID}",
     "ifIndex": 31, "Status": "Up", "MediaType": "802.3", "Virtual": True,
     "HardwareInterface": False},
    {"Name": "VPN", "InterfaceDescription": "Wintun Userspace Tunnel",
     "InterfaceGuid": "{VPN-GUID}", "ifIndex": 40, "Status": "Disconnected",
     "Virtual": False, "HardwareInterface": False},
])

CIM_ADAPTERS_JSON = _json([
    {"Name": "Intel(R) Ethernet Connection (10) I219-V", "NetConnectionID": "以太网",
     "GUID": "{ETH-GUID}", "PNPDeviceID": "PCI\\VEN_8086&DEV_0D4C&SUBSYS_1234",
     "PhysicalAdapter": True, "MACAddress": "00:11:22:33:44:55"},
    {"Name": "Intel(R) Wi-Fi 6 AX201 160MHz", "NetConnectionID": "WLAN",
     "GUID": "{WIFI-GUID}", "PNPDeviceID": "PCI\\VEN_8086&DEV_34F0&SUBSYS_5678",
     "PhysicalAdapter": True, "MACAddress": "66:77:88:99:AA:BB"},
])

IP_INTERFACES_JSON = _json([
    {"ifIndex": 12, "InterfaceAlias": "以太网", "AddressFamily": "IPv4",
     "InterfaceMetric": 25, "AutomaticMetric": "Enabled", "Dhcp": "Enabled",
     "ConnectionState": "Connected"},
    {"ifIndex": 12, "InterfaceAlias": "以太网", "AddressFamily": "IPv6",
     "InterfaceMetric": 25, "AutomaticMetric": "Enabled", "Dhcp": "Enabled",
     "ConnectionState": "Connected"},
    {"ifIndex": 7, "InterfaceAlias": "WLAN", "AddressFamily": "IPv4",
     "InterfaceMetric": 35, "AutomaticMetric": "Disabled", "Dhcp": "Enabled",
     "ConnectionState": "Connected"},
])

IP_ADDRESSES_JSON = _json([
    {"ifIndex": 12, "InterfaceAlias": "以太网", "AddressFamily": "IPv4",
     "IPAddress": "192.168.1.100", "PrefixLength": 24, "AddressState": "Preferred"},
    {"ifIndex": 12, "InterfaceAlias": "以太网", "AddressFamily": "IPv6",
     "IPAddress": "2408:8207:1::10", "PrefixLength": 64, "AddressState": "Preferred"},
    {"ifIndex": 7, "InterfaceAlias": "WLAN", "AddressFamily": "IPv4",
     "IPAddress": "10.252.37.88", "PrefixLength": 22, "AddressState": "Preferred"},
])

DEFAULT_ROUTES_JSON = _json([
    {"ifIndex": 12, "InterfaceAlias": "以太网", "DestinationPrefix": "0.0.0.0/0",
     "NextHop": "192.168.1.1", "RouteMetric": 1, "InterfaceMetric": 25,
     "Protocol": "NetMgmt", "PolicyStore": "ActiveStore"},
    {"ifIndex": 25, "InterfaceAlias": "ClashTUN", "DestinationPrefix": "0.0.0.0/0",
     "NextHop": "0.0.0.0", "RouteMetric": 0, "InterfaceMetric": 10,
     "Protocol": "NetMgmt", "PolicyStore": "ActiveStore"},
    {"ifIndex": 7, "InterfaceAlias": "WLAN", "DestinationPrefix": "::/0",
     "NextHop": "fe80::1", "RouteMetric": 1, "InterfaceMetric": 35,
     "Protocol": "NetMgmt", "PolicyStore": "ActiveStore"},
])

EGRESS_V4_JSON = _json({"IPAddress": "192.168.1.100", "InterfaceIndex": 12,
                        "InterfaceAlias": "以太网", "NextHop": "192.168.1.1",
                        "DestinationPrefix": "0.0.0.0/0", "RouteMetric": 1,
                        "InterfaceMetric": 25})

EGRESS_V6_JSON = _json({"IPAddress": "2408:8207:1::10", "InterfaceIndex": 12,
                        "InterfaceAlias": "以太网", "NextHop": "fe80::1",
                        "DestinationPrefix": "::/0", "RouteMetric": 1,
                        "InterfaceMetric": 25})

NETSH_INTERFACES_TEXT = """\
There is 1 interface on the system:

    Name                   : WLAN
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : {WIFI-GUID}
    Physical address       : 66-77-88-99-aa-bb
    State                  : connected
    SSID                   : SNNU
    BSSID                  : 00:aa:bb:cc:dd:ee
    Network type           : Infrastructure
    Radio type             : 802.11ac
    Authentication         : WPA2-Personal
    Cipher                 : CCMP
    Connection mode        : Profile
    Channel                : 36
    Signal                 : 90%
    Profile                : SNNU
"""

NETSH_NO_WIRELESS_TEXT = "There is no wireless interface on the system.\r\n"

CONNECTION_PROFILES_JSON = _json([
    {"InterfaceAlias": "以太网", "InterfaceIndex": 12,
     "NetworkCategory": "DomainAuthenticated", "IPv4Connectivity": "Internet",
     "IPv6Connectivity": "Internet"},
    {"InterfaceAlias": "WLAN", "InterfaceIndex": 7,
     "NetworkCategory": "DomainAuthenticated", "IPv4Connectivity": "Internet",
     "IPv6Connectivity": "NoTraffic"},
])

ADVANCED_PROPS_BENIGN_JSON = _json([
    {"Name": "以太网", "DisplayName": "流控制", "DisplayValue": "Rx 和 Tx 已启用",
     "RegistryKeyword": "*FlowControl", "RegistryValue": 3},
    {"Name": "WLAN", "DisplayName": "漫游主动性", "DisplayValue": "最低",
     "RegistryKeyword": "*RoamAggressiveness", "RegistryValue": 1},
])

ADVANCED_PROPS_BLOCKING_JSON = _json([
    {"Name": "以太网", "DisplayName": "流控制", "DisplayValue": "Rx 和 Tx 已启用",
     "RegistryKeyword": "*FlowControl", "RegistryValue": 3},
    {"Name": "WLAN", "DisplayName": "有线上网时禁用无线网卡",
     "DisplayValue": "启用", "RegistryKeyword": "*DisableWhenWired",
     "RegistryValue": 1},
])

POWER_MGMT_JSON = _json([
    {"Name": "以太网", "DisplayName": "允许计算机关闭此设备以节约电源",
     "AllowComputerToTurnOffDevice": "Disabled"},
    {"Name": "WLAN", "DisplayName": "允许计算机关闭此设备以节约电源",
     "AllowComputerToTurnOffDevice": "Disabled"},
])

WINLOGON_JSON = _json({"AutoAdminLogon": "1", "DefaultUserName": "lennonli",
                       "DefaultDomainName": "."})

RUN_ENTRIES_RAW = _json([
    {"PSPath": "Microsoft.PowerShell.Core\\Registry::HKEY_LOCAL_MACHINE\\"
               "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run",
     "CampusReconnect": "C:\\Users\\lennonli\\AppData\\Local\\Programs\\"
                        "CampusReconnect\\CampusReconnect.exe",
     "ToDesk": "C:\\Program Files\\ToDesk\\ToDesk.exe"},
    {"PSPath": "Microsoft.PowerShell.Core\\Registry::HKEY_CURRENT_USER\\"
               "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run",
     "CamApp": "C:\\Tools\\cam.exe"},
])

REMOTE_PROCESSES_JSON = _json([
    {"Id": 4120, "ProcessName": "ToDesk", "Path":
     "C:\\Program Files\\ToDesk\\ToDesk.exe"},
    {"Id": 5180, "ProcessName": "SunloginClient", "Path":
     "C:\\Program Files (x86)\\SunloginClient\\SunloginClient.exe"},
])

TCP_PORTS_JSON = _json([
    {"LocalAddress": "192.168.1.100", "LocalPort": 40001,
     "RemoteAddress": "203.0.113.9", "RemotePort": 55123, "State": "Established",
     "OwningProcess": 4120},
    {"LocalAddress": "0.0.0.0", "LocalPort": 40000, "RemoteAddress": "::",
     "RemotePort": 0, "State": "Listen", "OwningProcess": 4120},
])

POWERCFG_TEXT = """\
The following sleep states are available on this system:
    Standby (S3)
    Hibernate
    Fast Startup

The following sleep states are not available on this system:
    Standby (S1)
    Standby (S2)
    Connected Sleep
"""


# --------------------------------------------------------------------------- #
# U-0-01 物理网卡枚举（虚拟网卡排除但保留诊断）
# --------------------------------------------------------------------------- #
def test_u0_01_physical_adapters_only_keep_virtual_in_diagnostic():
    adapters = netdiag.parse_adapters(ADAPTERS_JSON, CIM_ADAPTERS_JSON)

    physical = [a for a in adapters if a.category.startswith("physical")]
    assert [a.name for a in physical] == ["以太网", "WLAN"]

    eth = physical[0]
    assert eth.category == "physical-ethernet"
    assert eth.media == "ethernet"
    assert eth.if_index == 12
    assert eth.guid == "{ETH-GUID}"
    # 稳定标识来自 CIM 关联（PNPDeviceID），不依赖显示名
    assert eth.pnp_device_id == "PCI\\VEN_8086&DEV_0D4C&SUBSYS_1234"

    wifi = physical[1]
    assert wifi.category == "physical-wifi"
    assert wifi.media == "wifi"
    assert wifi.pnp_device_id == "PCI\\VEN_8086&DEV_34F0&SUBSYS_5678"

    virtual = [a.name for a in adapters if a.category == "virtual"]
    # Clash TAP 与 Hyper-V/WSL 依 Virtual 标志；Wintun 无标志，依描述关键字
    assert sorted(virtual) == ["ClashTUN", "VPN", "vEthernet (WSL)"]

    disconnected = [a for a in adapters if a.status == "Disconnected"]
    assert [a.name for a in disconnected] == ["VPN"]


def test_u0_01_adapter_join_prefers_guid_and_falls_back_to_mac():
    """GUID 关联为主；GUID 缺失时退化为 MAC（大小写/分隔符不敏感）。"""
    cim = _json([{"Name": "Intel(R) Ethernet Connection (10) I219-V",
                  "GUID": "", "MACAddress": "00:11:22:33:44:55",
                  "PNPDeviceID": "PCI\\FALLBACK"}])
    adapters = netdiag.parse_adapters(ADAPTERS_JSON, cim)
    eth = [a for a in adapters if a.if_index == 12][0]
    assert eth.pnp_device_id == "PCI\\FALLBACK"


# --------------------------------------------------------------------------- #
# U-0-02 接口 Metric 解析（IPv4/IPv6 不混淆）
# --------------------------------------------------------------------------- #
def test_u0_02_interface_metrics_parse_ipv4_ipv6_separately():
    rows = netdiag.parse_ip_interfaces(IP_INTERFACES_JSON)
    assert len(rows) == 3
    eth = [r for r in rows if r["if_index"] == 12]
    assert sorted(r["address_family"] for r in eth) == ["IPv4", "IPv6"]
    wlan = [r for r in rows if r["if_index"] == 7]
    assert len(wlan) == 1 and wlan[0]["address_family"] == "IPv4"
    assert wlan[0]["interface_metric"] == 35
    assert wlan[0]["automatic_metric"] == "Disabled"
    assert eth[0]["automatic_metric"] == "Enabled"


# --------------------------------------------------------------------------- #
# U-0-03 默认路由读取
# --------------------------------------------------------------------------- #
def test_u0_03_default_routes_parsed_with_family_and_metrics():
    routes = netdiag.parse_default_routes(DEFAULT_ROUTES_JSON)
    assert len(routes) == 3
    v4 = [r for r in routes if r["address_family"] == "IPv4"]
    v6 = [r for r in routes if r["address_family"] == "IPv6"]
    assert len(v4) == 2 and len(v6) == 1
    assert v6[0]["next_hop"] == "fe80::1"
    by_if = {r["if_index"]: r for r in v4}
    assert by_if[12]["next_hop"] == "192.168.1.1"
    assert by_if[12]["route_metric"] == 1
    assert by_if[12]["interface_metric"] == 25
    assert by_if[25]["next_hop"] == "0.0.0.0"
    assert by_if[25]["interface_metric"] == 10


# --------------------------------------------------------------------------- #
# U-0-04 双网卡并存判定（策略允许/禁止两分支）
# --------------------------------------------------------------------------- #
def test_u0_04_coexistence_blocked_by_driver_or_policy():
    adapters = netdiag.parse_adapters(ADAPTERS_JSON, CIM_ADAPTERS_JSON)
    wifi = netdiag.parse_wifi(NETSH_INTERFACES_TEXT)
    verdict = netdiag.judge_coexistence(adapters, wifi,
                                        ADVANCED_PROPS_BLOCKING_JSON,
                                        POWER_MGMT_JSON)
    assert verdict["verdict"] == "blocked"
    assert any("有线上网时禁用无线网卡" in e["detail"] for e in verdict["evidence"])
    assert verdict["observed_both_up"] is True


def test_u0_04_coexistence_allowed_when_no_blocking_policy():
    adapters = netdiag.parse_adapters(ADAPTERS_JSON, CIM_ADAPTERS_JSON)
    wifi = netdiag.parse_wifi(NETSH_INTERFACES_TEXT)
    verdict = netdiag.judge_coexistence(adapters, wifi,
                                        ADVANCED_PROPS_BENIGN_JSON,
                                        POWER_MGMT_JSON)
    assert verdict["verdict"] == "allowed"
    assert verdict["observed_both_up"] is True
    # 良性高级属性不应被误判为阻断策略
    assert all("禁用" not in e["detail"] for e in verdict["evidence"])


def test_u0_04_coexistence_undetermined_when_wifi_not_connected():
    adapters = netdiag.parse_adapters(ADAPTERS_JSON, CIM_ADAPTERS_JSON)
    wifi = netdiag.parse_wifi(NETSH_INTERFACES_TEXT)
    wifi["interfaces"][0]["state"] = "disconnected"
    verdict = netdiag.judge_coexistence(adapters, wifi,
                                        ADVANCED_PROPS_BENIGN_JSON,
                                        POWER_MGMT_JSON)
    assert verdict["verdict"] == "undetermined"
    assert verdict["observed_both_up"] is False
    assert "插拔" in verdict["reason"] or "人工" in verdict["reason"]


# --------------------------------------------------------------------------- #
# U-0-05 当前出口识别（Find-NetRoute 成功/失败两分支）
# --------------------------------------------------------------------------- #
def test_u0_05_egress_from_net_route_target():
    egress = netdiag.parse_egress(EGRESS_V4_JSON, target="8.8.8.8")
    assert egress == {"target": "8.8.8.8", "source_ip": "192.168.1.100",
                      "if_index": 12, "interface_alias": "以太网",
                      "interface_metric": 25, "error": None}


def test_u0_05_egress_failure_records_error_without_assumption():
    egress = netdiag.parse_egress((1, "", "Find-NetRoute : No matching route"),
                                  target="8.8.8.8")
    assert egress["if_index"] is None
    assert egress["error"]


# --------------------------------------------------------------------------- #
# U-0-06 无 Wi-Fi 网卡边缘
# --------------------------------------------------------------------------- #
def test_u0_06_no_wifi_adapter_does_not_raise():
    wifi = netdiag.parse_wifi(NETSH_NO_WIRELESS_TEXT)
    assert wifi["available"] is False
    assert wifi["interfaces"] == []
    assert wifi["note"]


def test_u0_06_collect_without_wifi_marks_unavailable(fake_runner):
    eth_only = _json([a for a in json.loads(ADAPTERS_JSON) if a["ifIndex"] == 12])
    fake_runner.responses.update({
        "adapters": eth_only,
        "cim_adapters": "[]",
        "ip_interfaces": "[]",
        "ip_addresses": "[]",
        "default_routes": "[]",
        "egress_v4": EGRESS_V4_JSON,
        "egress_v6": (1, "", "no ipv6"),
        "wifi_interfaces": NETSH_NO_WIRELESS_TEXT,
        "connection_profiles": CONNECTION_PROFILES_JSON,
        "advanced_properties": "[]",
        "power_management": "[]",
        "remote_processes": "[]",
        "sleep_states": POWERCFG_TEXT,
        "winlogon": WINLOGON_JSON,
        "run_entries": RUN_ENTRIES_RAW,
    })
    report = netdiag.collect(fake_runner)
    assert report.wifi["available"] is False
    text = report.render_text()
    assert "无 Wi-Fi" in text


# --------------------------------------------------------------------------- #
# U-0-07 多以太网边缘（不臆断首选）
# --------------------------------------------------------------------------- #
def test_u0_07_multiple_ethernet_all_listed_no_preference():
    payload = json.loads(ADAPTERS_JSON)
    payload.append({"Name": "以太网 2", "InterfaceDescription": "Realtek PCIe GbE",
                    "InterfaceGuid": "{ETH2-GUID}", "ifIndex": 15,
                    "Status": "Up", "MediaType": "802.3", "Virtual": False,
                    "HardwareInterface": True})
    adapters = netdiag.parse_adapters(_json(payload), "[]")
    eths = [a for a in adapters if a.category == "physical-ethernet"]
    assert sorted(a.name for a in eths) == ["以太网", "以太网 2"]

    draft = netdiag.build_adapters_draft(netdiag.DiagReport(
        adapters=adapters, host="PC", generated_at="2026-10-14T00:00:00+08:00"))
    draft_map = {c["name"]: c for c in draft["adapters"]["ethernet_candidates"]}
    assert set(draft_map) == {"以太网", "以太网 2"}
    # 多候选时不得写入首选，必须留给人工确认
    assert draft["adapters"].get("ethernet") is None
    assert draft["warnings"]


# --------------------------------------------------------------------------- #
# Wi-Fi / Profile 解析（P0-T1 SSID 与 Profile；复用 F17 解析器）
# --------------------------------------------------------------------------- #
def test_wifi_interfaces_parse_ssid_profile_state():
    wifi = netdiag.parse_wifi(NETSH_INTERFACES_TEXT, target_ssid="SNNU")
    assert wifi["available"] is True
    assert len(wifi["interfaces"]) == 1
    iface = wifi["interfaces"][0]
    assert iface["name"] == "WLAN"
    assert iface["ssid"] == "SNNU"
    assert iface["profile"] == "SNNU"
    assert iface["state"] == "connected"
    assert iface["matches_target"] is True


def test_wifi_target_mismatch_is_flagged():
    wifi = netdiag.parse_wifi(NETSH_INTERFACES_TEXT, target_ssid="OtherSSID")
    assert wifi["interfaces"][0]["matches_target"] is False


# --------------------------------------------------------------------------- #
# 只读安全自检（防止有人日后往命令表里混入写命令）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [
    "Get-NetAdapter | ConvertTo-Json -Compress",
    "netsh wlan show interfaces",
    "powercfg /a",
    "Find-NetRoute -RemoteIPAddress 8.8.8.8 | ConvertTo-Json -Compress",
])
def test_read_only_guard_accepts_read_commands(script):
    netdiag.assert_read_only(script)  # 不抛异常即通过


@pytest.mark.parametrize("script", [
    "Set-NetIPInterface -InterfaceIndex 12 -InterfaceMetric 10",
    "Get-NetRoute | Remove-NetRoute -Confirm:$false",
    "netsh wlan connect name=SNNU",
    "Get-NetAdapter | Disable-NetAdapter -Confirm:$false",
    "Restart-NetAdapter -Name '以太网'",
])
def test_read_only_guard_rejects_write_commands(script):
    with pytest.raises(ValueError):
        netdiag.assert_read_only(script)


def test_all_diagnostic_commands_are_read_only():
    """命令表整体自检：每一项都必须是只读 cmdlet/工具。"""
    for name, script in netdiag.COMMANDS.items():
        netdiag.assert_read_only(script)


# --------------------------------------------------------------------------- #
# powercfg / 远控 / 自启 / 自动登录解析（P0-T5）
# --------------------------------------------------------------------------- #
def test_powercfg_parses_available_sleep_states():
    states = netdiag.parse_powercfg(POWERCFG_TEXT)
    assert states["standby_available"] is True
    assert states["hibernate_available"] is True
    assert states["mentions_standby"] is True


def test_remote_section_collects_clients_ports_and_unattended_settings():
    processes = netdiag.parse_remote_processes(REMOTE_PROCESSES_JSON)
    assert [p["process_name"] for p in processes] == ["ToDesk", "SunloginClient"]

    ports = netdiag.parse_tcp_ports(TCP_PORTS_JSON)
    listen = [p for p in ports if p["state"] == "Listen"]
    assert listen and listen[0]["local_port"] == 40000

    winlogon = netdiag.parse_winlogon(WINLOGON_JSON)
    assert winlogon["auto_admin_logon"] is True
    assert winlogon["user"] == "lennonli"

    entries = netdiag.parse_run_entries(RUN_ENTRIES_RAW)
    names = {e["name"] for e in entries}
    assert names == {"CampusReconnect", "ToDesk", "CamApp"}
    hives = {e["name"]: e["hive"] for e in entries}
    assert hives["CampusReconnect"] == "HKLM"
    assert hives["CamApp"] == "HKCU"
    assert all("command" in e for e in entries)
