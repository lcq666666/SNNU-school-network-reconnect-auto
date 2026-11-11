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

import argparse
import json
import logging
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import List, Optional

import keyring
import pystray
try:
    import winreg
except ImportError:  # Linux 离线测试：winreg 只在 Windows 存在
    winreg = None  # type: ignore[assignment]
from PIL import Image, ImageDraw

from campus_core import (E_CRED_BACKEND, E_CRED_MISSING, SCHEMA_VERSION,
                         CredentialClear, CredentialLoad, CredentialSave,
                         ProxyApplyResult, RepairAdapters, RepairOptions,
                         TimerTable, __version__, repair_service,
                         load_config_text)
import campus_reconnect as engine
from campus_reconnect import (direct_session, edge_fallback,
                              get_credentials,
                              internet_ok, portal_login, portal_reachable,
                              resolve_port, setup_logging, verify_internet,
                              wifi_reconnect, wifi_state)
from campus_windows import CredentialStore, ProxyController, default_refresh
from campus_tray_failover import (MODE_ORDER, TrayFailoverBridge, light_snapshot,
                                  parse_lock, parse_mode, tray_mode_label)
from campus_tray_observer import (ObservationResult, TraySnapshotSource,
                                  is_auto_executable_snapshot,
                                  is_full_snapshot)
from campus_failover import FailoverMode, FailoverPolicy, PATH_LABEL
from campus_network import HealthState
from campus_remote import (MonitorWatchdog, RemoteSafetyGuard,
                           apply_watchdog_decision, build_windows_remote_ops)
from campus_timeline import NetworkTimeline
from campus_clash_health import (assess_dual_axis, check_endpoint,
                                 clamp_cooldown as _clamp_cooldown,
                                 clamp_interval as _clamp_interval,
                                 clamp_threshold as _clamp_threshold,
                                 heal_plan, probe_via_proxy)
from campus_uu_recovery import (confirm_back as uu_confirm_back,
                                decide as uu_decide,
                                diagnose_snapshot as uu_diagnose,
                                should_escalate as uu_should_escalate,
                                tier2_processes as uu_tier2_processes)
from campus_wifi_bringup import (build_snnu_open_profile_xml,
                                 run_bringup as wifi_run_bringup)
from campus_update import (UPDATE_CHECK_INTERVAL_S,
                           UPDATE_FIRST_CHECK_DELAY_S, check_latest,
                           is_newer, should_notify)

# ---- V0.6：CustomTkinter 界面（缺失时回退原生 ttk，仍能启动）----
try:
    import customtkinter as ctk
except Exception:  # noqa: BLE001 - 无 CustomTkinter 即回退 ttk
    ctk = None  # type: ignore[assignment]


# ---- V0.6.1：导入时抓一份真实 sleep ----
# 托盘里 V0.6 新增的几处等待（Clash 自愈 / Wi-Fi 等 IP / UU Tier2 确认）
# 必须用真实时间，不能走可被计时器测试替换的模块级 time。
# V0.6 之前托盘里没有任何等待调用，只有 campus_remote_app._SLEEP
# 是导入时抓取的；V0.6 加的 4 处若走模块级 time，计时器测试装上
# 虚拟时钟后，残留的后台线程会把虚拟时间多推进几秒，i2_01 的探测
# 时刻从 [0, 15, 30, 45] 漂到 [0, 15, 30, 50]。
_REAL_SLEEP = time.sleep


def ui_backend() -> str:
    """当前可用的仪表盘后端：``customtkinter`` 或 ``ttk``（回退）。"""
    return "customtkinter" if ctk is not None else "ttk"


# ---- V0.6.1 A+D+E 融合仪表盘：仪表数值映射（纯函数，不碰 Tk）----
#
# 诚实规则：仪表环上的百分比**不是实测速率**，只是把 UiState 已有的状态结论
# 映射成弧长（已验证≈95%、降级≈50%、失败≈15%、无数据不画弧、不猜）。
# 环中央显示状态词（"已验证"/"未连接"/"未验证"）为主，百分比只作辅助，
# 仪表下方另有一行小字注明"状态映射，非实测速率"。
def gauge_from_card(card) -> "tuple[float, str] | None":
    """网卡卡片 →（弧长比例，中央状态词）；无数据返回 ``None``（不猜）。"""
    if card is None:
        return None
    if card.verified is True:
        return (0.95, "已验证")
    if card.verified is None:
        return None
    # verified is False：按色调分档，中央写卡片自己的状态词
    if card.tone == "bad":
        return (0.15, card.state_label or "未通过")
    if card.tone == "warn":
        return (0.5, card.state_label or "降级")
    return None


def gauge_from_uu(uu) -> "tuple[float, str] | None":
    """UU 视图 →（弧长比例，中央状态词）；未开启返回 ``None``（不猜）。"""
    if uu is None or not uu.enabled:
        return None
    if uu.last_ok is True:
        return (0.95, "就绪")
    if uu.last_ok is False:
        return (0.2, "异常")
    return (0.5, "待确认")


if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent   # PyInstaller 单文件: 用 exe 所在目录
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "CampusReconnectTray"
BACKGROUND_FLAG = "--background"
PYTHONW = str(Path(sys.executable).with_name("pythonw.exe"))
CLASH_SERVER_DEFAULT = "127.0.0.1:7897"   # 仅在用户显式确认时作为**建议值**展示
APP_NAME = "校园网自动重连助手"
UI_POLL_MS = 120
#: V0.5-5：主窗口仪表盘刷新周期（毫秒）。UI 只消费状态映射，不发起任何网络 I/O。
DASHBOARD_REFRESH_MS = 1000
#: V0.5-5：主窗口尺寸（方案 §5.5：820–920 × 560–650）。
DASHBOARD_WIDTH = 880
DASHBOARD_HEIGHT = 620
DASHBOARD_MIN_WIDTH = 820
DASHBOARD_MIN_HEIGHT = 560

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
    "interval": 5,
    "threshold": 2,
    "cooldown": 60,
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
    # ---- V0.4-3：确认在线后清退重启 UU 远程（方案 §3.3）----
    "remote_restart_enabled": False,  # **默认关闭**：不主动调用即零副作用
    "remote_restart_cooldown": 300,   # 两次真实清退最小间隔秒数
    # ---- V0.3 P7.5-1A：三轴观测参数（可配置，范围受限）----
    "snapshot_max_age": 8,            # 快照有效期（秒）；依据见 campus_tray_observer
    "snapshot_timeout": 12,           # 单次采集超时（秒）
    "snapshot_interval": 3,           # 两次采集最小间隔（秒）
    # ---- V0.6：自动切换确认 / Clash 自愈 / UU 升级 ----
    "v06_auto_prompted": False,    # 是否已弹过一次智能自动确认
    "clash_heal_enabled": True,    # Clash 假死自愈（开关代理 / 重启 Verge）
    "uu_escalation_enabled": True,  # UU Tier1 满 60s 未恢复则升 Tier2
    # ---- V0.6.2：自动更新提醒（只提醒，不下载、不安装）----
    "update_check_enabled": True,  # 启动 60 秒后首次检查，之后每 6 小时
    "update_notified_tag": None,   # 气泡已弹过的版本 tag（同一版本只弹一次）
    "update_dismissed_tag": None,  # 用户点过“以后再说 / 下载更新”的版本 tag
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


def _read_snapshot_keys(text: str) -> dict:
    """读取 P7.5-1A 三轴观测参数（非法值一律回退安全默认）。

    与 P6/P7 读取器分开，避免破坏既有整字典断言；
    各参数限幅防止任意放宽安全门槛：
    max_age 1..30s、timeout 3..60s、interval 1..30s。
    """
    raw = _raw_config(text)
    max_age = raw.get("snapshot_max_age")
    if not isinstance(max_age, int) or isinstance(max_age, bool):
        max_age = DEFAULT_CONFIG["snapshot_max_age"]
    timeout = raw.get("snapshot_timeout")
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        timeout = DEFAULT_CONFIG["snapshot_timeout"]
    interval = raw.get("snapshot_interval")
    if not isinstance(interval, int) or isinstance(interval, bool):
        interval = DEFAULT_CONFIG["snapshot_interval"]
    return {
        "snapshot_max_age": max(1, min(30, int(max_age))),
        "snapshot_timeout": max(3, min(60, int(timeout))),
        "snapshot_interval": max(1, min(30, int(interval))),
    }


def _read_remote_app_keys(text: str) -> dict:
    """读取 V0.4-3「确认在线后清退重启 UU 远程」两键（非法值一律回退安全默认）。

    与 P6/P7 的读取器分开实现，保持既有读取器返回值稳定（既有测试按整字典断言）：
    - ``remote_restart_enabled``：非 bool ⇒ **False**（默认关闭，fail-closed）；
    - ``remote_restart_cooldown``：限幅 60..3600s（防止把冷却压没或设成一天）。
    """
    raw = _raw_config(text)
    enabled = raw.get("remote_restart_enabled")
    if not isinstance(enabled, bool):
        enabled = DEFAULT_CONFIG["remote_restart_enabled"]
    cooldown = raw.get("remote_restart_cooldown")
    if not isinstance(cooldown, int) or isinstance(cooldown, bool):
        cooldown = DEFAULT_CONFIG["remote_restart_cooldown"]
    return {"remote_restart_enabled": enabled,
            "remote_restart_cooldown": max(60, min(3600, int(cooldown)))}


def _read_v06_keys(text: str) -> dict:
    """读取 V0.6 三键（非法值一律回退安全默认）。"""
    raw = _raw_config(text)
    prompted = raw.get("v06_auto_prompted")
    if not isinstance(prompted, bool):
        prompted = DEFAULT_CONFIG["v06_auto_prompted"]
    heal = raw.get("clash_heal_enabled")
    if not isinstance(heal, bool):
        heal = DEFAULT_CONFIG["clash_heal_enabled"]
    esc = raw.get("uu_escalation_enabled")
    if not isinstance(esc, bool):
        esc = DEFAULT_CONFIG["uu_escalation_enabled"]
    return {"v06_auto_prompted": prompted, "clash_heal_enabled": heal,
            "uu_escalation_enabled": esc}


def _read_update_keys(text: str) -> dict:
    """读取 V0.6.2 更新提醒三键（非法值一律回退安全默认）。

    开关非法 → 默认开启检查；tag 非字符串 / 空串 → None（未提醒过）。
    """
    raw = _raw_config(text)
    enabled = raw.get("update_check_enabled")
    if not isinstance(enabled, bool):
        enabled = DEFAULT_CONFIG["update_check_enabled"]

    def _tag(value):
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    return {"update_check_enabled": enabled,
            "update_notified_tag": _tag(raw.get("update_notified_tag")),
            "update_dismissed_tag": _tag(raw.get("update_dismissed_tag"))}


def should_prompt_auto_switch(cfg: dict) -> bool:
    """V0.6 首次启动是否弹一次智能自动确认（纯函数，可单测）。

    只在「从未问过 + 当前仍是仅监测 + 未授权」时弹一次；已问过、已是
    AUTO、已授权、手动锁定，一律不再弹。
    """
    if not isinstance(cfg, dict):
        return False
    return (not bool(cfg.get("v06_auto_prompted"))
            and cfg.get("failover_mode", "MONITOR") == "MONITOR"
            and not bool(cfg.get("auto_switch_authorized")))


