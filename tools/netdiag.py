# -*- coding: utf-8 -*-
"""V0.3 P0 只读网络诊断（netdiag）。

一条命令输出《双网卡与路由基线报告》：枚举物理/虚拟网卡、IP、网关、默认路由、
接口/路由 Metric、Wi-Fi SSID/Profile、当前公网实际出口、双网卡并存策略判定，
以及远控路径与无人值守设置快照。

安全约定（P0 出口条件 1）：
- **全部命令只读**（Get-/Find-/netsh show/powercfg /a），自带
  :func:`assert_read_only` 前置自检，写动词一律拒绝（fail-closed）；
- 不发任何网络探测包（``Find-NetRoute`` 只查路由表，不产生流量）；
- 不写注册表、不碰凭据、不改代理、不动路由；
- 仅在显式给定 ``--adapters-out`` / ``--json-out`` 时才落盘。

机器可读输出（``--json``）为本阶段冻结的 P1 接口契约
（``contract_version = v03-p0-diag-1``），字段覆盖后续
``campus_network.py``（P1）所需的全部输入。
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # 独立运行时导入 campus_windows（F17 解析器）
    sys.path.insert(0, str(PROJECT_ROOT))

from campus_windows import (CONNECTED_STATES,  # noqa: E402
                            parse_netsh_interfaces)

CONTRACT_VERSION = "v03-p0-diag-1"
ADAPTERS_CONTRACT_VERSION = "v03-p0-adapters-1"
DEFAULT_TARGET_SSID = "SNNU"

#: 公网出口识别用的目标地址（仅用于路由表查询，不发包）。
EGRESS_TARGETS: Tuple[Tuple[str, str], ...] = (
    ("public_ipv4", "8.8.8.8"),
    ("public_ipv6", "2606:4700:4700::1111"),
)

#: 远控客户端进程名关键词（常见国内 + 国际客户端 + Tailscale 远程网络层，
#: 可按需扩充）。
REMOTE_CLIENT_PATTERN = ("ToDesk|SunloginClient|AnyDesk|RustDesk|TeamViewer|"
                         "Parsec|Radmin|Splashtop|LiteManager|AnyViewer|"
                         "AweSun|Iperius|VncViewer|UltraVNC|TightVNC|"
                         "WinVNC|Ammyy|Netman|tmremoteclient|RemoteDesktop|"
                         "Tailscale|tailscaled")

#: 虚拟网卡描述关键字（Virtual 标志缺失时的兜底判定）。
VIRTUAL_DESCRIPTION_HINTS = ("tap-", "tun", "hyper-v", "wsl", "virtual",
                             "vpn", "loopback", "pseudo", "teredo",
                             "isatap", "6to4", "miniport")

#: 无线媒体识别关键字。
WIFI_MEDIA_HINTS = ("802.11", "wireless", "wi-fi", "wifi", "wlan", "native 802")
#: 有线媒体识别关键字。
ETHERNET_MEDIA_HINTS = ("802.3", "ethernet", "wired")

#: 阻断"有线上网时禁用无线"的驱动/策略属性名关键字（中英）。
BLOCKING_HINTS = ("有线上网时禁用", "有线上网时关闭", "有线上网时断开",
                  "disable wireless", "disable wifi", "disable wlan",
                  "prefer ethernet", "prefer wired", "prefer lan",
                  "ethernet over", "wired over wireless", "disable when wired",
                  "disablewhenwired", "disablewireless", "preferethernet")

VALUE_ENABLE_WORDS = ("启用", "已启用", "打开", "开启", "enabled", "true", "on", "是")
VALUE_DISABLE_WORDS = ("禁用", "已禁用", "关闭", "disabled", "false", "off", "否")

# --------------------------------------------------------------------------- #
# 只读命令表与安全自检
# --------------------------------------------------------------------------- #
#: 首段必须命中的只读前缀。
READ_ONLY_LEAD = ("Get-", "Find-", "Test-", "netsh", "powercfg")
#: 管道后续阶段允许的安全 cmdlet 前缀。
SAFE_PIPELINE_STAGES = ("Select-", "Where-", "Sort-", "ForEach-", "Measure-",
                        "Group-", "ConvertTo-", "Format-", "Compare-",
                        "Out-String", "Out-Null", "Out-Host", "Out-Default",
                        "Select-String")
#: 写操作动词/工具黑名单（任何管道段出现即拒绝）。
WRITE_VERBS = ("New-", "Set-", "Remove-", "Disable-", "Enable-", "Restart-",
               "Rename-", "Clear-", "Reset-", "Add-", "Start-", "Stop-",
               "Connect-", "Disconnect-", "Register-", "Unregister-",
               "Import-", "Export-", "Install-", "Uninstall-", "Write-",
               "Copy-", "Move-", "Initialize-", "Format-", "Block-",
               "Unblock-", "Grant-", "Revoke-", "Invoke-", "Tee-", "Out-File",
               "Out-GridView", "Suspend-", "Resume-", "Update-", "Optimize-",
               "Repair-", "Mount-", "Dismount-", "Setx", "reg", "sc", "net",
               "wmic", "taskkill", "ipconfig")

_COMMANDS: Dict[str, str] = {
    "adapters": (
        "Get-NetAdapter -IncludeHidden | Select-Object Name,InterfaceDescription,"
        "InterfaceGuid,ifIndex,Status,MacAddress,LinkSpeed,MediaType,"
        "PhysicalMediaType,Virtual,HardwareInterface,ConnectorPresent | "
        "ConvertTo-Json -Depth 2 -Compress"),
    "cim_adapters": (
        "Get-CimInstance Win32_NetworkAdapter | Select-Object Name,"
        "NetConnectionID,GUID,PNPDeviceID,PhysicalAdapter,AdapterType,MACAddress"
        " | ConvertTo-Json -Depth 2 -Compress"),
    "ip_interfaces": (
        "Get-NetIPInterface -IncludeAllCompartments | Select-Object ifIndex,"
        "InterfaceAlias,AddressFamily,InterfaceMetric,AutomaticMetric,Dhcp,"
        "ConnectionState | ConvertTo-Json -Depth 2 -Compress"),
    "ip_addresses": (
        "Get-NetIPAddress | Select-Object ifIndex,InterfaceAlias,AddressFamily,"
        "IPAddress,PrefixLength,PrefixOrigin,SuffixOrigin,AddressState | "
        "ConvertTo-Json -Depth 2 -Compress"),
    "default_routes": (
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0','::/0' -ErrorAction "
        "SilentlyContinue | Select-Object ifIndex,InterfaceAlias,"
        "DestinationPrefix,NextHop,RouteMetric,InterfaceMetric,Protocol,"
        "PolicyStore | ConvertTo-Json -Depth 2 -Compress"),
    "egress_v4": (
        "Find-NetRoute -RemoteIPAddress 8.8.8.8 -ErrorAction SilentlyContinue "
        "| Select-Object IPAddress,InterfaceIndex,InterfaceAlias,NextHop,"
        "DestinationPrefix,InterfaceMetric | ConvertTo-Json -Depth 2 -Compress"),
    "egress_v6": (
        "Find-NetRoute -RemoteIPAddress 2606:4700:4700::1111 -ErrorAction "
        "SilentlyContinue | Select-Object IPAddress,InterfaceIndex,"
        "InterfaceAlias,NextHop,DestinationPrefix,InterfaceMetric | "
        "ConvertTo-Json -Depth 2 -Compress"),
    "wifi_interfaces": "netsh wlan show interfaces",
    "connection_profiles": (
        "Get-NetConnectionProfile | Select-Object InterfaceAlias,"
        "InterfaceIndex,NetworkCategory,IPv4Connectivity,IPv6Connectivity | "
        "ConvertTo-Json -Depth 2 -Compress"),
    "advanced_properties": (
        "Get-NetAdapterAdvancedProperty | Select-Object Name,DisplayName,"
        "DisplayValue,RegistryKeyword,RegistryValue,ValidDisplayValues | "
        "ConvertTo-Json -Depth 3 -Compress"),
    "power_management": (
        "Get-NetAdapterPowerManagement | Select-Object Name,DisplayName,"
        "AllowComputerToTurnOffDevice | ConvertTo-Json -Depth 2 -Compress"),
    "remote_processes": (
        "Get-Process | Where-Object { $_.ProcessName -match '" +
        REMOTE_CLIENT_PATTERN + "' } | Select-Object Id,ProcessName,Path | "
        "ConvertTo-Json -Depth 2 -Compress"),
    "remote_tcp": (
        "Get-NetTCPConnection -OwningProcess {pids} -ErrorAction "
        "SilentlyContinue | Select-Object LocalAddress,LocalPort,"
        "RemoteAddress,RemotePort,State,OwningProcess | ConvertTo-Json "
        "-Depth 2 -Compress"),
    "remote_udp": (
        "Get-NetUDPEndpoint -OwningProcess {pids} -ErrorAction "
        "SilentlyContinue | Select-Object LocalAddress,LocalPort,"
        "OwningProcess | ConvertTo-Json -Depth 2 -Compress"),
    "sleep_states": "powercfg /a",
    "winlogon": (
        "Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\"
        "CurrentVersion\\Winlogon' -ErrorAction SilentlyContinue | "
        "Select-Object AutoAdminLogon,DefaultUserName,DefaultDomainName | "
        "ConvertTo-Json -Compress"),
    "run_entries": (
        "Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\"
        "CurrentVersion\\Run','HKCU:\\SOFTWARE\\Microsoft\\Windows\\"
        "CurrentVersion\\Run' -ErrorAction SilentlyContinue | ConvertTo-Json "
        "-Depth 2 -Compress"),
}

COMMANDS: Dict[str, str] = dict(_COMMANDS)  # 公开只读命令表（测试自检用）

#: PowerShell 前导（只读、静默进度条、强制 UTF-8 输出）。
_POWERSHELL_PRELUDE = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$ProgressPreference='SilentlyContinue';"
    "try{[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)}catch{};"
)


def _mask_script(text: str) -> str:
    """分析前置掩码：引号字符串与 ``{}`` 块替换为占位，避免正则/正文干扰分段。"""
    text = re.sub(r"'[^']*'", "''", text)
    text = re.sub(r'"[^"]*"', '""', text)
    text = re.sub(r"\{[^{}]*\}", " {} ", text)
    return text


def assert_read_only(script: str) -> None:
    """只读自检：拒绝一切可能产生副作用的命令（fail-closed）。

    规则：按 ``|`` / ``;`` 分段（引号与 ``{}`` 块先掩码，防止正则内容误伤）；
    首段必须以只读前缀开头；``netsh`` 仅允许 ``show``、``powercfg`` 仅允许
    ``/a``；任何段命中写动词黑名单即拒绝。
    """
    text = (script or "").strip()
    if not text:
        raise ValueError("空命令不允许执行")
    masked = _mask_script(text)
    chunks = [c.strip() for c in re.split(r"[|;&]+", masked) if c.strip()]
    if not chunks:
        raise ValueError("空命令不允许执行")
    for index, chunk in enumerate(chunks):
        parts = chunk.split()
        head = parts[0]
        if head.startswith("netsh"):
            if len(parts) < 3 or "show" not in parts[1:]:
                raise ValueError("netsh 仅允许 show 子命令：%r" % chunk)
            continue
        if head.startswith("powercfg"):
            if len(parts) != 2 or parts[1].lower() != "/a":
                raise ValueError("powercfg 仅允许只读参数 /a：%r" % chunk)
            continue
        low = head.lower()
        if index == 0 and not head.startswith(READ_ONLY_LEAD):
            raise ValueError("命令不在只读白名单（需以 %s 开头）：%r"
                             % ("/".join(READ_ONLY_LEAD), chunk))
        if index > 0 and not (head.startswith(READ_ONLY_LEAD)
                              or head.startswith(SAFE_PIPELINE_STAGES)):
            raise ValueError("管道阶段不在只读安全清单：%r" % chunk)
        for verb in WRITE_VERBS:
            if low.startswith(verb.lower()):
                raise ValueError("检测到写操作动词 %s：%r" % (verb, chunk))


# --------------------------------------------------------------------------- #
# 通用解析辅助
# --------------------------------------------------------------------------- #
def _pick(obj: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按优先级取属性（大小写/别名不敏感；空值视为缺失）。"""
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def _as_bool(value: Any) -> Optional[bool]:
    """PowerShell 布尔/开关值的宽容解析。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "1", "enabled", "启用", "已启用", "是", "yes", "on"):
        return True
    if text in ("false", "0", "disabled", "禁用", "已禁用", "否", "no", "off"):
        return False
    return None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_list(payload: Any) -> List[Dict[str, Any]]:
    """ConvertTo-Json 输出归一化：单对象→列表，坏 JSON→空列表。"""
    if payload is None:
        return []
    if isinstance(payload, (list, tuple)):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, str):
        text = payload.strip()
        if not text or text in ("null", "{}"):
            return []
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return []
        return _as_list(data)
    return []


def _first(obj: Dict[str, Any], *keys: str) -> Any:
    return _pick(obj, *keys)


# --------------------------------------------------------------------------- #
# 枚举归一化
# --------------------------------------------------------------------------- #
#: PowerShell 5.1 的 ConvertTo-Json 可能把枚举序列化为**整数**（实测本机
#: AddressFamily=23/2、TcpState=2/5、AutomaticMetric=1、RouteProtocol=3），
#: 这里按稳定枚举值还原名称；字符串值原样通过。
_TCP_STATES = {1: "Closed", 2: "Listen", 3: "SynSent", 4: "SynReceived",
               5: "Established", 6: "FinWait1", 7: "FinWait2", 8: "CloseWait",
               9: "Closing", 10: "LastAck", 11: "DeleteWait", 12: "TimeWait"}
_ADDRESS_FAMILIES = {2: "IPv4", 10: "IPv6", 23: "IPv6"}  # InterNetwork/V6
_AUTOMATIC_METRIC = {0: "Disabled", 1: "Enabled"}
_ROUTE_PROTOCOLS = {1: "Other", 2: "Local", 3: "NetMgmt", 4: "IcmpRedirect",
                    5: "Eigrp", 6: "Bgp", 7: "Rip", 8: "Static", 9: "Dump",
                    10: "Bbn", 11: "Ndis"}


def _enum_name(value: Any, mapping: Dict[int, str], default: str = "") -> str:
    """枚举值归一化：整数或数字字符串按映射还原，其余原样返回。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return mapping.get(value, str(value))
    text = str(value).strip()
    if not text:
        return default
    if text.isdigit():
        return mapping.get(int(text), text)
    return text


