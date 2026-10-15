# -*- coding: utf-8 -*-
"""校园网自动重连助手 · 托盘版 (Windows)

在引擎 campus_reconnect.py 之上提供:
  - 任务栏托盘图标与右键菜单(在线 / 重连中 / 已断开 / 已暂停 / 异常)
  - 气泡通知: 启动状态 / 网络断开开始重连 / 重连成功(耗时) / 重连失败(原因)
  - 设置面板(tkinter): 实时状态、参数、开关
  - "重连成功后自动开启系统代理" 的三态授权(每次询问 / 自动开启 / 不开启)
  - 开机自启开关: HKCU Run 注册表项 CampusReconnectTray

阶段 2（稳定性与系统副作用止血）后的关键结构:
  - **单网络工作线程 + 命令队列**：手动 / 自动 / 启动检查统一投递，同一时刻最多
    一个认证任务在途；重复手动请求合并（F01）。
  - **工作单元异常边界**：探测 / 重连 / 结果处理任一步抛错都转成可识别的 ERROR
    状态并保留心跳，线程不静默失效，主线程可查看具体故障并重启（F02）。
  - **配置校验与原子持久化**：非法配置回退安全默认值并给出可见错误；保存使用
    同目录临时文件 + fsync + os.replace，失败保留旧文件且不谎报成功（F03/F13）。
  - **代理事务**：先读快照、再验证端点、最后才写 ProxyEnable；失败按事务回滚，
    回滚与系统刷新结果分别报告；默认不启用、不猜测端点（F04/F05）。
  - **取消令牌**：退出 / 暂停后不再启动新的 POST / Edge / 代理写入（F10）。
  - **凭据存储**：区分未配置 / 后端不可用 / 读写失败，部分失败尝试恢复旧对（F11）。
  - **授权版本**：过期的授权询问结果不应用，新的拒绝优先；设置只保存实际变更项（F12）。

阶段 3（统一认证、状态识别与调度）后的关键结构:
  - **共享重连服务**：CLI 与托盘共用 ``campus_core.repair_service``，同输入同结果；
    Edge / 系统代理 / UI 由上层按结果决定，不混入底层认证流程（F14）。
  - **登录结构化校验**：状态页账号字段完整比较、区分已有会话与本次成功、
    重定向目标白名单（F06/F07）。
  - **状态分类**：探测严格匹配结构，失败后区分非目标/断开/不可达/需认证/
    已认证但公网异常/需人工处理（F08）。
  - **统一冷却**：恢复不抹掉最短间隔，冷却只禁新认证；Edge 兜底按故障周期限频、
    如实通知（F09/F19）。
  - **空运营商保留**：``yys=""`` 端到端保留，UI 用"默认校园网"标签（F15）。

依赖: py -m pip install pystray pillow
运行: py campus_reconnect_tray.py   (run_tray.bat 可双击启动)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

import keyring
import pystray
import winreg
from PIL import Image, ImageDraw

from campus_core import (E_CRED_BACKEND, E_CRED_MISSING, SCHEMA_VERSION,
                         CredentialClear, CredentialLoad, CredentialSave,
                         ProxyApplyResult, RepairAdapters, RepairOptions,
                         TimerTable, repair_service, load_config_text)
import campus_reconnect as engine
from campus_reconnect import (direct_session, edge_fallback,
                              ensure_single_instance, get_credentials,
                              internet_ok, portal_login, portal_reachable,
                              resolve_port, setup_logging, wifi_reconnect,
                              wifi_state)
from campus_windows import CredentialStore, ProxyController, default_refresh
from campus_tray_failover import (MODE_ORDER, TrayFailoverBridge, light_snapshot,
                                  parse_lock, parse_mode, tray_mode_label)
from campus_tray_observer import (ObservationResult, TraySnapshotSource,
                                  is_full_snapshot)
from campus_failover import FailoverMode, FailoverPolicy, PATH_LABEL
from campus_remote import (MonitorWatchdog, RemoteSafetyGuard,
                           apply_watchdog_decision, build_windows_remote_ops)

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent   # PyInstaller 单文件: 用 exe 所在目录
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "CampusReconnectTray"
PYTHONW = str(Path(sys.executable).with_name("pythonw.exe"))
CLASH_SERVER_DEFAULT = "127.0.0.1:7897"   # 仅在用户显式确认时作为**建议值**展示
APP_NAME = "校园网自动重连助手"
UI_POLL_MS = 120

#: V0.3 P7 看门狗：巡检间隔与心跳容忍窗口（秒）。
WATCHDOG_INTERVAL = 30.0
WATCHDOG_HEARTBEAT_TIMEOUT = 90.0
#: V0.3 P7 看门狗：重启频次上限（窗口内），超过转人工处理。
WATCHDOG_MAX_RESTARTS = 3
WATCHDOG_RESTART_WINDOW = 1800.0

#: V0.3 P7.5-1 三轴观测：快照有效期 / 采集超时 / 采集最小间隔（秒）。
#: 采集走**独立后台线程**（netdiag 的 PowerShell 只读管道），Tk 主线程不参与；
#: 限流用于避免诊断进程堆积；过期或代次失效的快照一律不得用于 AUTO 决策。
SNAPSHOT_MAX_AGE = 60.0
SNAPSHOT_COLLECT_TIMEOUT = 25.0
SNAPSHOT_MIN_INTERVAL = 10.0

#: 托盘配置的完整键集合（保存时只写这些键，未知键永不落盘）。
DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "ssid": "SNNU",
    "profile": None,
    "interface": None,
    "yys": "mobile",
    "account_format": "plain",
    "interval": 15,
    "threshold": 3,
    "cooldown": 180,
    "browser_fallback": True,
    "clash_auto_proxy": None,   # None=尚未询问; True=同意联动; False=拒绝
    "proxy_endpoint": None,     # 用户显式确认的代理端点
    "paused": False,
    # ---- V0.3 P6：托盘三模式与手动锁定（方案 §11 / §9）----
    "failover_mode": "MONITOR",     # MONITOR(仅监测,默认) / AUTO(智能自动) / LOCKED(手动锁定)
    "failover_lock": None,          # LOCKED 下的锁定目标: "ethernet" / "wifi" / None
    "clash_keep": True,             # 网络切换期间保持 Clash 原样（默认开启）
    "auto_switch_authorized": False,  # 智能自动接入真实执行器的显式授权（默认关闭）
    # ---- V0.3 P7：远程保障（方案 §7.3 / §4.3）----
    "remote_guard": True,             # 远控探测与远程会话敏感期（只读，默认开启）
    "remote_defer_failback": True,    # 远程会话敏感期推迟自动切回（默认开启）
    "remote_probe_interval": 30,      # 远控探测最小间隔秒数（节流）
}
CONFIG_KEYS = tuple(DEFAULT_CONFIG)

MODE_COLOR = {"online": (63, 185, 80), "reconnecting": (210, 153, 34),
              "offline": (248, 81, 73), "paused": (139, 148, 158),
              "boot": (88, 166, 255), "error": (188, 140, 255)}
MODE_TEXT = {"online": "在线", "reconnecting": "重连中", "offline": "已断开",
             "paused": "已暂停", "boot": "启动中", "error": "异常"}

#: V0.3 P6 三模式说明（方案 §11；菜单与通知用）。
_MODE_HINT = {
    FailoverMode.MONITOR: "仅观察双网络状态，不修改系统路由",
    FailoverMode.AUTO: "有线优先，故障自动切 Wi-Fi，稳定后切回（需显式授权）",
    FailoverMode.LOCKED: "锁定优先路径，仍显示健康告警，不自动禁用另一网卡",
}

#: 代理授权三态在设置面板中的显示标签。
CONSENT_CHOICES = (("每次询问", None), ("自动开启", True), ("不开启", False))
CONSENT_LABELS = [label for label, _ in CONSENT_CHOICES]
CONSENT_LABEL_TO_VALUE = {label: value for label, value in CONSENT_CHOICES}
CONSENT_VALUE_TO_LABEL = {value: label for label, value in CONSENT_CHOICES}

#: 运营商选择：空串是**合法值**（默认校园网线路），用标签映射便于理解（F15）。
YYS_CHOICES = (("mobile", "移动"), ("unicom", "联通"),
               ("telecom", "电信"), ("", "默认校园网"))
YYS_LABELS = [label for _, label in YYS_CHOICES]
YYS_LABEL_TO_VALUE = {label: value for value, label in YYS_CHOICES}
YYS_VALUE_TO_LABEL = {value: label for value, label in YYS_CHOICES}


# --------------------------------------------------------------------------- #
# 数据目录与配置路径（F13）
# --------------------------------------------------------------------------- #
def resolve_config_file() -> Path:
    """默认配置位置。

    - 打包（便携）模式：与 exe 同级；
    - 源码模式：``%LOCALAPPDATA%\\CampusReconnect\\tray_config.json``
      （exe 所在目录可能只读，见 F13/T37）。
    """
    if getattr(sys, "frozen", False):
        return SCRIPT_DIR / "tray_config.json"
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "CampusReconnect" / "tray_config.json"
    return SCRIPT_DIR / "tray_config.json"


CONFIG_FILE = resolve_config_file()


def _config_candidates() -> list:
    """候选配置路径：主路径优先，其次旧位置（仅只读加载，不移动/删除）。"""
    primary = Path(CONFIG_FILE)
    candidates = [primary]
    if primary == resolve_config_file():
        legacy = SCRIPT_DIR / "tray_config.json"
        if legacy != primary:
            candidates.append(legacy)
    return candidates


def _read_config_text():
    """返回 ``(文本, 来源路径)``；全部缺失时返回 ``("", 主路径)``。"""
    candidates = _config_candidates()
    for index, path in enumerate(candidates):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if index > 0:
            logging.getLogger("campus").warning(
                "检测到旧位置配置 %s，已只读加载（不移动、不删除旧文件）", path)
        return text, path
    return "", candidates[0]


def _read_paused_flag(text: str) -> bool:
    """从原始配置文本里读取 ``paused``（非 bool 一律按 ``False``）。"""
    raw = _raw_config(text)
    if isinstance(raw.get("paused"), bool):
        return raw["paused"]
    return False


def _raw_config(text: str) -> dict:
    """把原始配置文本解析为 dict（非法/非对象一律返回空 dict）。"""
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_failover_keys(text: str) -> dict:
    """读取 P6 三模式/锁定/Clash 键（非法值回退安全默认）。

    与 ``paused`` 一样，这些键由托盘自行读写（不进入 ``campus_core.Config``），
    因此从原始 JSON 文本读取并逐项校验：模式非法回退 MONITOR、锁定目标非法
    回退 ``None``、开关非 bool 回退默认；``AUTO`` 授权默认关闭（fail-closed）。
    """
    raw = _raw_config(text)
    mode = parse_mode(raw.get("failover_mode")).value
    lock = parse_lock(raw.get("failover_lock"))
    clash_keep = raw.get("clash_keep")
    if not isinstance(clash_keep, bool):
        clash_keep = DEFAULT_CONFIG["clash_keep"]
    authorized = raw.get("auto_switch_authorized")
    if not isinstance(authorized, bool):
        authorized = False
    return {"failover_mode": mode, "failover_lock": lock,
            "clash_keep": clash_keep, "auto_switch_authorized": authorized}


def _read_remote_keys(text: str) -> dict:
    """读取 P7 远程保障三键（非法值一律回退安全默认）。

    与 P6 的 ``_read_failover_keys`` 分开实现，保持既有读取器返回值稳定
    （P6 测试按整字典断言）；探测间隔限幅 5..600s，防止配置把轮询压到过密。
    """
    raw = _raw_config(text)
    remote_guard = raw.get("remote_guard")
    if not isinstance(remote_guard, bool):
        remote_guard = DEFAULT_CONFIG["remote_guard"]
    defer_failback = raw.get("remote_defer_failback")
    if not isinstance(defer_failback, bool):
        defer_failback = DEFAULT_CONFIG["remote_defer_failback"]
    probe_interval = raw.get("remote_probe_interval")
    if not isinstance(probe_interval, int) or isinstance(probe_interval, bool):
        probe_interval = DEFAULT_CONFIG["remote_probe_interval"]
    return {"remote_guard": remote_guard,
            "remote_defer_failback": defer_failback,
            "remote_probe_interval": max(5, min(600, int(probe_interval)))}


def config_to_dict(config) -> dict:
    """把 :class:`campus_core.Config` 转成托盘内部 dict。"""
    return {
        "schema_version": config.schema_version,
        "ssid": config.ssid,
        "profile": config.profile,
        "interface": config.interface,
        "yys": config.operator,
        "account_format": config.account_format,
        "interval": config.interval,
        "threshold": config.threshold,
        "cooldown": config.cooldown,
        "browser_fallback": config.browser_fallback,
        "clash_auto_proxy": config.proxy_consent,
        "proxy_endpoint": config.proxy_endpoint,
        "paused": False,
    }


def load_config() -> dict:
    """读取并**校验**配置，返回托盘内部 dict（F03）。

    非法输入一律回退到安全默认值（绝不因此启用代理），未知键隔离并记录
    **脱敏**告警；缺失文件按默认值处理。
    """
    text, path = _read_config_text()
    result = load_config_text(text)
    cfg = config_to_dict(result.config)
    cfg["paused"] = _read_paused_flag(text)
    cfg.update(_read_failover_keys(text))
    cfg.update(_read_remote_keys(text))
    if not result.ok:
        logger = logging.getLogger("campus")
        for issue in result.issues:
            logger.warning("配置问题 [%s] %s：%s", path.name, issue.key, issue.detail)
        if result.unknown_keys:
            logger.warning("配置存在未知键 %s，已隔离（不写入运行时配置）",
                           ", ".join(result.unknown_keys))
    return cfg


_SAVE_LOCK = threading.Lock()


def _sanitize_config(cfg: dict) -> dict:
    """只保留已知键，避免把未知键写回磁盘。"""
    return {key: cfg[key] for key in CONFIG_KEYS if key in cfg}


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_config(cfg: dict) -> bool:
    """**原子**保存配置（F13）。

    同目录临时文件 → 写入 → flush/fsync → ``os.replace``；任一步失败都返回
    ``False`` 且保留旧文件（绝不显示“已保存”）。并发保存由模块级锁串行化。
    """
    path = Path(CONFIG_FILE)
    tmp_path = None
    with _SAVE_LOCK:
        try:
            data = json.dumps(_sanitize_config(cfg), ensure_ascii=False, indent=2)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".tray_config-", suffix=".tmp",
                                            dir=str(path.parent))
            os.close(fd)
            tmp_path = Path(tmp_name)
            tmp_path.write_text(data, encoding="utf-8")
            _fsync_file(tmp_path)
            os.replace(str(tmp_path), str(path))
            return True
        except (OSError, TypeError, ValueError):
            return False
        finally:
            if tmp_path is not None:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass


# --------------------------------------------------------------------------- #
# 开机自启（F12 前置：核对命令内容，而非仅判断键是否存在）
# --------------------------------------------------------------------------- #
def launcher_command() -> str:
    """当前安装对应的自启命令（F21）。

    - **打包模式**：仅启动**当前 exe**（``sys.executable``），不按文件名切换入口，
      避免调试新分支时点到旁边的旧二进制；
    - **源码模式**：使用**当前解释器**（``sys.executable`` 同目录的 ``pythonw.exe``）
      启动本脚本，不再优先相邻 exe。
    """
    if getattr(sys, "frozen", False):
        return '"%s"' % Path(sys.executable).resolve()
    return '"%s" "%s"' % (PYTHONW, SCRIPT_DIR / "campus_reconnect_tray.py")


def _normalize_command(text) -> str:
    """归一化命令文本，用于比较（折叠空白、忽略大小写）。"""
    return " ".join(str(text or "").strip().split()).lower()


def get_autostart_command():
    """返回自启项当前记录的命令（不存在为 ``None``）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            return winreg.QueryValueEx(key, RUN_VALUE_NAME)[0]
    except OSError:
        return None


