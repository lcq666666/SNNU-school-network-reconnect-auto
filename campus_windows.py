# -*- coding: utf-8 -*-
"""Windows 副作用适配层（阶段 2）。

把「注册表 / 凭据 / 自启」这些**有系统副作用**的操作收敛到可注入的适配器里，
使它们既能被真实调用，也能在离线测试中整体替换（对应《审查方案》7.1/7.2 的
``WindowsAdapters``）：

- :class:`ProxyController`：先读完整快照、再验证端点、最后才写 ``ProxyEnable``；
  写入失败按事务回滚，并分别报告「回滚是否成功」「系统刷新是否成功」（F04/F05）；
- :class:`CredentialStore`：区分「未配置 / 后端不可用 / 读写失败」，两条写入
  部分失败时尝试恢复旧对（F11）；
- :class:`AutostartController`：读取自启命令内容而非仅判断键是否存在（F12 前置）。

安全约定：
- 代理端点默认**不猜测**（不硬编码 ``127.0.0.1:7897``），必须由调用方显式给出；
- 只接受本机端点（除非调用方显式放开），并做 TCP 监听探测；
- 凭据只经 Windows 安全后端读写，绝不写入配置文件或日志。
"""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from typing import (Any, Callable, Dict, List, Optional, Sequence, Tuple)

from campus_core import (E_CRED_BACKEND, E_CRED_MISSING, E_MUTEX_FAILED,
                         E_PROXY_ENDPOINT_INVALID, E_PROXY_WRITE, MISSING,
                         CredentialClear, CredentialLoad, CredentialSave,
                         ProxyApplyResult, Secret, SingleInstanceResult)

__all__ = [
    "LOCAL_HOSTS", "PROXY_REG_PATH", "parse_endpoint", "tcp_probe",
    "validate_proxy_endpoint", "default_refresh", "ProxyController",
    "CredentialStore", "AutostartController",
    # ---- 阶段 4：Wi-Fi 解析（F17）与单实例互斥体（F18）----
    "WifiInterface", "CONNECTED_STATES", "parse_netsh_interfaces",
    "select_wifi_interface", "wifi_state_from_text", "parse_ipconfig_ipv4",
    "MUTEX_NAME", "SingleInstanceGuard", "acquire_single_instance",
]

PROXY_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: 允许作为代理端点的本机地址（其余一律拒绝，避免误启用历史遗留代理）。
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


# --------------------------------------------------------------------------- #
# 端点解析与探测
# --------------------------------------------------------------------------- #
def parse_endpoint(endpoint: Any) -> Tuple[Optional[str], Optional[int]]:
    """解析 ``host:port``（可选 ``scheme://``，支持 ``[IPv6]:port``）。

    非法输入返回 ``(None, None)``，不抛异常。
    """
    if not isinstance(endpoint, str):
        return None, None
    text = endpoint.strip()
    if not text:
        return None, None
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].strip()
    if text.startswith("["):  # [::1]:7897
        host, sep, rest = text[1:].partition("]")
        if not sep or not rest.startswith(":"):
            return None, None
        port_text = rest[1:]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep:
            return None, None
    host = host.strip()
    if not host or not port_text.isdigit():
        return None, None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None, None
    return host, port


def _normalize(host: str, port: int) -> str:
    """归一化为 ``host:port``（IPv6 加方括号）。"""
    if ":" in host:
        return "[%s]:%d" % (host, port)
    return "%s:%d" % (host, port)


def tcp_probe(host: str, port: int, timeout: float = 0.5) -> bool:
    """TCP 可达性探测：仅说明「有进程在监听」，不证明一定是目标代理。"""
    with socket.create_connection((host, port), timeout=timeout):
        return True


def validate_proxy_endpoint(endpoint: Any, probe: Optional[Callable] = None,
                            allow_non_local: bool = False
                            ) -> Tuple[bool, Optional[str], Optional[str], str]:
    """验证代理端点：格式 → 本机白名单 → TCP 监听探测。

    返回 ``(ok, normalized, error_code, detail)``；``ok=False`` 时调用方
    **不得**修改任何代理配置。
    """
    host, port = parse_endpoint(endpoint)
    if host is None:
        return False, None, E_PROXY_ENDPOINT_INVALID, "代理端点格式无效（应为 host:port）"
    if not allow_non_local and host.lower() not in LOCAL_HOSTS:
        return False, None, E_PROXY_ENDPOINT_INVALID, "代理端点不是本机地址，未启用联动"
    normalized = _normalize(host, port)
    probe = probe or tcp_probe
    try:
        listening = bool(probe(host, port))
    except Exception:
        listening = False
    if not listening:
        return False, normalized, E_PROXY_ENDPOINT_INVALID, \
            "代理端点未在监听（%s），未启用联动" % normalized
    return True, normalized, None, ""