# --------------------------------------------------------------------------- #
# 结构化记录
# --------------------------------------------------------------------------- #
@dataclass
class AdapterRecord:
    """一张网卡的诊断记录（物理/虚拟分类 + 稳定标识）。"""

    name: str = ""
    description: str = ""
    guid: str = ""
    if_index: Optional[int] = None
    status: str = ""
    mac: str = ""
    link_speed: str = ""
    media_type: str = ""
    physical_media_type: str = ""
    virtual: Optional[bool] = None
    hardware: Optional[bool] = None
    connector: Optional[bool] = None
    category: str = "unknown"   # physical-ethernet/physical-wifi/physical-other/virtual
    media: str = "unknown"      # ethernet/wifi/other/virtual/unknown
    pnp_device_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "guid": self.guid, "if_index": self.if_index,
                "status": self.status, "mac": self.mac,
                "link_speed": self.link_speed, "media_type": self.media_type,
                "physical_media_type": self.physical_media_type,
                "virtual": self.virtual, "hardware": self.hardware,
                "connector": self.connector, "category": self.category,
                "media": self.media, "pnp_device_id": self.pnp_device_id}


def _media_kind(media_type: str, physical_media: str, description: str) -> str:
    text = ("%s %s %s" % (media_type, physical_media, description)).lower()
    if any(hint in text for hint in WIFI_MEDIA_HINTS):
        return "wifi"
    if any(hint in text for hint in ETHERNET_MEDIA_HINTS):
        return "ethernet"
    return "other"