def autostart_matches_current() -> bool:
    """自启命令是否指向**当前安装**（而非旧路径 / 旧 exe，F21）。"""
    command = get_autostart_command()
    return command is not None and \
        _normalize_command(command) == _normalize_command(launcher_command())


def autostart_foreign_command():
    """存在自启项但指向非当前安装时返回其命令，否则 ``None``。"""
    command = get_autostart_command()
    if command is None:
        return None
    if _normalize_command(command) == _normalize_command(launcher_command()):
        return None
    return command


def get_autostart() -> bool:
    """自启状态：**核对命令内容**，仅当对应当前安装才返回 ``True``（F21）。"""
    return autostart_matches_current()


def set_autostart(enabled: bool) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, launcher_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 系统代理（F04 / F05）
# --------------------------------------------------------------------------- #
def refresh_system_proxy() -> bool:
    """通知 Windows 代理设置已变更（独立函数，便于测试替换）。"""
    return default_refresh()


def apply_system_proxy(enabled: bool, endpoint=None,
                       registry=None, probe=None) -> ProxyApplyResult:
    """事务式切换系统代理，返回结构化结果（F04/F05）。

    ``enabled=True`` 时必须提供用户**显式确认**的 ``endpoint``；默认不猜测、
    不启用。任一步失败都不产生部分写入，已发生的写入会按事务回滚。
    """
    controller = ProxyController(registry if registry is not None else winreg,
                                 probe=probe, refresh=refresh_system_proxy)
    return controller.apply(enabled, endpoint)


def set_system_proxy(enabled: bool, endpoint=None) -> bool:
    """``apply_system_proxy`` 的布尔兼容层（仅表示“整体成功”）。"""
    return bool(apply_system_proxy(enabled, endpoint).ok)


# --------------------------------------------------------------------------- #
# 凭据（F11）
# --------------------------------------------------------------------------- #
def credential_store() -> CredentialStore:
    """构造凭据适配器（``keyring`` 为模块级引用，便于测试替换）。"""
    return CredentialStore(keyring)


def load_credentials() -> CredentialLoad:
    """读取凭据并区分「未配置 / 后端不可用」。"""
    return credential_store().load()


def save_credentials(username: str, password: str) -> CredentialSave:
    """保存凭据；第二条写入失败时尝试恢复旧对。"""
    return credential_store().save(username, password)


def clear_credentials() -> CredentialClear:
    """清除本程序保存的凭据。"""
    return credential_store().clear()