def default_refresh() -> bool:
    """通知 Windows 代理设置已变更（``InternetSetOptionW``）。失败返回 ``False``。

    注册表写成功**不等于**所有应用立即采用；本函数的结果单独报告，不夸大。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        wininet = ctypes.WinDLL("wininet", use_last_error=True)
        internet_option_settings_changed = 39
        internet_option_refresh = 37
        changed = wininet.InternetSetOptionW(None, internet_option_settings_changed, None, 0)
        refreshed = wininet.InternetSetOptionW(None, internet_option_refresh, None, 0)
        return bool(changed and refreshed)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 系统代理
# --------------------------------------------------------------------------- #
class ProxyController:
    """系统代理（HKCU Internet Settings）事务式读写。

    顺序约束（F04）：**先**以 ``KEY_QUERY_VALUE | KEY_SET_VALUE`` 打开句柄并读取
    完整快照 → 再验证端点 → **最后**才写 ``ProxyEnable``。任一步失败都不产生
    部分写入；已发生的写入在失败时按事务回滚。
    """

    VALUE_NAMES = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL")

    def __init__(self, registry: Any, probe: Optional[Callable] = None,
                 refresh: Optional[Callable] = None,
                 allow_non_local: bool = False) -> None:
        self.registry = registry
        self.probe = probe or tcp_probe
        self.refresh = refresh if refresh is not None else default_refresh
        self.allow_non_local = allow_non_local

    # ---- 内部工具 ----
    def _access(self) -> int:
        return int(self.registry.KEY_QUERY_VALUE) | int(self.registry.KEY_SET_VALUE)

    def _type_for(self, name: str) -> int:
        return self.registry.REG_DWORD if name == "ProxyEnable" else self.registry.REG_SZ

    def read_snapshot(self, key: Any) -> Dict[str, Any]:
        """读取代理相关值的完整快照；缺失值记为 :data:`MISSING`。"""
        snapshot: Dict[str, Any] = {}
        for name in self.VALUE_NAMES:
            try:
                snapshot[name] = self.registry.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                snapshot[name] = MISSING
        return snapshot

    def _safe_snapshot(self, key: Any) -> Dict[str, Any]:
        try:
            return self.read_snapshot(key)
        except OSError:
            return {}

    def _do_refresh(self) -> bool:
        try:
            return bool(self.refresh())
        except Exception:
            return False

    def _rollback(self, key: Any, applied: list) -> bool:
        ok = True
        for name, old, _new in reversed(applied):
            try:
                if old is MISSING:
                    self.registry.DeleteValue(key, name)
                else:
                    self.registry.SetValueEx(key, name, 0, self._type_for(name), old)
            except OSError:
                ok = False
        return ok

    # ---- 主流程 ----
    def apply(self, enabled: bool, endpoint: Optional[str] = None) -> ProxyApplyResult:
        """启用/停用系统代理。

        ``enabled=True`` 时必须给出**用户已确认**的 ``endpoint``，否则拒绝修改。
        """
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER,
                                       PROXY_REG_PATH, 0, self._access()) as key:
                try:
                    before = self.read_snapshot(key)
                except OSError as exc:
                    return ProxyApplyResult(
                        ok=False, changed=False, enabled=enabled, endpoint=endpoint,
                        error_code=E_PROXY_WRITE,
                        detail="读取系统代理快照失败（%s），未做任何修改" % type(exc).__name__)

                target_endpoint = endpoint
                if enabled:
                    ok, normalized, code, detail = validate_proxy_endpoint(
                        endpoint, probe=self.probe, allow_non_local=self.allow_non_local)
                    if not ok:
                        return ProxyApplyResult(
                            ok=False, changed=False, enabled=True, endpoint=endpoint,
                            error_code=code, detail=detail, before=before, after=before)
                    target_endpoint = normalized
                    plan = [("ProxyEnable", 1), ("ProxyServer", normalized)]
                else:
                    plan = [("ProxyEnable", 0)]

                pending = [(name, value) for name, value in plan
                           if before.get(name, MISSING) != value]
                if not pending:
                    refresh_ok = self._do_refresh()
                    return ProxyApplyResult(
                        ok=refresh_ok, changed=False, enabled=enabled,
                        endpoint=target_endpoint, refresh_ok=refresh_ok,
                        error_code=None if refresh_ok else E_PROXY_WRITE,
                        detail="" if refresh_ok else "系统代理已是目标状态，但刷新通知失败",
                        before=before, after=before)

                applied: list = []
                try:
                    for name, value in pending:
                        self.registry.SetValueEx(key, name, 0, self._type_for(name), value)
                        applied.append((name, before.get(name, MISSING), value))
                except OSError as exc:
                    rollback_ok = self._rollback(key, applied)
                    detail = "写入系统代理失败（%s）" % type(exc).__name__
                    if applied:
                        detail += "；已回滚" if rollback_ok else "；回滚失败，系统代理可能处于中间状态"
                    return ProxyApplyResult(
                        ok=False, changed=bool(applied), enabled=enabled,
                        endpoint=target_endpoint, error_code=E_PROXY_WRITE, detail=detail,
                        rollback_ok=rollback_ok, changes=tuple(applied),
                        before=before, after=self._safe_snapshot(key))

                refresh_ok = self._do_refresh()
                detail = "" if refresh_ok else \
                    "注册表已写入，但系统刷新通知失败（部分应用可能延迟生效）"
                return ProxyApplyResult(
                    ok=refresh_ok, changed=True, enabled=enabled, endpoint=target_endpoint,
                    error_code=None if refresh_ok else E_PROXY_WRITE, detail=detail,
                    refresh_ok=refresh_ok, changes=tuple(applied),
                    before=before, after=self._safe_snapshot(key))
        except OSError as exc:
            return ProxyApplyResult(
                ok=False, changed=False, enabled=enabled, endpoint=endpoint,
                error_code=E_PROXY_WRITE,
                detail="打开系统代理注册表失败（%s），未做任何修改" % type(exc).__name__)


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #
class CredentialStore:
    """Windows 凭据管理器适配器（F11）。

    保留兼容的 service 名；返回可区分结果，不把「后端不可用」当成「未配置」。
    两条写入不存在天然事务：第二步失败时尝试恢复旧对，并单独报告恢复结果。
    """

    def __init__(self, keyring_module: Any, service: str = "SNNU-Campus-Reconnect",
                 user_key: str = "username", pass_key: str = "password") -> None:
        self.keyring = keyring_module
        self.service = service
        self.user_key = user_key
        self.pass_key = pass_key

    def load(self) -> CredentialLoad:
        """读取凭据，区分「未配置」与「后端不可用」。"""
        try:
            username = self.keyring.get_password(self.service, self.user_key)
            password = self.keyring.get_password(self.service, self.pass_key)
        except Exception as exc:
            return CredentialLoad(error_code=E_CRED_BACKEND,
                                  detail="凭据后端不可用（%s）" % type(exc).__name__)
        if not username or not password:
            return CredentialLoad(error_code=E_CRED_MISSING, detail="尚未保存凭据")
        return CredentialLoad(ok=True, username=username, password=Secret(password))

    def _read_raw(self) -> Optional[Tuple[Optional[str], Optional[str]]]:
        try:
            return (self.keyring.get_password(self.service, self.user_key),
                    self.keyring.get_password(self.service, self.pass_key))
        except Exception:
            return None

    def _restore(self, old: Optional[Tuple[Optional[str], Optional[str]]],
                 new_username: str) -> bool:
        """恢复旧凭据对；原本没有账号时清理本次写入的账号（不删用户既有记录）。"""
        try:
            if old is not None and old[0]:
                self.keyring.set_password(self.service, self.user_key, old[0])
                if old[1]:
                    self.keyring.set_password(self.service, self.pass_key, old[1])
            elif new_username:
                self.keyring.delete_password(self.service, self.user_key)
        except Exception:
            return False
        return True

    def save(self, username: str, password: str) -> CredentialSave:
        """写入账号与密码；第二步失败时尝试恢复旧对。"""
        old = self._read_raw()
        try:
            self.keyring.set_password(self.service, self.user_key, username)
        except Exception as exc:
            return CredentialSave(error_code=E_CRED_BACKEND,
                                  detail="写入账号失败（%s）" % type(exc).__name__)
        try:
            self.keyring.set_password(self.service, self.pass_key, password)
        except Exception as exc:
            rolled_back = self._restore(old, username)
            return CredentialSave(
                error_code=E_CRED_BACKEND, rolled_back=rolled_back,
                detail="写入密码失败（%s）；%s" % (
                    type(exc).__name__,
                    "已恢复原凭据" if rolled_back else "恢复原凭据失败，请重新执行 configure"))
        return CredentialSave(ok=True)

    def clear(self) -> CredentialClear:
        """清除本程序的凭据记录；不存在时视为已清除，绝不误删其他记录。"""
        try:
            for key in (self.user_key, self.pass_key):
                if self.keyring.get_password(self.service, key) is not None:
                    self.keyring.delete_password(self.service, key)
        except Exception as exc:
            return CredentialClear(error_code=E_CRED_BACKEND,
                                   detail="清除凭据失败（%s）" % type(exc).__name__)
        return CredentialClear(ok=True)


# --------------------------------------------------------------------------- #
# 开机自启
# --------------------------------------------------------------------------- #
class AutostartController:
    """HKCU Run 自启项适配器：核对命令内容，而非仅判断键是否存在（F12 前置）。"""

    def __init__(self, registry: Any, command_provider: Callable[[], str],
                 value_name: str = "CampusReconnectTray") -> None:
        self.registry = registry
        self.command_provider = command_provider
        self.value_name = value_name

    def read_command(self) -> Optional[str]:
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER,
                                       AUTOSTART_REG_PATH) as key:
                return self.registry.QueryValueEx(key, self.value_name)[0]
        except OSError:
            return None

    def is_enabled(self) -> bool:
        return self.read_command() is not None

    def matches_current(self) -> bool:
        """自启命令是否指向**当前安装**（而非旧路径/旧 exe）。"""
        return self.read_command() == self.command_provider()

    def set_enabled(self, enabled: bool) -> bool:
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER,
                                       AUTOSTART_REG_PATH, 0,
                                       self.registry.KEY_SET_VALUE) as key:
                if enabled:
                    self.registry.SetValueEx(key, self.value_name, 0,
                                             self.registry.REG_SZ,
                                             self.command_provider())
                else:
                    try:
                        self.registry.DeleteValue(key, self.value_name)
                    except FileNotFoundError:
                        pass
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------- #
# Wi-Fi 解析（F17）
# --------------------------------------------------------------------------- #
#: netsh 键名（中 / 英）。**SSID 与 BSSID 必须分开匹配**，避免整行子串误判。
_NAME_KEYS = frozenset({"name", "名称"})
_SSID_KEYS = frozenset({"ssid"})
_BSSID_KEYS = frozenset({"bssid"})
_PROFILE_KEYS = frozenset({"profile", "配置文件"})
_STATE_KEYS = frozenset({"state", "状态"})

#: 视为“已连接”的状态文本（中 / 英）。
CONNECTED_STATES = frozenset({"connected", "已连接"})

#: 视为“未连接”的状态文本（用于明确区分，不做无根据推断）。
DISCONNECTED_STATES = frozenset({"disconnected", "已断开", "未连接"})


@dataclass(frozen=True)
class WifiInterface:
    """一个无线接口块的结构化结果（F17）。

    ``ssid`` / ``profile`` 分开保存：``netsh wlan connect name=`` 实际按 **profile**
    名操作，profile 未必等于 SSID（企业/多配置场景）。
    """

    name: str = ""
    ssid: Optional[str] = None
    bssid: Optional[str] = None
    profile: Optional[str] = None
    state: str = ""

    @property
    def connected(self) -> bool:
        return self.state.strip().lower() in CONNECTED_STATES

    @property
    def disconnected(self) -> bool:
        return self.state.strip().lower() in DISCONNECTED_STATES

    def matches_ssid(self, target: str) -> bool:
        """**完整**匹配 SSID（不做大小写折叠，见 F17）。"""
        return bool(self.ssid) and self.ssid.strip() == (target or "").strip()


def _iter_blocks(text: str) -> List[str]:
    """按空行切分接口块（netsh / ipconfig 的通用分块方式）。"""
    return [block for block in re.split(r"\r?\n\s*\r?\n", text or "") if block.strip()]


def _parse_interface_block(block: str) -> Optional[WifiInterface]:
    """解析单个接口块；每行按**第一个冒号**切分键值，无法识别的块返回 ``None``。"""
    name = ssid = bssid = profile = state = None
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key_l = key.strip().lower()
        value = value.strip()
        if key_l in _NAME_KEYS:
            name = value
        elif key_l in _BSSID_KEYS:      # 先判 BSSID，避免被 SSID 规则吞掉
            bssid = value
        elif key_l in _SSID_KEYS:
            ssid = value
        elif key_l in _PROFILE_KEYS:
            profile = value
        elif key_l in _STATE_KEYS:
            state = value
    if name is None and ssid is None and profile is None and state is None:
        return None
    return WifiInterface(name=name or "", ssid=ssid, bssid=bssid,
                         profile=profile, state=state or "")


def parse_netsh_interfaces(text: str) -> List[WifiInterface]:
    """按**接口块**解析 ``netsh wlan show interfaces`` 输出（F17）。

    修正原实现的三处缺陷：整行 ``"bssid" not in low`` 会误杀 SSID 含 "BSSID" 的行；
    多接口时把“第一个 SSID”与“最后一个状态”错配；不检查返回码 / 假设冒号存在。
    无有效接口时返回空列表（由调用方明确“不支持自动认证”，不误报）。
    """
    interfaces: List[WifiInterface] = []
    for block in _iter_blocks(text):
        iface = _parse_interface_block(block)
        if iface is not None:
            interfaces.append(iface)
    return interfaces


def select_wifi_interface(interfaces: Sequence[WifiInterface], ssid: str = "",
                          interface: Optional[str] = None,
                          ) -> Optional[WifiInterface]:
    """在接口集合里选择目标接口（F17）。

    优先级：名称过滤（若给定）→ SSID **完整**匹配 → 第一个“已连接且有 SSID”的接口
    → 第一个接口。找不到任何接口时返回 ``None``。
    """
    pool = list(interfaces)
    if interface:
        want = interface.strip().casefold()
        pool = [i for i in pool if i.name.strip().casefold() == want]
    if not pool:
        return None
    target = (ssid or "").strip()
    if target:
        for iface in pool:
            if iface.matches_ssid(target):
                return iface
    for iface in pool:
        if iface.connected and iface.ssid:
            return iface
    return pool[0]


def wifi_state_from_text(text: str, ssid: str = "",
                         interface: Optional[str] = None) -> Tuple[Optional[str], str]:
    """从 netsh 文本得到 ``(当前 SSID|None, 状态)``（F17）。

    - 无任何无线接口 → ``(None, "no-wireless")``，供上层明确“不支持自动认证”；
    - 仅返回**已连接接口**的 SSID，不再混用不同接口的 SSID/状态。
    """
    interfaces = parse_netsh_interfaces(text)
    if not interfaces:
        return None, "no-wireless"
    iface = select_wifi_interface(interfaces, ssid, interface)
    if iface is None:
        return None, "unknown"
    if iface.disconnected:        # 明确断开：不返回 SSID，避免误判为已连接
        return None, (iface.state or "disconnected")
    return (iface.ssid or None), (iface.state or "unknown")


_IPV4_KEY_HINTS = ("ipv4", "ip 地址", "ip address")

#: ipconfig 适配器**标题行**：英文 `... adapter <名>:` / 中文 `...适配器 <名>:`。
#: 真实 ipconfig 会在标题行与属性行之间插入空行，故不能用空行分块（F75-09）。
_ADAPTER_HEADER = re.compile(r"^\s*.*(?:adapter|适配器).*:\s*$", re.IGNORECASE)


def _valid_ipv4(addr: str) -> bool:
    """是否为可用（非 link-local / 非 0.0.0.0）的 IPv4 地址。"""
    addr = addr.split("(")[0].strip()
    parts = addr.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    if not all(0 <= int(p) <= 255 for p in parts):
        return False
    return addr != "0.0.0.0" and not addr.startswith("169.254.")


def parse_ipconfig_ipv4(text: str, interface: Optional[str] = None) -> bool:
    """判断 ``ipconfig`` 输出里是否已有可用 IPv4 地址（F17 / F75-09）。

    ``interface`` 给定时仅检查**归属该适配器**的 IPv4 行。真实 ``ipconfig`` 在适配器
    标题行与属性行之间**有空行**，因此不能按空行分块（否则标题所在的块永不含 IPv4 行，
    指定接口时恒判 False，F75-09）：这里逐行扫描，记住最近的适配器标题行，把其后的
    IPv4 行归属到该适配器后再做接口过滤。
    """
    want = interface.strip().casefold() if interface else None
    current = ""
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if _ADAPTER_HEADER.match(line):
            current = line.strip()
            continue
        low = line.lower()
        if not any(hint in low for hint in _IPV4_KEY_HINTS):
            continue
        _, _, value = line.partition(":")
        if not _valid_ipv4(value):
            continue
        if want is None or want in current.casefold():
            return True
    return False


# --------------------------------------------------------------------------- #
# 单实例互斥体（F18）
# --------------------------------------------------------------------------- #
#: 互斥体名。**不加 ``Global\`` 前缀** ⇒ 位于当前登录会话的命名空间，
#: 作用域为「每 Windows 会话」；同一用户不同会话 / 不同用户互不阻塞。
MUTEX_NAME = "SNNU_CampusReconnect_Watch_Mutex"

ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5


class SingleInstanceGuard:
    """基于命名互斥体的进程级排他（F18）。

    关键修正：

    - 显式声明 ``CreateMutexW`` 的 ``argtypes`` / ``restype``（``HANDLE`` 为指针
      宽度，不能依赖 ctypes 默认的 ``c_int`` 判存在性）；
    - 保存**可关闭**的句柄；重复实例路径及时 ``CloseHandle``，不泄漏句柄；
    - 创建失败 **fail-closed**：返回失败结果，由调用方拒绝后台认证 / 降级只读；
    - 不使用 ownership 语义，因此**不**调用 ``ReleaseMutex``（仅 CloseHandle）。
    """

    def __init__(self, kernel32: Any = None, name: str = MUTEX_NAME,
                 get_last_error: Optional[Callable[[], int]] = None,
                 log: Any = None) -> None:
        self._kernel32 = kernel32
        self.name = name
        self._get_last_error = get_last_error
        self.log = log
        self._handle: Any = None

    def _kernel(self) -> Any:
        if self._kernel32 is not None:
            return self._kernel32
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32
        return kernel32

    def _last_error(self) -> int:
        if self._get_last_error is not None:
            return int(self._get_last_error())
        import ctypes
        return int(ctypes.get_last_error())

    @property
    def handle(self) -> Any:
        return self._handle

    @property
    def held(self) -> bool:
        return self._handle is not None

    def _close(self, handle: Any) -> bool:
        try:
            return bool(self._kernel().CloseHandle(handle))
        except Exception:
            return False

    def acquire(self) -> SingleInstanceResult:
        """创建 / 打开命名互斥体并返回结构化结果。"""
        try:
            kernel32 = self._kernel()
            handle = kernel32.CreateMutexW(None, False, self.name)
        except Exception as exc:      # 平台不支持 / DLL 加载失败 ⇒ fail-closed
            return SingleInstanceResult(
                error_code=E_MUTEX_FAILED,
                detail="无法创建单实例互斥体（%s），已拒绝后台认证"
                       % type(exc).__name__)
        if not handle:
            err = self._last_error()
            return SingleInstanceResult(
                error_code=E_MUTEX_FAILED,
                detail="CreateMutexW 失败（last_error=%s），已拒绝后台认证" % err)
        if self._last_error() == ERROR_ALREADY_EXISTS:
            self._close(handle)       # 重复实例：及时关闭新句柄，避免泄漏
            return SingleInstanceResult(already_running=True,
                                        detail="已有一个实例在运行")
        self._handle = handle
        return SingleInstanceResult(ok=True, detail="已持有单实例互斥体")

    def release(self) -> None:
        """关闭句柄（不使用 ownership，故无需 ReleaseMutex）。"""
        handle, self._handle = self._handle, None
        if handle:
            self._close(handle)


def acquire_single_instance(log: Any = None, kernel32: Any = None,
                            get_last_error: Optional[Callable[[], int]] = None
                            ) -> bool:
    """兼容层：获取成功返回 ``True``；已有实例 / 创建失败均返回 ``False``。"""
    return SingleInstanceGuard(kernel32=kernel32, get_last_error=get_last_error,
                               log=log).acquire().ok
