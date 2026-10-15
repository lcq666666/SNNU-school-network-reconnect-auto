# -*- coding: utf-8 -*-
"""V0.3 P7 远程保障与远程重连验证（campus_remote）。

本模块实现主方案 §12-P4「远程保障」与执行计划 §0.2 中 P7 的行级定义：

> **P7 远程保障与远程重连验证**：远控探测、敏感期延迟切回、崩溃恢复；
> 允许真实副作用（受控、真实远控会话）；前置依赖 P5。

职责边界
========

1. **远控探测**（方案 §7.3）：检测远控客户端进程、其监听/已建立连接与 UDP 端点，
   给出「空闲 / 会话中」结论。**只读**，不做任何网络动作，也不强杀远控进程
   （AGENTS §3.12：``kill_remote_client`` 属危险操作黑名单，本模块无任何入口）。
2. **敏感期延迟切回**（方案 §4.3：「如果检测到敏感远程会话，允许推迟自动切回」）：
   以 :meth:`RemoteSafetyGuard.failback_gate` 形式注入 P3 状态机 —— 远程会话进行中
   时**推迟**自动切回有线；会话结束后仍需经过一段**静默窗口**才放行，避免刚切回
   就被再次连入而反复抖动。为免永久不切回，推迟时间有上限，超限后放行并告警。
3. **崩溃恢复**（方案 §7.2：「应有独立于主监控线程的本机恢复机制」）：
   :class:`MonitorWatchdog` 以「心跳新鲜度 + 时间跳变」判定监测线程是否死亡或
   系统是否从睡眠/待机恢复，给出可执行的决策；:func:`apply_watchdog_decision`
   按决策调用**注入的**恢复动作（重启监测线程、路由崩溃恢复），本身不做 I/O。
4. **无人值守就绪自检**（方案 §7.3）：睡眠/休眠、自动登录、远控与托盘自启、
   凭据在位、远控自动重连（人工项）逐项检查，输出可审计清单。
5. **日志关联**（方案 §12-P4「Wi-Fi、路由、Portal 与远程控制日志关联」）：
   :class:`RemoteEventLog` 以 ``correlation_id`` 把四类事件串成同一时间线；
   :func:`record_tick_events` 把一轮 :class:`campus_failover.TickResult` 转成事件。

依赖注入
========

全部 I/O 经 :class:`RemoteOps` 注入（进程/端口/睡眠/登录/自启/凭据）；默认测试集合
注入假件即可实现**零真实网络 / 零真实子进程 / 零真实注册表 / 零真实凭据读取**。
真实实现 :func:`build_windows_remote_ops` 复用 P0 ``netdiag`` **只读**管道
（``PowerShellRunner`` 逐命令 ``assert_read_only`` fail-closed），不新增任何写命令。

测试分级：``U-7-*`` 单元 / 离线 Mock；``I-7-*`` 离线集成闭合回路。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from campus_failover import OpKind

__all__ = [
    "CONTRACT_VERSION", "EVENT_LOG_CONTRACT_VERSION", "ESTABLISHED_STATE",
    "LISTEN_STATE", "LOG_KINDS", "WATCHDOG_ACTIONS", "DEFAULT_REMOTE_CLIENTS",
    "REMOTE_TRAY_MARKERS", "RemoteClientSpec", "RemoteProbe", "RemoteActivity",
    "RemoteStatus", "RemoteReadinessItem", "ReadinessReport", "RemoteOps",
    "RemoteSafetyGuard", "WatchdogDecision", "MonitorWatchdog",
    "RemoteEventLog", "record_tick_events", "apply_watchdog_decision",
    "build_windows_remote_ops", "format_remote_status", "format_readiness",
]

CONTRACT_VERSION = "v03-p7-remote-1"
EVENT_LOG_CONTRACT_VERSION = "v03-p7-remotelog-1"

#: TCP 连接状态：仅 ``Established`` 视为**正在进行的远控会话**。
ESTABLISHED_STATE = "Established"
#: 监听态：说明远控客户端**可能**被连入，但不等于会话进行中（Tailscale 常驻监听）。
LISTEN_STATE = "Listen"

#: 关联日志的事件类别（方案 §12-P4：Wi-Fi / 路由 / Portal / 远控 关联）。
LOG_KINDS: Tuple[str, ...] = ("wifi", "route", "portal", "remote", "system")

#: 看门狗可执行的动作。
WATCHDOG_ACTIONS: Tuple[str, ...] = (
    "OK", "RESUME_RECHECK", "RESTART_MONITOR", "MANUAL_REQUIRED",
)

#: 托盘自启项识别关键字（无人值守就绪自检用）。
REMOTE_TRAY_MARKERS = ("campusreconnect", "campus_reconnect")


@dataclass(frozen=True)
class RemoteClientSpec:
    """一个远控客户端的识别规则（按**进程名**子串匹配，不依赖显示名）。"""

    key: str
    label: str
    patterns: Tuple[str, ...]

    def matches(self, process_name: Any) -> bool:
        """进程名是否命中该客户端（大小写不敏感子串匹配）。"""
        name = str(process_name or "").strip().lower()
        if not name:
            return False
        return any(pattern in name for pattern in self.patterns)


#: 已知远控客户端（含本机实测在用的 Tailscale 与向日葵 AweSun，可按需扩充）。
DEFAULT_REMOTE_CLIENTS: Tuple[RemoteClientSpec, ...] = (
    RemoteClientSpec("tailscale", "Tailscale", ("tailscaled", "tailscale")),
    RemoteClientSpec("awesun", "向日葵 AweSun",
                     ("awesun", "sunloginclient", "sunlogin")),
    RemoteClientSpec("todesk", "ToDesk", ("todesk", "tmremoteclient")),
    RemoteClientSpec("anydesk", "AnyDesk", ("anydesk",)),
    RemoteClientSpec("rustdesk", "RustDesk", ("rustdesk",)),
    RemoteClientSpec("teamviewer", "TeamViewer", ("teamviewer",)),
    RemoteClientSpec("parsec", "Parsec", ("parsec",)),
    RemoteClientSpec("rdp", "远程桌面（RDP）", ("rdpclip",)),
)


# --------------------------------------------------------------------------- #
# 探测数据
# --------------------------------------------------------------------------- #
@dataclass
class RemoteProbe:
    """一次只读远控探测的原始数据（与 P0 诊断 ``remote_control`` 同构）。"""

    clients: List[Dict[str, Any]] = field(default_factory=list)
    ports: List[Dict[str, Any]] = field(default_factory=list)
    udp_endpoints: List[Dict[str, Any]] = field(default_factory=list)
    sleep_states: Dict[str, Any] = field(default_factory=dict)
    auto_logon: Dict[str, Any] = field(default_factory=dict)
    run_entries: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    #: 该探测是否包含"无人值守就绪"所需的完整数据（睡眠/登录/自启）。
    @property
    def has_readiness_data(self) -> bool:
        return bool(self.sleep_states or self.auto_logon or self.run_entries)

    @classmethod
    def from_mapping(cls, data: Any) -> "RemoteProbe":
        """从 ``dict`` 构造（非法/缺失字段一律按空处理，绝不抛错）。"""
        if not isinstance(data, dict):
            return cls(error="探测数据非对象")
        return cls(
            clients=[dict(row) for row in (data.get("clients") or [])
                     if isinstance(row, dict)],
            ports=[dict(row) for row in (data.get("ports") or [])
                   if isinstance(row, dict)],
            udp_endpoints=[dict(row) for row in (data.get("udp_endpoints") or [])
                           if isinstance(row, dict)],
            sleep_states=dict(data.get("sleep_states") or {}),
            auto_logon=dict(data.get("auto_logon") or {}),
            run_entries=[dict(row) for row in (data.get("run_entries") or [])
                         if isinstance(row, dict)],
            error=data.get("error"))

    @classmethod
    def from_report(cls, report: Any) -> "RemoteProbe":
        """从 P0 ``netdiag.DiagReport`` 构造（只读诊断报告的 ``remote_control``）。"""
        if report is None:
            return cls(error="诊断报告缺失")
        return cls.from_mapping(getattr(report, "remote_control", None) or {})

    def to_dict(self) -> Dict[str, Any]:
        return {"clients": [dict(c) for c in self.clients],
                "ports": [dict(p) for p in self.ports],
                "udp_endpoints": [dict(u) for u in self.udp_endpoints],
                "sleep_states": dict(self.sleep_states),
                "auto_logon": dict(self.auto_logon),
                "run_entries": [dict(r) for r in self.run_entries],
                "error": self.error}


class RemoteActivity(str):
    """远控活动结论（``str`` 子类，便于直接比较与序列化）。"""


#: 远控活动结论取值。
ACTIVITY_UNKNOWN = RemoteActivity("UNKNOWN")   # 未探测/探测失败
ACTIVITY_IDLE = RemoteActivity("IDLE")         # 无远控会话
ACTIVITY_ACTIVE = RemoteActivity("ACTIVE")     # 远控会话进行中（敏感期）


@dataclass
class RemoteStatus:
    """远控状态与敏感期结论。"""

    contract_version: str = CONTRACT_VERSION
    activity: RemoteActivity = ACTIVITY_UNKNOWN
    keys: List[str] = field(default_factory=list)
    clients: List[str] = field(default_factory=list)
    listening: bool = False
    established: int = 0
    udp_endpoints: int = 0
    sensitive: bool = False
    reason: str = ""
    at: Optional[float] = None
    probe_error: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.activity == ACTIVITY_ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        return {"contract_version": self.contract_version,
                "activity": str(self.activity), "keys": list(self.keys),
                "clients": list(self.clients), "listening": self.listening,
                "established": self.established,
                "udp_endpoints": self.udp_endpoints,
                "sensitive": self.sensitive, "reason": self.reason,
                "at": self.at, "probe_error": self.probe_error}

    def lines(self) -> List[str]:
        lines = ["远控状态    : %s%s" % (self.activity,
                                      "（远程会话进行中）" if self.sensitive else ""),
                 "远控客户端  : %s" % (", ".join(self.clients) or "未检测到已知客户端"),
                 "远控连接    : 已建立 %d 条；监听 %s；UDP 端点 %d 个"
                 % (self.established, "有" if self.listening else "无",
                    self.udp_endpoints)]
        if self.reason:
            lines.append("判定依据    : %s" % self.reason)
        if self.probe_error:
            lines.append("探测告警    : %s" % self.probe_error)
        return lines


# --------------------------------------------------------------------------- #
# 无人值守就绪自检
# --------------------------------------------------------------------------- #
@dataclass
class RemoteReadinessItem:
    """一项无人值守就绪检查结果。"""

    key: str
    label: str
    ok: bool
    detail: str = ""
    manual: bool = False
    severity: str = "info"

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "ok": self.ok,
                "detail": self.detail, "manual": self.manual,
                "severity": self.severity}


@dataclass
class ReadinessReport:
    """无人值守就绪清单（方案 §7.3）。"""

    contract_version: str = CONTRACT_VERSION
    items: List[RemoteReadinessItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """全部**可自动判定**的项通过时为 ``True``（人工项不参与判定）。"""
        return all(item.ok for item in self.items if not item.manual)

    @property
    def warnings(self) -> List[str]:
        return ["%s：%s" % (item.label, item.detail)
                for item in self.items if not item.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {"contract_version": self.contract_version, "ok": self.ok,
                "items": [item.to_dict() for item in self.items],
                "warnings": self.warnings}


# --------------------------------------------------------------------------- #
# 注入式能力集合
# --------------------------------------------------------------------------- #
@dataclass
class RemoteOps:
    """P7 依赖的只读能力（全部可注入，便于离线测试）。

    - ``probe()``：轻量探测（进程 + 端口 + UDP），用于周期性 tick；
    - ``probe_readiness()``：完整探测（额外含睡眠/自动登录/自启项），用于就绪自检；
    - ``credentials_present()``：凭据是否在位（**只判断存在性，不读取明文**）。
    """

    probe: Callable[[], RemoteProbe]
    probe_readiness: Optional[Callable[[], RemoteProbe]] = None
    credentials_present: Callable[[], bool] = lambda: True


def _clients_of(probe: RemoteProbe, clients: Tuple[RemoteClientSpec, ...]
                ) -> Tuple[List[str], List[str], List[Dict[str, Any]]]:
    """按客户端规格匹配进程，返回（键、标签、命中的进程行）。"""
    keys: List[str] = []
    labels: List[str] = []
    rows: List[Dict[str, Any]] = []
    for row in probe.clients:
        name = row.get("process_name")
        for spec in clients:
            if spec.matches(name):
                rows.append(dict(row))
                if spec.key not in keys:
                    keys.append(spec.key)
                    labels.append(spec.label)
                break
    return keys, labels, rows


# --------------------------------------------------------------------------- #
# 远程安全守卫
# --------------------------------------------------------------------------- #
class RemoteSafetyGuard:
    """远程保障守卫：远控探测 + 敏感期判定 + 切回门禁 + 就绪自检。

    **纯逻辑 + 注入式 I/O**：本类不直接读进程/注册表/网络，全部经
    :class:`RemoteOps`；且**没有任何副作用能力**（不写路由、不认证、不触碰 Clash、
    不杀进程），只向 P3 状态机提供 ``failback_gate`` 判定。

    敏感期判据（保守但不过度）：存在**已建立**（``Established``）的远控 TCP 连接；
    仅监听不算会话进行中（否则 Tailscale 常驻监听会导致永不切回），但会在状态中
    如实上报 ``listening``。UDP 端点同样只上报、不判定（无法可靠识别会话状态）。
    """

    def __init__(self, *, ops: Optional[RemoteOps] = None,
                 clients: Tuple[RemoteClientSpec, ...] = DEFAULT_REMOTE_CLIENTS,
                 quiet_seconds: float = 60.0, max_defer_seconds: float = 1800.0,
                 probe_interval: float = 30.0, defer_failback: bool = True,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.ops = ops
        self.clients = tuple(clients)
        self.quiet_seconds = float(quiet_seconds)
        self.max_defer_seconds = float(max_defer_seconds)
        self.probe_interval = float(probe_interval)
        self.defer_failback = bool(defer_failback)
        self._log = log
        self._status = RemoteStatus()
        self._last_probe: Optional[RemoteProbe] = None
        self._last_probe_at: Optional[float] = None
        self._last_sensitive_at: Optional[float] = None
        self._defer_since: Optional[float] = None
        #: 最近一次无人值守就绪自检结果（供托盘/CLI 展示；未执行时为 ``None``）。
        self.last_readiness: Optional[ReadinessReport] = None
        self.probe_count = 0
        self.defer_count = 0

    # ---- 探测 ---- #
    def poll(self, now: float) -> RemoteStatus:
        """按 ``probe_interval`` 节流地执行一次**轻量**探测并更新状态。

        未到间隔时直接返回上次状态（零 I/O）；无 ``ops`` 时返回 ``UNKNOWN``。
        """
        if self.ops is None:
            return self._status
        if (self._last_probe_at is not None
                and (now - self._last_probe_at) < self.probe_interval):
            return self._status
        self._last_probe_at = now
        self.probe_count += 1
        try:
            probe = self.ops.probe()
        except Exception as exc:                       # 探测故障不得上抛
            probe = RemoteProbe(error="%s: %s" % (type(exc).__name__, exc))
        return self.observe(probe, now)

    def observe(self, probe: RemoteProbe, now: float) -> RemoteStatus:
        """依据一次探测结果更新远控状态（不执行任何动作）。"""
        self._last_probe = probe
        keys, labels, _rows = _clients_of(probe, self.clients)
        listening = any(str(row.get("state") or "") == LISTEN_STATE
                        for row in probe.ports)
        established_rows = [row for row in probe.ports
                            if str(row.get("state") or "") == ESTABLISHED_STATE]
        established = len(established_rows)

        if probe.error:
            activity = ACTIVITY_UNKNOWN
            sensitive = True
            reason = "探测失败：远控状态 UNKNOWN，按保守策略保持敏感期"
        elif established:
            activity = ACTIVITY_ACTIVE
            sensitive = True
            reason = ("检测到 %d 条已建立的远控连接（%s）"
                      % (established,
                         ", ".join(sorted({str(r.get("remote_address") or "-")
                                           for r in established_rows}))))
        elif probe.udp_endpoints and keys:
            activity = ACTIVITY_UNKNOWN
            sensitive = True
            reason = "检测到远控客户端与 UDP 端点，无法确认会话状态，按保守策略处理"
        else:
            activity = ACTIVITY_IDLE
            sensitive = False
            reason = ("无已建立的远控会话（客户端 %s）"
                      % (", ".join(labels) if labels
                         else "未检测到已知客户端"))

        if sensitive:
            self._last_sensitive_at = now
            if self._defer_since is None:
                # 推迟计时从**敏感期开始**计（而非第一次询问门禁），
                # 因此 max_defer 上限反映"会话已持续多久"。
                self._defer_since = now
        else:
            self._defer_since = None
        status = RemoteStatus(activity=activity, keys=keys, clients=labels,
                              listening=listening, established=established,
                              udp_endpoints=len(probe.udp_endpoints),
                              sensitive=sensitive, reason=reason, at=now,
                              probe_error=probe.error)
        if sensitive != self._status.sensitive:
            self._note("远控敏感期变更：%s（%s）"
                       % ("进行中" if sensitive else "结束", reason))
        self._status = status
        return status

    def status(self) -> RemoteStatus:
        return self._status

    @property
    def last_probe(self) -> Optional[RemoteProbe]:
        return self._last_probe

    # ---- 切回门禁（注入 P3 状态机） ---- #
    def failback_gate(self, now: float) -> Tuple[bool, str]:
        """切回门禁：``(allowed, reason)``（P3 ``failback_gate`` 协议）。

        - 未启用延迟（``defer_failback=False``）→ 一律放行；
        - 远程会话进行中 → **推迟**，直至推迟时长达到 ``max_defer_seconds``
          上限后放行并告警（避免有线恢复后永不切回）；
        - 会话刚结束 → 需经过 ``quiet_seconds`` 静默窗口才放行（防抖）。
        """
        if not self.defer_failback:
            return True, "未启用「远程会话敏感期推迟切回」"
        status = self._status
        if status.sensitive:
            if self._defer_since is None:
                # 未 observe 到敏感期起点时以本次询问为起点兜底（异常路径）。
                self._defer_since = now
            deferred = max(0.0, now - self._defer_since)
            self.defer_count += 1
            if deferred >= self.max_defer_seconds:
                return False, ("远控状态仍为敏感/未知：超过等待上限，转人工确认，"
                               "不自动强制切回")
            return False, ("远程会话敏感期（%s）：推迟自动切回 %.0fs/%.0fs"
                           % (status.reason, deferred, self.max_defer_seconds))
        self._defer_since = None
        if (self._last_sensitive_at is not None
                and (now - self._last_sensitive_at) < self.quiet_seconds):
            remaining = self.quiet_seconds - (now - self._last_sensitive_at)
            self.defer_count += 1
            return False, ("远程会话刚结束，静默窗口剩余 %.0fs：暂不切回（防抖）"
                           % remaining)
        return True, "无远程会话敏感期：允许切回"

    # ---- 就绪自检 ---- #
    def readiness(self, probe: Optional[RemoteProbe] = None
                  ) -> ReadinessReport:
        """无人值守就绪自检（方案 §7.3）：全部**只读**判定。"""
        data = probe
        if data is None and self.ops is not None and self.ops.probe_readiness:
            try:
                data = self.ops.probe_readiness()
            except Exception as exc:
                data = RemoteProbe(error="%s: %s" % (type(exc).__name__, exc))
        if data is None:
            data = self._last_probe
        items: List[RemoteReadinessItem] = []
        if data is None:
            items.append(RemoteReadinessItem(
                "probe", "远控探测", False, "未采集到探测数据", severity="warn"))
            report = ReadinessReport(items=items)
            self.last_readiness = report
            return report

        items.append(self._sleep_item(data))
        items.append(self._logon_item(data))
        items.append(self._remote_client_item(data))
        items.append(self._tray_autostart_item(data))
        items.append(self._credentials_item())
        items.append(RemoteReadinessItem(
            "external_recovery", "外部进程恢复机制", False,
            "进程内看门狗无法应对整个进程崩溃：需人工配置并验证独立启动/服务恢复机制",
            manual=True, severity="manual"))
        items.append(RemoteReadinessItem(
            "remote_reconnect", "远控客户端无人值守/自动重连", False,
            "需人工确认：远控客户端允许无人值守访问，且断线后能自动重连"
            "（无法程序化验证）",
            manual=True, severity="manual"))
        if data.error:
            items.append(RemoteReadinessItem(
                "probe_error", "探测完整性", False, data.error, severity="warn"))
        report = ReadinessReport(items=items)
        self.last_readiness = report
        return report

    def _sleep_item(self, probe: RemoteProbe) -> RemoteReadinessItem:
        sleep = probe.sleep_states or {}
        standby = sleep.get("standby_available")
        hibernate = sleep.get("hibernate_available")
        if standby is None and hibernate is None:
            return RemoteReadinessItem(
                "sleep", "睡眠/休眠", False,
                "睡眠状态查询失败或无输出：请在电源选项中人工确认已关闭睡眠/休眠",
                severity="warn")
        ac_timeout = sleep.get("sleep_timeout_ac")
        dc_timeout = sleep.get("sleep_timeout_dc")
        if (standby or hibernate) and (ac_timeout not in (0, "0")
                                        or dc_timeout not in (0, "0")):
            return RemoteReadinessItem(
                "sleep", "睡眠/休眠", False,
                "睡眠能力可用且实际电源计划存在超时配置：需关闭睡眠/休眠",
                severity="warn")
        if standby or hibernate:
            return RemoteReadinessItem(
                "sleep", "睡眠/休眠", True,
                "系统支持睡眠但实际交流/电池超时均为 0（不自动睡眠）")
        return RemoteReadinessItem(
            "sleep", "睡眠/休眠", True, "睡眠/休眠均不可用（不会因待机失联）")

    def _logon_item(self, probe: RemoteProbe) -> RemoteReadinessItem:
        logon = probe.auto_logon or {}
        if logon.get("auto_admin_logon"):
            return RemoteReadinessItem(
                "auto_logon", "自动登录", True,
                "AutoAdminLogon 已启用（用户=%s）" % (logon.get("user") or "?"))
        return RemoteReadinessItem(
            "auto_logon", "自动登录", False,
            "AutoAdminLogon 未配置：Windows 重启后不会自动进入用户会话，"
            "托盘与远控客户端不会自启", severity="warn")

    def _remote_client_item(self, probe: RemoteProbe) -> RemoteReadinessItem:
        keys, labels, _rows = _clients_of(probe, self.clients)
        autostart = self._autostart_entries(probe)
        running = ", ".join(labels) if labels else "未运行"
        if labels:
            detail = "已检测到运行中的远控客户端：%s" % running
            if autostart:
                detail += "；自启项：%s" % ", ".join(autostart)
            return RemoteReadinessItem("remote_client", "远控客户端", True, detail)
        if autostart:
            return RemoteReadinessItem(
                "remote_client", "远控客户端", True,
                "当前未运行，但存在自启项（%s）：重启后可由系统拉起"
                % ", ".join(autostart))
        return RemoteReadinessItem(
            "remote_client", "远控客户端", False,
            "未检测到已知远控客户端进程，也未发现对应自启项：无人值守将无法远程接入",
            severity="warn")

    def _tray_autostart_item(self, probe: RemoteProbe) -> RemoteReadinessItem:
        found = [entry for entry in probe.run_entries
                 if any(marker in str(entry.get("command") or "").lower()
                        or marker in str(entry.get("name") or "").lower()
                        for marker in REMOTE_TRAY_MARKERS)]
        if found:
            return RemoteReadinessItem(
                "tray_autostart", "托盘自启", True,
                "已配置自启项：%s"
                % ", ".join(str(e.get("name") or "?") for e in found))
        return RemoteReadinessItem(
            "tray_autostart", "托盘自启", False,
            "未发现 CampusReconnectTray 自启项：重启后监测不会自动运行",
            severity="warn")

    def _credentials_item(self) -> RemoteReadinessItem:
        if self.ops is None:
            return RemoteReadinessItem(
                "credentials", "校园网凭据", False, "未注入凭据检查能力",
                severity="warn")
        try:
            present = bool(self.ops.credentials_present())
        except Exception as exc:
            return RemoteReadinessItem(
                "credentials", "校园网凭据", False,
                "凭据检查失败：%s" % type(exc).__name__, severity="warn")
        if present:
            return RemoteReadinessItem(
                "credentials", "校园网凭据", True,
                "Windows 凭据管理器中已保存账号密码（仅判断存在性）")
        return RemoteReadinessItem(
            "credentials", "校园网凭据", False,
            "未找到已保存的校园网凭据：故障后无法自动认证", severity="warn")

    @staticmethod
    def _autostart_entries(probe: RemoteProbe) -> List[str]:
        """自启项中命中已知远控客户端的条目名（不依赖显示名）。"""
        hits: List[str] = []
        for entry in probe.run_entries:
            text = "%s %s" % (entry.get("name") or "", entry.get("command") or "")
            lowered = text.lower()
            if any(spec_pattern in lowered
                   for spec in DEFAULT_REMOTE_CLIENTS
                   for spec_pattern in spec.patterns):
                hits.append(str(entry.get("name") or "?"))
        return hits

    def _note(self, message: str) -> None:
        if self._log is not None:
            self._log(message)


# --------------------------------------------------------------------------- #
# 崩溃恢复看门狗（独立于主监控线程）
# --------------------------------------------------------------------------- #
@dataclass
class WatchdogDecision:
    """看门狗一次巡检的决策。"""

    action: str = "OK"
    reason: str = ""
    resumed: bool = False
    recover_route: bool = False
    restarts_in_window: int = 0

    @property
    def requires_action(self) -> bool:
        return self.action in ("RESTART_MONITOR", "MANUAL_REQUIRED")

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason,
                "resumed": self.resumed, "recover_route": self.recover_route,
                "restarts_in_window": self.restarts_in_window}


class MonitorWatchdog:
    """后台监控看门狗（方案 §7.2「独立于主监控线程的本机恢复机制」）。

    纯逻辑：显式接收 ``now`` / ``alive`` / ``heartbeat_age``，不做 I/O；恢复动作
    由 :func:`apply_watchdog_decision` 调用**注入的**回调完成。规则：

    1. 心跳新鲜 → ``OK``（不打扰）；
    2. 线程已死且心跳过期 → ``RESTART_MONITOR``（重启次数在窗口内超上限则转
       ``MANUAL_REQUIRED``，不无限重启）；
    3. 时间跳变（``now`` 相对上次巡检跳进 ≥ ``resume_gap``）→ ``RESUME_RECHECK``：
       系统可能从睡眠/待机恢复，立即重新检测路径（**不**做危险切换）。
    """

    def __init__(self, *, heartbeat_timeout: float = 90.0,
                 resume_gap: float = 180.0, max_restarts: int = 3,
                 restart_window: float = 1800.0,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.resume_gap = float(resume_gap)
        self.max_restarts = int(max_restarts)
        self.restart_window = float(restart_window)
        self._log = log
        self._last_check_at: Optional[float] = None
        self._restarts: List[float] = []
        self.restart_count = 0
        self.resume_count = 0

    def check(self, now: float, *, alive: bool,
              heartbeat_age: Optional[float] = None,
              pending_transaction: Optional[Callable[[], bool]] = None
              ) -> WatchdogDecision:
        """巡检一次并给出决策（不执行任何动作）。"""
        gap = (None if self._last_check_at is None
               else (now - self._last_check_at))
        self._last_check_at = now
        if gap is not None and gap >= self.resume_gap:
            self.resume_count += 1
            self._note("检测到 %.0fs 时间跳变：系统可能从睡眠/待机恢复，"
                       "立即重新检测路径" % gap)
            return WatchdogDecision(
                action="RESUME_RECHECK",
                reason="时间跳变 %.0fs ≥%.0fs（睡眠/待机恢复）"
                       % (gap, self.resume_gap),
                resumed=True)
        age = float("inf") if heartbeat_age is None else float(heartbeat_age)
        if alive and age >= self.heartbeat_timeout:
            return WatchdogDecision(
                action="RESTART_MONITOR",
                reason="监测线程仍存活但心跳已陈旧 %.0fs（>%.0fs）"
                       % (age, self.heartbeat_timeout))
        if alive:
            return WatchdogDecision(action="OK", reason="监测线程存活")
        if age < self.heartbeat_timeout:
            return WatchdogDecision(
                action="OK",
                reason="心跳 %.0fs 仍在容忍窗口 %.0fs 内"
                       % (age, self.heartbeat_timeout))
        restarts = self._prune(now)
        if restarts >= self.max_restarts:
            self._note("监测线程反复退出（%.0fs 内 %d 次）：转人工处理"
                       % (self.restart_window, restarts))
            return WatchdogDecision(
                action="MANUAL_REQUIRED",
                reason="监测线程在 %.0fs 内已重启 %d 次（≥上限 %d）"
                       % (self.restart_window, restarts, self.max_restarts),
                restarts_in_window=restarts)
        recover = False
        if pending_transaction is not None:
            try:
                recover = bool(pending_transaction())
            except Exception:
                recover = False
        return WatchdogDecision(
            action="RESTART_MONITOR",
            reason="监测线程已退出且心跳过期 %.0fs（>%.0fs）：重启监测"
                   % (age, self.heartbeat_timeout),
            recover_route=recover, restarts_in_window=restarts)

    def note_restart(self, now: float) -> int:
        """登记一次重启（用于重启频次上限）。"""
        self._restarts.append(float(now))
        self.restart_count += 1
        return self._prune(now)

    def note_recover(self, now: float) -> None:
        self._note("已执行路由崩溃恢复（独立于主监控线程）")

    def _prune(self, now: float) -> int:
        self._restarts = [at for at in self._restarts
                          if (now - at) <= self.restart_window]
        return len(self._restarts)

    def _note(self, message: str) -> None:
        if self._log is not None:
            self._log(message)


def apply_watchdog_decision(decision: WatchdogDecision, *,
                            restart: Optional[Callable[[], Any]] = None,
                            recover: Optional[Callable[[], Any]] = None,
                            watchdog: Optional[MonitorWatchdog] = None,
                            now: Optional[float] = None,
                            log: Optional[Callable[[str], None]] = None
                            ) -> Dict[str, Any]:
    """按看门狗决策执行恢复动作（回调全部注入，便于离线断言）。

    - ``RESTART_MONITOR``：先执行路由崩溃恢复（若 ``recover_route``），再重启监测；
    - ``MANUAL_REQUIRED`` / ``OK`` / ``RESUME_RECHECK``：不产生任何动作
      （``RESUME_RECHECK`` 由调用方立即重新检测路径，不触发危险切换）。
    """
    result: Dict[str, Any] = {"action": decision.action, "reason": decision.reason,
                              "recovered": None, "restarted": False,
                              "errors": []}
    if decision.action != "RESTART_MONITOR":
        return result
    if decision.recover_route and recover is not None:
        try:
            result["recovered"] = recover()
            if watchdog is not None and now is not None:
                watchdog.note_recover(now)
        except Exception as exc:
            result["errors"].append("路由恢复失败：%s: %s"
                                    % (type(exc).__name__, exc))
    if restart is not None:
        try:
            result["restarted"] = bool(restart())
            if watchdog is not None and now is not None:
                watchdog.note_restart(now)
        except Exception as exc:
            result["errors"].append("重启监测失败：%s: %s"
                                    % (type(exc).__name__, exc))
    if log is not None:
        log("看门狗：%s（%s）；恢复=%s 重启=%s"
            % (decision.action, decision.reason, result["recovered"],
               result["restarted"]))
    return result


# --------------------------------------------------------------------------- #
# 关联日志（Wi-Fi / 路由 / Portal / 远控）
# --------------------------------------------------------------------------- #
class RemoteEventLog:
    """关联日志：以 ``correlation_id`` 串联四类事件（方案 §12-P4）。

    - ``path=None``：**内存日志**（默认测试集合用），零文件写入；
    - 提供 ``path`` 时按 JSONL 追加（每行一个对象），便于人工/工具关联排查；
    - 只记录**事件类别与说明**，绝不记录凭据、Cookie 或密码（AGENTS §6）。
    """

    def __init__(self, path: Optional[Any] = None, *,
                 stamp: Optional[Callable[[], str]] = None,
                 memory_limit: int = 200) -> None:
        self.path = Path(path) if path else None
        self._stamp = stamp or _iso_now
        self._memory: List[Dict[str, Any]] = []
        self.memory_limit = int(memory_limit)
        self.write_errors = 0
        self._seq = 0

    def new_correlation_id(self) -> str:
        """生成一个关联编号（同一轮故障处理共用一个）。"""
        self._seq += 1
        return "corr-%s-%03d" % (self._stamp().replace(":", "").replace("-", ""),
                                 self._seq)

    def record(self, kind: str, detail: str, *,
               correlation_id: Optional[str] = None,
               at: Optional[str] = None) -> Dict[str, Any]:
        """记录一条事件（``kind`` 非 :data:`LOG_KINDS` 时归入 ``system``）。"""
        entry = {"contract_version": EVENT_LOG_CONTRACT_VERSION,
                 "at": at or self._stamp(),
                 "kind": kind if kind in LOG_KINDS else "system",
                 "detail": str(detail),
                 "correlation_id": correlation_id or ""}
        self._memory.append(entry)
        if len(self._memory) > self.memory_limit:
            del self._memory[:len(self._memory) - self.memory_limit]
        if self.path is not None:
            self._append(entry)
        return dict(entry)

    def _append(self, entry: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # 日志写入失败不得影响保障流程：只计数，不抛错。
            self.write_errors += 1

    def entries(self, *, correlation_id: Optional[str] = None,
                limit: Optional[int] = None) -> List[Dict[str, Any]]:
        rows = [dict(row) for row in self._memory]
        if correlation_id:
            rows = [row for row in rows if row["correlation_id"] == correlation_id]
        if limit is not None:
            rows = rows[-int(limit):]
        return rows

    def summarize(self, correlation_id: Optional[str] = None) -> str:
        """按时间顺序渲染关联时间线（人工排查用）。"""
        rows = self.entries(correlation_id=correlation_id)
        if not rows:
            return "（无关联事件）"
        return "\n".join("  %s [%s] %s" % (row["at"], row["kind"], row["detail"])
                         for row in rows)


def record_tick_events(event_log: RemoteEventLog, result: Any, *,
                       correlation_id: Optional[str] = None,
                       at: Optional[str] = None) -> List[Dict[str, Any]]:
    """把一轮 :class:`campus_failover.TickResult` 转成关联事件。

    ``plan`` 中的动作按类别映射：``WIFI_RECONNECT``→``wifi``、``PORTAL_POST``→
    ``portal``、``METRIC_WRITE``→``route``、``CLASH_TOUCH``→``system``；
    状态转换与已执行结局各记一条。**只记录，不执行**。
    """
    kind_map = {OpKind.WIFI_RECONNECT: "wifi", OpKind.PORTAL_POST: "portal",
                OpKind.METRIC_WRITE: "route", OpKind.CLASH_TOUCH: "system"}
    events: List[Dict[str, Any]] = []
    for request in getattr(result, "plan", None) or []:
        events.append(event_log.record(
            kind_map.get(getattr(request, "kind", None), "system"),
            "计划：%s" % getattr(request, "reason", ""),
            correlation_id=correlation_id, at=at))
    for outcome in getattr(result, "executed", None) or []:
        events.append(event_log.record(
            kind_map.get(getattr(outcome, "kind", None), "system"),
            "已执行：%s" % (getattr(outcome, "detail", "") or "无说明"),
            correlation_id=correlation_id, at=at))
    if getattr(result, "changed", False):
        events.append(event_log.record(
            "system",
            "状态转换：%s → %s"
            % (getattr(getattr(result, "previous_state", None), "value", "?"),
               getattr(getattr(result, "state", None), "value", "?")),
            correlation_id=correlation_id, at=at))
    return events


# --------------------------------------------------------------------------- #
# 真实 Windows 适配层（复用 P0 只读管道，零新增写命令）
# --------------------------------------------------------------------------- #
def _read_command(runner: Any, name: str, pids: Optional[List[int]] = None) -> str:
    """执行一条**只读**诊断命令并返回 stdout（失败返回空串）。"""
    from tools import netdiag

    script = netdiag.COMMANDS[name]
    if pids:
        script = script.replace("{pids}", ",".join(str(p) for p in pids))
    try:
        _rc, out, _err = runner.run(name, script)
    except Exception:
        return ""
    return out or ""


def build_windows_remote_ops(runner: Any, *,
                             credentials_present: Optional[Callable[[], bool]] = None
                             ) -> RemoteOps:
    """组装真实 :class:`RemoteOps`（读经 P0 ``netdiag`` **只读**管道）。

    ``runner`` 为 :class:`tools.netdiag.PowerShellRunner`（逐命令
    ``assert_read_only`` fail-closed）或同构假件；本函数**不引入任何写命令**，
    也不读取凭据明文（``credentials_present`` 由调用方注入）。
    """
    from tools import netdiag

    def _probe(*, with_readiness: bool) -> RemoteProbe:
        processes = netdiag.parse_remote_processes(_read_command(
            runner, "remote_processes"))
        pids = [row["pid"] for row in processes if row.get("pid")]
        ports: List[Dict[str, Any]] = []
        udp: List[Dict[str, Any]] = []
        if pids:
            ports = netdiag.parse_tcp_ports(_read_command(runner, "remote_tcp", pids))
            udp = netdiag.parse_udp_ports(_read_command(runner, "remote_udp", pids))
        probe = RemoteProbe(clients=processes, ports=ports, udp_endpoints=udp)
        if with_readiness:
            probe.sleep_states = netdiag.parse_powercfg(
                _read_command(runner, "sleep_states"))
            probe.auto_logon = netdiag.parse_winlogon(
                _read_command(runner, "winlogon"))
            probe.run_entries = netdiag.parse_run_entries(
                _read_command(runner, "run_entries"))
        return probe

    return RemoteOps(
        probe=lambda: _probe(with_readiness=False),
        probe_readiness=lambda: _probe(with_readiness=True),
        credentials_present=credentials_present or _default_credentials_present)


def _default_credentials_present() -> bool:
    """默认凭据存在性检查（**只判断是否存在，不读取明文**）。"""
    try:
        from campus_reconnect import get_credentials

        return get_credentials() is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def format_remote_status(status: RemoteStatus, *,
                         readiness: Optional[ReadinessReport] = None,
                         events: Optional[RemoteEventLog] = None,
                         watchdog: Optional[WatchdogDecision] = None) -> str:
    """人类可读的远程保障报告（``remote`` 命令输出）。"""
    lines: List[str] = []
    add = lines.append
    add("=" * 72)
    add("V0.3 P7 远程保障（只读：不切换路由、不认证、不触碰远控进程）")
    add("契约: %s" % status.contract_version)
    add("=" * 72)
    add("")
    for line in status.lines():
        add(line)
    if watchdog is not None:
        add("看门狗决策  : %s（%s）" % (watchdog.action, watchdog.reason))
    if readiness is not None:
        add("")
        add(format_readiness(readiness))
    if events is not None:
        add("")
        add("[关联事件] %d 条" % len(events.entries()))
        add(events.summarize())
    add("=" * 72)
    return "\n".join(lines)


def format_readiness(report: ReadinessReport) -> str:
    """渲染无人值守就绪清单。"""
    lines: List[str] = ["[无人值守就绪自检] %s"
                        % ("通过（人工项除外）" if report.ok else "存在缺口")]
    for item in report.items:
        mark = "OK  " if item.ok else ("人工" if item.manual else "缺口")
        lines.append("  [%s] %s：%s" % (mark, item.label, item.detail))
    return "\n".join(lines)