class TrayApp:
    def __init__(self, log):
        self.log = log
        self.config = load_config()
        self.session = None
        self.state = {"mode": "boot", "detail": "", "port": None,
                      "last_ok": None, "failures": 0}
        self.stop_event = threading.Event()
        self.icon = None
        self.root = None
        self.settings_win = None

        # ---- 单网络工作线程与命令队列（F01 / F02）----
        self._commands: "queue.Queue" = queue.Queue()
        self._worker = None
        self._worker_lock = threading.Lock()
        self._heartbeat = 0.0
        self._repair_busy = threading.Event()
        self._repair_pending = threading.Event()
        self._repair_override = False
        self._last_error = None
        self._op_seq = 0
        self._last_result = None
        #: Edge 兜底：同一故障周期只自动打开一次（F19）；手动按钮可再次打开。
        self._edge_opened_this_cycle = False
        #: 三表分离计时器（V0.3 P2）：网络检测 / Portal 认证冷却 / 网卡切换冷却
        #: 互不干涉；认证冷却只限制 ``_run_repair``，绝不推迟探测（F09 修复）。
        self._timers = TimerTable()

        # ---- 取消令牌（F10）----
        self._cancel = threading.Event()

        # ---- 配置版本（F01：过期结果不得覆盖新状态）----
        self._config_lock = threading.RLock()
        self._config_version = 0

        # ---- 授权版本（F12：过期询问结果不应用，新拒绝优先）----
        self._consent_version = 0
        self._consent_lock = threading.Lock()

        # ---- Tk 事件队列（F10：网络线程不直接调用 Tk）----
        self._ui_actions: "queue.Queue" = queue.Queue()
        self._ui_pump_id = None
        self._startup_timer = None

        # ---- V0.3 P6 故障切换桥接（默认仅监测；AUTO 需显式授权才接入执行器）----
        #: 只读快照提供者（注入点）。P7.5-1 起**默认指向真实三轴观测**
        #: （:meth:`_full_snapshot`：后台采集 + 可信度门禁），返回 ``None`` 时
        #: 托盘只回退到轻量占位快照，且该快照**不授权**任何 AUTO 执行。
        self._failover_snapshot_provider = self._full_snapshot
        #: 三轴观测源（惰性构造；注入点 ``_snapshot_collector`` / ``_snapshot_spawn``
        #: 便于离线测试替换采集能力与线程启动方式）。
        self._snapshot_source = None
        self._snapshot_collector = None
        self._snapshot_spawn = None
        #: 最近一次三轴观测结论（含可信度与证据；供 UI 与告警展示）。
        self.last_observation = ObservationResult()
        #: 最近一次故障切换展示状态（由 :meth:`_failover_tick` 更新）。
        self.failover_status = None

        # ---- V0.3 P7 远程保障（默认未接线；由 :meth:`enable_remote_guard` 接入）----
        #: 远控只读探测能力（``None`` = 未接线：不产生任何子进程/文件 I/O）。
        self._remote_ops = None
        #: 远程保障守卫（接线后非 ``None``）。
        self.remote = None
        #: 后台监控看门狗（纯逻辑；巡检线程由 :meth:`start_supervisor` 启动）。
        self._watchdog = MonitorWatchdog(
            heartbeat_timeout=WATCHDOG_HEARTBEAT_TIMEOUT,
            max_restarts=WATCHDOG_MAX_RESTARTS,
            restart_window=WATCHDOG_RESTART_WINDOW,
            log=lambda message: self.log.info("[watchdog] %s", message))
        self._supervisor_thread = None
        self.last_watchdog = None

        self.failover = self._build_failover_bridge()

    # ---------- 故障切换桥接（V0.3 P6）----------
    def _build_failover_bridge(self) -> TrayFailoverBridge:
        """按当前配置构造故障切换桥接（默认**仅监测**）。

        执行器工厂指向 :meth:`_build_auto_executor`，但仅在 ``AUTO`` 与
        ``auto_switch_authorized`` **同时**成立时才会被调用（见
        :meth:`campus_tray_failover.TrayFailoverBridge._sync_executor`）；
        默认（仅监测/手动锁定/未授权）**不接入**任何真实执行器。
        """
        cfg = self.get_config()
        policy = FailoverPolicy(
            detection_interval=float(self._interval()),
            failure_threshold=int(self._threshold()),
            auth_cooldown=float(self._cooldown()))
        return TrayFailoverBridge(
            mode=cfg.get("failover_mode", "MONITOR"),
            lock_target=cfg.get("failover_lock"),
            clash_keep=bool(cfg.get("clash_keep", True)),
            authorized=bool(cfg.get("auto_switch_authorized", False)),
            executor_factory=self._build_auto_executor,
            policy=policy, timer_table=self._timers,
            remote_guard=getattr(self, "remote", None),
            defer_failback=bool(cfg.get("remote_defer_failback", True)),
            log=lambda message: self.log.info("[failover] %s", message))

    def _build_auto_executor(self):
        """构造 P5 真实组合执行器（**仅**在 AUTO + 显式授权时被调用）。

        复用 CLI 侧同一套构件（P4 SNNU + P5 路由），保证托盘与 CLI 行为一致；
        构造过程含一次只读观察（解析网卡角色与无线源地址），属只读、无副作用。
        真实路由/Metric 变更仍由 P5 ``RouteGuard`` 的事务与单一副作用门把关。
        """
        import campus_reconnect as engine
        from types import SimpleNamespace
        from tools import netdiag
        from campus_network import NetworkObserver, resolve_roles

        cfg = self.get_config()
        if self.session is None:
            self.session = direct_session()
        runner = netdiag.PowerShellRunner()
        observer = NetworkObserver(runner)
        ssid = cfg.get("ssid") or "SNNU"
        report = observer.observe(target_ssid=ssid)
        roles = resolve_roles(report, engine._load_adapters_config())
        eth, wifi = roles.get("ethernet"), roles.get("wifi")
        args = SimpleNamespace(
            ssid=ssid, port=cfg.get("port"), yys=cfg.get("yys", "mobile"),
            account_format=cfg.get("account_format", "plain"),
            interval=int(self._interval()), threshold=int(self._threshold()),
            auth_cooldown=int(self._cooldown()), switch_min_interval=300, yes=True)
        guard = engine._build_route_guard(observer, runner, eth, wifi, args,
                                          self.log, timers=self._timers)
        self.log.warning("智能自动已授权：接入真实组合执行器（受控路由/Metric 变更）")
        return engine._build_composite_executor(self.session, observer, report,
                                                wifi, args, self.log, guard)

    def _failover_owns_actions(self) -> bool:
        """**自动**副作用是否已由故障切换管理器独占（P7.5-1：全模式恒真）。

        V0.3 P7.5-1 修复 F75-02：旧实现仅 ``AUTO`` 返回 ``True``，于是
        MONITOR / LOCKED 下 V0.2 的 ``_run_repair()`` 自动重连照常触发
        Portal 认证与 Wi-Fi 重连，形成**第二个副作用源**（违反 AGENTS §3.3）。

        修复后：任何模式下**后台自动**副作用都只可能来自 P3 单一副作用门；
        V0.2 自动重连路径已彻底移除，仅保留**显式手动**重连（:meth:`on_reconnect`）。
        """
        return getattr(self, "failover", None) is not None

    # ---------- 真实三轴观测（V0.3 P7.5-1，修复 F75-01）----------
    def snapshot_source(self) -> TraySnapshotSource:
        """惰性构造三轴观测源（构造过程零 I/O）。"""
        if self._snapshot_source is None:
            spawn = self._snapshot_spawn
            self._snapshot_source = TraySnapshotSource(
                collect=lambda: self._collect_health_snapshot(),
                clock=lambda: time.monotonic(),
                generation=lambda: self.config_version(),
                max_age=SNAPSHOT_MAX_AGE,
                collect_timeout=SNAPSHOT_COLLECT_TIMEOUT,
                min_interval=SNAPSHOT_MIN_INTERVAL,
                spawn=spawn,
                log=lambda message: self.log.info("[观测] %s", message))
        return self._snapshot_source

    def _collect_health_snapshot(self):
        """一次三轴采集（**在后台线程执行**，绝不阻塞 Tk 主线程）。"""
        collector = self._snapshot_collector
        if collector is not None:          # 注入点（离线测试 / 受控环境）
            return collector()
        return self._real_collect_health_snapshot()

    def _real_collect_health_snapshot(self):
        """真实三轴采集：``NetworkObserver`` + ``InterfaceHealthChecker`` + ``evaluate``。

        全程**只读**（P0 ``netdiag`` 只读管道 + 只读探测），不切路由、不认证。
        """
        from tools import netdiag
        from campus_network import (NetworkObserver, WindowsProbeEngine,
                                    evaluate, resolve_roles)

        cfg = self.get_config()
        runner = netdiag.PowerShellRunner()
        observer = NetworkObserver(runner)
        report = observer.observe(target_ssid=cfg.get("ssid") or "SNNU")
        roles = resolve_roles(report, engine._load_adapters_config())
        return evaluate(report, WindowsProbeEngine(),
                        system_probe=lambda: internet_ok(self.session),
                        roles=roles, route_lookup=observer.route_lookup)

    def _observe_snapshot(self, now: Optional[float] = None) -> ObservationResult:
        """发起（必要时）并读取一次三轴观测；**永不阻塞**。

        返回 :class:`ObservationResult`：``trusted`` 综合存在性 / 新鲜度 / 代次 /
        采集错误 / 结构完整性。``light_snapshot`` 因缺少 ``recommendation`` 与
        ``generated_at``，结构判定即为**不可信**，永远无法授权 AUTO 执行。
        """
        source = self.snapshot_source()
        try:
            source.request(now)
        except Exception as exc:                     # 采集调度故障不得影响主流程
            self.log.warning("三轴观测调度异常：%s", type(exc).__name__)
        self.last_observation = source.observe(now)
        return self.last_observation

    def _full_snapshot(self):
        """默认快照提供者：仅在观测**可信**时返回完整三轴快照。

        不可信（缺失 / 过期 / 代次失效 / 结构不完整 / 采集失败）时返回 ``None``，
        由 :meth:`_build_failover_snapshot` 回退到轻量占位快照 —— 该占位快照
        **只用于显示**，不授权任何网络副作用。
        """
        observation = self._observe_snapshot()
        return observation.snapshot if observation.trusted else None

    def invalidate_snapshot(self, reason: str = "") -> int:
        """作废旧的三轴观测代次（SSID / 模式 / 配置变化后调用）。"""
        if self._snapshot_source is None:
            return 0
        return self._snapshot_source.invalidate(reason)

    def _build_failover_snapshot(self, online: bool):
        """构造喂给状态机的只读快照。

        - 提供者返回完整三轴快照 → 直接采用（AUTO 可据此执行）；
        - 提供者返回 ``None`` 或抛错 → 轻量占位快照（**仅显示**，不授权切换）。
        """
        provider = self._failover_snapshot_provider
        snapshot = None
        if provider is not None:
            try:
                snapshot = provider()
            except Exception as exc:
                self.log.warning("故障切换快照提供者异常：%s", type(exc).__name__)
                snapshot = None
        observation = getattr(self, "last_observation", None)
        if observation is None or observation.snapshot is not snapshot:
            # 外部注入的提供者（兼容路径 / 测试）：只做**结构**判定，
            # 不假定新鲜度 —— 轻量占位快照仍会被判为不可信。
            observation = ObservationResult(
                snapshot=snapshot,
                trusted=bool(snapshot is not None and is_full_snapshot(snapshot)),
                reason=("NO_DATA" if snapshot is None else "EXTERNAL_PROVIDER"),
                detail="" if snapshot is None else "外部注入快照（未经新鲜度校验）")
            self.last_observation = observation
        if snapshot is None:
            return light_snapshot(system_online=bool(online),
                                  system_source="托盘轻量探测（占位，不授权切换）")
        return snapshot

    def _failover_tick(self, now: float, online: bool):
        """推进一轮故障切换状态机并刷新展示（不额外产生副作用）。"""
        bridge = getattr(self, "failover", None)
        if bridge is None:
            return None
        self._remote_poll(now)      # V0.3 P7：先刷新远控状态（按间隔节流）
        snapshot = self._build_failover_snapshot(online)
        observation = getattr(self, "last_observation", None)
        trusted = bool(observation is not None and observation.trusted)
        # 可信度门禁（P7.5-1）：数据不足时 AUTO 一律拒绝执行网络副作用。
        bridge.set_snapshot_trust(
            trusted, observation.reason_text if observation is not None else "")
        if not trusted and observation is not None:
            self.log.info("三轴快照不可信（%s）：本轮不执行任何网络副作用",
                          observation.reason_text)
        result = bridge.tick(now, snapshot)
        self.failover_status = bridge.status()
        if result.executed:
            self.log.info("故障切换已执行动作：%s",
                          ", ".join(o.kind.value for o in result.executed))
        return result

    # ---------- 远程保障（V0.3 P7）----------
    def enable_remote_guard(self, ops=None) -> Optional[RemoteSafetyGuard]:
        """接线远程保障（生产入口；``ops`` 可注入假件用于测试）。

        构造守卫**不做任何 I/O**；真正的远控探测发生在 :meth:`_remote_poll`
        （按 ``remote_probe_interval`` 节流）。未调用本方法时（例如单元测试
        直接构造 ``TrayApp``），``_remote_ops`` 为 ``None``，托盘**零子进程**。
        """
        if ops is None:
            from tools import netdiag

            ops = build_windows_remote_ops(netdiag.PowerShellRunner())
        self._remote_ops = ops
        cfg = self.get_config()
        self.remote = RemoteSafetyGuard(
            ops=ops,
            probe_interval=float(cfg.get("remote_probe_interval", 30)),
            defer_failback=bool(cfg.get("remote_defer_failback", True)),
            log=lambda message: self.log.info("[remote] %s", message))
        bridge = getattr(self, "failover", None)
        if bridge is not None:
            bridge.set_remote_guard(
                self.remote, bool(cfg.get("remote_defer_failback", True)))
        self.log.info("远程保障已接线：探测间隔 %.0fs，敏感期推迟切回=%s",
                      self.remote.probe_interval, self.remote.defer_failback)
        return self.remote

    def _remote_poll(self, now: float) -> None:
        """刷新远控状态（未接线或配置关闭时直接返回，零 I/O）。"""
        if self._remote_ops is None or self.remote is None:
            return
        if not bool(self.config.get("remote_guard", True)):
            return
        try:
            self.remote.poll(now)
        except Exception as exc:                     # 远控探测故障不得影响主流程
            self.log.warning("远控探测异常：%s", type(exc).__name__)

    def apply_remote_guard(self, enabled: bool) -> bool:
        """开关远控探测（方案 §7.3；关闭后不再探测，也不推迟切回）。"""
        value = bool(enabled)
        self.update_config({"remote_guard": value})
        if not save_config(self.config):
            self.notify("保存失败", "远程保障设置未能写入配置（已保留原文件）")
        self.notify("远控探测已%s" % ("开启" if value else "关闭"),
                    "关闭后不再检测远程会话，也不会因敏感期推迟切回")
        if self.icon is not None:
            self.icon.update_menu()
        return value

    def on_toggle_remote_guard(self, icon=None, item=None):
        self.apply_remote_guard(not bool(self.config.get("remote_guard", True)))

    def apply_defer_failback(self, enabled: bool) -> bool:
        """开关「远程会话敏感期推迟自动切回」（方案 §4.3）。"""
        value = bool(enabled)
        self.update_config({"remote_defer_failback": value})
        if not save_config(self.config):
            self.notify("保存失败", "延迟切回设置未能写入配置（已保留原文件）")
        bridge = getattr(self, "failover", None)
        if bridge is not None:
            bridge.set_remote_guard(getattr(self, "remote", None), value)
        self.notify("远程会话敏感期推迟切回已%s" % ("开启" if value else "关闭"),
                    "远程会话进行中时%s自动切回有线"
                    % ("推迟" if value else "不再推迟"))
        if self.icon is not None:
            self.icon.update_menu()
        return value

    def on_toggle_defer_failback(self, icon=None, item=None):
        self.apply_defer_failback(
            not bool(self.config.get("remote_defer_failback", True)))

    def _remote_text(self, item=None):
        """菜单中的远程保障摘要（只读）。"""
        status = getattr(self, "remote", None)
        if status is None or self._remote_ops is None:
            return "远程保障：未接线"
        if not bool(self.config.get("remote_guard", True)):
            return "远程保障：已关闭"
        current = status.status()
        suffix = "（远程会话中，推迟切回）" if current.sensitive else ""
        return "远程保障：%s · 远控 %s%s" % (
            "探测中", current.activity, suffix)

    # ---------- 崩溃恢复看门狗（V0.3 P7）----------
    def _route_transaction_pending(self) -> bool:
        """是否存在未完成的路由变更事务（只读检查；无日志文件即无）。"""
        try:
            return bool(engine._route_journal_pending())
        except Exception:
            return False

    def _build_recovery_guard(self):
        """构造路由卫士用于**崩溃恢复**（授权 ``False``；``recover()`` 不受其限制）。

        与 :meth:`_build_auto_executor` 复用同一套构件；构造过程含一次只读观察
        （解析网卡角色），属只读、无副作用。
        """
        from types import SimpleNamespace
        from tools import netdiag
        from campus_network import NetworkObserver, resolve_roles

        cfg = self.get_config()
        runner = netdiag.PowerShellRunner()
        observer = NetworkObserver(runner)
        ssid = cfg.get("ssid") or "SNNU"
        report = observer.observe(target_ssid=ssid)
        roles = resolve_roles(report, engine._load_adapters_config())
        args = SimpleNamespace(ssid=ssid, interval=int(self._interval()),
                               threshold=int(self._threshold()),
                               auth_cooldown=int(self._cooldown()),
                               switch_min_interval=300, yes=False)
        return engine._build_route_guard(observer, runner, roles.get("ethernet"),
                                         roles.get("wifi"), args, self.log,
                                         timers=self._timers)

    def _recover_route_transaction(self):
        """路由崩溃恢复（独立于主监控线程；仅回滚日志中已记录的变更）。"""
        guard = self._build_recovery_guard()
        record = guard.recover()
        if record is not None:
            self.log.warning("路由崩溃恢复：状态=%s，说明=%s",
                             record.status.value, record.detail)
            self.notify("已恢复未完成的路由变更", record.detail)
        return record

    def _watchdog_check(self, now: float, status=None):
        """巡检一次看门狗并按决策执行恢复动作（纯逻辑 + 注入回调）。"""
        status = status or self.worker_status()
        decision = self._watchdog.check(
            now, alive=bool(status["alive"]),
            heartbeat_age=status.get("heartbeat_age"),
            pending_transaction=self._route_transaction_pending)
        self.last_watchdog = decision
        if decision.action == "OK":
            return decision
        if decision.action == "RESUME_RECHECK":
            self.log.info("看门狗：%s；立即重新检测路径", decision.reason)
            self._commands.put(("probe", None))
            return decision
        if decision.action == "MANUAL_REQUIRED":
            self._report_worker_error("监测线程反复退出", RuntimeError(decision.reason))
            self.notify("监测需人工处理", decision.reason)
            return decision
        result = apply_watchdog_decision(
            decision, restart=self._restart_worker_if_needed,
            recover=self._recover_route_transaction, watchdog=self._watchdog,
            now=now, log=lambda message: self.log.info("[watchdog] %s", message))
        self.log.warning("看门狗恢复：%s", result)
        return decision

    def _restart_worker_if_needed(self) -> bool:
        self.stop_event.clear()
        self._cancel.clear()
        return bool(self._ensure_worker())

    def start_supervisor(self) -> None:
        """启动独立巡检线程（方案 §7.2：独立于主监控线程的恢复机制）。"""
        if self._supervisor_thread is not None and self._supervisor_thread.is_alive():
            return
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop, name="campus-watchdog", daemon=True)
        self._supervisor_thread.start()

    def _supervisor_loop(self) -> None:
        while not self.stop_event.wait(WATCHDOG_INTERVAL):
            try:
                self._watchdog_check(time.monotonic())
            except Exception as exc:
                self.log.warning("看门狗巡检异常：%s", type(exc).__name__)

    def startup_route_recovery(self):
        """启动时的路由崩溃恢复（方案 §7.2：Windows 重启/崩溃后的安全兜底）。

        **仅在**路由变更日志存在未完成事务时才构造卫士；无待恢复项时零写入、
        零重型观察。构造与恢复过程可能耗时数十秒，调用方应在后台线程执行。
        """
        if not self._route_transaction_pending():
            self.log.info("启动检查：无未完成的路由变更事务（零写入）")
            return None
        try:
            return self._recover_route_transaction()
        except Exception as exc:
            self.log.warning("启动路由恢复失败：%s: %s", type(exc).__name__, exc)
            return None

    def startup_readiness_check(self):
        """启动时的无人值守就绪自检（只读；把缺口写入日志与气泡通知）。"""
        guard = getattr(self, "remote", None)
        if guard is None or self._remote_ops is None:
            return None
        if not bool(self.config.get("remote_guard", True)):
            return None
        try:
            report = guard.readiness()
        except Exception as exc:
            self.log.warning("就绪自检失败：%s", type(exc).__name__)
            return None
        for warning in report.warnings:
            self.log.warning("无人值守缺口：%s", warning)
        if report.warnings:
            self.notify("无人值守存在缺口", "；".join(report.warnings[:2]))
        else:
            self.log.info("无人值守就绪自检通过（人工项除外）")
        return report

    # ---------- 图标与通知 ----------
    def make_image(self):
        color = MODE_COLOR.get(self.state["mode"], (139, 148, 158))
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([6, 6, 58, 58], fill=color + (255,))
        d.ellipse([22, 22, 42, 42], fill=(255, 255, 255, 235))
        return img

    def tooltip(self):
        return "%s · %s · 模式 %s" % (APP_NAME,
                                     MODE_TEXT.get(self.state["mode"], "?"),
                                     tray_mode_label(self._current_mode()))

    def set_state(self, mode, detail="", notify=None):
        prev = self.state["mode"]
        self.state["mode"] = mode
        self.state["detail"] = detail
        if self.icon is not None:
            try:
                self.icon.icon = self.make_image()
                self.icon.title = self.tooltip()
            except Exception:
                pass
        self.log.info("状态 -> %s（%s）", mode, detail)
        if notify and prev != mode:
            self.notify(*notify)

    def notify(self, title, body):
        """发送气泡通知。注意 pystray 签名是 notify(message, title)。"""
        if self.icon is None:
            return
        message = (body or "")[:255]
        head = (title or "")[:63]
        try:
            self.icon.notify(message, head)
        except Exception:
            self.log.warning("通知发送失败:\n%s", traceback.format_exc())

    # ---------- 配置访问（带版本，F01）----------
    def get_config(self) -> dict:
        """返回当前配置的**不可变快照**（dict 副本）。"""
        with self._config_lock:
            return dict(self.config)

    def update_config(self, changes: dict) -> int:
        """应用配置变更并提升配置版本（使在途任务的旧结果失效）。"""
        with self._config_lock:
            self.config.update(changes)
            self._config_version += 1
            return self._config_version

    def config_version(self) -> int:
        return self._config_version

    def _new_op_id(self) -> str:
        with self._config_lock:
            self._op_seq += 1
            seq = self._op_seq
        return "op-%s-%03d" % (uuid.uuid4().hex[:6], seq)

    # ---------- 取消令牌（F10）----------
    def _cancelled(self) -> bool:
        if self.stop_event.is_set() or self._cancel.is_set():
            return True
        return bool(self.config.get("paused")) and not self._repair_override

    # ---------- Tk 事件队列（F10：网络线程不直接调用 Tk）----------
    def _ui_post(self, action) -> None:
        self._ui_actions.put(action)

    def _pump_ui(self):
        if self.root is None:
            return
        try:
            while True:
                action = self._ui_actions.get_nowait()
                try:
                    action()
                except Exception:
                    self.log.warning("UI 动作异常:\n%s", traceback.format_exc())
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            try:
                self._ui_pump_id = self.root.after(UI_POLL_MS, self._pump_ui)
            except tk.TclError:
                self._ui_pump_id = None

    def _start_ui_pump(self) -> None:
        if self.root is None:
            return
        try:
            self._ui_pump_id = self.root.after(UI_POLL_MS, self._pump_ui)
        except tk.TclError:
            self._ui_pump_id = None

    def _ui_ask(self, title, body, timeout=60.0):
        """在 Tk 主线程弹一个是/否对话框并同步等待（有界，不永久等待）。"""
        if self.root is None:
            return None
        holder = {"value": None}
        done = threading.Event()

        def action():
            try:
                holder["value"] = bool(messagebox.askyesno(title, body))
            except Exception:
                holder["value"] = None
            finally:
                done.set()

        self._ui_post(action)
        if not done.wait(timeout):
            self.log.warning("授权询问超时（%.0f 秒），本次不执行该操作", timeout)
            return None
        return holder["value"]

    # ---------- 重连核心（F01/F10/F14/F15）----------
    def _repair_adapters(self) -> RepairAdapters:
        """构造适配器集合：**在调用时**读取模块级函数，便于测试替换（F14）。

        Wi-Fi 重连按配置里的 ``profile`` / ``interface`` 分离指定（F17）：
        ``netsh wlan connect name=`` 实际按 profile 名操作，profile 未必等于 SSID。
        """
        cfg = self.get_config()
        return RepairAdapters(
            wifi_state=wifi_state,
            wifi_reconnect=lambda ssid: wifi_reconnect(
                ssid, self.log, profile=cfg.get("profile"),
                interface=cfg.get("interface")),
            resolve_port=resolve_port,
            portal_reachable=portal_reachable,
            probe_online=internet_ok,
            login=portal_login,
        )

    def repair_once(self):
        """执行一次完整重连, 返回 (成功?, 详情, 耗时秒)。

        - 入口即对配置做**不可变快照**（``RepairOptions``）：任务进行中修改运营商 /
          参数不影响本次请求（F01）；
        - 底层走 CLI/托盘共用的 ``repair_service``（F14），空运营商原样保留（F15）；
        - 取消令牌（退出 / 暂停）在任何副作用（POST / Edge / 代理写入）前重新校验（F10）。
        """
        cfg = self.get_config()
        if self.stop_event.is_set() or self._cancel.is_set():
            return False, "已退出或取消，未发起认证", 0.0
        if cfg.get("paused") and not self._repair_override:
            return False, "监测已暂停，未发起认证", 0.0
        if self.session is None:
            self.session = direct_session()
        options = RepairOptions.from_mapping(cfg)
        result = repair_service(
            self.session, options, time.monotonic, self._repair_adapters(),
            get_credentials, cancelled=self._cancelled,
            sleep=self.stop_event.wait, log=self.log)
        self._last_result = result
        if result.port is not None:
            self.state["port"] = result.port
        return result.ok, result.detail, result.elapsed

    def _after_repair(self, ok, detail, elapsed, op_id=None, config_version=None):
        if config_version is not None and config_version != self._config_version:
            self.log.info("丢弃过期重连结果（配置版本 %s != 当前 %s）",
                          config_version, self._config_version)
            return
        cfg = self.get_config()
        if ok:
            self.state["last_ok"] = datetime.now().strftime("%H:%M:%S")
            self.set_state("online", detail)
            self._edge_opened_this_cycle = False   # 网络恢复 → 重置 Edge 限频周期
            allow = cfg.get("clash_auto_proxy")
            if allow is None:
                token = self._consent_version
                answer = self._ask_proxy_consent()
                if (answer is not None and self._consent_version == token
                        and self.config.get("clash_auto_proxy") is None):
                    self._apply_consent(answer, save=True)
                allow = self.config.get("clash_auto_proxy")
            proxy_note = ""
            if allow:
                if self._cancelled():
                    proxy_note = "\n已取消，未改动系统代理"
                else:
                    endpoint = cfg.get("proxy_endpoint") or CLASH_SERVER_DEFAULT
                    result = apply_system_proxy(True, endpoint)
                    if result.ok:
                        proxy_note = "\n已自动开启系统代理（%s）" % (result.endpoint or endpoint)
                    else:
                        proxy_note = "\n网络已恢复，但系统代理未开启：%s" % (
                            result.detail or "原因未知")
            self.notify("重连成功", "用时约 %.0f 秒 · %s%s" % (elapsed, detail, proxy_note))
        else:
            self.set_state("offline", detail)
            extra = self._edge_note(cfg)
            self.notify("重连失败", "%s%s" % (detail, extra))

    def _edge_note(self, cfg) -> str:
        """按结果如实报告 Edge 兜底；仅在需人工处理时触发，且同周期限一次（F19）。"""
        if not cfg.get("browser_fallback"):
            return ""
        result = self._last_result
        needs_user = bool(getattr(result, "needs_user", False))
        if not needs_user:
            return ""   # 非人工处理类故障（如探测不可信）不打开浏览器
        if self._cancelled():
            return "\n已取消，未打开 Edge"
        if self._edge_opened_this_cycle:
            return "\n本故障周期已打开过 Edge，未重复打开"
        launch = edge_fallback(self.log)
        self._edge_opened_this_cycle = True
        if getattr(launch, "launched", False):
            return "\n已打开 Edge 供手动登录"
        return "\n未能自动打开 Edge（可手动访问 Portal 登录页）"

    def _apply_consent(self, value, save=False):
        """用户显式改变代理授权：提升授权版本，使过期的询问结果失效（F12）。"""
        with self._config_lock:
            self._consent_version += 1
            self.config["clash_auto_proxy"] = value
            if save and not save_config(self.config):
                self.log.error("代理授权写入配置失败，本次改动可能不会持久化")
        return value

    def _ask_proxy_consent(self):
        """首次重连成功时询问是否同意联动开启系统代理（同一时刻只允许一个询问）。"""
        if not self._consent_lock.acquire(blocking=False):
            self.log.info("已有授权询问进行中，跳过重复询问")
            return None
        try:
            if self.root is None:
                return None
            endpoint = self.config.get("proxy_endpoint") or CLASH_SERVER_DEFAULT
            body = ("校园网重连成功。\n\n是否同意：以后每次重连成功后，自动开启系统代理？\n"
                    "将启用端点：%s\n（可随时在托盘菜单或设置面板更改）" % endpoint)
            return self._ui_ask("系统代理联动", body, timeout=60.0)
        finally:
            self._consent_lock.release()

    # ---------- 网络工作线程（F01 / F02）----------
    def _ensure_worker(self) -> bool:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return True
            if self.stop_event.is_set():
                return False
            try:
                self._worker = threading.Thread(target=self._worker_loop,
                                                name="campus-net", daemon=True)
                self._worker.start()
            except Exception as exc:
                self._report_worker_error("网络工作线程启动失败", exc)
                self._worker = None
                return False
            return True

    def worker_status(self) -> dict:
        """工作线程与心跳状态（供 UI 显示“监测已停止”并提供重启动作）。"""
        thread = self._worker
        alive = bool(thread is not None and thread.is_alive())
        age = (time.monotonic() - self._heartbeat) if self._heartbeat else None
        if alive:
            label = "监测运行中"
        elif self.stop_event.is_set():
            label = "监测已停止（已退出）"
        else:
            label = "监测已停止"
        return {"alive": alive, "heartbeat_age": age, "label": label,
                "last_error": self._last_error}

    def restart_monitor(self, icon=None, item=None):
        """线程意外退出后的显式重启动作（F02）。"""
        if self.worker_status()["alive"]:
            self.notify("监测运行中", "网络工作线程已在运行")
            return
        self.stop_event.clear()
        self._cancel.clear()
        self._last_error = None
        if self._ensure_worker():
            self.notify("监测已重启", "网络工作线程已重新启动")
        else:
            self.notify("重启失败", "无法启动网络工作线程")

    def _report_worker_error(self, context, exc, op_id=None):
        detail = "%s：%s" % (context, type(exc).__name__)
        if op_id:
            detail += "（%s）" % op_id
        self.log.error("%s\n%s", detail, traceback.format_exc())
        self._last_error = detail
        self.set_state("error", detail, notify=("监测异常", detail))

    def _interval(self) -> float:
        try:
            value = int(self.config.get("interval", 15))
        except (TypeError, ValueError):
            value = 15
        return float(max(1, value))

    def _cooldown(self) -> float:
        try:
            value = int(self.config.get("cooldown", 180))
        except (TypeError, ValueError):
            value = 180
        return float(max(60, value))

    def _threshold(self) -> int:
        try:
            value = int(self.config.get("threshold", 3))
        except (TypeError, ValueError):
            value = 3
        return max(1, value)

    def _wait_timeout(self, next_probe: float) -> float:
        if self.config.get("paused") or not next_probe:
            return self._interval()
        return max(0.1, next_probe - time.monotonic())

    def _worker_loop(self):
        """单网络工作线程：独占 Session，串行消费命令（F01）。"""
        self._heartbeat = time.monotonic()
        self.log.info("网络工作线程启动")
        try:
            if self.session is None:
                self.session = direct_session()
        except Exception as exc:
            self._report_worker_error("初始化网络会话失败", exc)
            return
        if self.config.get("paused"):
            self.set_state("paused", "监测已暂停")
        else:
            self.set_state("boot", "正在检查网络状态")
        next_probe = 0.0
        try:
            while not self.stop_event.is_set():
                self._heartbeat = time.monotonic()
                try:
                    try:
                        cmd = self._commands.get(timeout=self._wait_timeout(next_probe))
                    except queue.Empty:
                        cmd = ("probe", None)
                    if self.stop_event.is_set():
                        break
                    next_probe = self._dispatch(cmd, next_probe)
                except Exception as exc:   # 工作单元整体异常边界（F02）
                    self._report_worker_error("工作单元异常", exc)
                    self._repair_busy.clear()
                    self._repair_pending.clear()
                    next_probe = time.monotonic() + self._interval()
        finally:
            self.log.info("网络工作线程已退出")

    def _dispatch(self, cmd, next_probe: float) -> float:
        kind = cmd[0] if cmd else "probe"
        if kind == "reconnect":
            force = bool(cmd[1]) if len(cmd) > 1 else False
            # P7.5-1：手动重连携带**请求时的配置版本**。若期间模式/配置已变更，
            # 该请求即视为过期并丢弃 —— 旧任务不得在新模式下继续执行。
            version = cmd[2] if len(cmd) > 2 else None
            if version is not None and version != self._config_version:
                self.log.info("丢弃过期手动重连请求（版本 %s != 当前 %s）",
                              version, self._config_version)
                self._repair_pending.clear()
                return time.monotonic() + self._interval()
            self._run_repair(force=force)
            # 手动/强制重连已完成一次认证 → 登记**认证冷却**；但**不**推迟探测（P2）。
            self._timers.note_auth(time.monotonic(), self._cooldown())
            return time.monotonic() + self._interval()
        if kind == "probe":
            return self._probe_tick(next_probe)
        if kind == "quit":
            self.stop_event.set()
            return 0.0
        return next_probe

    def _next_probe_time(self, now: float) -> float:
        """下一次**探测**时刻（V0.3 P2 三表分离）。

        探测节奏只由「网络检测计时器」决定，恒为 ``now + interval``；
        **认证冷却不得推迟探测** —— 修复 P0-T6 取证缺陷（旧实现取
        ``max(now + interval, _next_attempt_at)``，使 180s 认证冷却顺带暂停
        公网探测）。认证冷却只影响 :meth:`_probe_tick` 中是否发起认证。
        """
        return self._timers.schedule_detection(now, self._interval())

    def _probe_tick(self, next_probe: float) -> float:
        now = time.monotonic()
        if self.config.get("paused"):
            if self.state["mode"] != "paused":
                self.set_state("paused", "监测已暂停")
            return now + self._interval()
        if self.state["mode"] == "paused":
            self.set_state("boot", "监测恢复，正在检查网络")
        if next_probe and now < next_probe:
            return next_probe
        try:
            online, _ = internet_ok(self.session)
        except Exception as exc:
            self.log.warning("探测异常: %s", type(exc).__name__)
            online = False
        # V0.3 P6：把本轮只读观测喂给故障切换状态机（默认仅监测，零动作）。
        self._failover_tick(now, online)
        if online:
            if self.state["mode"] != "online":
                self.set_state("online", "网络正常")
            self.state["failures"] = 0
            self._edge_opened_this_cycle = False
            return self._next_probe_time(now)
        self.state["failures"] += 1
        threshold = self._threshold()
        if self.state["failures"] < threshold:
            if self.state["mode"] != "offline":
                self.set_state("offline", "公网探测失败（%d/%d）"
                               % (self.state["failures"], threshold))
            return self._next_probe_time(now)
        # ---- P7.5-1（F75-02）：后台**永不**自动认证 ----
        # 旧实现在此调用 V0.2 的 ``_run_repair()``（Portal POST + Wi-Fi 重连），
        # 于是 MONITOR / LOCKED 下也会出现第二个副作用源。修复后：自动副作用
        # 只可能来自 P3 单一副作用门（AUTO 且已授权且快照可信）；其余模式
        # 只如实显示故障并等待，或在菜单/设置面板由用户**显式**手动重连。
        detail = self._offline_detail()
        if self.state["mode"] != "offline" or self.state["detail"] != detail:
            self.set_state("offline", detail)
        return self._next_probe_time(now)

    def _offline_detail(self) -> str:
        """达到失败阈值后的说明（按模式如实区分，不谎称"正在重连"）。"""
        mode = self._current_mode()
        failures = self.state["failures"]
        threshold = self._threshold()
        prefix = "公网探测连续失败（%d/%d）" % (failures, threshold)
        if mode is FailoverMode.AUTO:
            return (prefix + "：已交由故障切换调度器（单一副作用门），"
                    "是否需要动作由三轴快照决定")
        if mode is FailoverMode.LOCKED:
            return prefix + "：手动锁定模式不自动认证/切换（可手动重连）"
        return prefix + "：仅监测模式不自动认证（可在托盘菜单手动重连）"

    def _run_repair(self, force=False):
        """串行执行一次重连；重复请求合并（F01）。"""
        if self._repair_busy.is_set():
            self.log.info("已有一次重连在途，本次请求合并")
            return
        self._repair_busy.set()
        self._repair_override = bool(force)
        version = self._config_version
        op_id = self._new_op_id()
        try:
            if self._cancelled():
                self.log.info("已暂停/退出，跳过本次重连（%s）", op_id)
                return
            try:
                ok, detail, elapsed = self.repair_once()
            except Exception as exc:   # 预期外异常 → 可识别内部错误，不静默失效（F02）
                self._report_worker_error("重连任务异常", exc, op_id=op_id)
                return
            self._after_repair(ok, detail, elapsed, op_id=op_id,
                               config_version=version)
        finally:
            self._repair_override = False
            self._repair_busy.clear()
            self._repair_pending.clear()

    # ---------- 菜单动作 ----------
    def _status_text(self, item=None):
        extra = ""
        if self.state.get("port"):
            extra += " · 端口 %s" % self.state["port"]
        if self.state.get("last_ok"):
            extra += " · 上次重连 %s" % self.state["last_ok"]
        status = self.worker_status()
        if not status["alive"]:
            extra += " · %s" % status["label"]
        return "状态：%s · 模式：%s%s" % (MODE_TEXT.get(self.state["mode"], "?"),
                                       tray_mode_label(self._current_mode()), extra)

    def _pause_text(self, item=None):
        return "恢复监测" if self.config.get("paused") else "暂停监测"

    def on_reconnect(self, icon=None, item=None):
        """手动重连：投递到单网络工作线程；重复请求合并并提示“正在重连”。"""
        if self._repair_pending.is_set() or self._repair_busy.is_set():
            self.notify("正在重连", "已有一次重连在进行，请稍候…")
            return
        if self.config.get("paused"):
            self._request_manual_while_paused()
            return
        self._edge_opened_this_cycle = False   # 手动按钮可再次打开 Edge（F19）
        self._enqueue_repair(force=False)

    def _enqueue_repair(self, force=False) -> bool:
        if not self._ensure_worker():
            self.notify("无法启动", "网络工作线程启动失败")
            return False
        self._repair_pending.set()
        # 第三个元素 = 请求时的配置版本（P7.5-1：模式/配置变更后丢弃旧请求）。
        self._commands.put(("reconnect", bool(force), self.config_version()))
        return True

    def _request_manual_while_paused(self):
        """暂停中手动重连需单独确认（F10：默认暂停不启动新认证）。"""
        answer = self._ui_ask("监测已暂停",
                              "监测当前处于暂停状态。\n是否仍要执行一次手动重连？")
        if not answer:
            self.notify("已取消", "监测处于暂停状态，未发起重连")
            return
        self._enqueue_repair(force=True)

    def on_toggle_pause(self, icon=None, item=None):
        paused = not bool(self.config.get("paused"))
        self.update_config({"paused": paused})
        if not save_config(self.config):
            self.notify("保存失败", "暂停状态未能写入配置（已保留原文件）")
        bridge = getattr(self, "failover", None)
        if paused:
            self.set_state("paused", "监测已暂停")
            if bridge is not None:
                bridge.pause(time.monotonic())      # 状态机粘性 PAUSED：零动作
        else:
            self.set_state("boot", "监测恢复，正在检查网络")
            if bridge is not None:
                bridge.resume(time.monotonic())
        self.notify("监测已暂停" if paused else "监测已恢复",
                    "可在菜单或设置面板随时切换")
        if self.icon is not None:
            self.icon.update_menu()

    # ---------- 三模式与手动锁定（V0.3 P6，方案 §11 / §9）----------
    def _current_mode(self) -> FailoverMode:
        bridge = getattr(self, "failover", None)
        if bridge is not None:
            return bridge.mode
        return parse_mode(self.config.get("failover_mode"))

    def _current_lock(self) -> Optional[str]:
        bridge = getattr(self, "failover", None)
        if bridge is not None:
            return bridge.lock_target
        return parse_lock(self.config.get("failover_lock"))

    def _apply_failover_config(self, changes: dict) -> None:
        """保存并应用故障切换相关配置，同步桥接与托盘菜单。"""
        self.update_config(changes)
        if not save_config(self.config):
            self.notify("保存失败", "故障切换设置未能写入配置（已保留原文件）")
        self._sync_failover_from_config()
        if self.icon is not None:
            self.icon.update_menu()

    def _sync_failover_from_config(self) -> None:
        """把当前配置同步到桥接（模式/锁定/Clash/授权）并作废旧观测代次。

        P7.5-1：模式 / SSID / 锁定目标等变化后，必须作废在途的三轴采集结果，
        否则旧数据（采集于变更之前）会被当作当前依据喂给状态机。
        """
        cfg = self.get_config()
        # 作废旧代次（内部自增 + 配置版本已变更，双重保险）。
        self.invalidate_snapshot("故障切换配置已变更")
        bridge = getattr(self, "failover", None)
        if bridge is None:
            self.failover = self._build_failover_bridge()
            return
        bridge.set_mode(cfg.get("failover_mode", "MONITOR"))
        bridge.set_lock_target(cfg.get("failover_lock"))
        bridge.set_clash_keep(bool(cfg.get("clash_keep", True)))
        bridge.set_authorized(bool(cfg.get("auto_switch_authorized", False)))
        bridge.set_remote_guard(getattr(self, "remote", None),
                                bool(cfg.get("remote_defer_failback", True)))

    def apply_mode(self, mode) -> FailoverMode:
        """设置运行模式（纯逻辑；菜单、设置面板与测试共用）。"""
        resolved = parse_mode(mode)
        self._apply_failover_config({"failover_mode": resolved.value})
        self.notify("运行模式：%s" % tray_mode_label(resolved),
                    _MODE_HINT.get(resolved, ""))
        return resolved

    def on_set_mode(self, mode):
        """菜单选择模式：切到「智能自动」且未授权时，先征求**显式授权**。"""
        resolved = parse_mode(mode)
        if resolved is FailoverMode.AUTO \
                and not self.config.get("auto_switch_authorized"):
            answer = self._ui_ask(
                "智能自动授权",
                "「智能自动」允许程序在有线故障时自动切换网络出口"
                "（真实修改路由 / Metric，受控、前台上人）。\n\n"
                "是否授权启用？\n（默认「仅监测」不改动任何路由，可随时切回）")
            if answer is not True:
                self.notify("未授权", "仍保持当前模式（未启用智能自动）")
                if self.icon is not None:
                    self.icon.update_menu()
                return
            self.update_config({"auto_switch_authorized": True})
            if not save_config(self.config):
                self.notify("保存失败", "授权未能写入配置（已保留原文件）")
        self.apply_mode(resolved)

    def apply_lock(self, target) -> Optional[str]:
        """设置手动锁定的优先路径（``ethernet`` / ``wifi`` / ``None``）。"""
        resolved = parse_lock(target)
        self._apply_failover_config({"failover_lock": resolved})
        self.notify("锁定目标：%s"
                    % (PATH_LABEL.get(resolved, resolved) if resolved else "未锁定"),
                    "手动锁定不自动禁用另一张网卡")
        return resolved

    def apply_clash_keep(self, flag: bool) -> bool:
        """设置「网络切换期间保持 Clash 原样」（方案 §9，默认开启）。"""
        value = bool(flag)
        self._apply_failover_config({"clash_keep": value})
        self.notify("网络切换期间%s Clash"
                    % ("保持原样" if value else "允许联动"), "")
        return value

    def on_toggle_clash_keep(self, icon=None, item=None):
        self.apply_clash_keep(not bool(self.config.get("clash_keep", True)))

    def _failover_text(self, item=None):
        """菜单中的故障切换状态摘要（只读）。"""
        status = getattr(self, "failover_status", None)
        if status is None:
            return "故障切换：%s（等待首次观测）" % tray_mode_label(self._current_mode())
        primary = PATH_LABEL.get(status.primary, "无可用路径") if status.primary else "无可用路径"
        return "故障切换：%s · 主 %s · 有线 %s · Wi-Fi %s" % (
            status.mode_label, primary, status.ethernet_status, status.wifi_status)

    def on_toggle_proxy(self, icon=None, item=None):
        new_value = self.config.get("clash_auto_proxy") is not True
        self._apply_consent(new_value, save=True)
        self.notify("Clash 联动已%s" % ("开启" if new_value else "关闭"),
                    "重连成功后%s自动开启系统代理" % ("会" if new_value else "不会"))
        if self.icon is not None:
            self.icon.update_menu()

    def on_toggle_fallback(self, icon=None, item=None):
        new_value = not bool(self.config.get("browser_fallback"))
        self.update_config({"browser_fallback": new_value})
        if not save_config(self.config):
            self.notify("保存失败", "Edge 兜底设置未能写入配置（已保留原文件）")
        self.notify("Edge 兜底已%s" % ("开启" if new_value else "关闭"), "")
        if self.icon is not None:
            self.icon.update_menu()

    def on_toggle_autostart(self, icon=None, item=None):
        target = not get_autostart()
        if target:
            foreign = autostart_foreign_command()
            if foreign:
                # 外部旧路径：先确认再接管，不静默覆盖（F21）
                answer = self._ui_ask(
                    "开机自启",
                    "检测到已存在的自启项指向其他位置：\n%s\n\n是否改为当前安装路径？" % foreign)
                if answer is not True:
                    self.notify("开机自启未更改", "已保留原有自启项")
                    if self.icon is not None:
                        self.icon.update_menu()
                    return
        if set_autostart(target):
            self.notify("开机自启已%s" % ("开启" if target else "关闭"),
                        "登录 Windows 后自动启动托盘助手" if target else "不再自动启动")
        else:
            self.notify("设置失败", "无法写入开机自启注册表项")
        if self.icon is not None:
            self.icon.update_menu()

    def on_update_credentials(self, icon=None, item=None):
        """更新账号 / 密码（F11：失败保持应用可见，不崩溃）。"""
        if self.root is None:
            return
        self._ui_post(self._open_credentials_dialog)

    def on_clear_credentials(self, icon=None, item=None):
        """清除凭据（F11）。"""
        if self.root is None:
            return
        self._ui_post(self._clear_credentials_dialog)

    def on_open_log(self, icon=None, item=None):
        log_file = LOG_DIR / "campus_reconnect.log"
        if log_file.exists():
            try:
                os.startfile(str(log_file))
            except OSError:
                pass

    def on_settings(self, icon=None, item=None):
        if self.root is not None:
            self._ui_post(self._open_settings)

    def on_quit(self, icon=None, item=None):
        self.log.info("用户退出托盘助手")
        self.stop_event.set()
        self._cancel.set()
        try:
            self._commands.put_nowait(("quit", None))
        except Exception:
            pass
        self._cancel_startup_timer()
        if self._ui_pump_id is not None and self.root is not None:
            try:
                self.root.after_cancel(self._ui_pump_id)
            except Exception:
                pass
            self._ui_pump_id = None
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self.root is not None:
            try:
                self.root.after(0, self.root.destroy)
            except Exception:
                pass
        self._join_worker(3.0)

    def _join_worker(self, timeout: float) -> None:
        thread = self._worker
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout)
        if thread.is_alive():
            self.log.warning("网络工作线程在 %.1f 秒内未退出（可能在等待网络超时）", timeout)

    def _mode_action(self, mode):
        """返回符合 pystray 签名 ``(icon, item)`` 的模式切换动作。"""
        def action(icon, item):
            self.on_set_mode(mode)
        return action

    def _mode_checked(self, mode):
        """返回符合 pystray 签名 ``(item)`` 的模式单选勾选判定。"""
        def checked(item):
            return self._current_mode() is mode
        return checked

    def build_menu(self):
        mode_menu = pystray.Menu(*[
            pystray.MenuItem(
                tray_mode_label(mode),
                self._mode_action(mode),
                checked=self._mode_checked(mode),
                radio=True)
            for mode in MODE_ORDER])
        lock_menu = pystray.Menu(
            pystray.MenuItem(
                "以太网优先", lambda icon, item: self.apply_lock("ethernet"),
                checked=lambda item: self._current_lock() == "ethernet",
                radio=True),
            pystray.MenuItem(
                "Wi-Fi 优先", lambda icon, item: self.apply_lock("wifi"),
                checked=lambda item: self._current_lock() == "wifi",
                radio=True),
            pystray.MenuItem(
                "不锁定", lambda icon, item: self.apply_lock(None),
                checked=lambda item: self._current_lock() is None,
                radio=True),
        )
        return pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.MenuItem(self._failover_text, None, enabled=False),
            pystray.MenuItem(self._remote_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("运行模式", mode_menu),
            pystray.MenuItem("手动锁定目标", lock_menu),
            pystray.MenuItem("网络切换期间保持 Clash 原样", self.on_toggle_clash_keep,
                             checked=lambda item: bool(self.config.get("clash_keep", True))),
            pystray.MenuItem("远控探测（远程保障）", self.on_toggle_remote_guard,
                             checked=lambda item: bool(self.config.get("remote_guard", True))),
            pystray.MenuItem("远程会话中推迟切回", self.on_toggle_defer_failback,
                             checked=lambda item: bool(
                                 self.config.get("remote_defer_failback", True))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即重连", self.on_reconnect),
            pystray.MenuItem(self._pause_text, self.on_toggle_pause),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", self.on_toggle_autostart,
                             checked=lambda item: get_autostart()),
            pystray.MenuItem("重连后自动开启系统代理", self.on_toggle_proxy,
                             checked=lambda item: self.config.get("clash_auto_proxy") is True),
            pystray.MenuItem("失败时 Edge 兜底", self.on_toggle_fallback,
                             checked=lambda item: bool(self.config.get("browser_fallback"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置面板", self.on_settings),
            pystray.MenuItem("更新账号/密码", self.on_update_credentials),
            pystray.MenuItem("清除凭据", self.on_clear_credentials),
            pystray.MenuItem("重启监测", self.restart_monitor),
            pystray.MenuItem("打开日志", self.on_open_log),
            pystray.MenuItem("退出", self.on_quit),
        )

    # ---------- 凭据面板（F11）----------
    def _open_credentials_dialog(self):
        loaded = load_credentials()
        if loaded.error_code == E_CRED_BACKEND:
            messagebox.showerror("凭据库不可用", loaded.detail)
            return
        initial_user = loaded.username if loaded.ok else ""
        creds = prompt_credentials_dialog(self.root, initial_user=initial_user)
        if not creds:
            return
        result = save_credentials(*creds)
        if result.ok:
            self.notify("凭据已更新", "新的账号密码已保存到 Windows 凭据管理器")
        else:
            messagebox.showerror("凭据保存失败", result.detail)

    def _clear_credentials_dialog(self):
        if not messagebox.askyesno("清除凭据", "确定要删除本程序保存的校园网账号密码吗？"):
            return
        result = clear_credentials()
        if result.ok:
            self.notify("凭据已清除", "如需继续使用，请重新设置账号密码")
        else:
            messagebox.showerror("清除失败", result.detail)

    # ---------- 设置面板 ----------
    def _open_settings(self):
        if self.settings_win is not None:
            try:
                if self.settings_win.winfo_exists():
                    self.settings_win.lift()
                    self.settings_win.focus_force()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("校园网自动重连助手 · 设置面板")
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=12)
        frame.grid(sticky="nsew")

        status = self.worker_status()
        ttk.Label(frame, text="运行状态", font=("Microsoft YaHei", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text=self._status_text()).grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="监测：%s" % status["label"]).grid(
            row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="上次重连成功：%s" % (self.state.get("last_ok") or "—")).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(0, 10))

        v_auto = tk.BooleanVar(value=get_autostart())
        v_fallback = tk.BooleanVar(value=bool(self.config.get("browser_fallback")))
        consent_value = self.config.get("clash_auto_proxy")
        if consent_value not in CONSENT_VALUE_TO_LABEL:
            consent_value = None
        v_consent = tk.StringVar(value=CONSENT_VALUE_TO_LABEL[consent_value])
        v_endpoint = tk.StringVar(value=self.config.get("proxy_endpoint") or "")
        # V0.3 P6：三模式 / 手动锁定 / Clash 保持原样（方案 §11 / §9）。
        v_fmode = tk.StringVar(value=tray_mode_label(self._current_mode()))
        lock_label = PATH_LABEL.get(self._current_lock(), "不锁定")
        v_flock = tk.StringVar(value=lock_label)
        v_clash_keep = tk.BooleanVar(value=bool(self.config.get("clash_keep", True)))
        # V0.3 P7：远程保障（远控探测 / 敏感期推迟切回）。
        v_remote = tk.BooleanVar(value=bool(self.config.get("remote_guard", True)))
        v_defer = tk.BooleanVar(
            value=bool(self.config.get("remote_defer_failback", True)))

        ttk.Checkbutton(frame, text="开机自启（登录 Windows 后自动启动）",
                        variable=v_auto).grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="重连成功后系统代理").grid(row=5, column=0, sticky="e", padx=4, pady=3)
        ttk.Combobox(frame, textvariable=v_consent, width=16, state="readonly",
                     values=CONSENT_LABELS).grid(row=5, column=1, columnspan=2, sticky="w")
        ttk.Label(frame, text="代理端点 host:port").grid(row=6, column=0, sticky="e",
                                                     padx=4, pady=3)
        ttk.Entry(frame, textvariable=v_endpoint, width=18).grid(
            row=6, column=1, columnspan=2, sticky="w")
        ttk.Checkbutton(frame, text="重连失败时自动打开 Edge 兜底",
                        variable=v_fallback).grid(row=7, column=0, columnspan=3, sticky="w")

        pad = {"padx": 4, "pady": 3}
        ttk.Label(frame, text="Wi-Fi 名称").grid(row=8, column=0, sticky="e", **pad)
        v_ssid = tk.StringVar(value=self.config.get("ssid") or "SNNU")
        ttk.Entry(frame, textvariable=v_ssid, width=18).grid(row=8, column=1, sticky="w", **pad)
        ttk.Label(frame, text="运营商").grid(row=9, column=0, sticky="e", **pad)
        yys_value = self.config.get("yys", "mobile")
        if yys_value not in YYS_VALUE_TO_LABEL:
            yys_value = "mobile"
        v_yys = tk.StringVar(value=YYS_VALUE_TO_LABEL[yys_value])
        ttk.Combobox(frame, textvariable=v_yys, width=15, state="readonly",
                     values=YYS_LABELS).grid(
                         row=9, column=1, sticky="w", **pad)
        ttk.Label(frame, text="检测间隔（秒）").grid(row=10, column=0, sticky="e", **pad)
        v_interval = tk.IntVar(value=self._int_or(self.config.get("interval"), 15))
        ttk.Spinbox(frame, from_=5, to=120, textvariable=v_interval, width=16).grid(
            row=10, column=1, sticky="w", **pad)
        ttk.Label(frame, text="连续失败阈值").grid(row=11, column=0, sticky="e", **pad)
        v_thr = tk.IntVar(value=self._int_or(self.config.get("threshold"), 3))
        ttk.Spinbox(frame, from_=1, to=10, textvariable=v_thr, width=16).grid(
            row=11, column=1, sticky="w", **pad)
        ttk.Label(frame, text="重连冷却（秒）").grid(row=12, column=0, sticky="e", **pad)
        v_cd = tk.IntVar(value=self._int_or(self.config.get("cooldown"), 180))
        ttk.Spinbox(frame, from_=60, to=3600, textvariable=v_cd, width=16).grid(
            row=12, column=1, sticky="w", **pad)

        # 面板打开时的原始值：保存时**只提交用户实际变更的项**（F12/T24）。
        initial = {
            "ssid": self.config.get("ssid") or "SNNU",
            "yys": YYS_VALUE_TO_LABEL[yys_value],
            "interval": self._int_or(self.config.get("interval"), 15),
            "threshold": self._int_or(self.config.get("threshold"), 3),
            "cooldown": self._int_or(self.config.get("cooldown"), 180),
            "browser_fallback": bool(self.config.get("browser_fallback")),
            "consent": CONSENT_VALUE_TO_LABEL[consent_value],
            "endpoint": self.config.get("proxy_endpoint") or "",
            "autostart": bool(get_autostart()),
            "failover_mode": tray_mode_label(self._current_mode()),
            "failover_lock": lock_label,
            "clash_keep": bool(self.config.get("clash_keep", True)),
            "remote_guard": bool(self.config.get("remote_guard", True)),
            "remote_defer_failback": bool(
                self.config.get("remote_defer_failback", True)),
        }

        # V0.3 P6：三模式 / 手动锁定 / Clash 保持原样 + 故障切换状态（只读展示）。
        ttk.Separator(frame, orient="horizontal").grid(
            row=13, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        ttk.Label(frame, text="双网络故障切换（V0.3）",
                  font=("Microsoft YaHei", 10, "bold")).grid(
            row=14, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="运行模式").grid(row=15, column=0, sticky="e", **pad)
        ttk.Combobox(frame, textvariable=v_fmode, width=15, state="readonly",
                     values=[tray_mode_label(m) for m in MODE_ORDER]).grid(
            row=15, column=1, sticky="w", **pad)
        ttk.Label(frame, text="锁定目标").grid(row=16, column=0, sticky="e", **pad)
        ttk.Combobox(frame, textvariable=v_flock, width=15, state="readonly",
                     values=["不锁定", PATH_LABEL["ethernet"],
                             PATH_LABEL["wifi"]]).grid(
            row=16, column=1, sticky="w", **pad)
        ttk.Checkbutton(frame, text="网络切换期间保持 Clash 原样（默认）",
                        variable=v_clash_keep).grid(
            row=17, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(frame, text="远控探测（远程保障，只读）",
                        variable=v_remote).grid(
            row=18, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(frame, text="远程会话进行中推迟自动切回",
                        variable=v_defer).grid(
            row=19, column=0, columnspan=3, sticky="w")
        failover_status = getattr(self, "failover_status", None)
        failover_text = ("\n".join(failover_status.lines()) if failover_status
                         is not None else "故障切换：等待首次观测（模式 %s）"
                         % tray_mode_label(self._current_mode()))
        ttk.Label(frame, text=failover_text, justify="left").grid(
            row=20, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def on_save():
            try:
                interval = max(5, int(v_interval.get()))
                threshold = max(1, int(v_thr.get()))
                cooldown = max(60, int(v_cd.get()))
            except (tk.TclError, ValueError):
                messagebox.showwarning("输入无效", "间隔/阈值/冷却必须是数字")
                return
            changes = {}
            ssid = v_ssid.get().strip() or "SNNU"
            if ssid != initial["ssid"]:
                changes["ssid"] = ssid
            yys_label = v_yys.get()
            if yys_label != initial["yys"]:
                changes["yys"] = YYS_LABEL_TO_VALUE.get(yys_label, "mobile")
            if interval != initial["interval"]:
                changes["interval"] = interval
            if threshold != initial["threshold"]:
                changes["threshold"] = threshold
            if cooldown != initial["cooldown"]:
                changes["cooldown"] = cooldown
            if bool(v_fallback.get()) != initial["browser_fallback"]:
                changes["browser_fallback"] = bool(v_fallback.get())
            if v_consent.get() != initial["consent"]:
                changes["clash_auto_proxy"] = CONSENT_LABEL_TO_VALUE.get(v_consent.get())
            endpoint = v_endpoint.get().strip()
            if endpoint != initial["endpoint"]:
                changes["proxy_endpoint"] = endpoint or None
            # V0.3 P6：三模式 / 锁定 / Clash（切到智能自动需显式授权）。
            fmode_label = v_fmode.get()
            if fmode_label != initial["failover_mode"]:
                mode = next((m for m in MODE_ORDER if tray_mode_label(m) == fmode_label),
                            FailoverMode.MONITOR)
                if mode is FailoverMode.AUTO \
                        and not self.config.get("auto_switch_authorized"):
                    if not messagebox.askyesno(
                            "智能自动授权",
                            "「智能自动」将允许自动切换网络出口"
                            "（真实修改路由 / Metric）。\n是否授权启用？"):
                        messagebox.showinfo("未授权", "已保持「仅监测」，未启用智能自动。")
                        return
                    changes["auto_switch_authorized"] = True
                changes["failover_mode"] = mode.value
            flock_label = v_flock.get()
            if flock_label != initial["failover_lock"]:
                if flock_label == "不锁定":
                    changes["failover_lock"] = None
                elif flock_label == PATH_LABEL["ethernet"]:
                    changes["failover_lock"] = "ethernet"
                else:
                    changes["failover_lock"] = "wifi"
            if bool(v_clash_keep.get()) != initial["clash_keep"]:
                changes["clash_keep"] = bool(v_clash_keep.get())
            # V0.3 P7：远程保障两键（远控探测 / 敏感期推迟切回）。
            if bool(v_remote.get()) != initial["remote_guard"]:
                changes["remote_guard"] = bool(v_remote.get())
            if bool(v_defer.get()) != initial["remote_defer_failback"]:
                changes["remote_defer_failback"] = bool(v_defer.get())

            if changes:
                new_cfg = self.get_config()
                new_cfg.update(changes)
                if not save_config(new_cfg):
                    messagebox.showerror("保存失败",
                                         "配置写入失败，已保留原文件。请检查目录权限后重试。")
                    return
                self.update_config(changes)
                self._sync_failover_from_config()
                if "clash_auto_proxy" in changes:
                    with self._config_lock:
                        self._consent_version += 1
            want_autostart = bool(v_auto.get())
            if want_autostart != initial["autostart"]:
                foreign = autostart_foreign_command() if want_autostart else None
                if foreign and not messagebox.askyesno(
                        "开机自启",
                        "检测到已存在的自启项指向其他位置：\n%s\n\n是否改为当前安装路径？" % foreign):
                    messagebox.showinfo("开机自启", "已保留原有自启项")
                elif not set_autostart(want_autostart):
                    messagebox.showerror("设置失败", "无法写入开机自启注册表项")
                    return
            self.notify("设置已保存", "新参数即时生效")
            win.destroy()

        def on_reconnect_click():
            win.destroy()
            self.on_reconnect(None)

        def on_pause_click():
            self.on_toggle_pause(None)
            win.destroy()
            self._open_settings()

        ttk.Button(frame, text="保存", command=on_save).grid(row=19, column=0, pady=(12, 0))
        ttk.Button(frame, text="立即重连", command=on_reconnect_click).grid(
            row=19, column=1, sticky="w", padx=4)
        ttk.Button(frame, text="暂停/恢复", command=on_pause_click).grid(
            row=19, column=2, sticky="w")
        win.protocol("WM_DELETE_WINDOW", win.destroy)

    @staticmethod
    def _int_or(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    # ---------- 启动 ----------
    def start(self):
        self.icon = pystray.Icon(APP_NAME, self.make_image(), self.tooltip(),
                                 self.build_menu())
        threading.Thread(target=self.icon.run, daemon=True).start()
        self._ensure_worker()
        self._startup_timer = threading.Timer(2.0, self._startup_notify)
        self._startup_timer.daemon = True
        self._startup_timer.start()

    def _cancel_startup_timer(self):
        timer = self._startup_timer
        self._startup_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _startup_notify(self):
        if self.stop_event.is_set():
            return
        try:
            ssid, _ = wifi_state()
        except Exception:
            ssid = None
        online = False
        if self.session is not None:
            try:
                online, _ = internet_ok(self.session)
            except Exception:
                online = False
        loaded = load_credentials()
        body = "Wi-Fi：%s · %s" % (ssid or "未连接", "网络正常" if online else "网络不可用")
        if loaded.error_code == E_CRED_MISSING:
            body += "\n尚未保存凭据：请运行 py campus_reconnect.py configure"
        elif loaded.error_code == E_CRED_BACKEND:
            body += "\n凭据库不可访问：%s" % loaded.detail
        self.notify("校园网助手已启动", body)


def prompt_credentials_dialog(root, initial_user=""):
    """图形界面输入账号密码（exe 版自包含所需）。"""
    result = {"value": None}
    win = tk.Toplevel(root)
    win.title("校园网账号设置")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    f = ttk.Frame(win, padding=14)
    f.grid(sticky="nsew")
    ttk.Label(f, text="请输入校园网账号与密码",
              font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=2)
    ttk.Label(f, text="仅保存在本机 Windows 凭据管理器中").grid(
        row=1, column=0, columnspan=2, pady=(0, 10))
    ttk.Label(f, text="账号").grid(row=2, column=0, sticky="e", padx=4)
    v_user = tk.StringVar(value=initial_user)
    ttk.Entry(f, textvariable=v_user, width=22).grid(row=2, column=1, sticky="w", padx=4)
    ttk.Label(f, text="密码").grid(row=3, column=0, sticky="e", padx=4, pady=6)
    v_pwd = tk.StringVar()
    ttk.Entry(f, textvariable=v_pwd, width=22, show="*").grid(
        row=3, column=1, sticky="w", padx=4, pady=6)

    def on_ok():
        u, p = v_user.get().strip(), v_pwd.get()
        if u and p:
            result["value"] = (u, p)
            win.destroy()
        else:
            messagebox.showwarning("信息不完整", "账号和密码都不能为空")

    ttk.Button(f, text="保存", command=on_ok).grid(row=4, column=0, columnspan=2, pady=(10, 0))
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.grab_set()
    root.wait_window(win)
    return result["value"]


def main():
    log = setup_logging(False)
    if not ensure_single_instance(log):
        root0 = tk.Tk()
        root0.withdraw()
        messagebox.showerror(APP_NAME,
                             "已有一个监控实例在运行。\n如需重启，请先在托盘右键退出旧实例。")
        root0.destroy()
        return
    root = tk.Tk()
    root.withdraw()
    loaded = load_credentials()
    if loaded.error_code == E_CRED_BACKEND:
        log.error("凭据后端不可用：%s", loaded.detail)
        messagebox.showerror(APP_NAME, "凭据库不可访问：%s\n可在设置面板稍后重试。" % loaded.detail)
    elif loaded.error_code == E_CRED_MISSING:
        log.warning("尚未保存凭据，弹出账号设置窗口")
        creds = prompt_credentials_dialog(root)
        if creds:
            saved = save_credentials(*creds)
            if saved.ok:
                log.info("凭据已通过界面保存")
            else:
                log.error("凭据保存失败：%s", saved.detail)
                messagebox.showerror("凭据保存失败", saved.detail)
        else:
            log.warning("未设置账号，自动重连暂不可用（可在凭据管理器中补存后重启）")
    app = TrayApp(log)
    app.root = root
    log.info("托盘助手启动（autostart=%s, clash_auto_proxy=%s, remote_guard=%s）",
             get_autostart(), app.config.get("clash_auto_proxy"),
             app.config.get("remote_guard", True))
    # V0.3 P7：远程保障接线（只读探测）+ 独立看门狗线程。
    if app.config.get("remote_guard", True):
        try:
            app.enable_remote_guard()
        except Exception:
            log.warning("远程保障接线失败：\n%s", traceback.format_exc())
    app.start_supervisor()
    # V0.3 P7：启动时的崩溃恢复与就绪自检放到后台线程（含只读观察，可能耗时数十秒）。
    threading.Thread(target=app.startup_route_recovery,
                     name="campus-recovery", daemon=True).start()
    threading.Thread(target=app.startup_readiness_check,
                     name="campus-readiness", daemon=True).start()
    app._start_ui_pump()
    app.start()
    root.mainloop()
    log.info("托盘助手已退出")


if __name__ == "__main__":
    main()
