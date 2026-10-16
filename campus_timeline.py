# -*- coding: utf-8 -*-
"""V0.5-6 网络事件时间线（Recovery Event 可复盘日志）。

方案 §5.6 要求把每个恢复事件留下的证据串成**可复盘时间线**：

    timestamp / network_epoch / active_path / Ethernet state / WiFi state / Portal state
    / verification result / route action / UU action / final state

本模块把这条要求落成一件**纯逻辑**台账，并回答用户最先问的两个问题：

- **「我在哪个时刻断的网？」** → ``NETWORK_DOWN``（含断因、两轴结论、主链路）；
- **「你哪个时刻帮我连上的？花了多久？」** → ``NETWORK_UP`` + ``RECOVERY_COMPLETE``
  （含**恢复耗时**、恢复后主链路、最终状态机状态）。

一次「断网 → 恢复」记为一个 :class:`Episode`（故障事件）：开始时记断网时刻，结束时
输出该事件的**复盘时间线**（``T+mm:ss`` 里程碑序列），并与用户关心的时刻一起写入日志。

职责边界（方案 §2 的「只新增接缝」）
----------------------------------

- **纯逻辑**：零 I/O、零 Tk、零网络。``now``（单调，用于时长）与挂钟（用于人类可读
  时刻）都可注入，因此可以完整离线测试（零真实 sleep）。
- **不判定网络**：是否「断网」由**调用方**给出（``offline`` 入参 = 托盘已有的真实探测
  + 状态机滞回）；本模块只负责**记录、去重、计时、复盘**，绝不自己下结论。
- **不触发任何动作**：没有路由变更、没有清退、没有引用计数副作用 —— 它只写日志。
- **不写凭据**：日志里不出现密码 / Portal 凭据 / Cookie（沿用项目脱敏规则），
  也不记账号与 MAC；只记接口角色、健康结论、源地址、逐站结论等已可入日志的事实。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import (Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence,
                    Tuple)

from campus_failover import PATH_LABEL
from campus_reconnect import PATH_OK, PATH_PROBE_UNRELIABLE

__all__ = [
    "CONTRACT_VERSION", "LATE_ATTACH_WINDOW", "PATH_ROLES",
    "KIND_NETWORK_DOWN", "KIND_NETWORK_UP", "KIND_RECOVERY_COMPLETE",
    "KIND_MONITOR_PAUSED", "KIND_MONITOR_RESUMED", "KIND_AXIS_STATE",
    "KIND_FAILOVER_STATE", "KIND_ACTIVE_PATH", "KIND_RECOVERY_EVENT",
    "KIND_RECOVERY_STAGE", "KIND_VERIFY_GATE", "KIND_VERIFY_CROSS",
    "KIND_INTERNET_VERIFIED", "KIND_INTERNET_UNVERIFIED",
    "KIND_VERIFY_UNRELIABLE", "KIND_STANDBY_VERIFIED", "KIND_DEGRADED",
    "KIND_ROUTE_SWITCH", "KIND_ROUTE_SWITCH_DONE", "KIND_UU_RESTART_STARTED",
    "KIND_UU_RESTARTED", "KIND_UU_RESTART_FAILED", "KIND_UU_SKIPPED",
    "KIND_RECONNECT_OK", "KIND_RECONNECT_FAILED", "KIND_SNAPSHOT_UNTRUSTED",
    "LEVEL_INFO", "LEVEL_WARNING",
    "TimelineEvent", "Episode", "TimelineUpdate", "NetworkTimeline",
]

CONTRACT_VERSION = "v05-6-timeline-1"

#: 故障事件**已结束**后，仍在窗口内到达的事件（UU 恢复在后台线程完成才回报）
#: 会被挂回该事件，便于复盘「恢复流水线最后一步」。
LATE_ATTACH_WINDOW = 900.0

#: 接口角色顺序（固定顺序 → 日志与复盘稳定可读）。
PATH_ROLES: Tuple[str, ...] = ("ethernet", "wifi")

# ---- 事件类型 ------------------------------------------------------------- #
KIND_NETWORK_DOWN = "NETWORK_DOWN"            # 断网时刻（开启一个故障事件）
KIND_NETWORK_UP = "NETWORK_UP"                # 恢复时刻（真实探测确认）
KIND_RECOVERY_COMPLETE = "RECOVERY_COMPLETE"  # 一次故障事件的收尾
KIND_DEGRADED = "NETWORK_DEGRADED"            # 探测失败但尚未确认（确认次数 > 1 时）
KIND_MONITOR_PAUSED = "MONITOR_PAUSED"
KIND_MONITOR_RESUMED = "MONITOR_RESUMED"
KIND_AXIS_STATE = "AXIS_STATE"
KIND_FAILOVER_STATE = "FAILOVER_STATE"
KIND_ACTIVE_PATH = "ACTIVE_PATH"
KIND_RECOVERY_EVENT = "RECOVERY_EVENT"
KIND_RECOVERY_STAGE = "RECOVERY_STAGE"
KIND_VERIFY_GATE = "VERIFY_GATE"              # 百度闸门
KIND_VERIFY_CROSS = "VERIFY_CROSS"            # 交叉站点
KIND_INTERNET_VERIFIED = "INTERNET_VERIFIED"
KIND_INTERNET_UNVERIFIED = "INTERNET_UNVERIFIED"
KIND_VERIFY_UNRELIABLE = "VERIFY_UNRELIABLE"
KIND_STANDBY_VERIFIED = "STANDBY_VERIFIED"
KIND_ROUTE_SWITCH = "ROUTE_SWITCH"            # 计划切换但本轮未执行
KIND_ROUTE_SWITCH_DONE = "ROUTE_SWITCH_DONE"  # 默认出口切换已执行
KIND_UU_RESTART_STARTED = "UU_RESTART_STARTED"
KIND_UU_RESTARTED = "GAMEVIEWER_RESTARTED"
KIND_UU_RESTART_FAILED = "UU_RESTART_FAILED"
KIND_UU_SKIPPED = "UU_SKIPPED"
KIND_RECONNECT_OK = "RECONNECT_OK"
KIND_RECONNECT_FAILED = "RECONNECT_FAILED"
KIND_SNAPSHOT_UNTRUSTED = "SNAPSHOT_UNTRUSTED"

LEVEL_INFO = "info"
LEVEL_WARNING = "warning"

#: 路由切换动作的类型值（``OpKind.METRIC_WRITE.value``）。
_METRIC_WRITE = "metric_write"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _value_of(obj: Any) -> Any:
    """枚举取 ``.value``，其余原样返回（鸭子类型，对 ``None`` 安全）。"""
    return getattr(obj, "value", obj)


def _fmt_offset(value: float) -> str:
    """相对故障开始的偏移 ``mm:ss``（超过 1 小时带小时位）。"""
    total = int(max(0.0, float(value)))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%02d:%02d" % (minutes, seconds)


def _fmt_seconds(value: Optional[float]) -> str:
    """人类可读时长（秒 / 分 / 小时）。"""
    if value is None:
        return "未知"
    value = max(0.0, float(value))
    if value < 60:
        return "%.0fs" % value
    minutes, seconds = divmod(int(value), 60)
    if minutes < 60:
        return "%d分%02ds" % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return "%d小时%02d分" % (hours, minutes)


def _role_label(role: Optional[str]) -> str:
    return PATH_LABEL.get(role or "", role or "未知")


def _health_label(state: Any) -> str:
    """健康结论的中文标签（复用 UI 状态映射，单一事实来源）。"""
    if state is None:
        return "未知"
    try:
        from campus_ui_state import health_view

        return health_view(state)[1]
    except Exception:                                  # noqa: BLE001 - 标签缺失不影响记录
        return str(_value_of(state))


def _probe_text(result: Any) -> str:
    """单点探测的可读明细（``主机(ok/错误)``）。"""
    if result is None:
        return ""
    from urllib.parse import urlsplit

    source = str(getattr(result, "source", "") or "")
    try:
        host = urlsplit(source).hostname or source
    except ValueError:                                 # pragma: no cover - source 恒为 URL
        host = source
    if bool(getattr(result, "ok", False)):
        return "%s(ok)" % host
    return "%s(%s)" % (host, getattr(result, "error", None)
                       or getattr(result, "status_code", None))


def _record_parts(record: Any) -> Tuple[Optional[float], Any]:
    """把整合层的 ``(at, result)`` 归一为 ``(at, result)``（容错裸结果）。"""
    if isinstance(record, (tuple, list)) and len(record) >= 2:
        at = record[0]
        return (None if at is None else float(at)), record[1]
    return None, record


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TimelineEvent:
    """时间线上的一个事件（不可变；写入后不再修改）。"""

    at: float
    wall: str
    kind: str
    detail: str = ""
    level: str = LEVEL_INFO
    epoch: int = 0
    offset: Optional[float] = None

    @property
    def clock(self) -> str:
        """``HH:MM:SS``（完整时刻见 :attr:`wall`）。"""
        return self.wall[11:] if len(self.wall) >= 19 else self.wall

    def line(self) -> str:
        """单行日志文本（``时刻 (+偏移) 类型 明细``）。"""
        parts = [self.clock]
        if self.offset is not None:
            parts.append("(+%s)" % _fmt_offset(self.offset))
        parts.append(self.kind)
        if self.detail:
            parts.append(self.detail)
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {"at": self.at, "wall": self.wall, "kind": self.kind,
                "detail": self.detail, "level": self.level,
                "epoch": self.epoch, "offset": self.offset}


@dataclass
class Episode:
    """一次「断网 → 恢复」（故障事件）。

    可变：``ended_at`` 等在恢复时补齐，迟到的里程碑（UU 恢复在后台线程完成）也会
    追加进来 —— 这样 :meth:`report_lines` 始终是该事件**完整**的复盘。
    """

    index: int
    started_at: float
    started_wall: str
    reason: str = ""
    ended_at: Optional[float] = None
    ended_wall: str = ""
    recovered_path: Optional[str] = None
    final_state: str = ""
    aborted: bool = False
    end_reason: str = ""
    milestones: List[TimelineEvent] = field(default_factory=list)

    @property
    def ongoing(self) -> bool:
        return self.ended_at is None and not self.aborted

    @property
    def duration(self) -> Optional[float]:
        """恢复耗时（秒）；未恢复返回 ``None``。"""
        if self.ended_at is None:
            return None
        return max(0.0, float(self.ended_at) - float(self.started_at))

    def elapsed(self, now: float) -> float:
        """截至 ``now`` 已持续多久（未恢复时用于「仍在离线」展示）。"""
        end = self.ended_at if self.ended_at is not None else now
        return max(0.0, float(end) - float(self.started_at))

    def moment_lines(self) -> List[str]:
        """用户最关心的两个时刻（断网 / 恢复 + 耗时）。"""
        out = ["断网时刻：%s（%s）" % (self.started_wall, self.reason or "原因未记录")]
        if self.aborted:
            out.append("记录结束：%s（%s；未能观测到恢复时刻）"
                       % (self.ended_wall, self.end_reason or "记录中止"))
        elif self.ended_at is None:
            out.append("恢复时刻：尚未恢复")
        else:
            out.append("恢复时刻：%s（恢复耗时 %s；恢复后主链路：%s；最终状态：%s）"
                       % (self.ended_wall, _fmt_seconds(self.duration),
                          _role_label(self.recovered_path), self.final_state or "未知"))
        return out

    def report_lines(self) -> List[str]:
        """该故障事件的**可复盘时间线**（时刻 + ``T+mm:ss`` 里程碑）。"""
        head = "【故障事件 #%d】" % self.index
        out = ["%s%s" % (head, text) for text in self.moment_lines()]
        for event in sorted(self.milestones, key=lambda item: item.at):
            out.append("%sT+%s %s %s" % (head, _fmt_offset(event.offset or 0.0),
                                         event.kind, event.detail))
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "started_at": self.started_at,
                "started_wall": self.started_wall, "reason": self.reason,
                "ended_at": self.ended_at, "ended_wall": self.ended_wall,
                "duration": self.duration, "recovered_path": self.recovered_path,
                "final_state": self.final_state, "aborted": self.aborted,
                "end_reason": self.end_reason,
                "milestones": [item.to_dict() for item in self.milestones]}


@dataclass(frozen=True)
class TimelineUpdate:
    """一次 :meth:`NetworkTimeline.observe` 的结果（纯数据）。"""

    at: float = 0.0
    offline: Optional[bool] = None
    index: int = 0
    episode: Optional[Episode] = None
    events: Tuple[TimelineEvent, ...] = ()
    started: bool = False
    finished: bool = False
    paused: bool = False

    def lines(self) -> List[str]:
        return [event.line() for event in self.events]


# --------------------------------------------------------------------------- #
# 时间线
# --------------------------------------------------------------------------- #
class NetworkTimeline:
    """网络事件时间线（V0.5-6）。

    典型接线（托盘，每轮真实探测后调用一次）::

        timeline = NetworkTimeline(emit=lambda text, level: log.log(level, text))
        timeline.observe(now=now, offline=not online, failover_state=result.state,
                         ethernet_state=snap.ethernet.state, wifi_state=snap.wifi.state,
                         active_path=..., verification={"ethernet": gate.evidence(...)},
                         op_plan=result.plan, op_outcomes=result.executed,
                         recovery_event=tick.event, recovery_steps=tick.steps,
                         trusted=trusted)
        timeline.note_uu_started(request)      # UU 恢复流水线最后一步
        timeline.note_uu_outcome(outcome)

    线程安全：托盘会从「监测线程」（``observe``）、「UU 后台线程」（``note_uu_*``）与
    Tk 主线程（``status_lines`` / ``report_lines``）三处访问，内部一把 ``RLock``。
    """

    def __init__(self, *, clock: Optional[Callable[[], float]] = None,
                 wall_clock: Optional[Callable[[], float]] = None,
                 emit: Optional[Callable[..., None]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 max_events: int = 400, max_episodes: int = 60,
                 offline_confirmations: int = 1) -> None:
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        #: 日志出口：``emit(text, level)``（唯一出口，便于测试与统一格式）。
        self._emit = emit
        #: 内部错误出口（``log(message)``）：日志出口本身异常时不得影响监测主流程。
        self._log = log
        self.max_events = max(10, int(max_events))
        self.max_episodes = max(1, int(max_episodes))
        #: 连续多少次「离线」观测才确认断网（1 = 首次探测失败即记）。
        self.offline_confirmations = max(1, int(offline_confirmations))
        self._lock = threading.RLock()
        self._events: Deque[TimelineEvent] = deque(maxlen=self.max_events)
        self._episodes: List[Episode] = []
        self._open: Optional[Episode] = None
        self._index = 0
        # 上一次观测（``None`` = 尚未观测：**不**用它判定迁移，避免启动即误报）
        self._offline: Optional[bool] = None
        self._streak = 0
        self._paused: Optional[bool] = None
        self._trusted: Optional[bool] = None
        self._failover_state: Any = None
        self._axis: Dict[str, Any] = {}
        self._active_path: Optional[str] = None
        self._recovery_event: Optional[str] = None
        self._steps: Optional[Tuple[str, ...]] = None
        #: 同一故障事件内的动作去重（`metric_write` 每轮都会重复计划）。
        self._seen_ops: set = set()
        #: 每条链路最近一次验证的指纹（同一份结果不重复记）。
        self._seen_verification: Dict[str, Tuple[Any, ...]] = {}

    # ---- 只读视图 ---- #
    @property
    def index(self) -> int:
        """最近一个故障事件的编号（0 = 尚未发生过）。"""
        return self._index

    def recent(self, limit: Optional[int] = None) -> Tuple[TimelineEvent, ...]:
        """最近的事件（时间正序；``limit`` 取最新 N 条）。"""
        with self._lock:
            return self._recent_locked(limit)

    def episodes(self, limit: Optional[int] = None) -> Tuple[Episode, ...]:
        with self._lock:
            items = list(self._episodes)
        if limit is not None and limit >= 0:
            items = items[-int(limit):]
        return tuple(items)

    def current(self) -> Optional[Episode]:
        """**仍在进行中**的故障事件（离线中）；否则 ``None``。"""
        with self._lock:
            return self._open

    def last_episode(self) -> Optional[Episode]:
        with self._lock:
            return self._episodes[-1] if self._episodes else None

    # ---- 主入口 ---- #
    def observe(self, *, now: Optional[float] = None,
                offline: Optional[bool] = None,
                failover_state: Any = None,
                ethernet_state: Any = None, wifi_state: Any = None,
                active_path: Optional[str] = None,
                standby_path: Optional[str] = None,
                verification: Optional[Mapping[str, Any]] = None,
                op_plan: Sequence[Any] = (), op_outcomes: Sequence[Any] = (),
                recovery_event: Any = None, recovery_steps: Sequence[Any] = (),
                trusted: Optional[bool] = None, paused: bool = False,
                detail: str = "", final_state: str = "") -> TimelineUpdate:
        """记录一轮观测（**只记录，不判定**）。

        所有入参都是「本轮看到的事实」；传 ``None`` 表示"本项未观测"，此时既不改状态
        也不产生事件（避免用缺失数据伪造迁移）。``offline`` 传 ``None`` 表示"本次不更新
        断网/恢复状态"（例如重连结果回报时只补一条里程碑）。
        """
        moment = self._now(now)
        out: List[TimelineEvent] = []
        started = finished = False
        with self._lock:
            if paused:
                if self._paused is not True:
                    out.append(self._record(self._event(
                        KIND_MONITOR_PAUSED,
                        "监测已暂停：时间线停止记录（恢复后继续）", moment)))
                self._paused = True
                self._abandon_open(moment, "监测暂停")
                return self._update(moment, offline, out, started, finished,
                                    paused=True)
            if self._paused:
                self._paused = False
                out.append(self._record(self._event(
                    KIND_MONITOR_RESUMED, "监测已恢复", moment)))

            # 1) 断网：先开事件，本轮后续迁移即成为它的里程碑。
            if offline is not None:
                if bool(offline):
                    self._streak += 1
                    if self._open is None and self._streak >= self.offline_confirmations:
                        reason = self._down_reason(ethernet_state, wifi_state,
                                                  active_path, detail)
                        episode = Episode(index=self._index + 1, started_at=moment,
                                          started_wall=self._wall_text(), reason=reason)
                        self._index = episode.index
                        self._episodes.append(episode)
                        if len(self._episodes) > self.max_episodes:
                            del self._episodes[0:len(self._episodes) - self.max_episodes]
                        self._open = episode
                        out.append(self._record(self._event(
                            KIND_NETWORK_DOWN, reason, moment, level=LEVEL_WARNING)))
                        started = True
                    elif self._open is None:
                        out.append(self._record(self._event(
                            KIND_DEGRADED,
                            "公网探测失败 %d 次（尚未确认断网，需连续 %d 次）"
                            % (self._streak, self.offline_confirmations),
                            moment, level=LEVEL_WARNING)))
                else:
                    self._streak = 0
                self._offline = bool(offline)

            # 2) 状态机迁移
            if failover_state is not None:
                value = _value_of(failover_state)
                if self._failover_state is not None \
                        and value != self._failover_state:
                    out.append(self._record(self._event(
                        KIND_FAILOVER_STATE,
                        "状态机 %s → %s" % (self._failover_state, value), moment)))
                self._failover_state = value

            # 3) 两轴健康结论（区分「链路断了」与「链路在但上不了网」）
            for role, state in zip(PATH_ROLES, (ethernet_state, wifi_state)):
                if state is None:
                    continue
                before = self._axis.get(role)
                self._axis[role] = state
                if before is None or _value_of(before) == _value_of(state):
                    continue
                out.append(self._record(self._event(
                    KIND_AXIS_STATE,
                    "%s %s → %s（%s → %s）"
                    % (_role_label(role), _health_label(before), _health_label(state),
                       _value_of(before), _value_of(state)), moment,
                    level=(LEVEL_INFO if _value_of(state) == "HEALTHY"
                           else LEVEL_WARNING))))

            # 4) 主链路（活动路径）变更
            if active_path is not None and active_path != self._active_path:
                if self._active_path is not None:
                    out.append(self._record(self._event(
                        KIND_ACTIVE_PATH,
                        "主链路 %s → %s" % (_role_label(self._active_path),
                                           _role_label(active_path)), moment)))
                self._active_path = active_path

            # 5) 恢复事件与恢复阶段
            if recovery_event is not None:
                value = str(_value_of(recovery_event))
                if value != self._recovery_event:
                    self._recovery_event = value
                    out.append(self._record(self._event(
                        KIND_RECOVERY_EVENT,
                        "恢复事件 %s（主链路 %s）" % (value, _role_label(active_path)),
                        moment)))
            steps = tuple(str(_value_of(step)) for step in (recovery_steps or ()))
            if steps and steps != self._steps:
                self._steps = steps
                out.append(self._record(self._event(
                    KIND_RECOVERY_STAGE,
                    "恢复阶段 %s" % " → ".join(steps), moment)))
            elif not steps:
                self._steps = None

            # 6) 接口受限验证结果（逐条链路；同一份结果只记一次）
            if verification:
                for role in PATH_ROLES:
                    record = verification.get(role)
                    if record is None:
                        continue
                    at, result = _record_parts(record)
                    if result is None:
                        continue
                    fingerprint = (at, bool(getattr(result, "allows_recovery", False)),
                                   str(getattr(result, "reliability", "") or ""),
                                   str(getattr(result, "reason", "") or ""))
                    if self._seen_verification.get(role) == fingerprint:
                        continue
                    self._seen_verification[role] = fingerprint
                    out.extend(self._verification_events(
                        role, result, moment, active_path))

            # 7) 路由动作（计划 / 已执行）——「什么时候切换的链路」
            out.extend(self._route_events(op_plan, op_outcomes, moment))

            # 8) 可信度：不可信时如实标注（连续不可信只记一次）
            if trusted is not None:
                if not bool(trusted) and self._trusted is not False:
                    out.append(self._record(self._event(
                        KIND_SNAPSHOT_UNTRUSTED,
                        "三轴快照不可信：本轮不做恢复判定（%s）"
                        % (detail or "数据不足"), moment, level=LEVEL_WARNING)))
                self._trusted = bool(trusted)

            # 9) 恢复：**最后**一步（保证本轮的验证/切换/重启都先成为里程碑）
            if offline is not None and not bool(offline) and self._open is not None:
                finished = True
                episode = self._open
                out.append(self._record(self._event(
                    KIND_NETWORK_UP,
                    "公网探测恢复：主链路 %s" % _role_label(active_path), moment)))
                final = str(final_state or _value_of(failover_state) or "")
                out.append(self._record(self._event(
                    KIND_RECOVERY_COMPLETE,
                    "恢复完成：断网 %s → 恢复 %s，耗时 %s（主链路 %s；状态机 %s）"
                    % (episode.started_wall, self._wall_text(),
                       _fmt_seconds(moment - episode.started_at),
                       _role_label(active_path), final or "未知"), moment)))
                episode.ended_at = moment
                episode.ended_wall = self._wall_text()
                episode.recovered_path = active_path
                episode.final_state = final
                self._open = None
                self._recovery_event = None
                self._steps = None
                self._emit_report(episode)

            return self._update(moment, offline, out, started, finished)

    # ---- 显式里程碑（后台线程回报） ---- #
    def note_paused(self, *, now: Optional[float] = None) -> Optional[TimelineEvent]:
        """监测被用户暂停：停止记录，并把进行中的故障事件标记为「记录中止」。"""
        update = self.observe(now=now, paused=True)
        return update.events[0] if update.events else None

    def note_uu_started(self, request: Any = None, *, active_path: Any = None,
                        network_epoch: Any = None, reason: Any = None,
                        now: Optional[float] = None) -> Optional[TimelineEvent]:
        """UU 恢复流水线**最后一步**开始（方案 §5.4 第 5 步之后）。

        ``request`` 可为 :class:`campus_recovery.UuRestartRequest`（自动恢复路径）；
        手动重启没有该请求时，用 ``active_path`` / ``network_epoch`` / ``reason``
        三个关键字直接说明事实。
        """
        moment = self._now(now)
        if active_path is None:
            active_path = getattr(request, "active_path", None)
        if network_epoch is None:
            network_epoch = getattr(request, "network_epoch", "?")
        if reason is None:
            reason = getattr(request, "reason", "") or "验证通过后的恢复"
        detail = ("UU 远程清退重启开始（主链路 %s；epoch=%s；%s）"
                  % (_role_label(active_path), network_epoch, str(reason)))
        with self._lock:
            return self._record(self._event(KIND_UU_RESTART_STARTED, detail, moment),
                                attach_last=True)

    def note_uu_outcome(self, outcome: Any = None, *,
                        restarted: Any = None, ok: Any = None,
                        action: Any = None, detail: Any = None,
                        now: Optional[float] = None) -> Optional[TimelineEvent]:
        """UU 重启的**实际结局**（成功 / 无需 / 失败）。"""
        moment = self._now(now)
        if restarted is None:
            restarted = getattr(outcome, "restarted", False)
        if ok is None:
            ok = getattr(outcome, "ok", False)
        if action is None:
            action = getattr(outcome, "action", "")
        if detail is None:
            detail = getattr(outcome, "detail", "")
        restarted = bool(restarted)
        ok = bool(ok)
        action = str(action or "")
        detail = str(detail or "")
        if restarted:
            kind, level = KIND_UU_RESTARTED, LEVEL_INFO
            text = "UU 远程已清退并重启（%s）%s" % (action or "restarted", detail)
        elif ok:
            kind, level = KIND_UU_SKIPPED, LEVEL_INFO
            text = "UU 远程无需重启（%s）%s" % (action or "noop", detail)
        else:
            kind, level = KIND_UU_RESTART_FAILED, LEVEL_WARNING
            text = "UU 远程未重启（%s）%s" % (action or "failed", detail)
        with self._lock:
            return self._record(self._event(kind, text, moment, level=level),
                                attach_last=True)

    def note_reconnect(self, ok: bool, *, detail: str = "",
                       elapsed: Optional[float] = None,
                       now: Optional[float] = None) -> Optional[TimelineEvent]:
        """一次重连尝试的结局。

        注意：**Portal 认证成功 ≠ 能上网**（方案 §1）。``ok=True`` 只表示认证完成，
        故障事件**不会**因此结束 —— 结束只由真实探测（``offline=False``）决定。
        """
        moment = self._now(now)
        if ok:
            kind, level = KIND_RECONNECT_OK, LEVEL_INFO
            text = ("重连成功（Portal 认证完成%s）：%s"
                    % ("" if elapsed is None else "，用时 %.0fs" % float(elapsed),
                       detail or "已恢复认证"))
        else:
            kind, level = KIND_RECONNECT_FAILED, LEVEL_WARNING
            text = "重连未成功：%s" % (detail or "原因未知")
        with self._lock:
            return self._record(self._event(kind, text, moment, level=level),
                                attach_last=True)

    # ---- 人类可读摘要 ---- #
    def status_lines(self, *, now: Optional[float] = None, limit: int = 4) -> List[str]:
        """仪表盘/菜单用的短摘要：最近一次断网结论 + 最近若干事件。"""
        moment = self._now(now)
        with self._lock:
            out: List[str] = []
            episode = self._open if self._open is not None else self.last_episode()
            if self._open is not None:
                out.append("⚠ 断网中：%s 起，已持续 %s"
                           % (self._open.started_wall[11:],
                              _fmt_seconds(self._open.elapsed(moment))))
            elif episode is not None:
                if episode.aborted:
                    out.append("最近一次断网：%s → %s（%s）"
                               % (episode.started_wall[11:], episode.ended_wall[11:],
                                  episode.end_reason or "记录中止"))
                else:
                    out.append("最近一次断网：%s → %s（耗时 %s；主链路 %s）"
                               % (episode.started_wall[11:], episode.ended_wall[11:],
                                  _fmt_seconds(episode.duration),
                                  _role_label(episode.recovered_path)))
            for event in self._recent_locked(max(0, int(limit))):
                out.append(event.line())
            return out

    def outage_summary(self, *, now: Optional[float] = None) -> str:
        """一行说明：最近一次断网的起止与耗时（无记录时如实说明）。"""
        moment = self._now(now)
        with self._lock:
            episode = self._open if self._open is not None else self.last_episode()
            if episode is None:
                return "尚无网络故障事件记录"
            if self._open is not None:
                return ("故障事件 #%d：%s 断网，已持续 %s（尚未恢复）"
                        % (episode.index, episode.started_wall,
                           _fmt_seconds(episode.elapsed(moment))))
            if episode.aborted:
                return ("故障事件 #%d：%s 断网 → %s 记录中止（%s）"
                        % (episode.index, episode.started_wall, episode.ended_wall,
                           episode.end_reason or "记录中止"))
            return ("故障事件 #%d：%s 断网 → %s 恢复，耗时 %s（主链路 %s）"
                    % (episode.index, episode.started_wall, episode.ended_wall,
                       _fmt_seconds(episode.duration),
                       _role_label(episode.recovered_path)))

    def report_lines(self, *, limit: int = 1) -> List[str]:
        """最近 N 个故障事件的**复盘时间线**（默认最后一个）。"""
        with self._lock:
            if not self._episodes:
                return ["尚无网络故障事件记录"]
            count = max(1, int(limit))
            out: List[str] = []
            for episode in self._episodes[-count:]:
                out.extend(episode.report_lines())
            return out

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {"contract_version": CONTRACT_VERSION,
                    "episodes": len(self._episodes), "index": self._index,
                    "offline": self._offline, "paused": self._paused,
                    "events": len(self._events),
                    "open": self._open.to_dict() if self._open is not None else None,
                    "last": (self._episodes[-1].to_dict() if self._episodes else None)}

    # ---- 内部：记录 ---- #
    def _now(self, now: Optional[float]) -> float:
        return self._clock() if now is None else float(now)

    def _wall_text(self) -> str:
        return datetime.fromtimestamp(self._wall_clock()).strftime("%Y-%m-%d %H:%M:%S")

    def _offset(self, moment: float) -> Optional[float]:
        if self._open is None:
            return None
        return max(0.0, float(moment) - float(self._open.started_at))

    def _event(self, kind: str, detail: str, moment: float,
               *, level: str = LEVEL_INFO) -> TimelineEvent:
        return TimelineEvent(at=moment, wall=self._wall_text(), kind=kind,
                             detail=detail, level=level,
                             epoch=(self._open.index if self._open is not None else 0),
                             offset=self._offset(moment))

    def _record(self, event: TimelineEvent, *,
                attach_last: bool = False) -> TimelineEvent:
        """写入事件：环形缓冲 + （若在故障事件内）里程碑 + 日志出口。"""
        target = self._attach_target(event, attach_last)
        if target is not None:
            event = replace(event, epoch=target.index,
                            offset=max(0.0, event.at - target.started_at))
            target.milestones.append(event)
        self._events.append(event)
        self._write(event.line(), event.level)
        return event

    def _attach_target(self, event: TimelineEvent, attach_last: bool
                       ) -> Optional[Episode]:
        """事件归属的故障事件：进行中的优先；否则（允许时）刚结束的那个。"""
        if self._open is not None:
            return self._open
        if not attach_last or not self._episodes:
            return None
        last = self._episodes[-1]
        if last.ended_at is None or last.aborted:
            return None
        if (event.at - last.ended_at) > LATE_ATTACH_WINDOW:
            return None
        return last

    def _write(self, text: str, level: str) -> None:
        sink = self._emit
        if sink is None:
            return
        try:
            sink(text, level)
        except Exception as exc:                       # noqa: BLE001 - 日志出口不得影响监测
            if self._log is not None:
                try:
                    self._log("时间线日志写入失败：%s" % type(exc).__name__)
                except Exception:                      # pragma: no cover - 兜底
                    pass

    def _emit_report(self, episode: Episode) -> None:
        for line in episode.report_lines():
            self._write(line, LEVEL_INFO)

    def _recent_locked(self, limit: Optional[int]) -> Tuple[TimelineEvent, ...]:
        items = list(self._events)
        if limit is not None and limit >= 0:
            items = items[-int(limit):]
        return tuple(items)

    def _update(self, moment: float, offline: Optional[bool],
                events: List[TimelineEvent], started: bool, finished: bool,
                *, paused: bool = False) -> TimelineUpdate:
        return TimelineUpdate(at=moment, offline=offline, index=self._index,
                              episode=self._open, events=tuple(events),
                              started=started, finished=finished, paused=paused)

    def _abandon_open(self, moment: float, reason: str) -> None:
        """把进行中的故障事件标记为「记录中止」（例如监测被暂停）。

        不伪造恢复时刻：如实写 "未能观测到恢复"，避免用户以为网络在那时恢复了。
        """
        episode = self._open
        if episode is None or episode.ended_at is not None:
            return
        self._record(self._event(
            KIND_MONITOR_PAUSED,
            "故障事件 #%d 记录中止：%s（恢复时刻未知）" % (episode.index, reason),
            moment, level=LEVEL_WARNING))
        episode.ended_at = moment
        episode.ended_wall = self._wall_text()
        episode.aborted = True
        episode.end_reason = reason
        self._open = None

    # ---- 内部：事件构造 ---- #
    def _down_reason(self, ethernet_state: Any, wifi_state: Any,
                     active_path: Optional[str], detail: str) -> str:
        parts: List[str] = []
        if active_path:
            parts.append("断网前主链路 %s" % _role_label(active_path))
        for role, state in zip(PATH_ROLES, (ethernet_state, wifi_state)):
            if state is None:
                continue
            parts.append("%s %s" % (_role_label(role), _health_label(state)))
        if detail:
            parts.append("本轮观测：%s" % detail)
        return "；".join(parts) or "公网探测失败"

    def _verification_events(self, role: str, result: Any, moment: float,
                             active_path: Optional[str]) -> List[TimelineEvent]:
        out: List[TimelineEvent] = []
        label = _role_label(role)
        reliability = str(getattr(result, "reliability", "") or "")
        reason = str(getattr(result, "reason", "") or "")
        allowed = bool(getattr(result, "allows_recovery", False))
        if reliability == PATH_PROBE_UNRELIABLE:
            out.append(self._record(self._event(
                KIND_VERIFY_UNRELIABLE,
                "%s 验证不可信（%s）：不切换、不重启 UU" % (label, reason), moment,
                level=LEVEL_WARNING)))
        else:
            gate = getattr(result, "gate_result", None)
            if gate is not None:
                ok = bool(getattr(gate, "ok", False))
                out.append(self._record(self._event(
                    KIND_VERIFY_GATE,
                    "%s 百度闸门 %s（%s）" % (label, "PASS" if ok else "FAIL",
                                             _probe_text(gate)), moment,
                    level=(LEVEL_INFO if ok else LEVEL_WARNING))))
            cross = tuple(getattr(result, "cross_results", ()) or ())
            if cross:
                hits = len(tuple(getattr(result, "successful_sites", ()) or ()))
                failed = "、".join(str(site) for site in
                                   (getattr(result, "failed_sites", ()) or ()))
                text = ("%s 交叉命中 %d/%d" % (label, hits, len(cross))
                        + ("（失败：%s）" % failed if failed else ""))
                out.append(self._record(self._event(
                    KIND_VERIFY_CROSS, text, moment,
                    level=(LEVEL_INFO if hits else LEVEL_WARNING))))
            out.append(self._record(self._event(
                KIND_INTERNET_VERIFIED if allowed else KIND_INTERNET_UNVERIFIED,
                "%s %s：%s" % (label,
                               "接口受限验证通过（可直连上网）" if allowed
                               else "接口受限验证未通过",
                               reason or ("可靠性 " + (reliability or PATH_OK))),
                moment, level=(LEVEL_INFO if allowed else LEVEL_WARNING))))
        if allowed and active_path is not None and role != active_path:
            out.append(self._record(self._event(
                KIND_STANDBY_VERIFIED, "%s 备用链路已证实能直连上网" % label, moment)))
        return out

    def _route_events(self, op_plan: Sequence[Any], op_outcomes: Sequence[Any],
                      moment: float) -> List[TimelineEvent]:
        out: List[TimelineEvent] = []
        executed = [o for o in (op_outcomes or ())
                    if _value_of(getattr(o, "kind", None)) == _METRIC_WRITE
                    and bool(getattr(o, "executed", False))]
        planned = [r for r in (op_plan or ())
                   if _value_of(getattr(r, "kind", None)) == _METRIC_WRITE]
        for outcome in executed:
            key = (self._index, "done", str(getattr(outcome, "detail", "") or ""))
            if key in self._seen_ops:
                continue
            self._seen_ops.add(key)
            out.append(self._record(self._event(
                KIND_ROUTE_SWITCH_DONE,
                "默认出口切换已执行：%s" % (str(getattr(outcome, "detail", "") or "")
                                         or "已切换"), moment)))
        if executed:
            return out
        for request in planned:
            payload = getattr(request, "payload", None)
            target = payload.get("target") if isinstance(payload, dict) else None
            reason = str(getattr(request, "reason", "") or "")
            key = (self._index, "blocked", target, reason,
                   str(getattr(request, "detail", "") or ""))
            if key in self._seen_ops:
                continue
            self._seen_ops.add(key)
            out.append(self._record(self._event(
                KIND_ROUTE_SWITCH,
                "计划切换默认出口（目标 %s）但本轮未执行：%s"
                % (_role_label(target), reason or "等待接口受限验证通过"),
                moment, level=LEVEL_WARNING)))
        return out