def _classify(media_type: str, physical_media: str, description: str,
              virtual: Optional[bool], hardware: Optional[bool]) -> Tuple[str, str]:
    """返回 ``(category, media)``；无法判定时给 unknown，不臆断。"""
    desc_l = (description or "").lower()
    virtual_hint = any(h in desc_l for h in VIRTUAL_DESCRIPTION_HINTS)
    if virtual is True or (virtual is None and virtual_hint):
        return "virtual", "virtual"
    media = _media_kind(media_type, physical_media, desc_l)
    if media in ("ethernet", "wifi"):
        return "physical-%s" % media, media
    if virtual is False and hardware is True:
        return "physical-other", "other"
    if virtual is False and hardware is False:
        # 非硬件且无 Virtual 标志：视为虚拟/伪接口（如部分 VPN/TUN）。
        return "virtual", "virtual"
    return "unknown", "unknown"


def parse_adapters(payload: Any, cim_payload: Any = None) -> List[AdapterRecord]:
    """解析 ``Get-NetAdapter`` 输出并与 CIM 信息关联（PNPDeviceID）。"""
    cim_rows = _as_list(cim_payload)
    by_guid: Dict[str, Dict[str, Any]] = {}
    by_mac: Dict[str, Dict[str, Any]] = {}
    for row in cim_rows:
        guid = str(_pick(row, "GUID", "InterfaceGuid") or "").strip()
        mac = re.sub(r"[^0-9a-f]", "", str(
            _pick(row, "MACAddress", "MacAddress") or "").lower())
        if guid:
            by_guid[guid.lower()] = row
        if mac and mac != "000000000000":
            by_mac[mac] = row

    records: List[AdapterRecord] = []
    for row in _as_list(payload):
        guid = str(_pick(row, "InterfaceGuid", "GUID") or "").strip()
        mac = re.sub(r"[^0-9a-f]", "", str(
            _pick(row, "MacAddress", "MACAddress") or "").lower())
        media_type = str(_pick(row, "MediaType") or "")
        physical_media = str(_pick(row, "PhysicalMediaType") or "")
        description = str(_pick(row, "InterfaceDescription") or "")
        virtual = _as_bool(_first(row, "Virtual"))
        hardware = _as_bool(_first(row, "HardwareInterface"))
        category, media = _classify(media_type, physical_media, description,
                                    virtual, hardware)
        matched = by_guid.get(guid.lower()) if guid else None
        if matched is None and mac:
            matched = by_mac.get(mac)
        records.append(AdapterRecord(
            name=str(_pick(row, "Name", "InterfaceAlias") or ""),
            description=description,
            guid=guid,
            if_index=_as_int(_pick(row, "ifIndex", "InterfaceIndex")),
            status=str(_pick(row, "Status") or ""),
            mac=str(_pick(row, "MacAddress", "MACAddress") or ""),
            link_speed=str(_pick(row, "LinkSpeed") or ""),
            media_type=media_type,
            physical_media_type=physical_media,
            virtual=virtual,
            hardware=hardware,
            connector=_as_bool(_first(row, "ConnectorPresent")),
            category=category,
            media=media,
            pnp_device_id=str((matched or {}).get("PNPDeviceID") or ""),
        ))
    return records