def apply_auto_switch_consent(cfg: dict, agreed: bool) -> dict:
    """把一次智能自动确认的结果写进配置副本（纯函数，不写盘）。

    同意 ⇒ ``failover_mode=AUTO`` + ``auto_switch_authorized=True``；
    拒绝 ⇒ 保持 MONITOR，但记 ``v06_auto_prompted=True``，下次不再弹。
    """
    out = dict(cfg or {})
    out["v06_auto_prompted"] = True
    if agreed:
        out["failover_mode"] = "AUTO"
        out["auto_switch_authorized"] = True
    return out


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
    cfg.update(_read_snapshot_keys(text))
    cfg.update(_read_remote_app_keys(text))
    cfg.update(_read_v06_keys(text))
    cfg.update(_read_update_keys(text))
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
    """当前安装的启动命令（F21）；注册自启时另加后台标志。

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
    return _autostart_is_current(get_autostart_command())


def _autostart_is_current(command) -> bool:
    current = _normalize_command(launcher_command())
    return _normalize_command(command) in (current, f"{current} {BACKGROUND_FLAG}")


def autostart_foreign_command():
    """存在自启项但指向非当前安装时返回其命令，否则 ``None``。"""
    command = get_autostart_command()
    if command is None:
        return None
    if _autostart_is_current(command):
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
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  f"{launcher_command()} {BACKGROUND_FLAG}")
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
    def __init__(self, log, *, config=None):
        self.log = log
        self.config = load_config() if config is None else dict(config)
        self.session = None
        self.state = {"mode": "boot", "detail": "", "port": None,
                      "last_ok": None, "failures": 0}
        self.stop_event = threading.Event()
        self.icon = None
        self.root = None
        self.settings_win = None
        #: V0.5-5：主窗口仪表盘（惰性创建；关闭后置回 ``None``）。
        self.dashboard_win = None
        #: V0.5-5：最近一次**手动**验证结果（``role`` → ``PathVerificationResult``）。
        #: 只用于展示逐站明细，不参与任何判定（判定一律走整合层）。
        self.manual_verification = {}
        self._dash_widgets = None
        self._dash_refresh_id = None

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
        #: V0.4-1：**本「在线段」**是否已做过连接后真实上网校验（进入在线段校验一次，
        #: 判为离线时重置 ⇒ 掉线后再连上/重连成功会重新校验）。
        self._online_verified = False
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
        self._last_system_probe: Optional[tuple[bool, str]] = None
        #: 最近一次三轴观测结论（含可信度与证据；供 UI 与告警展示）。
        self.last_observation = ObservationResult()
        #: 最近一次故障切换展示状态（由 :meth:`_failover_tick` 更新）。
        self.failover_status = None

        # ---- V0.5-4 双链路恢复整合（接口受限验证 + 恢复协调器 + UU 恢复）----
        #: 整合控制器（惰性接线；**构造零 I/O**，真正的接口受限探测在后台线程）。
        self.dual_path = None
        #: 最近一次整合判定（供日志 / 后续 UI；由 :meth:`_failover_tick` 更新）。
        self.dual_path_tick = None
        #: 接口受限验证能力（惰性构造；注入点便于离线测试替换）。
        self._path_verifier = None

        # ---- V0.5-6 网络事件时间线（纯逻辑；只记录，不判定）----
        #: 「**什么时候断的网 / 什么时候恢复 / 花了多久**」的可复盘台账（方案 §5.6）。
        #: 是否离线仍由真实探测 + 状态机滞回给出（``observe(offline=...)``）；本对象
        #: 只做记录、去重、计时与复盘，不触发任何网络动作。时钟与托盘其余接线同源。
        self.timeline = NetworkTimeline(
            clock=lambda: time.monotonic(), emit=self._emit_timeline_event,
            log=lambda message: self.log.warning("[时间线] %s", message))

        # ---- V0.3 P7 远程保障（默认未接线；由 :meth:`enable_remote_guard` 接入）----
        #: 远控只读探测能力（``None`` = 未接线：不产生任何子进程/文件 I/O）。
        self._remote_ops = None
        self._remote_probe_busy = threading.Event()
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

        # ---- V0.6.2 自动更新提醒（只记状态，不发网络）----
        #: 最近一次查到的最新 Release（``campus_update.UpdateInfo`` 或 None）。
        #: 由后台线程写入；仪表盘渲染（1 秒周期）读取后决定横幅是否显示，
        #: 检查线程本身不直接碰 Tk 控件。
        self._update_info = None
        #: 检查线程在途标记（避免一次检查跑两个线程；测试可等待它清零）。
        self._update_busy = threading.Event()
        #: 最近一次检查线程（测试用 ``join`` 等它跑完；生产不依赖它）。
        self._update_thread = None
        #: 下一次定时检查时刻（monotonic）；启动后 60 秒首次检查。
        self._update_next_check_at = (time.monotonic()
                                      + UPDATE_FIRST_CHECK_DELAY_S)
        #: 注入点：测试可替换为返回假 session 的工厂；默认用专用直连会话。
        self._update_session_factory = None
        #: 注入点：测试可替换为记录调用的 opener；默认打开系统浏览器。
        self._update_opener = webbrowser.open

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
        controller = self._build_dual_path_controller()
        return TrayFailoverBridge(
            mode=cfg.get("failover_mode", "MONITOR"),
            lock_target=cfg.get("failover_lock"),
            clash_keep=bool(cfg.get("clash_keep", True)),
            authorized=bool(cfg.get("auto_switch_authorized", False)),
            executor_factory=self._build_auto_executor,
            policy=policy, timer_table=self._timers,
            remote_guard=getattr(self, "remote", None),
            defer_failback=bool(cfg.get("remote_defer_failback", True)),
            action_gate=(controller.gate if controller is not None else None),
            log=lambda message: self.log.info("[failover] %s", message))

    # ---------- V0.5-4 双链路恢复整合（构造，零 I/O）----------
    def _build_dual_path_controller(self):
        """构造 V0.5-4 双链路恢复整合层（**构造零 I/O**）。

        只把「接口受限验证（V0.5-1）+ 恢复协调器（V0.5-2）+ UU 恢复（V0.5-3）」
        接成一条流水线；**真实探测不在这里发生**，而在后台短命线程
        （:meth:`_verify_paths_async` / :meth:`_uu_recovery_async`）。

        构造失败时返回 ``None``：此时桥接不接入验证前置门（行为与 P6 一致），
        如实记日志，绝不让托盘崩溃。
        """
        if self.dual_path is not None:
            return self.dual_path
        try:
            from campus_dual_path import DualPathRecoveryController

            # 验证能力**惰性解析**：``_build_path_verifier()`` 每次调用取当前能力，
            # 于是运行期替换（含离线测试注入假件）立即生效，且构造仍是零 I/O。
            controller = DualPathRecoveryController(
                verifier=lambda request: self._build_path_verifier().verify(request),
                gate_verifier=(
                    lambda request: self._build_path_verifier().verify_gate(request)),
                uu_restart_enabled=lambda: bool(
                    self.get_config().get("remote_restart_enabled", False)),
                refresh_active_verification=True,
                # 与托盘其余接线同源：整合层与验证前置门共用**本进程**时钟
                # （测试用 VirtualClock 替换 ``tray.time`` 时保持一致）。
                clock=lambda: time.monotonic(),
                log=lambda message: self.log.info("[恢复] %s", message))
        except Exception as exc:                     # 接线失败不得让托盘崩溃
            self.log.warning("双链路恢复整合层构造失败：%s", type(exc).__name__)
            return None
        self.dual_path = controller
        self.log.info("双链路恢复整合层已接线：接口受限验证 + 恢复协调器 + UU 恢复"
                      "（UU 自动恢复默认关闭；路由切换须先通过接口受限验证）")
        return controller

    def _build_path_verifier(self):
        """接口受限验证能力（``PathVerifier`` + ``WindowsProbeEngine``，零 I/O 构造）。"""
        if self._path_verifier is None:
            from campus_network import WindowsProbeEngine
            from campus_reconnect import PathVerifier

            self._path_verifier = PathVerifier(WindowsProbeEngine())
        return self._path_verifier

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
        runner = netdiag.PowerShellRunner()
        observer = NetworkObserver(runner)
        ssid = cfg.get("ssid") or "SNNU"
        report = netdiag.collect_health(netdiag.PowerShellRunner(timeout=5))
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
        return engine._build_composite_executor(observer, report, wifi, args, self.log, guard)

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
                max_age=float(self.config.get("snapshot_max_age",
                                              SNAPSHOT_MAX_AGE)),
                collect_timeout=float(self.config.get("snapshot_timeout",
                                                       SNAPSHOT_COLLECT_TIMEOUT)),
                min_interval=float(self.config.get("snapshot_interval",
                                                    SNAPSHOT_MIN_INTERVAL)),
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
        """采集三轴健康所需的网卡、地址、DNS 和路由，再进行只读探测。

        全程**只读**（P0 ``netdiag`` 只读管道 + 只读探测），不切路由、不认证。
        """
        from tools import netdiag
        from campus_network import WindowsProbeEngine, evaluate

        system_probe = self._last_system_probe
        cfg = self.get_config()
        report = netdiag.collect_health(netdiag.PowerShellRunner(timeout=5))
        # 网关不开放 80/443 很常见。端口探测只用于详细诊断，不参与健康判定。
        return evaluate(report, WindowsProbeEngine(timeout=2), parallel_probes=True,
                        system_probe=(lambda: system_probe) if system_probe is not None else None,
                        portal_probe=lambda alias, scoped: engine.probe_snnu_portal(
                            scoped, alias, cfg.get("ssid") or "SNNU", cfg.get("port")),
                        config=engine._load_adapters_config(), gateway_ports=())

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
        if observation is None or (provider != self._full_snapshot
                                   and observation.snapshot is not snapshot):
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
        reason = observation.reason_text if observation is not None else ""
        # AUTO 需要识别目标网卡。已确认只有 Wi-Fi 的电脑仅执行无线恢复。
        if trusted and bridge.mode is FailoverMode.AUTO:
            ok, auto_reason = is_auto_executable_snapshot(snapshot)
            if not ok:
                trusted = False
                reason = "AUTO 快照契约不满足：%s" % auto_reason
                self.log.warning(reason)
        # 可信度门禁（P7.5-1）：数据不足时 AUTO 一律拒绝执行网络副作用。
        bridge.set_snapshot_trust(trusted, reason)
        if not trusted:
            self.log.info("三轴快照不可信（%s）：本轮不执行任何网络副作用",
                          reason)
        elif (self.config.get("remote_restart_enabled", False)
              and not getattr(self, "_v06_had_outage", False)
              and getattr(getattr(snapshot, "ethernet", None), "state", None) is not HealthState.HEALTHY
              and getattr(getattr(snapshot, "wifi", None), "state", None) in (
                  HealthState.CAPTIVE_PORTAL, HealthState.INTERNET_UNREACHABLE, HealthState.DNS_FAILURE)):
            # 系统 TCP 探测仍可能显示在线。用绑定 Wi-Fi 的证据登记掉线。
            self._note_outage_for_uu()
        result = bridge.tick(now, snapshot)
        self.failover_status = bridge.status()
        if result is not None and result.executed:
            self.log.info("故障切换已执行动作：%s",
                          ", ".join(o.kind.value for o in result.executed))
        # V0.5-4：把本轮迁移交给恢复整合层（零 I/O 判定；需要验证时起后台线程）。
        self._dual_path_step(now, snapshot, trusted=trusted, result=result)
        # V0.5-6：把本轮观测记入网络事件时间线（只记录，不判定；零 I/O）。
        self._timeline_observe(now, online, trusted=trusted, snapshot=snapshot,
                               result=result, detail=reason)
        return result

    # ---------- V0.5-4：双链路恢复整合（路径验证 + 恢复协调器 + UU 恢复）----------
    def _dual_path_step(self, now, snapshot, *, trusted, result):
        """本轮整合判定（纯逻辑）：把状态迁移翻译成恢复事件与验证安排。

        快照不可信时整合层**什么都不做**（fail-closed）：不安排验证、不产生 UU 重启
        请求；此时桥接的单一副作用门也已换成拒绝执行器（P7.5-1），双保险。
        """
        controller = getattr(self, "dual_path", None)
        if controller is None or result is None:
            return None
        try:
            tick = controller.tick(result, snapshot, trusted=bool(trusted), now=now)
        except Exception as exc:                     # 判定故障不得影响监测主流程
            self.log.warning("恢复整合层判定异常：%s", type(exc).__name__)
            return None
        self.dual_path_tick = tick
        if tick.verify_requests:
            self._verify_paths_async(tick.verify_requests)
        return tick

    def _verify_paths_async(self, requests):
        """在独立短命线程里**依次**做本轮要求的接口受限验证，并交回整合层（V0.5-4）。

        同一时刻只允许一轮在途验证（``busy``）；某个接口的验证失败/异常一律如实记
        日志，**不**影响监测与状态机主流程，也不影响同一轮里另一条链路的验证。
        """
        busy = getattr(self, "_verify_path_busy", None)
        if busy is None:
            busy = self._verify_path_busy = threading.Event()
        if busy.is_set():
            self.log.info("[恢复] 已有接口受限验证在途：本次跳过（下一轮重试）")
            self.dual_path.cancel_verifications(requests)
            return
        busy.set()
        controller = self.dual_path

        def _run():
            try:
                verifier = self._build_path_verifier()
                for request in requests:
                    result = verifier.verify(request)
                    if result.allows_recovery:
                        self.log.info("[恢复] %s 接口受限验证通过：%s",
                                      request.role, result.reason)
                    else:
                        self.log.warning("[恢复] %s 接口受限验证未通过：%s",
                                         request.role, result.reason)
                    follow = controller.on_verification(
                        result, now=time.monotonic(), request_epoch=request.network_generation)
                    self._uu_recovery_after_verification(follow, result)
            except Exception as exc:   # 验证不得影响监测主流程（防御性边界）
                self.log.warning("[恢复] 接口受限验证异常：%s", type(exc).__name__)
                controller.cancel_verifications(requests)
            finally:
                busy.clear()

        threading.Thread(target=_run, daemon=True,
                         name="campus-path-verify").start()

    def _uu_recovery_after_verification(self, follow, verification):
        """恢复流水线的**最后一步**：验证通过 ⇒ 清退重启 UU 远程（默认关闭）。"""
        request = getattr(follow, "uu_request", None)
        if request is None:
            return
        had_outage = bool(getattr(self, "_v06_had_outage", False))
        was_running = bool(getattr(self, "_v06_uu_was_running", False))
        self._v06_had_outage = False
        self._v06_uu_was_running = False
        if not getattr(follow, "schedules_uu_restart", False):
            self.log.info("[恢复] 已满足恢复条件，但 UU 自动恢复未开启"
                          "（remote_restart_enabled=False）：不清退")
            return
        # V0.6：恢复事件触发条件（断网再恢复 + 恢复前 UU 在运行 + 验证通过）。
        try:
            _cooldown_ok, _ = self._get_restart_guard().allow()
        except Exception:
            _cooldown_ok = False
        _decision = uu_decide(
            had_outage=had_outage,
            was_running_before_outage=was_running,
            verified_recovery=bool(getattr(verification, "allows_recovery", False)),
            remote_restart_enabled=True,
            cooldown_ok=bool(_cooldown_ok),
            epoch_already_restarted=self._uu_recovery_ledger().seen(
                request.network_epoch))
        if not _decision.should_restart:
            self.log.info("[UU V0.6] 不重启：%s", _decision.reason)
            return
        self._uu_recovery_async(request, verification)

    def _uu_recovery_async(self, request, verification):
        """后台执行一次「验证通过后的 UU 恢复」（复用 V0.5-3 与既有冷退能力）。"""
        busy = getattr(self, "_uu_recovery_busy", None)
        if busy is None:
            busy = self._uu_recovery_busy = threading.Event()
        if busy.is_set():
            self.log.info("[恢复] 已有 UU 恢复在途：本次跳过")
            return
        busy.set()
        try:
            ops = self._get_remote_app_ops()
            guard = self._get_restart_guard()
            ledger = self._uu_recovery_ledger()
        except Exception as exc:       # 能力构建失败不得影响监测线程
            busy.clear()
            self.log.warning("[恢复] UU 清退能力不可用：%s", type(exc).__name__)
            return
        controller = self.dual_path
        verify_gate = controller.build_recheck(request.active_path)

        def recheck():
            current = controller.status()
            if (not self.config.get("remote_restart_enabled", False)
                    or current.network_epoch != request.network_epoch
                    or current.active_path != request.active_path):
                return False, "UU 恢复开关或活动网络已变化"
            return verify_gate()

        def _run():
            try:
                from campus_remote_app import restart_after_verified_recovery

                self._timeline_uu_started(request)
                try:
                    from campus_remote_app import discover as _discover2

                    _status_before = _discover2(ops)
                except Exception:
                    _status_before = None
                outcome = restart_after_verified_recovery(
                    ops, active_path=request.active_path,
                    network_epoch=request.network_epoch, verification=verification,
                    reason=request.reason, enabled=True, guard=guard,
                    ledger=ledger, recheck=recheck)
                if outcome.restarted:
                    self.log.info("[恢复] UU 远程已清退重启：%s", outcome.detail)
                    self.notify("UU 远程已重启",
                                "网络恢复且接口受限验证通过：已清退重启 UU 远程后台")
                elif outcome.ok:
                    self.log.info("[恢复] UU 远程无需重启：%s", outcome.detail)
                else:
                    self.log.warning("[恢复] UU 远程未重启：%s（%s）",
                                     outcome.detail, outcome.action)
                    self.notify("UU 远程未重启", outcome.detail)
                self._timeline_uu_outcome(outcome)
                controller.note_uu_outcome(bool(outcome.restarted),
                                           detail=outcome.detail)
                if outcome.restarted and not outcome.alive \
                        and _status_before is not None:
                    self._schedule_uu_tier2_check(ops, _status_before)
            except Exception as exc:       # 清退不得影响监测主流程（防御性边界）
                self.log.warning("[恢复] UU 远程恢复异常：%s", type(exc).__name__)
                controller.note_uu_outcome(
                    False, detail="异常：%s" % type(exc).__name__)
                self._timeline_uu_outcome(restarted=False, ok=False,
                                          action="exception",
                                          detail="异常：%s" % type(exc).__name__)
            finally:
                busy.clear()

        threading.Thread(target=_run, daemon=True,
                         name="campus-uu-recovery").start()

    def _schedule_uu_tier2_check(self, ops, status) -> None:
        """Tier1 重启后 60 秒再确认一次，未回来则升 Tier2（同一事件升级）。"""
        if not bool(self.config.get("uu_escalation_enabled", True)):
            return

        def _check():
            try:
                from campus_remote_app import discover as _discover3

                after = _discover3(ops)
                names = {proc.name for proc in after.user_processes}
                try:
                    _ok, _detail = engine.internet_ok(engine.direct_session())
                except Exception:
                    _ok = False
                confirmed = uu_confirm_back(
                    names, {proc.name for proc in status.user_processes}, bool(_ok))
                decision = uu_should_escalate(confirmed, 60.0, confirmed)
                if not decision.escalate:
                    self.log.info("[UU] %s", decision.reason)
                    return
                self.log.warning("[UU] %s", decision.reason)
                self._uu_tier2_restart(ops, status)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("[UU] Tier2 检查异常：%s", type(exc).__name__)

        timer = threading.Timer(60.0, _check)
        timer.daemon = True
        timer.start()

    def _uu_recovery_ledger(self):
        """UU 恢复的**世代去重**台账（进程内；与冷却守卫正交，方案 §31）。"""
        ledger = getattr(self, "_uu_restart_ledger", None)
        if ledger is None:
            from campus_remote_app import RecoveryRestartLedger

            ledger = self._uu_restart_ledger = RecoveryRestartLedger()
        return ledger

    # ---------- V0.5-6：网络事件时间线（只记录，不判定；零 I/O）----------
    def _emit_timeline_event(self, text: str, level: str = "info") -> None:
        """时间线的**唯一日志出口**（级别随事件；绝不写入凭据/Cookie）。"""
        line = "[时间线] %s" % text
        if level == "error":
            self.log.error(line)
        elif level == "warning":
            self.log.warning(line)
        else:
            self.log.info(line)

    def _path_roles(self):
        """当前 ``(主链路, 备用链路)``：整合层优先，其次状态机展示状态；未知为 ``None``。"""
        active = standby = None
        controller = getattr(self, "dual_path", None)
        if controller is not None:
            try:
                status = controller.status()
            except Exception:                    # 状态读取失败不得让调用方崩溃
                status = None
            if status is not None:
                active, standby = status.active_path, status.standby_path
        fs = getattr(self, "failover_status", None)
        if active is None:
            active = getattr(fs, "primary", None)
        if standby is None:
            standby = getattr(fs, "backup", None)
        return active, standby

    def _timeline_observe(self, now, online, *, trusted=None, detail="",
                          snapshot=None, result=None):
        """把本轮**只读**观测写进时间线（判定仍在真实探测 + 状态机里）。

        ``online`` 传 ``None`` 表示"本次不更新断网/恢复状态"（只补里程碑）。
        """
        timeline = getattr(self, "timeline", None)
        if timeline is None:
            return None
        try:
            observation = getattr(self, "last_observation", None)
            if snapshot is None:
                snapshot = getattr(observation, "snapshot", None)
            if trusted is None:
                trusted = getattr(observation, "trusted", None)
                detail = detail or getattr(observation, "reason_text", "") or ""
            verification = {}
            status = None
            if self.dual_path is not None:
                try:
                    status = self.dual_path.status(now=now)
                except Exception as exc:      # 整合层异常不得阻断时间线记录
                    self.log.warning("时间线读取整合层状态异常：%s", type(exc).__name__)
            if status is not None:
                verification = {"ethernet": status.ethernet, "wifi": status.wifi}
            active_path, standby_path = self._path_roles()
            fs = getattr(self, "failover_status", None)
            tick = getattr(self, "dual_path_tick", None)
            return timeline.observe(
                now=now,
                offline=(None if online is None else (not bool(online))),
                failover_state=getattr(fs, "state", None),
                ethernet_state=getattr(getattr(snapshot, "ethernet", None),
                                       "state", None),
                wifi_state=getattr(getattr(snapshot, "wifi", None), "state", None),
                active_path=active_path, standby_path=standby_path,
                verification=verification,
                op_plan=getattr(result, "plan", ()) or (),
                op_outcomes=getattr(result, "executed", ()) or (),
                recovery_event=getattr(tick, "event", None),
                recovery_steps=getattr(tick, "steps", ()) or (),
                trusted=trusted, detail=detail,
                final_state=str(getattr(getattr(fs, "state", None), "value", "") or ""))
        except Exception as exc:      # 时间线记录不得影响监测主流程（防御性边界）
            self.log.warning("时间线记录异常：%s", type(exc).__name__)
            return None

    def _timeline_paused(self, now) -> None:
        """监测被暂停：时间线一并停记（**不**伪造恢复时刻，方案 §5.6）。"""
        timeline = getattr(self, "timeline", None)
        if timeline is None:
            return
        try:
            timeline.note_paused(now=now)
        except Exception as exc:
            self.log.warning("时间线暂停记录异常：%s", type(exc).__name__)

    def _timeline_reconnect(self, ok, detail, elapsed=None) -> None:
        """一次重连尝试的结局入时间线（``ok`` 只代表 Portal 认证完成，见方案 §1）。"""
        timeline = getattr(self, "timeline", None)
        if timeline is None:
            return
        try:
            timeline.note_reconnect(bool(ok), detail=detail or "", elapsed=elapsed)
        except Exception as exc:
            self.log.warning("时间线重连记录异常：%s", type(exc).__name__)

    def _timeline_uu_started(self, request=None, *, active_path=None,
                             network_epoch=None, reason=None) -> None:
        timeline = getattr(self, "timeline", None)
        if timeline is None:
            return
        try:
            timeline.note_uu_started(request, active_path=active_path,
                                     network_epoch=network_epoch, reason=reason)
        except Exception as exc:
            self.log.warning("时间线 UU 记录异常：%s", type(exc).__name__)

    def _timeline_uu_outcome(self, outcome=None, **kwargs) -> None:
        timeline = getattr(self, "timeline", None)
        if timeline is None:
            return
        try:
            timeline.note_uu_outcome(outcome, **kwargs)
        except Exception as exc:
            self.log.warning("时间线 UU 记录异常：%s", type(exc).__name__)

    def timeline_report_lines(self, limit: int = 1):
        """最近故障事件的**复盘时间线**（诊断页 / 日志用；不含凭据/Cookie）。"""
        timeline = getattr(self, "timeline", None)
        if timeline is None:
            return ["时间线未接线"]
        try:
            return list(timeline.report_lines(limit=limit))
        except Exception:
            return ["时间线读取失败：\n%s" % traceback.format_exc()]

    def timeline_status_lines(self, limit: int = 3):
        """仪表盘用短摘要（最近一次断网结论 + 最近事件）；失败时返回空表。"""
        timeline = getattr(self, "timeline", None)
        if timeline is None:
            return []
        try:
            return list(timeline.status_lines(limit=limit))
        except Exception:
            self.log.warning("时间线摘要读取异常")
            return []

    # ---------- V0.5-5：UI 状态映射 + 主界面 + 手动操作 ----------
    def ui_state(self, *, now: Optional[float] = None):
        """把当前只读状态映射成仪表盘视图模型（**零 I/O**，方案 §5.5）。

        只读 ``last_observation`` / ``failover_status`` / ``dual_path_tick`` /
        整合层 ``status()``；**不**发起探测、**不**碰 Tk、**不**清退进程 ——
        因此可以在 Tk 主线程与 pystray 菜单线程里安全调用。
        """
        from campus_ui_state import build_ui_state

        moment = time.monotonic() if now is None else float(now)
        controller = getattr(self, "dual_path", None)
        status = None
        if controller is not None:
            try:
                status = controller.status(now=moment)
            except Exception as exc:                 # 状态映射不得让 UI 崩溃
                self.log.warning("整合层状态读取异常：%s", type(exc).__name__)
                status = None
        verification = {}
        uu_enabled = False
        uu_last_ok = None
        network_epoch = 0
        events = []
        max_age = None
        if status is not None:
            verification = {"ethernet": status.ethernet, "wifi": status.wifi}
            uu_enabled = bool(status.uu_enabled)
            uu_last_ok = status.last_uu_ok
            network_epoch = int(status.network_epoch)
            event = getattr(status, "event", None)
            if event is not None:
                events.append("最近恢复事件：%s" % getattr(event, "value", event))
            steps = getattr(status, "steps", ()) or ()
            if steps:
                events.append("恢复阶段：%s"
                              % " → ".join(str(getattr(s, "value", s)) for s in steps))
            gate = getattr(controller, "gate", None)
            max_age = getattr(gate, "max_age", None)
        observation = getattr(self, "last_observation", None)
        snapshot = observation.snapshot if observation is not None and observation.trusted else None
        fs = getattr(self, "failover_status", None)
        failover_state = getattr(fs, "state", None)
        observation_note = ""
        if snapshot is None:
            active_path = standby_path = failover_state = None
            observation_note = observation.reason_text if observation is not None else "等待网络采集"
        else:
            egress = getattr(getattr(getattr(snapshot, "system", None), "egress", None),
                             "if_index", None)
            observed = {axis.if_index: role for role in ("ethernet", "wifi")
                        if (axis := getattr(snapshot, role, None)) is not None and axis.if_index}
            active_path = observed.get(egress)
            standby_path = (next((role for role in observed.values() if role != active_path), None)
                            if active_path is not None else None)
        tick = getattr(self, "dual_path_tick", None)
        verify_roles = tuple(getattr(status, "pending_roles", ()) or ())
        uu_request = getattr(tick, "uu_request", None) if tick is not None else None
        recovery_event = None
        if tick is not None:
            event = getattr(tick, "event", None)
            if event is not None:
                recovery_event = str(getattr(event, "value", event))
        warnings = list(getattr(fs, "warnings", ()) or ()) if fs is not None else []
        events.extend(str(item) for item in (getattr(fs, "remote_warnings", ()) or ()))
        worker = self.worker_status()
        kwargs = {}
        if max_age is not None:
            kwargs["max_age"] = float(max_age)
        return build_ui_state(
            now=moment, ethernet=getattr(snapshot, "ethernet", None),
            wifi=getattr(snapshot, "wifi", None), verification=verification,
            active_path=active_path, standby_path=standby_path,
            failover_state=failover_state,
            mode_label=tray_mode_label(self._current_mode()),
            monitor_label=str(worker.get("label") or ""),
            monitor_alive=bool(worker.get("alive")),
            paused=bool(self.config.get("paused")),
            observation_note=observation_note,
            recovery_event=recovery_event, verify_roles=verify_roles,
            uu_enabled=uu_enabled, uu_last_ok=uu_last_ok,
            uu_pending=uu_request is not None, events=events,
            warnings=warnings, network_epoch=network_epoch,
            ssid=self.config.get("ssid") or "SNNU",
            manual=getattr(self, "manual_verification", None) or {}, **kwargs)

    def ui_state_lines(self) -> List[str]:
        """仪表盘状态的可日志化摘要（诊断用；**不**含凭据/Cookie）。"""
        try:
            return self.ui_state().lines()
        except Exception:
            return ["UI 状态映射失败：\n%s" % traceback.format_exc()]

    def _link_text(self, item=None):
        """菜单：主链路 / 备用链路（只读）。"""
        try:
            state = self.ui_state()
        except Exception:
            return "主/备链路：未知"
        return "主链路：%s · 备用：%s" % (
            PATH_LABEL.get(state.active_path, "未知"),
            PATH_LABEL.get(state.standby_path, "未知"))

    def _internet_text(self, item=None):
        """菜单：Internet 是否**真正**可用（真值来自接口受限验证）。"""
        try:
            state = self.ui_state()
        except Exception:
            return "Internet：未知"
        card = state.card(state.active_path) if state.active_path else None
        return "Internet：%s" % (card.field("Internet") if card is not None else "未验证")

    def _uu_text(self, item=None):
        """菜单：UU 远程自动恢复状态。"""
        try:
            state = self.ui_state()
        except Exception:
            return "UU 远程：未知"
        return "UU 远程：%s" % state.uu.label

    def _active_role_hint(self) -> str:
        """当前活动路径（用于手动验证 / 手动清退选目标；fail-closed 回退 ethernet）。"""
        controller = getattr(self, "dual_path", None)
        if controller is not None:
            try:
                active = controller.status().active_path
            except Exception:
                active = None
            if active in PATH_LABEL:
                return active
        primary = getattr(getattr(self, "failover_status", None), "primary", None)
        if primary in PATH_LABEL:
            return primary
        return "ethernet"

    # ---- 主界面（仪表盘） ---- #
    def on_dashboard(self, icon=None, item=None):
        """打开主界面：只把动作投递到 Tk 线程（网络线程不直接碰 Tk）。"""
        if self.root is None:
            self.log.info("无 Tk 根窗口：忽略“打开主界面”请求")
            return
        self._ui_post(self._open_dashboard)

    def _open_dashboard(self):
        """构建主窗口仪表盘（顶部状态灯 + 双网卡卡片 + 恢复流水线 + 操作按钮）。"""
        if self.root is None:
            return
        if ui_backend() == "customtkinter":
            try:
                return self._open_dashboard_ctk()
            except Exception as exc:  # noqa: BLE001 - 回退原生 ttk
                self.log.warning("CustomTkinter 仪表盘打开失败，回退 ttk：%s",
                                 type(exc).__name__)
        win = self.dashboard_win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    return
            except tk.TclError:
                pass
        from campus_ui_state import CARD_ROWS, PIPELINE_LABELS

        win = tk.Toplevel(self.root)
        self.dashboard_win = win
        win.title("%s · 双链路监控" % APP_NAME)
        win.geometry("%dx%d" % (DASHBOARD_WIDTH, DASHBOARD_HEIGHT))
        win.minsize(DASHBOARD_MIN_WIDTH, DASHBOARD_MIN_HEIGHT)
        win.protocol("WM_DELETE_WINDOW", self._close_dashboard)
        outer = ttk.Frame(win, padding=12)
        outer.grid(sticky="nsew")
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=APP_NAME,
                  font=("Microsoft YaHei", 13, "bold")).grid(row=0, column=0, sticky="w")
        self._dash_light = tk.Canvas(header, width=18, height=18, highlightthickness=0)
        self._dash_light.grid(row=0, column=2, sticky="e", padx=(8, 4))
        self._dash_overall = tk.StringVar(value="未知")
        ttk.Label(header, textvariable=self._dash_overall,
                  font=("Microsoft YaHei", 11, "bold")).grid(row=0, column=3, sticky="e")
        self._dash_headline = tk.StringVar(value="正在读取状态…")
        ttk.Label(header, textvariable=self._dash_headline, foreground="#8b949e").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 8))

        cards = ttk.Frame(outer)
        cards.grid(row=1, column=0, sticky="ew")
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        refs = {}
        for column, role in enumerate(("ethernet", "wifi")):
            frame = ttk.LabelFrame(
                cards, text=("以太网" if role == "ethernet" else "SNNU Wi-Fi"), padding=8)
            frame.grid(row=0, column=column, sticky="nsew",
                       padx=(0, 6) if column == 0 else (6, 0))
            title = ttk.Label(frame, text="—", font=("Microsoft YaHei", 10, "bold"))
            title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
            rows, link_label = {}, None
            for index, label in enumerate(CARD_ROWS, start=1):
                name = ttk.Label(frame, text=("Wi-Fi" if label == "链路" else label),
                                 foreground="#8b949e")
                name.grid(row=index, column=0, sticky="e", padx=(0, 6), pady=1)
                var = tk.StringVar(value="—")
                ttk.Label(frame, textvariable=var).grid(row=index, column=1, sticky="w", pady=1)
                rows[label] = var
                if label == "链路":
                    link_label = name
            refs[role] = {"title": title, "rows": rows, "link_label": link_label}

        pipeline = ttk.LabelFrame(outer, text="恢复流水线", padding=8)
        pipeline.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        step_widgets = {}
        for index, (key, label) in enumerate(PIPELINE_LABELS):
            widget = ttk.Label(pipeline, text="%s\n—" % label, justify="center")
            widget.grid(row=0, column=index * 2, padx=4)
            step_widgets[key] = widget
            if index < len(PIPELINE_LABELS) - 1:
                ttk.Label(pipeline, text="→", foreground="#8b949e").grid(
                    row=0, column=index * 2 + 1)
        refs["steps"] = step_widgets

        self._dash_uu = tk.StringVar(value="UU 远程自动恢复：—")
        ttk.Label(outer, textvariable=self._dash_uu).grid(row=3, column=0, sticky="w",
                                                          pady=(8, 0))
        self._dash_monitor = tk.StringVar(value="监测：—")
        ttk.Label(outer, textvariable=self._dash_monitor, foreground="#8b949e").grid(
            row=4, column=0, sticky="w")
        self._dash_events = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self._dash_events, foreground="#8b949e",
                  justify="left").grid(row=5, column=0, sticky="w")
        # V0.5-6：网络事件时间线（什么时候断的网 / 什么时候恢复 / 花了多久）。
        ttk.Label(outer, text="网络时间线（断网 / 恢复）", foreground="#8b949e").grid(
            row=6, column=0, sticky="w", pady=(6, 0))
        self._dash_timeline = tk.StringVar(value="—")
        ttk.Label(outer, textvariable=self._dash_timeline, foreground="#8b949e",
                  justify="left", wraplength=DASHBOARD_WIDTH - 60).grid(
                      row=7, column=0, sticky="w")
        ttk.Label(outer, text="最近手动验证（逐站）", foreground="#8b949e").grid(
            row=8, column=0, sticky="w", pady=(6, 0))
        self._dash_manual = tk.StringVar(value="—")
        ttk.Label(outer, textvariable=self._dash_manual, foreground="#8b949e",
                  justify="left").grid(row=9, column=0, sticky="w")

        buttons = ttk.Frame(outer)
        buttons.grid(row=10, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="立即检测", command=self.on_detect_now).grid(
            row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="立即验证以太网",
                   command=lambda: self.on_verify_now("ethernet")).grid(
                       row=0, column=1, padx=6)
        ttk.Button(buttons, text="立即验证 SNNU Wi-Fi",
                   command=lambda: self.on_verify_now("wifi")).grid(
                       row=0, column=2, padx=6)
        ttk.Button(buttons, text="手动重连", command=self.on_reconnect).grid(
            row=0, column=3, padx=6)
        ttk.Button(buttons, text="重启 UU Remote",
                   command=self.on_restart_uu_remote).grid(row=0, column=4, padx=6)
        ttk.Button(buttons, text="设置", command=self.on_settings).grid(
            row=0, column=5, padx=6)
        ttk.Button(buttons, text="打开日志", command=self.on_open_log).grid(
            row=0, column=6, padx=6)
        ttk.Button(buttons, text="关闭", command=self._close_dashboard).grid(
            row=0, column=7, padx=6)

        self._dash_widgets = refs
        self._refresh_dashboard()

    def _open_dashboard_ctk(self):
        """V0.6.1 A+D+E 融合仪表盘（失败时由调用方回退 ttk）。

        布局（上 → 下）：A 大状态区 → E 三环仪表 → 恢复流水线 →
        详情折叠（双网卡 10 行卡片，默认收起）→ D 历史时间线 →
        UU 行 / 监测行 / 手动验证行 → 最近一小时状态图 → 8 个操作按钮。
        内容区用 CTkScrollableFrame，窗口小也不挤；ttk 回退路径不动。
        """
        if self.root is None:
            return
        win = self.dashboard_win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    return
            except Exception:
                pass
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        from campus_ui_state import CARD_ROWS, PIPELINE_LABELS

        win = ctk.CTkToplevel(self.root)
        self.dashboard_win = win
        win.title("%s · 双链路监控" % APP_NAME)
        win.geometry("%dx%d" % (DASHBOARD_WIDTH, DASHBOARD_HEIGHT))
        win.minsize(DASHBOARD_MIN_WIDTH, DASHBOARD_MIN_HEIGHT)
        win.protocol("WM_DELETE_WINDOW", self._close_dashboard)
        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        outer = ctk.CTkFrame(scroll, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        # ---- A：顶部大状态区（一眼只看好坏）----
        hero = ctk.CTkFrame(outer, fg_color="transparent")
        hero.pack(fill="x")
        light = ctk.CTkLabel(hero, text="●", font=ctk.CTkFont(size=30),
                             text_color="#3fb950")
        light.pack(side="left", padx=(0, 10))
        overall = ctk.CTkLabel(hero, text="未知",
                               font=ctk.CTkFont(size=30, weight="bold"))
        overall.pack(side="left")
        ctk.CTkLabel(hero, text=APP_NAME, text_color="#94a3b8").pack(
            side="right")
        headline = ctk.CTkLabel(outer, text="正在读取状态…",
                                text_color="#94a3b8",
                                font=ctk.CTkFont(size=13))
        headline.pack(fill="x", pady=(2, 10))
        refs = {"light": light, "overall": overall, "headline": headline}

        # ---- V0.6.2：更新横幅（有新版本且未点过“以后再说”时才显示）----
        banner = ctk.CTkFrame(outer, corner_radius=12, fg_color="#451a03",
                              border_color="#d29922", border_width=1)
        refs["update_banner"] = banner
        refs["update_banner_visible"] = False
        banner_label = ctk.CTkLabel(banner, text="",
                                    text_color="#fcd34d",
                                    font=ctk.CTkFont(size=13))
        banner_label.pack(side="left", padx=10, pady=8)
        refs["update_banner_label"] = banner_label
        ctk.CTkButton(banner, text="下载更新", width=90,
                      command=self._on_update_download).pack(
                          side="right", padx=(4, 10), pady=6)
        ctk.CTkButton(banner, text="以后再说", width=90,
                      fg_color="#334155",
                      command=self._on_update_dismiss).pack(
                          side="right", padx=4, pady=6)

        # ---- E：三环仪表（Canvas 手绘圆环，不引新依赖）----
        gauges = ctk.CTkFrame(outer, fg_color="transparent")
        gauges.pack(fill="x")
        refs["gauges_frame"] = gauges
        gauge_refs = {}
        for role, title in (("ethernet", "以太网"),
                            ("wifi", "SNNU Wi-Fi"), ("uu", "UU 远程")):
            frame = ctk.CTkFrame(gauges, corner_radius=12,
                                 fg_color="#1e293b")
            frame.pack(side="left", fill="both", expand=True,
                       padx=(0, 6) if role == "ethernet"
                       else (6, 0) if role == "uu" else (6, 6))
            ctk.CTkLabel(frame, text=title,
                         font=ctk.CTkFont(size=14, weight="bold")).pack(
                             pady=(8, 0))
            canvas = tk.Canvas(frame, width=110, height=110, bg="#1e293b",
                               highlightthickness=0)
            canvas.pack(pady=(2, 0))
            fact = ctk.CTkLabel(frame, text="—", text_color="#94a3b8",
                                wraplength=220, justify="center")
            fact.pack(padx=8, pady=(0, 8))
            gauge_refs[role] = {"canvas": canvas, "fact": fact}
        refs["gauges"] = gauge_refs
        ctk.CTkLabel(outer, text="仪表百分比是状态映射（已验证≈95%），不是实测速率",
                     text_color="#64748b").pack(anchor="w", pady=(2, 0))

        # ---- 恢复流水线（仪表与时间线之间）----
        pipe = ctk.CTkFrame(outer, corner_radius=12, fg_color="#1e293b")
        pipe.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(pipe, text="恢复流水线",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
                         anchor="w", padx=10, pady=(8, 2))
        steps_frame = ctk.CTkFrame(pipe, fg_color="transparent")
        steps_frame.pack(fill="x", padx=10)
        step_widgets = {}
        for key, label in PIPELINE_LABELS:
            cell = ctk.CTkFrame(steps_frame, fg_color="transparent")
            cell.pack(side="left", fill="x", expand=True)
            w = ctk.CTkLabel(cell, text="%s\n—" % label)
            w.pack()
            step_widgets[key] = w
        progress = ctk.CTkProgressBar(pipe, height=8)
        progress.pack(fill="x", padx=10, pady=(4, 8))
        progress.set(0.0)
        refs["steps"] = step_widgets
        refs["progress"] = progress

        # ---- 详情折叠：双网卡 10 行卡片，默认收起 ----
        toggle = ctk.CTkButton(outer, text="展开详情 ▾", width=120,
                               fg_color="#334155",
                               command=self._toggle_dashboard_details)
        toggle.pack(anchor="w", pady=(10, 0))
        refs["details_toggle"] = toggle
        refs["details_open"] = False
        details = ctk.CTkFrame(outer, fg_color="transparent")
        refs["details_frame"] = details
        cards = ctk.CTkFrame(details, fg_color="transparent")
        cards.pack(fill="x")
        for role in ("ethernet", "wifi"):
            frame = ctk.CTkFrame(cards, corner_radius=12,
                                 fg_color="#1e293b")
            frame.pack(side="left", fill="both", expand=True,
                       padx=(0, 6) if role == "ethernet" else (6, 0))
            title = "以太网" if role == "ethernet" else "SNNU Wi-Fi"
            ctk.CTkLabel(frame, text=title,
                         font=ctk.CTkFont(size=14, weight="bold")).pack(
                             anchor="w", padx=10, pady=(8, 2))
            rows = {}
            for label in CARD_ROWS:
                row = ctk.CTkFrame(frame, fg_color="transparent")
                row.pack(fill="x", padx=10)
                ctk.CTkLabel(row, text=label, text_color="#94a3b8",
                             width=70).pack(side="left")
                var = ctk.CTkLabel(row, text="—")
                var.pack(side="left")
                rows[label] = var
            refs[role] = rows

        # ---- D：历史时间线（真实事件，最新在上，最多 5 条）----
        tl_frame = ctk.CTkFrame(outer, corner_radius=12, fg_color="#1e293b")
        tl_frame.pack(fill="x", pady=(10, 0))
        refs["timeline_frame"] = tl_frame
        tl_head = ctk.CTkFrame(tl_frame, fg_color="transparent")
        tl_head.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(tl_head, text="历史时间线",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
                         side="left")
        ctk.CTkButton(tl_head, text="查看全部日志", width=110,
                      fg_color="#334155",
                      command=self.on_open_log).pack(side="right")
        timeline = ctk.CTkLabel(tl_frame, text="尚无网络故障事件记录",
                                text_color="#94a3b8", wraplength=820,
                                justify="left")
        timeline.pack(anchor="w", padx=10, pady=(0, 8))
        refs["timeline"] = timeline

        uu = ctk.CTkLabel(outer, text="UU 远程自动恢复：—")
        uu.pack(anchor="w", pady=(8, 0))
        monitor = ctk.CTkLabel(outer, text="监测：—", text_color="#94a3b8")
        monitor.pack(anchor="w")
        manual = ctk.CTkLabel(outer, text="—", text_color="#94a3b8",
                              wraplength=820, justify="left")
        manual.pack(anchor="w")
        refs["uu"] = uu
        refs["monitor"] = monitor
        refs["manual"] = manual
        chart = tk.Canvas(outer, height=64, bg="#0f172a",
                          highlightthickness=0)
        chart.pack(fill="x", pady=(8, 0))
        refs["chart"] = chart
        refs["history"] = []
        refs["pulse"] = 0
        buttons = ctk.CTkFrame(outer, fg_color="transparent")
        buttons.pack(fill="x", pady=(12, 0))
        btns = []
        for text, cmd in (
                ("立即检测", self.on_detect_now),
                ("立即验证以太网", lambda: self.on_verify_now("ethernet")),
                ("立即验证 SNNU Wi-Fi", lambda: self.on_verify_now("wifi")),
                ("手动重连", self.on_reconnect),
                ("重启 UU Remote", self.on_restart_uu_remote),
                ("设置", self.on_settings),
                ("打开日志", self.on_open_log),
                ("关闭", self._close_dashboard)):
            btn = ctk.CTkButton(buttons, text=text, command=cmd, width=96)
            btn.pack(side="left", padx=3)
            btns.append(btn)
        refs["buttons"] = btns
        self._dash_ctk_widgets = refs
        self._dash_widgets = None
        self._refresh_dashboard()

    def _draw_gauge(self, canvas, gauge, color) -> None:
        """在 110×110 Canvas 上画一个环形仪表（无数据时只画底环 + “未验证”）。"""
        try:
            canvas.delete("all")
            canvas.create_arc(8, 8, 102, 102, start=90, extent=-360,
                              style="arc", outline="#334155", width=10)
            if gauge is None:
                canvas.create_text(55, 48, text="未验证", fill="#8b949e",
                                   font=("Microsoft YaHei", 13, "bold"))
                canvas.create_text(55, 68, text="无数据，不猜",
                                   fill="#64748b",
                                   font=("Microsoft YaHei", 10))
                return
            fraction, label = gauge
            canvas.create_arc(8, 8, 102, 102, start=90,
                              extent=-max(0.0, min(1.0, fraction)) * 360,
                              style="arc", outline=color, width=10)
            canvas.create_text(55, 48, text=label, fill=color,
                               font=("Microsoft YaHei", 13, "bold"))
            canvas.create_text(55, 68, text="%.0f%%" % (fraction * 100),
                               fill="#94a3b8",
                               font=("Microsoft YaHei", 10))
        except Exception:
            pass

    def _dashboard_timeline_lines(self, limit: int = 5):
        """仪表盘时间线：真实事件（断网/假死/切换/恢复/UU 重启），最新在上。

        用 ``timeline.recent``（时间正序）再倒序；无时间线或无事件返回空表，
        渲染时显示占位文案。只读，不写日志、不触发动作。
        """
        timeline = getattr(self, "timeline", None)
        if timeline is None:
            return []
        try:
            events = timeline.recent(limit)
        except Exception:
            return []
        return [event.line() for event in reversed(events)]

    def _toggle_dashboard_details(self):
        """详情折叠开关：默认收起；展开时把详情框插到流水线与时间线之间。"""
        refs = getattr(self, "_dash_ctk_widgets", None)
        if refs is None:
            return
        open_ = not refs.get("details_open", False)
        refs["details_open"] = open_
        frame = refs.get("details_frame")
        toggle = refs.get("details_toggle")
        if frame is not None:
            if open_:
                anchor = refs.get("timeline_frame")
                if anchor is not None:
                    frame.pack(fill="x", pady=(6, 0), before=anchor)
                else:
                    frame.pack(fill="x", pady=(6, 0))
            else:
                frame.pack_forget()
        if toggle is not None:
            toggle.configure(text="收起详情 ▴" if open_ else "展开详情 ▾")

    def _render_dashboard_ctk(self, state) -> None:
        """把 UiState 写进 CustomTkinter 控件（纯赋值，不做网络操作）。"""
        refs = getattr(self, "_dash_ctk_widgets", None)
        if refs is None:
            return
        refs["light"].configure(text_color=state.overall_color)
        refs["overall"].configure(text=state.overall_label,
                                  text_color=state.overall_color)
        refs["headline"].configure(text=state.headline or "—")
        # V0.6.2：更新横幅显隐（只在状态变化时改布局）。
        self._render_update_banner(refs)
        for role in ("ethernet", "wifi"):
            card = state.card(role)
            if card is None:
                continue
            for label, var in (refs.get(role) or {}).items():
                var.configure(text=card.field(label))
        # E：三环仪表（数值只从 UiState 状态推导，见 gauge_from_*）
        gauges = refs.get("gauges") or {}
        for role in ("ethernet", "wifi"):
            slot = gauges.get(role)
            if slot is None:
                continue
            card = state.card(role)
            gauge = gauge_from_card(card)
            color = card.color if card is not None else "#6e7681"
            self._draw_gauge(slot["canvas"], gauge, color)
            if card is None:
                slot["fact"].configure(text="—")
            else:
                slot["fact"].configure(
                    text="源地址 %s · %s" % (card.field("源地址"),
                                            card.field("最近验证")))
        uu_slot = gauges.get("uu")
        if uu_slot is not None:
            gauge = gauge_from_uu(state.uu)
            from campus_ui_state import TONE_COLOR
            color = TONE_COLOR.get(state.uu.tone, "#6e7681")
            self._draw_gauge(uu_slot["canvas"], gauge, color)
            uu_slot["fact"].configure(
                text=state.uu.detail or ("已开启" if state.uu.enabled
                                        else "默认关闭（设置里打开）"))
        done = 0
        for step in state.pipeline:
            widget = (refs.get("steps") or {}).get(step.key)
            if widget is not None:
                widget.configure(text=step.display, text_color=step.color)
            if step.status == "done":
                done += 1
        refs["progress"].set(done / 6.0 if state.pipeline else 0.0)
        refs["uu"].configure(
            text="UU 远程自动恢复：%s%s" % (
                state.uu.label,
                ("（%s）" % state.uu.detail) if state.uu.detail else ""))
        notes = [state.monitor_label or "监测状态未知"]
        if state.network_epoch:
            notes.append("epoch=%d" % state.network_epoch)
        if state.mode_label:
            notes.append("模式：%s" % state.mode_label)
        refs["monitor"].configure(text=" · ".join(notes))
        lines = self._dashboard_timeline_lines(limit=5)
        refs["timeline"].configure(
            text="\n".join(lines) if lines else "尚无网络故障事件记录")
        manual = []
        for role in ("ethernet", "wifi"):
            card = state.card(role)
            if card is not None and card.manual:
                manual.extend(card.manual)
        refs["manual"].configure(text="\n".join(manual) if manual else "—")
        history = refs["history"]
        history.append((time.time(), state.overall))
        refs["history"] = [h for h in history if h[0] > time.time() - 3600]
        self._draw_history_chart(refs["chart"], refs["history"])
        refs["pulse"] = refs.get("pulse", 0) + 1
        if state.overall in ("RECOVERING", "DEGRADED"):
            refs["light"].configure(
                text_color="#facc15" if refs["pulse"] % 2 else state.overall_color)

    def _draw_history_chart(self, canvas, history) -> None:
        """最近一小时双链路状态图（Canvas 手绘，不引入新依赖）。"""
        try:
            canvas.delete("all")
            width = int(canvas.winfo_width() or 820)
            height = int(canvas.winfo_height() or 64)
            colors = {"HEALTHY": "#3fb950", "DEGRADED": "#d29922",
                      "RECOVERING": "#58a6ff", "OFFLINE": "#f85149",
                      "PAUSED": "#8b949e", "UNKNOWN": "#6e7681"}
            if not history:
                canvas.create_text(width // 2, height // 2,
                                   text="最近一小时状态图：暂无数据",
                                   fill="#94a3b8")
                return
            now = time.time()
            for stamp, overall in history:
                x = width - max(0.0, (now - stamp)) / 3600.0 * width
                canvas.create_rectangle(x - 2, 6, x + 2, height - 6,
                                        fill=colors.get(overall, "#6e7681"),
                                        outline="")
            canvas.create_text(4, 8, anchor="w",
                               text="最近 1 小时：绿正常 / 黄降级 / 蓝恢复中 / 红离线",
                               fill="#94a3b8")
        except Exception:
            pass

    def _close_dashboard(self):
        win = self.dashboard_win
        self.dashboard_win = None
        self._dash_widgets = None
        self._dash_ctk_widgets = None
        refresh = self._dash_refresh_id
        self._dash_refresh_id = None
        if refresh is not None and win is not None:
            try:
                win.after_cancel(refresh)
            except Exception:
                pass
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _refresh_dashboard(self):
        """按周期把状态映射渲染到主界面（**只读**：不做任何网络操作）。"""
        win = self.dashboard_win
        if win is None or self.stop_event.is_set():
            return
        try:
            if not win.winfo_exists():
                self._close_dashboard()
                return
        except tk.TclError:
            self._close_dashboard()
            return
        if getattr(self, "_dash_ctk_widgets", None) is not None:
            try:
                self._render_dashboard_ctk(self.ui_state())
            except Exception:                # 渲染故障不得让托盘崩溃
                self.log.warning("主界面渲染异常：\n%s", traceback.format_exc())
            try:
                self._dash_refresh_id = win.after(
                    DASHBOARD_REFRESH_MS, self._refresh_dashboard)
            except tk.TclError:
                self._dash_refresh_id = None
            return
        try:
            self._render_dashboard(self.ui_state())
        except Exception:                    # 渲染故障不得让托盘崩溃
            self.log.warning("主界面渲染异常：\n%s", traceback.format_exc())
        try:
            self._dash_refresh_id = win.after(DASHBOARD_REFRESH_MS, self._refresh_dashboard)
        except tk.TclError:
            self._dash_refresh_id = None

    def _render_dashboard(self, state) -> None:
        """把 :class:`campus_ui_state.UiState` 写进已建好的控件（纯赋值）。"""
        refs = self._dash_widgets
        if refs is None:
            return
        try:
            self._dash_light.configure(bg=state.overall_color)
        except tk.TclError:
            pass
        self._dash_overall.set("%s · %s" % (state.overall_label, state.overall))
        self._dash_headline.set(state.headline or "—")
        for role in ("ethernet", "wifi"):
            card = state.card(role)
            card_refs = refs.get(role) or {}
            if card is None:
                continue
            title = card_refs.get("title")
            if title is not None:
                try:
                    title.configure(text="%s（%s）" % (card.title, card.assignment_label),
                                    foreground=card.color)
                except tk.TclError:
                    pass
            for label, var in (card_refs.get("rows") or {}).items():
                var.set(card.field(label))
            link_label = card_refs.get("link_label")
            if link_label is not None:
                try:
                    link_label.configure(text="Wi-Fi" if role == "wifi" else "网卡")
                except tk.TclError:
                    pass
        for step in state.pipeline:
            widget = (refs.get("steps") or {}).get(step.key)
            if widget is None:
                continue
            try:
                widget.configure(text=step.display, foreground=step.color)
            except tk.TclError:
                pass
        self._dash_uu.set("UU 远程自动恢复：%s%s"
                          % (state.uu.label,
                             ("（%s）" % state.uu.detail) if state.uu.detail else ""))
        notes = [state.monitor_label or "监测状态未知"]
        if state.network_epoch:
            notes.append("epoch=%d" % state.network_epoch)
        if state.mode_label:
            notes.append("模式：%s" % state.mode_label)
        self._dash_monitor.set(" · ".join(notes))
        lines = list(state.events) + ["告警：%s" % w for w in state.warnings]
        self._dash_events.set("\n".join(lines))
        timeline = self.timeline_status_lines(limit=3)
        self._dash_timeline.set("\n".join(timeline) if timeline
                                else "尚无网络故障事件记录")
        manual = []
        for role in ("ethernet", "wifi"):
            card = state.card(role)
            if card is not None and card.manual:
                manual.extend(card.manual)
        self._dash_manual.set("\n".join(manual) if manual else "—")

    # ---- 手动操作（全部在后台线程；UI 线程不发起网络 I/O） ---- #
    def on_detect_now(self, icon=None, item=None):
        """立即检测：请求工作线程**立刻**跑一轮探测（不等待下一个周期）。"""
        if self.config.get("paused"):
            self.notify("监测已暂停", "监测处于暂停状态；请先恢复监测再检测")
            return
        if not self._ensure_worker():
            self.notify("无法启动", "网络工作线程启动失败")
            return
        self._commands.put(("detect", None))
        self.notify("正在检测", "已请求一次即时网络检测")

    def on_verify_now(self, role: str, icon=None, item=None):
        """立即验证某条链路：构造接口受限请求并在**后台线程**执行（展示逐站结果）。"""
        controller = getattr(self, "dual_path", None)
        if controller is None:
            self.notify("无法验证", "恢复整合层未接线，无法做接口受限验证")
            return
        try:
            request = controller.verification_request(role)
        except Exception as exc:
            self.log.warning("构造接口受限验证请求异常：%s", type(exc).__name__)
            request = None
        if request is None:
            self.notify("无法验证",
                        "缺少 %s 的接口事实（ifIndex / 源地址 / 该接口 DNS）："
                        "按 fail-closed 不发起探测" % PATH_LABEL.get(role, role))
            return
        self._manual_verify_async(role, request)
        self.notify("正在验证",
                    "已请求 %s 的接口受限验证（后台线程，只看真实上网结果）"
                    % PATH_LABEL.get(role, role))

    def _manual_verify_async(self, role: str, request) -> None:
        """在独立短命线程里执行一次**手动**接口受限验证，并把证据交回整合层。"""
        controller = self.dual_path

        def _run():
            try:
                verifier = self._build_path_verifier()
                result = verifier.verify(request)
            except Exception as exc:              # 验证不得影响监测主流程
                self.log.warning("手动接口受限验证异常：%s", type(exc).__name__)
                return
            self._remember_manual_verification(role, result)
            if result.allows_recovery:
                self.log.info("手动验证 %s 通过：%s", role, result.reason)
            else:
                self.log.warning("手动验证 %s 未通过：%s", role, result.reason)
            try:
                follow = controller.on_verification(result, now=time.monotonic())
            except Exception as exc:
                self.log.warning("手动验证结果回灌异常：%s", type(exc).__name__)
                return
            self._uu_recovery_after_verification(follow, result)

        threading.Thread(target=_run, daemon=True,
                         name="campus-manual-verify").start()

    def _remember_manual_verification(self, role: str, result) -> None:
        store = getattr(self, "manual_verification", None)
        if store is None:
            store = self.manual_verification = {}
        store[role] = result

    def on_restart_uu_remote(self, icon=None, item=None):
        """手动「重启 UU Remote」：**先确认 + 先验证 + 清退前再确认**（方案 §5.4）。

        这是唯一允许用户显式触发的清退入口；即便配置默认关闭
        （``remote_restart_enabled=False``），用户当面确认即视为本次授权，但仍
        **必须**先通过接口受限验证与轻量再确认（fail-closed），且只动用户态进程。
        """
        answer = self._ui_ask(
            "重启 UU 远程",
            "将清退并重启 UU 远程（网易 GameViewer）的**用户态**进程。\n"
            "只会处理当前用户的 GameViewer / GameViewerLauncher，"
            "不动 GameViewerService，也不会断开远程会话服务。\n\n"
            "继续前会先做一次接口受限上网验证；验证不过则不清退。\n\n是否继续？")
        if not answer:
            self.notify("已取消", "未清退任何 UU 进程")
            return
        self._manual_uu_restart_async()

    def _manual_uu_restart_async(self) -> None:
        busy = getattr(self, "_manual_uu_busy", None)
        if busy is None:
            busy = self._manual_uu_busy = threading.Event()
        if busy.is_set():
            self.notify("正在处理", "已有一次 UU 重启在进行，请稍候…")
            return
        busy.set()

        def _run():
            try:
                self._manual_uu_restart_run()
            except Exception as exc:              # 清退不得影响监测主流程
                self.log.warning("手动 UU 重启异常：%s", type(exc).__name__)
            finally:
                busy.clear()

        threading.Thread(target=_run, daemon=True,
                         name="campus-manual-uu").start()

    def _manual_uu_restart_run(self) -> None:
        from campus_remote_app import restart_after_verified_recovery

        controller = getattr(self, "dual_path", None)
        role = self._active_role_hint()
        if controller is None:
            self.notify("未重启 UU", "恢复整合层未接线，无法做接口受限验证")
            return
        request = controller.verification_request(role)
        if request is None:
            self.notify("未重启 UU",
                        "缺少活动路径（%s）的接口事实：按 fail-closed 不动作"
                        % PATH_LABEL.get(role, role))
            self._timeline_uu_outcome(
                restarted=False, ok=False, action="fail-closed",
                detail="缺少 %s 的接口事实（ifIndex / 源地址 / 该接口 DNS）"
                       % PATH_LABEL.get(role, role))
            return
        try:
            ops = self._get_remote_app_ops()
            guard = self._get_restart_guard()
            verifier = self._build_path_verifier()
        except Exception as exc:
            self.log.warning("UU 清退能力不可用：%s", type(exc).__name__)
            self.notify("未重启 UU", "UU 清退能力不可用")
            return
        verification = verifier.verify(request)
        self._remember_manual_verification(role, verification)
        if not verification.allows_recovery:
            self.log.warning("手动重启 UU 前验证未通过：%s", verification.reason)
            self.notify("未重启 UU",
                        "活动路径未通过接口受限验证（%s）：不清退" % (
                            verification.reason or "未验证"))
            self._timeline_uu_outcome(
                restarted=False, ok=False, action="verification-failed",
                detail="活动路径未通过接口受限验证：%s"
                       % (verification.reason or "未验证"))
            return
        outcome = restart_after_verified_recovery(
            ops, active_path=role, network_epoch=int(controller.network_epoch),
            verification=verification, reason="手动重启 UU 远程",
            enabled=True, guard=guard,
            recheck=controller.build_recheck(role))
        if outcome.restarted:
            self.log.info("手动 UU 远程已清退重启：%s", outcome.detail)
            self.notify("UU 远程已重启", outcome.detail)
        elif outcome.ok:
            self.log.info("手动 UU 远程无需重启：%s（%s）", outcome.detail, outcome.action)
            self.notify("UU 远程无需重启", outcome.detail)
        else:
            self.log.warning("手动 UU 远程未重启：%s（%s）", outcome.detail, outcome.action)
            self.notify("UU 远程未重启", outcome.detail)
        # V0.5-6：手动清退同样入时间线（STARTED 由调用方在验证通过后登记）。
        self._timeline_uu_started(active_path=role,
                                  network_epoch=int(controller.network_epoch),
                                  reason="手动重启 UU 远程")
        self._timeline_uu_outcome(outcome)

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
        """独立刷新远控状态，避免端口枚举阻塞网络观察。"""
        guard = self.remote
        if self._remote_ops is None or guard is None:
            return
        if (not self.config.get("remote_guard", True) or self._remote_probe_busy.is_set()
                or not guard.probe_due(now)):
            return
        self._remote_probe_busy.set()

        def poll():
            try:
                guard.poll(now)
            except Exception as exc:
                self.log.warning("远控探测异常：%s", type(exc).__name__)
            finally:
                self._remote_probe_busy.clear()

        threading.Thread(target=poll, name="campus-remote-probe", daemon=True).start()

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

    # ---------- V0.4-3：确认在线后清退重启 UU 远程 ----------
    def apply_remote_restart(self, enabled: bool) -> bool:
        """开关「确认在线后清退重启 UU 远程后台」（方案 §3.3，**默认关闭**）。"""
        value = bool(enabled)
        self.update_config({"remote_restart_enabled": value})
        if not save_config(self.config):
            self.notify("保存失败", "UU 远程重启设置未能写入配置（已保留原文件）")
        self.notify("连上后重启 UU 远程已%s" % ("开启" if value else "关闭"),
                    "确认**真的**连上（连接后上网校验通过）后，清退用户态 "
                    "GameViewer 进程并按自启项重启（不动服务）"
                    if value else "不再自动清退 UU 远程后台")
        if self.icon is not None:
            self.icon.update_menu()
        return value

    def on_toggle_remote_restart(self, icon=None, item=None):
        self.apply_remote_restart(
            not bool(self.config.get("remote_restart_enabled", False)))

    def _get_remote_app_ops(self):
        """惰性构建真实清退能力（**默认关闭时不构建、不探测**）。

        注意：方法名刻意**不**叫 ``_remote_app_ops`` —— 实例属性 ``_remote_app_ops``
        才是可注入的能力对象，同名会在类方法/实例属性之间互相遮蔽。
        """
        ops = getattr(self, "_remote_app_ops", None)
        if ops is None:
            from campus_remote_app import build_windows_remote_app_ops

            ops = self._remote_app_ops = build_windows_remote_app_ops()
        return ops

    def _get_restart_guard(self):
        """清退冷却守卫（进程内保存；冷却值每次按最新配置刷新）。"""
        guard = getattr(self, "_remote_restart_guard", None)
        if guard is None:
            from campus_remote_app import RestartGuard

            guard = self._remote_restart_guard = RestartGuard()
        from campus_remote_app import DEFAULT_RESTART_COOLDOWN

        guard.cooldown = float(self.config.get("remote_restart_cooldown",
                                               DEFAULT_RESTART_COOLDOWN))
        return guard

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
        try:
            _ssid, _wstate = engine.wifi_state()
            if _ssid != cfg.get("ssid", "SNNU"):
                self._wifi_bringup(cfg.get("ssid", "SNNU"))
        except Exception:
            pass
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
            # V0.4-1：重连成功即视为「刚连上」⇒ 做一次真实上网校验（不可只依赖探测跃迁）。
            self._maybe_verify_online()
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
            # V0.5-6：Portal 认证成功**不等于**能上网（方案 §1）⇒ 只记里程碑，
            # 故障事件仍由真实探测确认后结束（下一次探测 offline=False）。
            self._timeline_reconnect(True, detail, elapsed)
        else:
            self.set_state("offline", detail)
            extra = self._edge_note(cfg)
            self.notify("重连失败", "%s%s" % (detail, extra))
            self._timeline_reconnect(False, detail, elapsed)

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
            value = int(self.config.get("interval", 5))
        except (TypeError, ValueError):
            value = 5
        return float(_clamp_interval(value))

    def _cooldown(self) -> float:
        try:
            value = int(self.config.get("cooldown", 60))
        except (TypeError, ValueError):
            value = 60
        return float(_clamp_cooldown(value))

    def _threshold(self) -> int:
        try:
            value = int(self.config.get("threshold", 2))
        except (TypeError, ValueError):
            value = 2
        return _clamp_threshold(value)

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
                    # V0.6.2：更新检查挂在已有工作循环上，不另起常驻线程。
                    self._maybe_check_update()
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
        if kind == "detect":
            # V0.5-5：手动“立即检测” —— 立刻跑一轮探测，不等下一个检测周期
            # （``next_probe=0.0`` 是假值，:meth:`_probe_tick` 的节流判断直接放行）。
            return self._probe_tick(0.0)
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
            # V0.5-6：暂停期间不探测 ⇒ 时间线也停记（不伪造恢复时刻）。
            self._timeline_paused(now)
            return now + self._interval()
        if self.state["mode"] == "paused":
            self.set_state("boot", "监测恢复，正在检查网络")
        if next_probe and now < next_probe:
            return next_probe
        try:
            online, detail = internet_ok(self.session)
            self._last_system_probe = (online, detail)
        except Exception as exc:
            self.log.warning("探测异常: %s", type(exc).__name__)
            online = False
            self._last_system_probe = None
        # V0.3 P6：把本轮只读观测喂给故障切换状态机（默认仅监测，零动作）。
        self._failover_tick(now, online)
        if online:
            if self.state["mode"] != "online":
                self.set_state("online", "网络正常")
            # V0.4-1：每个「在线段」做一次真实上网校验（防「连着 Wi-Fi 却打不开网页」）。
            self._maybe_verify_online()
            self.state["failures"] = 0
            self._edge_opened_this_cycle = False
            return self._next_probe_time(now)
        # V0.4-1：判为离线即结束本在线段 ⇒ 下次连上重新校验。
        self._online_verified = False
        self.state["failures"] += 1
        threshold = self._threshold()
        if self.state["failures"] < threshold:
            if self.state["mode"] != "offline":
                self.set_state("offline", "公网探测失败（%d/%d）"
                               % (self.state["failures"], threshold))
            return self._next_probe_time(now)
        if self.config.get("remote_restart_enabled", False) and not getattr(self, "_v06_had_outage", False):
            self._note_outage_for_uu()
        # ---- P7.5-1（F75-02）：后台**永不**自动认证 ----
        # 旧实现在此调用 V0.2 的 ``_run_repair()``（Portal POST + Wi-Fi 重连），
        # 于是 MONITOR / LOCKED 下也会出现第二个副作用源。修复后：自动副作用
        # 只可能来自 P3 单一副作用门（AUTO 且已授权且快照可信）；其余模式
        # 只如实显示故障并等待，或在菜单/设置面板由用户**显式**手动重连。
        detail = self._offline_detail()
        if self.state["mode"] != "offline" or self.state["detail"] != detail:
            self.set_state("offline", detail)
        return self._next_probe_time(now)

    def _maybe_verify_online(self):
        """一个「在线段」内只做一次连接后校验（V0.4-1）。

        ``_after_repair`` 成功时会**直接**把状态置为 online（不经过 ``_probe_tick`` 的
        状态跃迁），因此重连成功必须**单独**调用本方法，否则「重连成功」这一需求核心
        场景会被漏掉。离线时由 :meth:`_probe_tick` 重置标记。
        """
        if getattr(self, "_online_verified", False):
            return
        self._online_verified = True
        self._verify_online_async()

    def _verify_online_async(self):
        """连接后真实上网校验（V0.4-1）——独立短命线程，绝不阻塞监测/重连循环。

        用**专用** :func:`direct_session`（不与工作线程共享 Session，避免并发复用连接池）；
        结果只记日志/气泡，**不影响**状态机、不触发重认证与路由切换（V0.4 边界）。
        """
        busy = getattr(self, "_verify_busy", None)
        if busy is None:
            busy = self._verify_busy = threading.Event()
        if busy.is_set():
            return
        busy.set()

        def _run():
            ok = None
            try:
                ok, detail = verify_internet(direct_session())
                if ok:
                    self.log.info("连接后上网校验通过：%s", detail)
                else:
                    self.log.warning("连接后上网校验未通过，疑似「假死」：%s", detail)
                    self.notify("网络校验未通过",
                                "已连上校园网，但真实网页访问失败（疑似假死）：\n%s"
                                % detail)
            except Exception as exc:   # 校验不得影响工作线程（防御性边界）
                self.log.warning("连接后上网校验异常: %s", type(exc).__name__)
            finally:
                busy.clear()
                # V0.6.1：双轴 Clash 检查放后台线程（见 _start_clash_check_async）。
                # V0.6 曾在这里同步做代理轴探测（每站 3 秒超时，最多约 9 秒，
                # 自愈还要等 taskkill + 2 秒），校验通过后的 UU 重启与失败提示
                # 都被它拖后，Windows CI 上 3 秒等待上限内收不到结果。
                if ok is not None:
                    self._start_clash_check_async(ok)

        threading.Thread(target=_run, daemon=True, name="campus-verify").start()

    def _start_clash_check_async(self, direct_ok: bool) -> None:
        """双轴 Clash 检查放独立短命线程（V0.6.1）。

        校验线程只负责结果处理（UU 重启 / 失败提示），不再等代理轴；
        自愈仍在后台完成，行为不变。已有一路检查在跑时不再叠加。
        """
        if not bool(self.config.get("clash_heal_enabled", True)):
            return
        busy = getattr(self, "_clash_check_busy", None)
        if busy is None:
            busy = self._clash_check_busy = threading.Event()
        if busy.is_set():
            return
        busy.set()

        def _run():
            try:
                self._dual_axis_clash_check(direct_ok)
            finally:
                busy.clear()

        threading.Thread(target=_run, daemon=True,
                         name="campus-clash-check").start()


    # ---------- V0.6：双轴 Clash 自愈 / Wi-Fi 自启 / UU 两档 ----------
    def _note_outage_for_uu(self) -> None:
        """断网段开始时记一笔：UU 恢复前是否在运行（V0.6 触发条件）。

        只读探测（进程列表），失败时如实记 ``was_running=False``，不抛异常；
        假死诊断行（PID + TCP 状态）一并记入日志，绝不含凭据。
        """
        self._v06_had_outage = True
        was_running = False
        try:
            ops = self._get_remote_app_ops()
            from campus_remote_app import AppProcess

            processes = [proc for row in ops.list_processes() or ()
                         if (proc := AppProcess.from_mapping(row)) is not None
                         and proc.in_restart_scope]
            was_running = bool(processes)
            try:
                rows = uu_diagnose(
                    [{"pid": p.pid, "name": p.name} for p in processes],
                    [])
                for row in rows:
                    self.log.info("[UU 诊断] %s", row)
            except Exception:
                pass
        except Exception as exc:
            self.log.warning("[UU 诊断] 断网时读取 UU 进程失败：%s",
                             type(exc).__name__)
        self._v06_uu_was_running = was_running
        self.log.info("[UU] 断网段开始：UU 恢复前在运行=%s", was_running)

    def _dual_axis_clash_check(self, direct_ok: bool) -> None:
        """直连轴结果出来后补测代理轴；Clash 假死时按计划自愈。

        只在 ``clash_heal_enabled`` 时动作；任何异常都只记日志，绝不影响
        监测主流程。代理轴用显式 proxies 的会话（不走环境变量猜测）。
        """
        if not bool(self.config.get("clash_heal_enabled", True)):
            return
        try:
            endpoint = self.config.get("proxy_endpoint") or CLASH_SERVER_DEFAULT
            host, _, port = endpoint.partition(":")
            session = engine.direct_session()
            session.proxies.update({
                "http": "http://%s" % endpoint,
                "https": "http://%s" % endpoint})
            proxy_result = probe_via_proxy(session, engine.DEFAULT_PROBES)
            health = assess_dual_axis(bool(direct_ok), proxy_result)
            if health.verdict == "healthy":
                self._clash_heal_attempts = 0
                return
            self.log.warning("[Clash] 双轴判定：%s", health.reason)
            if health.verdict != "clash_fake_dead":
                return
            try:
                endpoint_ok = bool(check_endpoint(
                    host or "127.0.0.1", int(port or 7897), engine.tcp_probe))
            except Exception:
                endpoint_ok = False
            attempts = int(getattr(self, "_clash_heal_attempts", 0) or 0)
            plan = heal_plan(health, endpoint_ok, attempts)
            self.log.warning("[Clash] 自愈计划（第 %d 次）：%s",
                             attempts, plan.reason)
            if plan.actions:
                self._clash_heal_execute(plan)
                self._clash_heal_attempts = attempts + 1
                self.notify("Clash 假死自愈",
                            "本地能上网但 Clash 代理不通，已尝试：%s"
                            % "、".join(plan.actions))
        except Exception as exc:  # noqa: BLE001 - 自愈不得影响监测
            self.log.warning("[Clash] 双轴自愈异常：%s", type(exc).__name__)

    def _clash_heal_execute(self, plan) -> bool:
        """执行一次 Clash 自愈计划（toggle / restart_verge）。

        注入点 ``_clash_heal_ops``（须有 ``execute(plan) -> bool``）供离线
        测试替换；真实执行只在 Windows 上做，失败如实记日志、不谎报成功。
        """
        ops = getattr(self, "_clash_heal_ops", None)
        if ops is not None:
            return bool(ops.execute(plan))
        if os.name != "nt":
            self.log.warning("[Clash] 非 Windows：跳过自愈执行 %s", plan.actions)
            return False
        ok = True
        for action in plan.actions:
            if action == "toggle_system_proxy":
                endpoint = self.config.get("proxy_endpoint") or CLASH_SERVER_DEFAULT
                try:
                    apply_system_proxy(False)
                    _REAL_SLEEP(1.0)
                    result = apply_system_proxy(True, endpoint)
                    ok = bool(result.ok)
                    self.log.info("[Clash] 系统代理开关一次：%s",
                                  "成功" if ok else "失败")
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("[Clash] 开关系统代理失败：%s",
                                     type(exc).__name__)
                    ok = False
            elif action == "restart_verge":
                try:
                    import subprocess

                    subprocess.run(["taskkill", "/f", "/im", "Clash Verge.exe"],
                                   capture_output=True, timeout=15)
                    _REAL_SLEEP(2.0)
                    started = False
                    for candidate in ("Clash Verge.exe", "clash-verge.exe"):
                        path = shutil.which(candidate)
                        if path:
                            subprocess.Popen([path],
                                             stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
                            started = True
                            break
                    self.log.info("[Clash] 重启 Verge：%s",
                                  "已尝试拉起" if started else
                                  "未找到可执行文件，请手动打开 Verge")
                    ok = started
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("[Clash] 重启 Verge 失败：%s",
                                     type(exc).__name__)
                    ok = False
        return ok

    def _wifi_bringup(self, ssid: str):
        """V0.6 Wi-Fi 自启链：网卡启用 → 射频开 → profile → 连接 → 等 IP。

        只在 Windows 上执行；返回 :class:`BringupResult` 或 ``None``
        （非 Windows / 异常时返回 None，调用方继续走旧的 ``wifi_reconnect``）。
        profile XML 先写进临时文件再执行 ``add profile``（模块本身不写文件）。
        """
        if os.name != "nt":
            return None
        try:
            import subprocess
            import tempfile


            code, text = engine._run_console(
                ["netsh", "wlan", "show", "interfaces"])
            from campus_wifi_bringup import adapter_state_from_netsh_text

            state = adapter_state_from_netsh_text(text, ssid)
            from campus_wifi_bringup import plan as _plan

            has_profile = False
            try:
                _code, _out = engine._run_console(
                    ["netsh", "wlan", "show", "profiles"])
                has_profile = ssid in (_out or "")
            except Exception:
                has_profile = False
            bringup_plan = _plan(state, has_profile, ssid)

            class _Ops:
                def __init__(self, owner):
                    self.owner = owner

                def run(self, cmd):
                    cmd = list(cmd)
                    if "add" in cmd and "profile" in cmd:
                        xml_path = Path(tempfile.gettempdir()) / ("%s.xml" % ssid)
                        xml_path.write_text(
                            build_snnu_open_profile_xml(ssid), encoding="utf-8")
                        cmd = [c if c != "%s.xml" % ssid else str(xml_path)
                               for c in cmd]
                    done = subprocess.run(cmd, capture_output=True,
                                          timeout=20)
                    return done.returncode, done.stdout.decode(
                        "gbk", errors="replace")

                def wait_ip(self):
                    for _ in range(10):
                        try:
                            current, _state = engine.wifi_state()
                            if current == ssid:
                                return True
                        except Exception:
                            pass
                        _REAL_SLEEP(2.0)
                    return False

            result = wifi_run_bringup(bringup_plan, _Ops(self))
            self.log.info("[Wi-Fi 自启] %s（步骤：%s）",
                          result.detail, ",".join(result.steps_done) or "无")
            return result
        except Exception as exc:  # noqa: BLE001 - 自启失败不阻断旧路径
            self.log.warning("[Wi-Fi 自启] 异常：%s", type(exc).__name__)
            return None

    def _uu_tier2_restart(self, ops, status) -> bool:
        """UU Tier2 升级清退：Tier1 + GameViewerServer/Healthd 一起清退。

        同一恢复事件的升级，不另计一次、不重置冷却；服务
        ``GameViewerService`` 永远不动。成功 = Tier2 进程清退后按自启项
        重启，且 10 秒内确认用户态进程回来。
        """
        try:
            from campus_remote_app import _norm, launch_command

            wanted = {_norm(name) for name in uu_tier2_processes()}
            present = [p for p in status.user_processes + status.service_processes
                       if _norm(p.name) in wanted]
            if not present:
                self.log.info("[UU Tier2] 无 Tier2 进程在运行，不清退")
                return False
            command = launch_command(status)
            if not command:
                self.log.warning("[UU Tier2] 未找到重启命令：未做任何改动")
                return False
            killed = []
            for proc in present:
                if not bool(ops.kill(proc.pid)):
                    self.log.warning("[UU Tier2] 清退 %s（PID %d）失败，已中止",
                                     proc.name, proc.pid)
                    return False
                killed.append(proc.pid)
            if not bool(ops.launch(command)):
                self.log.warning("[UU Tier2] 重启命令执行失败")
                return False
            _REAL_SLEEP(10.0)
            from campus_remote_app import discover

            after = discover(ops)
            names = {p.name for p in after.user_processes}
            ok = uu_confirm_back(names, {p.name for p in status.user_processes},
                                 True)
            self.log.info("[UU Tier2] 清退 %d 个进程，确认回来：%s",
                          len(killed), ok)
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("[UU Tier2] 异常：%s", type(exc).__name__)
            return False

    def _prompt_auto_switch_once(self) -> None:
        """V0.6 首次启动弹一次智能自动确认（只弹一次，拒绝也记下）。"""
        if not should_prompt_auto_switch(self.config):
            return
        answer = self._ui_ask(
            "开启智能自动切换？",
            "检测到你还在「仅监测」模式：有线坏了不会自动切 Wi-Fi。\n"
            "同意后，有线故障时自动开 Wi-Fi 连 SNNU，稳定后切回有线。\n"
            "不同意就保持仅监测，以后可以在设置里再改。")
        if answer is None:
            return
        new_cfg = apply_auto_switch_consent(self.config, bool(answer))
        self.update_config(new_cfg)
        try:
            save_config(new_cfg)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("智能自动确认保存失败：%s", type(exc).__name__)
        self.log.info("[V0.6] 智能自动确认：%s",
                      "已开启 AUTO" if answer else "保持仅监测")

    # ---------- V0.6.2：自动更新提醒（只提醒，不下载、不安装）----------

    def _maybe_check_update(self, now: float = None) -> None:
        """工作循环每轮调用：到点才触发一次检查，其余时间直接返回。

        首次检查在启动后 60 秒，之后每 6 小时；开关关闭时只推迟下次
        检查，不发请求。检查本身走独立短命线程，不阻塞探测循环。
        """
        if self.stop_event.is_set():
            return
        now = time.monotonic() if now is None else now
        if now < self._update_next_check_at:
            return
        self._update_next_check_at = now + UPDATE_CHECK_INTERVAL_S
        if not bool(self.config.get("update_check_enabled", True)):
            return
        self._check_update_async()

    def _check_update_async(self) -> None:
        """在独立短命线程里查一次 GitHub 最新 Release（不阻塞工作循环）。

        会话用专用直连会话（不与工作线程共享 Session，避免并发复用
        连接池；与 :meth:`_verify_online_async` 同一做法）；测试可经
        ``_update_session_factory`` 注入假 session。结果经
        :meth:`_on_update_checked` 处理，该方法不直接碰 Tk 控件，
        横幅由 1 秒渲染周期读取 ``_update_info`` 后决定是否显示。
        """
        if self._update_busy.is_set():
            return
        self._update_busy.set()

        def _run():
            try:
                factory = getattr(self, "_update_session_factory", None)
                session = factory() if factory is not None else direct_session()
                info, reason = check_latest(session)
                self._on_update_checked(info, reason)
            except Exception as exc:  # noqa: BLE001 - 检查不得影响工作线程
                self.log.info("[更新] 检查异常：%s", type(exc).__name__)
            finally:
                self._update_busy.clear()

        thread = threading.Thread(target=_run, daemon=True,
                                  name="campus-update")
        self._update_thread = thread
        thread.start()

    def _on_update_checked(self, info, reason: str) -> None:
        """一次检查的结果处理（后台线程调用；不直接碰 Tk）。

        失败 → 只写日志，不弹窗、不报错、不影响主流程。
        查到新版本 → 记下 ``_update_info``（仪表盘横幅与设置页读取它）；
        同一版本只弹一次气泡（``update_notified_tag`` 已记在配置里时
        不再弹）。是否显示横幅另由 ``update_dismissed_tag`` 决定。
        """
        if info is None:
            self.log.info("[更新] 检查失败：%s", reason)
            return
        self._update_info = info
        if not should_notify(info, __version__,
                             self.config.get("update_notified_tag")):
            return
        self.log.info("[更新] 发现新版本 %s（当前 v%s）",
                      info.tag, __version__)
        self.notify("发现新版本 %s" % info.tag,
                    "校园网助手有新版本可用：%s\n"
                    "点仪表盘顶部的横幅或设置页查看并下载。" % info.name)
        cfg = self.get_config()
        cfg["update_notified_tag"] = info.tag
        try:
            save_config(cfg)
        except Exception as exc:  # noqa: BLE001 - 保存失败只记日志
            self.log.warning("[更新] 已提醒版本保存失败：%s",
                             type(exc).__name__)
        self.update_config({"update_notified_tag": info.tag})

    def _update_mark_dismissed(self, tag: str) -> None:
        """用户点过“以后再说 / 下载更新”：记下该版本，横幅不再出现。"""
        cfg = self.get_config()
        cfg["update_dismissed_tag"] = tag
        try:
            save_config(cfg)
        except Exception as exc:  # noqa: BLE001 - 保存失败只记日志
            self.log.warning("[更新] 已处理版本保存失败：%s",
                             type(exc).__name__)
        self.update_config({"update_dismissed_tag": tag})

    def _update_banner_should_show(self) -> bool:
        """仪表盘更新横幅是否显示：有新版本，且用户没点过“以后再说”。"""
        info = getattr(self, "_update_info", None)
        if info is None:
            return False
        if not is_newer(info.tag, __version__):
            return False
        return info.tag != (self.config.get("update_dismissed_tag") or "")

    def _on_update_download(self) -> None:
        """横幅“下载更新”：打开浏览器到 Release 页面，不下载、不安装。"""
        info = getattr(self, "_update_info", None)
        url = info.html_url if info is not None else (
            "https://github.com/lcq666666/"
            "SNNU-school-network-reconnect-auto/releases")
        try:
            self._update_opener(url)
        except Exception as exc:  # noqa: BLE001 - 打开浏览器失败只记日志
            self.log.warning("[更新] 打开下载页面失败：%s",
                             type(exc).__name__)
            return
        if info is not None:
            self._update_mark_dismissed(info.tag)
            self._hide_update_banner()

    def _on_update_dismiss(self) -> None:
        """横幅“以后再说”：记下该版本，本次运行及以后启动都不再显示横幅。"""
        info = getattr(self, "_update_info", None)
        if info is not None:
            self._update_mark_dismissed(info.tag)
        self._hide_update_banner()

    def _hide_update_banner(self) -> None:
        """立即收起仪表盘横幅（按钮在 UI 线程调用，可直接碰 Tk）。"""
        refs = getattr(self, "_dash_ctk_widgets", None)
        if refs is None:
            return
        banner = refs.get("update_banner")
        if banner is not None:
            try:
                banner.pack_forget()
            except Exception:
                pass
        refs["update_banner_visible"] = False

    def _render_update_banner(self, refs) -> None:
        """按 1 秒渲染周期同步横幅显隐（只在状态变化时改布局）。"""
        banner = refs.get("update_banner")
        if banner is None:
            return
        show = self._update_banner_should_show()
        if show == refs.get("update_banner_visible"):
            return
        refs["update_banner_visible"] = show
        if show:
            info = self._update_info
            refs["update_banner_label"].configure(
                text="发现新版本 %s（当前 v%s）— 只提醒，不自动下载安装"
                     % (info.tag, __version__))
            anchor = refs.get("gauges_frame")
            if anchor is not None:
                banner.pack(fill="x", pady=(0, 10), before=anchor)
            else:
                banner.pack(fill="x", pady=(0, 10))
        else:
            banner.pack_forget()

    def _update_status_text(self) -> str:
        """设置页“更新”区文案：当前版本与最新版本（只显示，不下载）。"""
        info = getattr(self, "_update_info", None)
        if info is None:
            latest = "最新版本：尚未检查到（启动 60 秒后首次检查；失败只记日志）"
        elif is_newer(info.tag, __version__):
            latest = "最新版本：%s（有更新可用）" % info.tag
        else:
            latest = "最新版本：%s（已是最新）" % info.tag
        return "当前版本：v%s · %s" % (__version__, latest)

    def on_check_update_now(self, icon=None, item=None):
        """设置页“立即检查更新”：跳过定时，马上查一次（开关关闭时也可用）。

        手动检查不绕过去重逻辑：同一版本已弹过气泡的不再重复弹，
        横幅是否显示仍按 ``update_dismissed_tag`` 决定。
        """
        self._update_next_check_at = (time.monotonic()
                                      + UPDATE_CHECK_INTERVAL_S)
        self._check_update_async()

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
            pystray.MenuItem(self._link_text, None, enabled=False),
            pystray.MenuItem(self._failover_text, None, enabled=False),
            pystray.MenuItem(self._internet_text, None, enabled=False),
            pystray.MenuItem(self._uu_text, None, enabled=False),
            pystray.MenuItem(self._remote_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开主界面", self.on_dashboard),
            pystray.MenuItem("立即检测", self.on_detect_now),
            pystray.MenuItem("立即验证以太网",
                             lambda icon, item: self.on_verify_now("ethernet")),
            pystray.MenuItem("立即验证 SNNU Wi-Fi",
                             lambda icon, item: self.on_verify_now("wifi")),
            pystray.MenuItem("重启 UU Remote（手动）", self.on_restart_uu_remote),
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
            pystray.MenuItem("连上后重启 UU 远程后台", self.on_toggle_remote_restart,
                             checked=lambda item: bool(
                                 self.config.get("remote_restart_enabled", False))),
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
        """设置面板：Notebook 五页（基本 / 网络 / 恢复 / UU Remote / 诊断，方案 §5.5）。

        面板**只读写配置**，不发起任何网络操作；网络动作一律经后台线程入口
        （:meth:`on_detect_now` / :meth:`on_verify_now` / :meth:`on_reconnect` /
        :meth:`on_restart_uu_remote`）。保存时**只提交用户实际变更的项**（F12/T24）。
        """
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
        win.title("校园网自动重连助手 · 设置")
        win.resizable(False, False)
        outer = ttk.Frame(win, padding=10)
        outer.grid(sticky="nsew")
        notebook = ttk.Notebook(outer)
        notebook.grid(sticky="nsew")

        pad = {"padx": 4, "pady": 3}

        def page(title):
            frame = ttk.Frame(notebook, padding=12)
            notebook.add(frame, text=title)
            return frame

        # ---- 变量：取值与保存逻辑一一对应，默认取当前配置 ----
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
        # V0.4-3 / V0.5-3：确认真正连通后清退重启 UU 远程（默认关闭）。
        v_restart = tk.BooleanVar(
            value=bool(self.config.get("remote_restart_enabled", False)))
        v_restart_cd = tk.IntVar(
            value=self._int_or(self.config.get("remote_restart_cooldown"), 300))
        # V0.3 P7.5-1A：三轴观测参数。
        v_snap_age = tk.IntVar(value=self._int_or(self.config.get("snapshot_max_age"), 8))
        v_snap_timeout = tk.IntVar(
            value=self._int_or(self.config.get("snapshot_timeout"), 12))
        v_snap_interval = tk.IntVar(
            value=self._int_or(self.config.get("snapshot_interval"), 3))
        v_ssid = tk.StringVar(value=self.config.get("ssid") or "SNNU")
        yys_value = self.config.get("yys", "mobile")
        if yys_value not in YYS_VALUE_TO_LABEL:
            yys_value = "mobile"
        v_yys = tk.StringVar(value=YYS_VALUE_TO_LABEL[yys_value])
        v_interval = tk.IntVar(value=self._int_or(self.config.get("interval"), 15))
        v_thr = tk.IntVar(value=self._int_or(self.config.get("threshold"), 3))
        v_cd = tk.IntVar(value=self._int_or(self.config.get("cooldown"), 180))
        # V0.6.2：自动更新提醒开关（默认开启；只提醒，不下载、不安装）。
        v_update = tk.BooleanVar(
            value=bool(self.config.get("update_check_enabled", True)))

        # ---- 页 1：基本 ----
        basic = page("基本")
        ttk.Checkbutton(basic, text="开机自启（登录 Windows 后自动启动）",
                        variable=v_auto).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(basic, text="重连成功后系统代理").grid(row=1, column=0, sticky="e", **pad)
        ttk.Combobox(basic, textvariable=v_consent, width=16, state="readonly",
                     values=CONSENT_LABELS).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(basic, text="代理端点 host:port").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(basic, textvariable=v_endpoint, width=20).grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Checkbutton(basic, text="重连失败时自动打开 Edge 兜底",
                        variable=v_fallback).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Separator(basic, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(basic, text="Wi-Fi 名称").grid(row=5, column=0, sticky="e", **pad)
        ttk.Entry(basic, textvariable=v_ssid, width=20).grid(row=5, column=1, sticky="w", **pad)
        ttk.Label(basic, text="运营商").grid(row=6, column=0, sticky="e", **pad)
        ttk.Combobox(basic, textvariable=v_yys, width=15, state="readonly",
                     values=YYS_LABELS).grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(basic, text="检测间隔（秒）").grid(row=7, column=0, sticky="e", **pad)
        ttk.Spinbox(basic, from_=5, to=120, textvariable=v_interval, width=18).grid(
            row=7, column=1, sticky="w", **pad)
        ttk.Label(basic, text="连续失败阈值").grid(row=8, column=0, sticky="e", **pad)
        ttk.Spinbox(basic, from_=1, to=10, textvariable=v_thr, width=18).grid(
            row=8, column=1, sticky="w", **pad)
        ttk.Label(basic, text="重连冷却（秒）").grid(row=9, column=0, sticky="e", **pad)
        ttk.Spinbox(basic, from_=60, to=3600, textvariable=v_cd, width=18).grid(
            row=9, column=1, sticky="w", **pad)

        # ---- 页 2：网络 ----
        net = page("网络")
        ttk.Label(net, text="双网络故障切换（V0.3 / V0.5）",
                  font=("Microsoft YaHei", 10, "bold")).grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(net, text="运行模式").grid(row=1, column=0, sticky="e", **pad)
        ttk.Combobox(net, textvariable=v_fmode, width=15, state="readonly",
                     values=[tray_mode_label(m) for m in MODE_ORDER]).grid(
                         row=1, column=1, sticky="w", **pad)
        ttk.Label(net, text="锁定目标").grid(row=2, column=0, sticky="e", **pad)
        ttk.Combobox(net, textvariable=v_flock, width=15, state="readonly",
                     values=["不锁定", PATH_LABEL["ethernet"],
                             PATH_LABEL["wifi"]]).grid(
                         row=2, column=1, sticky="w", **pad)
        ttk.Checkbutton(net, text="网络切换期间保持 Clash 原样（默认）",
                        variable=v_clash_keep).grid(
                            row=3, column=0, columnspan=2, sticky="w")
        ttk.Separator(net, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(net, text="三轴观测（P7.5-1A）",
                  font=("Microsoft YaHei", 10, "bold")).grid(
                      row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(net, text="快照有效期（秒）").grid(row=6, column=0, sticky="e", **pad)
        ttk.Spinbox(net, from_=2, to=60, textvariable=v_snap_age, width=18).grid(
            row=6, column=1, sticky="w", **pad)
        ttk.Label(net, text="采集超时（秒）").grid(row=7, column=0, sticky="e", **pad)
        ttk.Spinbox(net, from_=3, to=60, textvariable=v_snap_timeout, width=18).grid(
            row=7, column=1, sticky="w", **pad)
        ttk.Label(net, text="采集最小间隔（秒）").grid(row=8, column=0, sticky="e", **pad)
        ttk.Spinbox(net, from_=1, to=60, textvariable=v_snap_interval, width=18).grid(
            row=8, column=1, sticky="w", **pad)

        # ---- 页 3：恢复 ----
        recover = page("恢复")
        ttk.Label(recover, text="远程保障与切回策略",
                  font=("Microsoft YaHei", 10, "bold")).grid(
                      row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Checkbutton(recover, text="远控探测（远程保障，只读）",
                        variable=v_remote).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(recover, text="远程会话进行中推迟自动切回",
                        variable=v_defer).grid(row=2, column=0, sticky="w")
        ttk.Separator(recover, orient="horizontal").grid(
            row=3, column=0, sticky="ew", pady=6)
        failover_status = getattr(self, "failover_status", None)
        failover_text = ("\n".join(failover_status.lines()) if failover_status
                         is not None else "故障切换：等待首次观测（模式 %s）"
                         % tray_mode_label(self._current_mode()))
        ttk.Label(recover, text=failover_text, justify="left").grid(
            row=4, column=0, sticky="w")

        # ---- 页 4：UU Remote ----
        uu = page("UU Remote")
        ttk.Label(uu, text="网易 GameViewer（UU 远程）自动恢复",
                  font=("Microsoft YaHei", 10, "bold")).grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Checkbutton(
            uu, text="网络恢复且接口受限验证通过后，自动清退重启 UU 远程（默认关闭）",
            variable=v_restart).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(uu, text="两次清退最小间隔（秒）").grid(
            row=2, column=0, sticky="e", **pad)
        ttk.Spinbox(uu, from_=60, to=3600, textvariable=v_restart_cd, width=18).grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Label(uu, foreground="#8b949e", justify="left",
                  text=("只处理当前用户的 GameViewer / GameViewerLauncher 用户态进程；\n"
                        "不动 GameViewerService，也不重启 Windows 服务。\n"
                        "真机首次清退仍会再次当面确认。")).grid(
                            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(uu, text="重启 UU Remote（手动）",
                   command=self.on_restart_uu_remote).grid(
                       row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # ---- 页 5：诊断 ----
        diag = page("诊断")
        status = self.worker_status()
        ttk.Label(diag, text="运行状态", font=("Microsoft YaHei", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(diag, text=self._status_text()).grid(
            row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(diag, text="监测：%s" % status["label"]).grid(
            row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(diag, text="上次重连成功：%s" % (self.state.get("last_ok") or "—")).grid(
            row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(diag, text="\n".join(self.ui_state_lines()), justify="left",
                  foreground="#8b949e", wraplength=520).grid(
                      row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        # V0.5-6：最近一次故障事件的可复盘时间线（断网时刻 / 恢复时刻 / 耗时）。
        ttk.Label(diag, text="\n".join(self.timeline_report_lines()), justify="left",
                  foreground="#8b949e", wraplength=520).grid(
                      row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(diag, text="立即检测", command=self.on_detect_now).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(diag, text="立即验证以太网",
                   command=lambda: self.on_verify_now("ethernet")).grid(
                       row=7, column=0, columnspan=2, sticky="w")
        ttk.Button(diag, text="立即验证 SNNU Wi-Fi",
                   command=lambda: self.on_verify_now("wifi")).grid(
                       row=8, column=0, columnspan=2, sticky="w")
        ttk.Button(diag, text="打开日志", command=self.on_open_log).grid(
            row=9, column=0, columnspan=2, sticky="w")
        # V0.6.2：软件更新（只显示版本，不下载、不安装）。
        ttk.Separator(diag, orient="horizontal").grid(
            row=10, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(diag, text="软件更新", font=("Microsoft YaHei", 10, "bold")).grid(
            row=11, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(
            diag, text="自动检查更新（启动 60 秒后首次检查，之后每 6 小时）",
            variable=v_update).grid(row=12, column=0, columnspan=2, sticky="w")
        ttk.Label(diag, text=self._update_status_text(), justify="left",
                  foreground="#8b949e", wraplength=520).grid(
                      row=13, column=0, columnspan=2, sticky="w")
        ttk.Button(diag, text="立即检查更新",
                   command=self.on_check_update_now).grid(
                       row=14, column=0, columnspan=2, sticky="w")

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
            "remote_restart_enabled": bool(
                self.config.get("remote_restart_enabled", False)),
            "remote_restart_cooldown": self._int_or(
                self.config.get("remote_restart_cooldown"), 300),
            "snapshot_max_age": self._int_or(self.config.get("snapshot_max_age"), 8),
            "snapshot_timeout": self._int_or(self.config.get("snapshot_timeout"), 12),
            "snapshot_interval": self._int_or(self.config.get("snapshot_interval"), 3),
            "update_check_enabled": bool(
                self.config.get("update_check_enabled", True)),
        }

        def on_save():
            try:
                interval = max(5, int(v_interval.get()))
                threshold = max(1, int(v_thr.get()))
                cooldown = max(60, int(v_cd.get()))
                restart_cd = max(60, int(v_restart_cd.get()))
                snap_age = max(2, int(v_snap_age.get()))
                snap_timeout = max(3, int(v_snap_timeout.get()))
                snap_interval = max(1, int(v_snap_interval.get()))
            except (tk.TclError, ValueError):
                messagebox.showwarning("输入无效", "间隔/阈值/冷却/观测参数必须是数字")
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
            # V0.5-3：UU 远程自动恢复开关与冷却。
            if bool(v_restart.get()) != initial["remote_restart_enabled"]:
                changes["remote_restart_enabled"] = bool(v_restart.get())
            if restart_cd != initial["remote_restart_cooldown"]:
                changes["remote_restart_cooldown"] = restart_cd
            if snap_age != initial["snapshot_max_age"]:
                changes["snapshot_max_age"] = snap_age
            if snap_timeout != initial["snapshot_timeout"]:
                changes["snapshot_timeout"] = snap_timeout
            if snap_interval != initial["snapshot_interval"]:
                changes["snapshot_interval"] = snap_interval
            # V0.6.2：自动更新提醒开关。
            if bool(v_update.get()) != initial["update_check_enabled"]:
                changes["update_check_enabled"] = bool(v_update.get())

            if changes:
                new_cfg = self.get_config()
                new_cfg.update(changes)
                if not save_config(new_cfg):
                    messagebox.showerror("保存失败",
                                         "配置写入失败，已保留原文件。请检查目录权限后重试。")
                    return
                self.update_config(changes)
                if any(key in changes for key in
                       ("snapshot_max_age", "snapshot_timeout", "snapshot_interval")):
                    # 观测源按旧参数构造，参数变更后丢弃重建（避免"改了不生效"）。
                    self._snapshot_source = None
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

        footer = ttk.Frame(outer, padding=(0, 8, 0, 0))
        footer.grid(sticky="ew")
        ttk.Button(footer, text="保存", command=on_save).grid(row=0, column=0)
        ttk.Button(footer, text="立即重连", command=on_reconnect_click).grid(
            row=0, column=1, padx=6)
        ttk.Button(footer, text="暂停/恢复", command=on_pause_click).grid(row=0, column=2)
        ttk.Button(footer, text="关闭", command=win.destroy).grid(row=0, column=3, padx=6)
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
        threading.Timer(5.0, self._prompt_auto_switch_once).start()
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


def _show_startup_error(detail: str) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        show = ctypes.WinDLL("user32").MessageBoxW
        show.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
        show.restype = ctypes.c_int
        # MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST：不依赖已隐藏的 Tk 根窗口。
        show(None, detail, APP_NAME, 0x30 | 0x10000 | 0x40000)
    else:
        messagebox.showerror(APP_NAME, detail)


def _startup_check(report: Path) -> int:
    """验证打包后的仪表盘，不读取用户配置或凭据，不启动监测。"""
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = TrayApp(logging.getLogger("campus.startup_check"), config=DEFAULT_CONFIG)
        app.root = root
        # 发布包必须能打开深色仪表盘；不走可能掩盖打包错误的 ttk 回退路径。
        if ctk is None:
            raise RuntimeError("CustomTkinter failed to import")
        callback_errors = []
        root.report_callback_exception = lambda *error: callback_errors.append(
            "".join(traceback.format_exception(*error)))
        app._open_dashboard_ctk()
        # CTk 在 Windows 用延时回调恢复窗口并设置图标，必须实际跑 Tk 事件循环。
        root.after(500, root.quit)
        root.mainloop()
        if callback_errors:
            raise RuntimeError(callback_errors[0])
        if app.dashboard_win is None or not app.dashboard_win.winfo_viewable():
            raise RuntimeError("Dashboard window was not displayed")
        app._close_dashboard()
        report.write_text("ok\n", encoding="utf-8")
        return 0
    except Exception:
        report.write_text(traceback.format_exc(), encoding="utf-8")
        return 1
    finally:
        if root is not None:
            root.destroy()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(BACKGROUND_FLAG, action="store_true", help="在托盘后台启动")
    parser.add_argument("--startup-check", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.startup_check is not None:
        return _startup_check(args.startup_check)

    log = setup_logging(False)
    instance = engine.claim_single_instance(log)
    if not instance.ok:
        if not args.background:
            if instance.already_running:
                detail = ("已有一份校园网助手在运行。\n\n"
                          "要打开正在运行的助手，请点击任务栏时钟旁的 ^，找到校园网助手图标。\n"
                          "要升级，请在该图标上右键选择「退出」，先退出旧版，再双击新版。\n"
                          "升级后，可在新版里重新开启「开机自启」。")
            else:
                detail = "无法启用单实例保护，监测未启动。请查看日志后重试。"
            _show_startup_error(detail)
        return 1
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
    if not args.background:
        app.on_dashboard()
    root.mainloop()
    log.info("托盘助手已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