def parse_ip_interfaces(payload: Any) -> List[Dict[str, Any]]:
    """解析 ``Get-NetIPInterface``（IPv4/IPv6 分开，Metric 不混淆）。"""
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        rows.append({
            "if_index": _as_int(_pick(row, "ifIndex", "InterfaceIndex")),
            "interface_alias": str(_pick(row, "InterfaceAlias") or ""),
            "address_family": _enum_name(_pick(row, "AddressFamily"),
                                          _ADDRESS_FAMILIES),
            "interface_metric": _as_int(_pick(row, "InterfaceMetric")),
            "automatic_metric": _enum_name(_pick(row, "AutomaticMetric"),
                                            _AUTOMATIC_METRIC),
            "dhcp": str(_pick(row, "Dhcp") or ""),
            "connection_state": str(_pick(row, "ConnectionState") or ""),
        })
    return rows


def parse_ip_addresses(payload: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        rows.append({
            "if_index": _as_int(_pick(row, "ifIndex", "InterfaceIndex")),
            "interface_alias": str(_pick(row, "InterfaceAlias") or ""),
            "address_family": _enum_name(_pick(row, "AddressFamily"),
                                          _ADDRESS_FAMILIES),
            "ip_address": str(_pick(row, "IPAddress") or ""),
            "prefix_length": _as_int(_pick(row, "PrefixLength")),
            "address_state": str(_pick(row, "AddressState") or ""),
        })
    return rows


def parse_default_routes(payload: Any) -> List[Dict[str, Any]]:
    """解析默认路由（0.0.0.0/0 与 ::/0）；地址族由前缀推导。"""
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        prefix = str(_pick(row, "DestinationPrefix") or "")
        family = "IPv6" if ":" in prefix else "IPv4"
        rows.append({
            "if_index": _as_int(_pick(row, "ifIndex", "InterfaceIndex")),
            "interface_alias": str(_pick(row, "InterfaceAlias") or ""),
            "destination_prefix": prefix,
            "address_family": family,
            "next_hop": str(_pick(row, "NextHop") or ""),
            "route_metric": _as_int(_pick(row, "RouteMetric")),
            "interface_metric": _as_int(_pick(row, "InterfaceMetric")),
            "protocol": _enum_name(_pick(row, "Protocol"), _ROUTE_PROTOCOLS),
            "policy_store": str(_pick(row, "PolicyStore") or "") or None,
        })
    return rows


def parse_egress(payload: Any, target: str) -> Dict[str, Any]:
    """解析 ``Find-NetRoute`` 出口识别结果；失败如实记录，不假定成功。

    注意：PowerShell 存在**非终止错误**时退出码为非 0 但仍可能产出有效
    输出（实测），因此以 stdout 是否可解析为准，而非退出码。
    """
    outcome: Dict[str, Any] = {"target": target, "source_ip": None,
                               "if_index": None, "interface_alias": None,
                               "interface_metric": None, "error": None}
    if isinstance(payload, tuple):
        rc, out, err = int(payload[0]), str(payload[1]), str(payload[2])
        if not out.strip():
            outcome["error"] = (err or out or ("命令返回码 %s" % rc)).strip()[:200]
            return outcome
        payload = out
    rows = _as_list(payload)
    if not rows:
        outcome["error"] = "路由表未给出可达路径（可能无默认路由）"
        return outcome
    row = rows[0]
    outcome["source_ip"] = str(_pick(row, "IPAddress") or "") or None
    outcome["if_index"] = _as_int(_pick(row, "InterfaceIndex", "ifIndex"))
    outcome["interface_alias"] = str(_pick(row, "InterfaceAlias") or "") or None
    outcome["interface_metric"] = _as_int(_pick(row, "InterfaceMetric"))
    return outcome


def parse_wifi(text: Any, target_ssid: str = "") -> Dict[str, Any]:
    """解析 ``netsh wlan show interfaces``（复用 F17 解析器，零重复实现）。"""
    text = text or ""
    interfaces = parse_netsh_interfaces(text)
    target = (target_ssid or "").strip()
    rows: List[Dict[str, Any]] = []
    for iface in interfaces:
        rows.append({
            "name": iface.name,
            "ssid": iface.ssid,
            "profile": iface.profile,
            "bssid": iface.bssid,
            "state": iface.state,
            "connected": iface.connected,
            "matches_target": (iface.ssid or "").strip() == target if target else None,
        })
    if not interfaces:
        lowered = str(text).lower()
        note = ("系统未报告无线接口（netsh 无输出或无无线网卡）"
                if not str(text).strip()
                else "netsh 输出中未解析到无线接口")
        if "no wireless interface" in lowered or "没有无线" in lowered:
            note = "系统无无线接口（netdiag 判定：本机不具备 Wi-Fi 网卡）"
        return {"available": False, "interfaces": [], "note": note,
                "target_ssid": target or None}
    return {"available": True, "interfaces": rows, "note": None,
            "target_ssid": target or None}


def parse_connection_profiles(payload: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        rows.append({
            "interface_alias": str(_pick(row, "InterfaceAlias") or ""),
            "if_index": _as_int(_pick(row, "InterfaceIndex", "ifIndex")),
            "network_category": str(_pick(row, "NetworkCategory") or ""),
            "ipv4_connectivity": str(_pick(row, "IPv4Connectivity") or ""),
            "ipv6_connectivity": str(_pick(row, "IPv6Connectivity") or ""),
        })
    return rows


def _value_active(prop: Dict[str, Any]) -> bool:
    """高级属性值是否处于"生效"状态（RegistryValue 优先，显示值兜底）。"""
    registry = _pick(prop, "RegistryValue")
    if isinstance(registry, bool):
        return registry
    if registry is not None and _as_int(registry) == 1:
        return True
    display = str(_pick(prop, "DisplayValue") or "").strip().lower()
    if not display:
        return False
    if display in VALUE_DISABLE_WORDS:
        return False
    return display in VALUE_ENABLE_WORDS


def parse_advanced_properties(payload: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        display_name = str(_pick(row, "DisplayName") or "")
        keyword = str(_pick(row, "RegistryKeyword") or "").lower()
        blocks = (any(hint in display_name.lower() for hint in BLOCKING_HINTS)
                  or any(hint in keyword for hint in BLOCKING_HINTS))
        rows.append({
            "adapter": str(_pick(row, "Name") or ""),
            "display_name": display_name,
            "display_value": str(_pick(row, "DisplayValue") or ""),
            "registry_keyword": keyword,
            "blocks_wifi_when_wired": bool(blocks and _value_active(row)),
            "value_active": _value_active(row),
        })
    return rows


def parse_power_management(payload: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        rows.append({
            "adapter": str(_pick(row, "Name") or ""),
            "display_name": str(_pick(row, "DisplayName") or ""),
            "allow_turn_off": str(
                _pick(row, "AllowComputerToTurnOffDevice") or ""),
        })
    return rows


def judge_coexistence(adapters: Sequence[AdapterRecord],
                      wifi: Dict[str, Any],
                      advanced_raw: Any = None,
                      power_raw: Any = None,
                      ip_addresses: Any = None) -> Dict[str, Any]:
    """双网卡并存判定（P0-T3）。

    判定阶梯：
    1. 存在生效的"有线上网时禁用无线"驱动/策略属性 → ``blocked``；
    2. 以太网 Up 且 WLAN 已关联且持有 IPv4（当前实测并存）→ ``allowed``；
    3. 其余（含任一条件缺失）→ ``undetermined``，注明需人工插拔验证。

    所有证据（含良性属性）都进入 ``evidence`` 供人工复核（R-0-01）。
    """
    advanced = parse_advanced_properties(advanced_raw)
    power = parse_power_management(power_raw)

    ethernet_up = any(a.category == "physical-ethernet" and a.status == "Up"
                      for a in adapters)
    wifi_connected = bool(wifi.get("available")) and any(
        str(i.get("state") or "").strip().lower() in CONNECTED_STATES
        for i in wifi.get("interfaces", []))
    wifi_indices = {a.if_index for a in adapters if a.category == "physical-wifi"}
    wifi_has_ipv4 = None
    if ip_addresses is not None and wifi_indices:
        wifi_has_ipv4 = any(
            row.get("if_index") in wifi_indices
            and row.get("address_family") == "IPv4"
            and str(row.get("ip_address") or "").startswith(("10.", "172.",
                                                             "192.168."))
            for row in parse_ip_addresses(ip_addresses))

    observed_both_up = bool(ethernet_up and wifi_connected
                           and wifi_has_ipv4 is not False)

    evidence: List[Dict[str, Any]] = []
    blocking: List[str] = []
    for prop in advanced:
        detail = "%s=%s「%s」" % (prop["adapter"], prop["display_name"],
                                 prop["display_value"])
        entry = {"source": "网卡高级属性", "detail": detail,
                 "blocks": prop["blocks_wifi_when_wired"]}
        evidence.append(entry)
        if prop["blocks_wifi_when_wired"]:
            blocking.append(detail)
    for entry in power:
        if entry["allow_turn_off"].lower() in ("enabled", "启用", "已启用"):
            evidence.append({
                "source": "电源管理",
                "detail": "%s：允许计算机关闭此设备（ Risk：可能中断无线关联）"
                          % entry["adapter"],
                "blocks": False})
        else:
            evidence.append({
                "source": "电源管理",
                "detail": "%s：已禁止关机管理（有利于保持关联）" % entry["adapter"],
                "blocks": False})

    if blocking:
        verdict = "blocked"
        reason = ("发现生效的“有线上网时禁用无线”类驱动/策略属性：%s；"
                  "结论：系统/驱动会在以太网在线时限制 WLAN。" % "；".join(blocking))
    elif observed_both_up:
        verdict = "allowed"
        reason = ("未发现阻断策略，且当前实测以太网 Up、WLAN 已关联并持有 IPv4"
                  "（两张网卡并存）。最终确认仍需人工插拔验证（R-0-02）。")
    else:
        verdict = "undetermined"
        reason = ("未发现阻断策略，但当前无法同时观测到两网卡在线"
                  "（以太网 Up=%s，WLAN 关联=%s），需人工插拔网线/Wi-Fi 验证"
                  "（R-0-02）后给出最终结论。" % (ethernet_up, wifi_connected))

    return {"verdict": verdict, "reason": reason, "evidence": evidence,
            "observed_both_up": observed_both_up,
            "observed": {"ethernet_up": ethernet_up,
                         "wifi_connected": wifi_connected,
                         "wifi_has_ipv4": wifi_has_ipv4}}


def parse_powercfg(text: Any) -> Dict[str, Any]:
    """解析 ``powercfg /a``（区分"可用/不可用"两段，防误读）。"""
    raw = str(text or "").strip()
    lowered = raw.lower()
    if not raw:
        return {"standby_available": None, "hibernate_available": None,
                "mentions_standby": None, "note": "powercfg 无输出", "raw": ""}
    split = re.search(r"(not available|不可用|无法使用)", lowered)
    head = lowered[:split.start()] if split else lowered
    return {
        "standby_available": ("standby" in head or "待机" in head),
        "hibernate_available": ("hibernate" in head or "休眠" in head),
        "mentions_standby": ("standby" in lowered or "待机" in lowered),
        "note": None,
        "raw": raw[:1500],
    }


def parse_winlogon(payload: Any) -> Dict[str, Any]:
    rows = _as_list(payload)
    row = rows[0] if rows else {}
    return {
        "auto_admin_logon": _as_bool(_pick(row, "AutoAdminLogon")),
        "user": str(_pick(row, "DefaultUserName") or "") or None,
        "domain": str(_pick(row, "DefaultDomainName") or "") or None,
    }


def parse_run_entries(payload: Any) -> List[Dict[str, Any]]:
    """解析 Run 键（PS 元数据属性剔除，hive 从 PSPath 还原）。"""
    rows: List[Dict[str, Any]] = []
    meta = {"pspath", "psparentpath", "pschildname", "psdrive", "psprovider"}
    for row in _as_list(payload):
        path = str(_pick(row, "PSPath") or "")
        hive = "?"
        if "HKEY_LOCAL_MACHINE" in path.upper():
            hive = "HKLM"
        elif "HKEY_CURRENT_USER" in path.upper():
            hive = "HKCU"
        for key, value in row.items():
            name = str(key)
            if name.lower() in meta or name.startswith("PS"):
                continue
            rows.append({"hive": hive, "name": name,
                         "command": str(value or "")})
    return rows


def parse_remote_processes(payload: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        rows.append({"pid": _as_int(_pick(row, "Id")),
                     "process_name": str(_pick(row, "ProcessName") or ""),
                     "path": str(_pick(row, "Path") or "") or None})
    return rows


def parse_tcp_ports(payload: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        rows.append({
            "local_address": str(_pick(row, "LocalAddress") or ""),
            "local_port": _as_int(_pick(row, "LocalPort")),
            "remote_address": str(_pick(row, "RemoteAddress") or ""),
            "remote_port": _as_int(_pick(row, "RemotePort")),
            "state": _enum_name(_pick(row, "State"), _TCP_STATES),
            "pid": _as_int(_pick(row, "OwningProcess")),
        })
    return rows


def parse_udp_ports(payload: Any) -> List[Dict[str, Any]]:
    """解析 UDP 端点（Tailscale 等仅用 UDP 的远程网络层）。"""
    rows: List[Dict[str, Any]] = []
    for row in _as_list(payload):
        rows.append({
            "local_address": str(_pick(row, "LocalAddress") or ""),
            "local_port": _as_int(_pick(row, "LocalPort")),
            "pid": _as_int(_pick(row, "OwningProcess")),
        })
    return rows


def build_interface_summary(adapters: Sequence[AdapterRecord],
                            ip_interfaces: Sequence[Dict[str, Any]],
                            ip_addresses: Sequence[Dict[str, Any]],
                            routes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按物理网卡汇总 IP/网关/Metric（人类可读报告用）。"""
    rows: List[Dict[str, Any]] = []
    for adapter in adapters:
        if not adapter.category.startswith("physical") or adapter.if_index is None:
            continue
        v4 = [r for r in ip_addresses
              if r["if_index"] == adapter.if_index and r["address_family"] == "IPv4"]
        v6 = [r for r in ip_addresses
              if r["if_index"] == adapter.if_index and r["address_family"] == "IPv6"]
        metric = {}
        for row in ip_interfaces:
            if row["if_index"] == adapter.if_index:
                metric[row["address_family"]] = row
        gw = {r["address_family"]: r["next_hop"] for r in routes
              if r["if_index"] == adapter.if_index and r["next_hop"]}
        rows.append({
            "name": adapter.name,
            "if_index": adapter.if_index,
            "status": adapter.status,
            "ipv4": [r["ip_address"] for r in v4],
            "ipv6": [r["ip_address"] for r in v6],
            "gateway_v4": gw.get("IPv4"),
            "gateway_v6": gw.get("IPv6"),
            "interface_metric_v4": metric.get("IPv4", {}).get("interface_metric"),
            "interface_metric_v6": metric.get("IPv6", {}).get("interface_metric"),
            "automatic_metric_v4": metric.get("IPv4", {}).get("automatic_metric"),
            "automatic_metric_v6": metric.get("IPv6", {}).get("automatic_metric"),
        })
    return rows


# --------------------------------------------------------------------------- #
# 汇总报告
# --------------------------------------------------------------------------- #
@dataclass
class DiagReport:
    """一份完整只读诊断（P1 接口契约的数据源）。"""

    contract_version: str = CONTRACT_VERSION
    generated_at: str = ""
    host: str = ""
    os_version: str = ""
    adapters: List[AdapterRecord] = field(default_factory=list)
    ip_interfaces: List[Dict[str, Any]] = field(default_factory=list)
    ip_addresses: List[Dict[str, Any]] = field(default_factory=list)
    default_routes: List[Dict[str, Any]] = field(default_factory=list)
    egress: Dict[str, Any] = field(default_factory=dict)
    wifi: Dict[str, Any] = field(default_factory=dict)
    connection_profiles: List[Dict[str, Any]] = field(default_factory=list)
    coexistence: Dict[str, Any] = field(default_factory=dict)
    interface_summary: List[Dict[str, Any]] = field(default_factory=list)
    remote_control: Dict[str, Any] = field(default_factory=dict)
    errors: List[Dict[str, str]] = field(default_factory=list)
    commands_executed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "generated_at": self.generated_at,
            "host": self.host,
            "os_version": self.os_version,
            "adapters": [a.to_dict() for a in self.adapters],
            "ip_interfaces": list(self.ip_interfaces),
            "ip_addresses": list(self.ip_addresses),
            "default_routes": list(self.default_routes),
            "egress": dict(self.egress),
            "wifi": dict(self.wifi),
            "connection_profiles": list(self.connection_profiles),
            "coexistence": dict(self.coexistence),
            "interface_summary": list(self.interface_summary),
            "remote_control": dict(self.remote_control),
            "errors": list(self.errors),
            "commands_executed": list(self.commands_executed),
        }

    # ---- 人类可读渲染 ---- #
    def render_text(self) -> str:
        lines: List[str] = []
        add = lines.append
        add("=" * 72)
        add("V0.3 P0 双网卡与路由基线报告（只读诊断，零写入）")
        add("契约版本: %s    生成时间: %s" % (self.contract_version, self.generated_at))
        add("主机: %s    系统: %s" % (self.host, self.os_version))
        add("=" * 72)

        add("")
        add("[1] 网卡清单")
        physical = [a for a in self.adapters if a.category.startswith("physical")]
        virtual = [a for a in self.adapters if a.category == "virtual"]
        unknown = [a for a in self.adapters if a.category == "unknown"]
        add("  物理网卡（%d 张）：" % len(physical))
        for a in physical:
            role = {"physical-ethernet": "以太网", "physical-wifi": "Wi-Fi",
                    "physical-other": "其他物理"}.get(a.category, "未分类")
            add("    - %s [%s] ifIndex=%s 状态=%s MAC=%s" %
                (a.name, role, a.if_index, a.status, a.mac))
            add("      InterfaceGuid=%s" % (a.guid or "?"))
            add("      PNPDeviceID=%s" % (a.pnp_device_id or "(未关联)"))
            add("      描述=%s 速率=%s" % (a.description or "?", a.link_speed or "?"))
        if virtual:
            add("  虚拟/软件网卡（%d 张，已排除自动优先级修改，仅保留诊断）："
                % len(virtual))
            for a in virtual:
                add("    - %s ifIndex=%s 状态=%s 描述=%s" %
                    (a.name, a.if_index, a.status, a.description))
        if unknown:
            add("  无法分类网卡（%d 张，需人工确认）：" % len(unknown))
            for a in unknown:
                add("    - %s ifIndex=%s 状态=%s 描述=%s" %
                    (a.name, a.if_index, a.status, a.description))

        add("")
        add("[2] 接口汇总（IP / 网关 / Metric）")
        if self.interface_summary:
            for row in self.interface_summary:
                add("  %s (ifIndex=%s, 状态=%s)" %
                    (row["name"], row["if_index"], row["status"]))
                add("    IPv4=%s 网关=%s Metric=%s(自动=%s)" %
                    (",".join(row["ipv4"]) or "-", row["gateway_v4"] or "-",
                     row["interface_metric_v4"], row["automatic_metric_v4"]))
                add("    IPv6=%s 网关=%s Metric=%s(自动=%s)" %
                    (",".join(row["ipv6"]) or "-", row["gateway_v6"] or "-",
                     row["interface_metric_v6"], row["automatic_metric_v6"]))
        else:
            add("  （无物理网卡数据）")

        add("")
        add("[3] 默认路由")
        if self.default_routes:
            for r in self.default_routes:
                add("    %-8s via %-15s ifIndex=%-4s RouteMetric=%s "
                    "InterfaceMetric=%s (%s)" %
                    (r["destination_prefix"], r["next_hop"] or "-", r["if_index"],
                     r["route_metric"], r["interface_metric"],
                     r["interface_alias"]))
        else:
            add("    （无默认路由数据）")

        add("")
        add("[4] 当前公网出口（Find-NetRoute 路由表查询，不发包）")
        for key, label in (("public_ipv4", "目标 8.8.8.8 (IPv4)"),
                           ("public_ipv6", "目标 2606:4700:4700::1111 (IPv6)")):
            item = self.egress.get(key, {})
            if item.get("error"):
                add("    %s: 识别失败 —— %s" % (label, item["error"]))
            else:
                add("    %s: 源地址=%s 出口 ifIndex=%s（%s）InterfaceMetric=%s" %
                    (label, item.get("source_ip"), item.get("if_index"),
                     item.get("interface_alias"), item.get("interface_metric")))

        add("")
        add("[5] Wi-Fi")
        if self.wifi.get("available"):
            for iface in self.wifi["interfaces"]:
                add("    接口=%s SSID=%r Profile=%r 状态=%s 匹配目标=%s" %
                    (iface["name"], iface["ssid"], iface["profile"],
                     iface["state"], iface["matches_target"]))
        else:
            add("    无 Wi-Fi 适配器（%s）" % (self.wifi.get("note") or
                                             "netsh 未报告无线接口"))

        add("")
        add("[6] 双网卡并存判定（P0-T3）")
        coexist = self.coexistence or {}
        verdict_cn = {"allowed": "允许并存（当前实测）", "blocked": "被策略/驱动阻止",
                      "undetermined": "暂不能确定"}.get(coexist.get("verdict"),
                                                      coexist.get("verdict"))
        add("    结论: %s" % verdict_cn)
        add("    依据: %s" % coexist.get("reason", ""))
        if coexist.get("evidence"):
            add("    证据明细:")
            for item in coexist["evidence"]:
                add("      [%s]%s%s" % (item["source"], item["detail"],
                                        "  ← 阻断项" if item["blocks"] else ""))

        add("")
        add("[7] 远控路径与无人值守设置（P0-T5）")
        remote = self.remote_control or {}
        clients = remote.get("clients") or []
        add("    远控客户端进程: %s" % (", ".join(
            "%s(pid=%s)" % (c["process_name"], c["pid"]) for c in clients)
            or "未检测到已知远控客户端"))
        ports = remote.get("ports") or []
        listening = [p for p in ports if p["state"] == "Listen"]
        established = [p for p in ports if p["state"] == "Established"]
        add("    远控相关端口: 监听 %d 个%s；已建立 %d 条" %
            (len(listening),
             ("（%s）" % ", ".join(str(p["local_port"]) for p in listening))
             if listening else "",
             len(established)))
        udp = remote.get("udp_endpoints") or []
        if udp:
            add("    UDP 端点: %s" % ", ".join(
                "%s:%s(pid=%s)" % (u["local_address"], u["local_port"], u["pid"])
                for u in udp[:10]))
        sleep = remote.get("sleep_states") or {}
        if sleep.get("mentions_standby") is None:
            add("    睡眠状态: 查询失败/无输出")
        else:
            add("    睡眠状态: Standby 可用=%s 休眠可用=%s（无人值守需在电源选项"
                "中关闭睡眠/休眠）" % (sleep.get("standby_available"),
                                      sleep.get("hibernate_available")))
        logon = remote.get("auto_logon") or {}
        add("    自动登录: AutoAdminLogon=%s 用户=%s" %
            (logon.get("auto_admin_logon"), logon.get("user")))
        entries = remote.get("run_entries") or []
        add("    自启项 Run（%d 个）:" % len(entries))
        for entry in entries:
            add("      [%s] %s = %s" % (entry["hive"], entry["name"],
                                        entry["command"]))

        if self.errors:
            add("")
            add("[8] 采集错误（%d 项，不影响已采集部分）:" % len(self.errors))
            for item in self.errors:
                add("    - %s: %s" % (item["command"], item["error"]))

        add("")
        add("-" * 72)
        add("说明: 本报告全部命令只读（已通过 assert_read_only 自检）；")
        add("      --json 输出为 P1 接口契约（%s）。" % CONTRACT_VERSION)
        add("-" * 72)
        return "\n".join(lines)


def collect(runner: Any, *, target_ssid: str = DEFAULT_TARGET_SSID,
            host: Optional[str] = None) -> DiagReport:
    """执行全部只读诊断项并汇总（单项失败不影响其他项）。"""
    errors: List[Dict[str, str]] = []
    executed: List[str] = []

    def _call(name: str, pids: Optional[Sequence[int]] = None):
        script = COMMANDS[name]
        if pids:
            script = script.replace("{pids}", ",".join(str(p) for p in pids))
        executed.append(name)
        try:
            rc, out, err = runner.run(name, script)
            rc, out, err = int(rc), str(out), str(err)
        except Exception as exc:
            errors.append({"command": name,
                           "error": "%s: %s" % (type(exc).__name__, exc)})
            return None
        if rc != 0 and not out.strip():
            errors.append({"command": name,
                           "error": ((err or out) or ("返回码 %s" % rc)).strip()[:300]})
        elif rc != 0:
            # PowerShell 非终止错误：rc!=0 但产出有效输出（实测常见），采用输出
            errors.append({"command": name,
                           "error": "命令返回码 %s 但有有效输出（非终止错误，已采用）：%s"
                                    % (rc, (err or "").strip()[:200])})
        return rc, out, err

    def _out(name: str, pids: Optional[Sequence[int]] = None) -> Optional[str]:
        raw = _call(name, pids=pids)
        if raw is None:
            return None
        return raw[1] or None

    adapters = parse_adapters(_out("adapters"), _out("cim_adapters"))
    ip_interfaces = parse_ip_interfaces(_out("ip_interfaces"))
    ip_addresses = parse_ip_addresses(_out("ip_addresses"))
    routes = parse_default_routes(_out("default_routes"))

    egress: Dict[str, Any] = {}
    for key, target in EGRESS_TARGETS:
        egress[key] = parse_egress(_call("egress_%s" % ("v4" if "4" in key
                                                        else "v6")), target)

    wifi = parse_wifi(_out("wifi_interfaces"), target_ssid=target_ssid)
    profiles = parse_connection_profiles(_out("connection_profiles"))
    advanced_raw = _out("advanced_properties")
    power_raw = _out("power_management")
    coexistence = judge_coexistence(adapters, wifi, advanced_raw, power_raw,
                                    ip_addresses=_out("ip_addresses"))

    processes_raw = _call("remote_processes")
    processes = parse_remote_processes(processes_raw[1]) \
        if processes_raw and processes_raw[1].strip() else []
    ports: List[Dict[str, Any]] = []
    udp_ports: List[Dict[str, Any]] = []
    pids = [p["pid"] for p in processes if p["pid"]]
    if pids:
        tcp_raw = _call("remote_tcp", pids=pids)
        if tcp_raw and tcp_raw[1].strip():
            ports = parse_tcp_ports(tcp_raw[1])
        udp_raw = _call("remote_udp", pids=pids)
        if udp_raw and udp_raw[1].strip():
            udp_ports = parse_udp_ports(udp_raw[1])

    remote_control = {
        "clients": processes,
        "ports": ports,
        "udp_endpoints": udp_ports,
        "sleep_states": parse_powercfg(_out("sleep_states")),
        "auto_logon": parse_winlogon(_out("winlogon")),
        "run_entries": parse_run_entries(_out("run_entries")),
    }

    return DiagReport(
        generated_at=datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"),
        host=host or platform.node(),
        os_version=platform.platform(),
        adapters=adapters,
        ip_interfaces=ip_interfaces,
        ip_addresses=ip_addresses,
        default_routes=routes,
        egress=egress,
        wifi=wifi,
        connection_profiles=profiles,
        coexistence=coexistence,
        interface_summary=build_interface_summary(adapters, ip_interfaces,
                                                  ip_addresses, routes),
        remote_control=remote_control,
        errors=errors,
        commands_executed=executed,
    )


def build_adapters_draft(report: DiagReport) -> Dict[str, Any]:
    """生成 ``adapters.json`` 草案（P0-T2：稳定标识 + 角色指派，不依赖显示名）。"""

    def _entry(adapter: AdapterRecord) -> Dict[str, Any]:
        return {"name": adapter.name, "description": adapter.description,
                "guid": adapter.guid, "if_index": adapter.if_index,
                "mac": adapter.mac, "pnp_device_id": adapter.pnp_device_id,
                "status": adapter.status}

    eth = [a for a in report.adapters if a.category == "physical-ethernet"]
    wifi = [a for a in report.adapters if a.category == "physical-wifi"]
    warnings: List[str] = []
    if len(eth) != 1:
        warnings.append("物理以太网网卡数量为 %d（预期 1）：ethernet 角色未自动指派，"
                        "需人工确认后编辑本文件。" % len(eth))
    if len(wifi) != 1:
        warnings.append("物理 Wi-Fi 网卡数量为 %d（预期 1）：wifi 角色未自动指派，"
                        "需人工确认后编辑本文件。" % len(wifi))
    for adapter in eth + wifi:
        if not adapter.pnp_device_id:
            warnings.append("%s：未关联到 PNPDeviceID（CIM 关联失败），稳定标识仅"
                            " InterfaceGuid。" % adapter.name)
    return {
        "contract_version": ADAPTERS_CONTRACT_VERSION,
        "generated_at": report.generated_at,
        "host": report.host,
        "adapters": {
            "ethernet": _entry(eth[0]) if len(eth) == 1 else None,
            "wifi": _entry(wifi[0]) if len(wifi) == 1 else None,
            "ethernet_candidates": [_entry(a) for a in eth],
            "wifi_candidates": [_entry(a) for a in wifi],
        },
        "warnings": warnings,
        "notes": "标识优先级：PNPDeviceID > InterfaceGuid > MAC；重启 Wi-Fi/插拔"
                 "网线后需复测（P0 出口条件 4）。",
    }


# --------------------------------------------------------------------------- #
# 真实执行器（Windows only；每条命令都先过只读自检）
# --------------------------------------------------------------------------- #
_CREATE_NO_WINDOW = 0x08000000


def _decode(raw: bytes) -> str:
    """PowerShell 重定向输出解码（UTF-8 优先，GBK 兜底，不抛异常）。"""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    if "\ufffd" in text:
        for encoding in ("gbk", "mbcs", "latin-1"):
            try:
                candidate = raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            if "\ufffd" not in candidate:
                return candidate
    return text


class PowerShellRunner:
    """通过 PowerShell 执行只读诊断命令（Windows only）。

    fail-closed：每条脚本先过 :func:`assert_read_only`，写动词拒绝执行；
    仅捕获 stdout/stderr，不继承父进程句柄；单命令超时可配。
    """

    def __init__(self, shell: str = "powershell", timeout: float = 25.0) -> None:
        self.shell = shell
        self.timeout = timeout
        self.commands: List[Tuple[str, str]] = []

    def run(self, name: str, script: str) -> Tuple[int, str, str]:
        assert_read_only(script)
        self.commands.append((name, script))
        full = _POWERSHELL_PRELUDE + "\n" + script
        args = [self.shell, "-NoProfile", "-NonInteractive", "-Command", full]
        try:
            done = subprocess.run(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", "%s: %s" % (type(exc).__name__, exc)
        return done.returncode, _decode(done.stdout), _decode(done.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="V0.3 P0 只读网络诊断（netdiag）：一条命令输出双网卡与路由基线报告")
    parser.add_argument("--ssid", default=DEFAULT_TARGET_SSID,
                        help="目标校园 Wi-Fi SSID（用于关联判定，默认 SNNU）")
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读 JSON（P1 接口契约）到 stdout")
    parser.add_argument("--json-out", default=None, metavar="PATH",
                        help="把 JSON 契约另存到文件（需显式指定才落盘）")
    parser.add_argument("--adapters-out", nargs="?", const="adapters.json",
                        default=None, metavar="PATH",
                        help="生成 adapters.json 草案（需显式指定才落盘）")
    parser.add_argument("--shell", default="powershell",
                        choices=("powershell", "pwsh"), help="PowerShell 可执行名")
    parser.add_argument("--timeout", type=float, default=25.0,
                        help="单条命令超时秒数（默认 25）")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("netdiag 仅支持 Windows（需要 NetTCPIP/netsh/powercfg）。",
              file=sys.stderr)
        return 3

    runner = PowerShellRunner(shell=args.shell, timeout=args.timeout)
    report = collect(runner, target_ssid=args.ssid)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2,
                         default=str)

    if args.json_out:
        path = Path(args.json_out)
        path.write_text(payload, encoding="utf-8")
    if args.adapters_out:
        path = Path(args.adapters_out)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        draft = build_adapters_draft(report)
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2,
                                   default=str) + "\n", encoding="utf-8")
        print("adapters.json 草案已写入: %s" % path, file=sys.stderr)

    if args.json:
        print(payload)
    else:
        print(report.render_text())
    if report.errors:
        print("警告: %d 个诊断项采集失败（详见报告错误节）。" % len(report.errors),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
