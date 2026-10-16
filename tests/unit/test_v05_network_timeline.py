# -*- coding: utf-8 -*-
"""V0.5-6 网络事件时间线（Recovery Event 可复盘日志，方案 §5.6）。

出口条件：每个恢复事件都留下 ``timestamp / network_epoch / active_path /
Ethernet state / WiFi state / verification result / route action / UU action /
final state`` 的**可复盘时间线**，并且用户能一眼看到三个事实：

    我是什么时候断的网？     → NETWORK_DOWN（含断因、两轴结论、当时主链路）
    你什么时候帮我连上的？   → NETWORK_UP / RECOVERY_COMPLETE（含恢复耗时）
    这段时间都干了什么？     → T+mm:ss 里程碑（验证 / 切换 / UU 清退 / 最终状态）

- ``L01..L42``：时间线自身契约（**纯逻辑**：零 I/O、零真实 sleep、零真实网络）；
- ``K01..K08``：托盘接线（真实 ``NetworkTimeline`` 装进真实 ``TrayApp``；探测 /
  验证 / 清退能力全部注入假件 ⇒ 零真实网络、零真实进程）。

核心不变量（每条用例都在守护它）：

    重复观测**不**产生重复事件；``None`` 入参表示"未观测"，绝不伪造迁移；
    ``Portal 认证成功 ≠ 能上网``（故障事件只由真实探测确认后结束）；
    时间线只写日志 —— 不判定、不切换、不清退、不读写凭据。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import campus_reconnect as cr
import campus_reconnect_tray as tray
import campus_remote_app as cra
import campus_timeline as tim
from campus_core import ProbeResult
from campus_failover import FailoverState, OpKind, OpOutcome, OpRequest
from campus_network import HealthState
from campus_recovery import PATH_ETHERNET, PATH_WIFI
from campus_tray_observer import ObservationResult
from tests.fakes import FakeRemoteAppOps
from tests.unit.test_tray_worker import wait_until
from tests.unit.test_v05_dual_path_integration import ScriptedVerifier, snapshot

LOG = logging.getLogger("campus")

H = HealthState.HEALTHY
D = HealthState.LINK_DOWN
C = HealthState.CAPTIVE_PORTAL
IU = HealthState.INTERNET_UNREACHABLE

WIFI_IF = 14
EPOCH = 1_760_000_000.0          # 固定挂钟：断言人类可读时刻用
UU_PROCESSES = [
    {"pid": 100, "process_name": "GameViewer",
     "path": r"C:\_study\GameViewer\GameViewer.exe"},
    {"pid": 200, "process_name": "GameViewerLauncher",
     "path": r"C:\_study\GameViewer\GameViewerLauncher.exe"},
]
UU_RUN_ENTRIES = [{"name": "GameViewer",
                   "command": r'"C:\_study\GameViewer\GameViewer.exe" --auto-run'}]


def wall_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 假件
# --------------------------------------------------------------------------- #
class Sink:
    """日志出口假件：收集 ``(level, text)``；可注入异常验证防御性边界。"""

    def __init__(self) -> None:
        self.items: List[Tuple[str, str]] = []
        self.fail = False

    def __call__(self, text: str, level: str = "info") -> None:
        if self.fail:
            raise RuntimeError("sink boom")
        self.items.append((str(level), str(text)))

    @property
    def texts(self) -> List[str]:
        return [text for _level, text in self.items]

    @property
    def levels(self) -> List[str]:
        return [level for level, _text in self.items]


class Rig:
    """时间线台架：可控单调时钟 + 可控挂钟 + 收集型日志出口。"""

    def __init__(self, *, confirmations: int = 1, max_events: int = 400,
                 max_episodes: int = 60) -> None:
        self.now = 0.0
        self.wall = EPOCH
        self.sink = Sink()
        self.messages: List[str] = []
        self.timeline = tim.NetworkTimeline(
            clock=lambda: self.now, wall_clock=lambda: self.wall, emit=self.sink,
            log=self.messages.append, offline_confirmations=confirmations,
            max_events=max_events, max_episodes=max_episodes)

    def tick(self, dt: float = 5.0, **kwargs: Any) -> tim.TimelineUpdate:
        self.now += float(dt)
        return self.timeline.observe(now=self.now, **kwargs)

    def kinds(self, update: tim.TimelineUpdate) -> List[str]:
        return [event.kind for event in update.events]


def _probe(source: str, ok: bool = True) -> ProbeResult:
    return ProbeResult(ok=ok, source=source, status_code=200 if ok else None,
                       error=None if ok else "timeout")


def verified(role: str = PATH_WIFI, *, gate_ok: bool = True, hits: int = 2,
             cross: int = 3, ok: bool = True,
             reliability: str = cr.PATH_OK,
             reason: Optional[str] = None) -> cr.PathVerificationResult:
    """一份接口受限验证结果（闸门 + 交叉，逐站可读）。"""
    names = ("qq", "163", "sohu")[:int(cross)]
    cross_results = tuple(_probe("http://www.%s.com/" % name, index < hits)
                          for index, name in enumerate(names))
    good = tuple("www.%s.com" % name for index, name in enumerate(names)
                 if index < hits)
    bad = tuple("www.%s.com" % name for index, name in enumerate(names)
                if index >= hits)
    if reason is None:
        reason = ("接口受限验证通过（闸门 + 交叉 %d/%d）" % (hits, cross) if ok
                  else "交叉命中不足（%d/%d，需 ≥2）" % (hits, cross))
    return cr.PathVerificationResult(
        verified=bool(ok), role=role, interface_index=WIFI_IF,
        source_ip="10.20.30.40", gate_result=_probe("http://www.baidu.com/", gate_ok),
        cross_results=cross_results, successful_sites=good, failed_sites=bad,
        reason=reason, reliability=reliability)


def open_episode(rig: Rig, *, dt: float = 5.0) -> tim.Episode:
    """把一个故障事件推进到「已断网」。"""
    rig.tick(dt, offline=False, failover_state=FailoverState.ETHERNET_PRIMARY,
             ethernet_state=H, wifi_state=D, active_path=PATH_ETHERNET)
    rig.tick(dt, offline=True, failover_state=FailoverState.BOTH_OFFLINE,
             ethernet_state=IU, wifi_state=D, active_path=PATH_ETHERNET)
    episode = rig.timeline.current()
    assert episode is not None
    return episode


# --------------------------------------------------------------------------- #
# L01–L06：断网 / 恢复时刻与耗时
# --------------------------------------------------------------------------- #
def test_l01_offline_opens_one_episode_recording_the_down_moment():
    rig = Rig()
    update = rig.tick(5.0, offline=True, failover_state=FailoverState.BOTH_OFFLINE,
                      ethernet_state=IU, wifi_state=C, active_path=PATH_ETHERNET)
    assert update.started is True and update.index == 1
    episode = rig.timeline.current()
    assert episode is not None and episode.ongoing
    assert episode.started_at == rig.now
    assert episode.started_wall == wall_of(EPOCH)
    assert "主链路 以太网" in episode.reason
    assert "以太网 已连接（公网不可达）" in episode.reason
    assert "Wi-Fi 已连接（需要认证）" in episode.reason
    assert rig.kinds(update) == [tim.KIND_NETWORK_DOWN]
    event = update.events[0]
    assert event.level == tim.LEVEL_WARNING
    assert event.offset == 0.0 and event.epoch == 1
    assert event.wall == wall_of(EPOCH) and event.clock == wall_of(EPOCH)[11:]


def test_l02_staying_offline_never_opens_a_second_episode():
    rig = Rig()
    open_episode(rig)
    first = rig.timeline.current()
    again = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=D)
    assert again.started is False and again.index == 1
    assert tim.KIND_NETWORK_DOWN not in rig.kinds(again)
    assert rig.timeline.current() is first
    assert len(rig.timeline.episodes()) == 1


def test_l03_online_closes_the_episode_with_duration_and_restored_path():
    rig = Rig()
    open_episode(rig)
    update = rig.tick(20.0, offline=False,
                      failover_state=FailoverState.WIFI_BACKUP_ACTIVE,
                      active_path=PATH_WIFI, wifi_state=H)
    assert update.finished is True and update.episode is None
    assert rig.timeline.current() is None
    assert rig.kinds(update)[-2:] == [tim.KIND_NETWORK_UP, tim.KIND_RECOVERY_COMPLETE]
    complete = update.events[-1]
    assert "耗时 20s" in complete.detail and "主链路 Wi-Fi" in complete.detail
    assert "WIFI_BACKUP_ACTIVE" in complete.detail
    episode = rig.timeline.last_episode()
    assert episode.ended_at == rig.now and episode.duration == 20.0
    assert episode.recovered_path == PATH_WIFI
    assert episode.final_state == "WIFI_BACKUP_ACTIVE"
    assert episode.ongoing is False


def test_l04_online_to_online_records_nothing():
    rig = Rig()
    update = rig.tick(5.0, offline=False, failover_state=FailoverState.ETHERNET_PRIMARY,
                      ethernet_state=H, wifi_state=H, active_path=PATH_ETHERNET)
    assert update.events == () and update.started is False
    assert update.finished is False and rig.timeline.current() is None
    assert rig.timeline.last_episode() is None
    assert rig.sink.items == []


def test_l05_second_outage_gets_its_own_index_and_its_own_duration():
    rig = Rig()
    open_episode(rig)                       # #1：now=10
    rig.tick(30.0, offline=False, active_path=PATH_WIFI)
    assert rig.timeline.last_episode().duration == 30.0

    rig.tick(60.0, offline=True, ethernet_state=IU, wifi_state=D)
    rig.tick(7.0, offline=False, active_path=PATH_ETHERNET, ethernet_state=H)
    assert rig.timeline.index == 2
    second = rig.timeline.last_episode()
    assert second.index == 2 and second.duration == 7.0
    assert rig.timeline.episodes()[0].duration == 30.0


def test_l06_milestones_carry_offsets_relative_to_the_outage():
    rig = Rig()
    open_episode(rig)                       # 断网于 rig.now
    started = rig.now
    rig.tick(6.0, offline=True, failover_state=FailoverState.WIFI_PREPARING,
             ethernet_state=IU, wifi_state=H, active_path=PATH_ETHERNET)
    rig.tick(9.0, offline=True, failover_state=FailoverState.WIFI_SWITCHING,
             verification={PATH_WIFI: (rig.now, verified())},
             active_path=PATH_ETHERNET)
    rig.tick(4.0, offline=False, failover_state=FailoverState.WIFI_BACKUP_ACTIVE,
             active_path=PATH_WIFI)
    episode = rig.timeline.last_episode()
    pairs = [(event.kind, event.offset) for event in episode.milestones]
    assert (tim.KIND_NETWORK_DOWN, 0.0) in pairs
    assert (tim.KIND_FAILOVER_STATE, 0.0) in pairs
    assert (tim.KIND_FAILOVER_STATE, 6.0) in pairs
    assert (tim.KIND_INTERNET_VERIFIED, 15.0) in pairs
    assert (tim.KIND_RECOVERY_COMPLETE, 19.0) in pairs
    assert all(event.epoch == episode.index for event in episode.milestones)
    assert episode.duration == 19.0
    assert rig.now - started == 19.0


# --------------------------------------------------------------------------- #
# L07–L10：状态变更（网络状态变更逐条留痕）
# --------------------------------------------------------------------------- #
def test_l07_axis_state_change_is_recorded_once_with_raw_and_chinese_labels():
    rig = Rig()
    rig.tick(5.0, offline=False, ethernet_state=H, wifi_state=D)
    update = rig.tick(5.0, offline=False, ethernet_state=IU, wifi_state=D)
    assert rig.kinds(update) == [tim.KIND_AXIS_STATE]
    event = update.events[0]
    assert event.detail == "以太网 正常 → 已连接（公网不可达）（HEALTHY → INTERNET_UNREACHABLE）"
    assert event.level == tim.LEVEL_WARNING
    assert rig.tick(5.0, offline=False, ethernet_state=IU, wifi_state=D).events == ()


def test_l08_unobserved_state_never_overwrites_or_fakes_a_change():
    rig = Rig()
    rig.tick(5.0, offline=False, ethernet_state=IU, wifi_state=D)
    assert rig.tick(5.0, offline=False, wifi_state=D).events == ()     # ethernet 未观测
    assert rig.tick(5.0, offline=False, ethernet_state=IU).events == ()  # 仍是旧状态
    update = rig.tick(5.0, offline=False, ethernet_state=H)
    assert rig.kinds(update) == [tim.KIND_AXIS_STATE]
    assert "已连接（公网不可达） → 正常" in update.events[0].detail


def test_l09_failover_state_change_is_recorded():
    rig = Rig()
    rig.tick(5.0, offline=False, failover_state=FailoverState.ETHERNET_PRIMARY)
    update = rig.tick(5.0, offline=False, failover_state=FailoverState.ETHERNET_SUSPECT)
    assert rig.kinds(update) == [tim.KIND_FAILOVER_STATE]
    assert update.events[0].detail == "状态机 ETHERNET_PRIMARY → ETHERNET_SUSPECT"
    assert update.events[0].level == tim.LEVEL_INFO


def test_l10_active_path_change_is_recorded_once():
    rig = Rig()
    rig.tick(5.0, offline=False, active_path=PATH_ETHERNET)     # 首次观测：不记
    update = rig.tick(5.0, offline=False, active_path=PATH_WIFI)
    assert rig.kinds(update) == [tim.KIND_ACTIVE_PATH]
    assert update.events[0].detail == "主链路 以太网 → Wi-Fi"
    assert rig.tick(5.0, offline=False, active_path=PATH_WIFI).events == ()


# --------------------------------------------------------------------------- #
# L11–L17：接口受限验证结果（闸门 / 交叉 / 结论）
# --------------------------------------------------------------------------- #
def test_l11_passing_verification_emits_gate_cross_verdict_and_standby():
    rig = Rig()
    update = rig.tick(5.0, offline=False, active_path=PATH_ETHERNET,
                      verification={PATH_WIFI: (rig.now, verified())})
    assert rig.kinds(update) == [tim.KIND_VERIFY_GATE, tim.KIND_VERIFY_CROSS,
                                 tim.KIND_INTERNET_VERIFIED,
                                 tim.KIND_STANDBY_VERIFIED]
    assert update.events[0].detail == "Wi-Fi 百度闸门 PASS（www.baidu.com(ok)）"
    assert update.events[0].level == tim.LEVEL_INFO
    assert update.events[1].detail == "Wi-Fi 交叉命中 2/3（失败：www.sohu.com）"
    assert "接口受限验证通过（可直连上网）" in update.events[2].detail
    assert update.events[3].detail == "Wi-Fi 备用链路已证实能直连上网"


def test_l12_the_same_verification_record_is_never_recorded_twice():
    rig = Rig()
    record = (rig.now + 5.0, verified())
    rig.tick(5.0, offline=False, active_path=PATH_ETHERNET,
             verification={PATH_WIFI: record})
    assert rig.tick(5.0, offline=False, active_path=PATH_ETHERNET,
                    verification={PATH_WIFI: record}).events == ()
    assert rig.tick(5.0, offline=False, active_path=PATH_ETHERNET,
                    verification={PATH_WIFI: record}).events == ()


def test_l13_a_newer_verification_record_is_recorded_again():
    rig = Rig()
    rig.tick(5.0, offline=False, verification={PATH_WIFI: (rig.now, verified())})
    update = rig.tick(5.0, offline=False,
                      verification={PATH_WIFI: (rig.now, verified())})
    assert tim.KIND_INTERNET_VERIFIED in rig.kinds(update)


def test_l14_failed_verification_is_a_warning_with_no_standby_credit():
    rig = Rig()
    update = rig.tick(5.0, offline=False, active_path=PATH_ETHERNET,
                      verification={PATH_WIFI: (rig.now, verified(
                          gate_ok=False, hits=0, ok=False,
                          reason="闸门未通过（www.baidu.com(timeout)）"))})
    assert rig.kinds(update) == [tim.KIND_VERIFY_GATE, tim.KIND_VERIFY_CROSS,
                                 tim.KIND_INTERNET_UNVERIFIED]
    assert update.events[0].level == tim.LEVEL_WARNING
    assert "百度闸门 FAIL" in update.events[0].detail
    assert update.events[1].level == tim.LEVEL_WARNING
    assert update.events[2].level == tim.LEVEL_WARNING
    assert tim.KIND_STANDBY_VERIFIED not in rig.kinds(update)


def test_l15_light_gate_only_result_has_no_cross_event():
    rig = Rig()
    update = rig.tick(5.0, offline=False, active_path=PATH_WIFI,
                      verification={PATH_WIFI: (rig.now, verified(cross=0, hits=0))})
    assert rig.kinds(update) == [tim.KIND_VERIFY_GATE, tim.KIND_INTERNET_VERIFIED]
    assert update.events[0].detail.startswith("Wi-Fi 百度闸门 PASS")


def test_l16_unreliable_probe_is_reported_and_never_becomes_evidence():
    rig = Rig()
    update = rig.tick(5.0, offline=False, active_path=PATH_WIFI,
                      verification={PATH_WIFI: (rig.now, verified(
                          ok=False, gate_ok=False, hits=0,
                          reliability=cr.PATH_PROBE_UNRELIABLE,
                          reason="PROBE_UNRELIABLE：接口约束失败"))})
    assert rig.kinds(update) == [tim.KIND_VERIFY_UNRELIABLE]
    assert update.events[0].level == tim.LEVEL_WARNING
    assert "不切换、不重启 UU" in update.events[0].detail
    assert "PROBE_UNRELIABLE" in update.events[0].detail


def test_l17_verification_of_the_active_path_has_no_standby_event():
    rig = Rig()
    update = rig.tick(5.0, offline=False, active_path=PATH_WIFI,
                      verification={PATH_WIFI: (rig.now, verified())})
    assert tim.KIND_STANDBY_VERIFIED not in rig.kinds(update)
    assert tim.KIND_INTERNET_VERIFIED in rig.kinds(update)


# --------------------------------------------------------------------------- #
# L18–L21：路由动作（什么时候切换的链路 / 为什么没切）
# --------------------------------------------------------------------------- #
def _metric_outcome(*, executed: bool, attempted: bool = True,
                    gate_passed: bool = True,
                    detail: str = "已执行") -> OpOutcome:
    return OpOutcome(OpKind.METRIC_WRITE, gate_passed=gate_passed,
                     attempted=attempted, executed=executed, detail=detail)


def test_l18_executed_route_switch_is_a_milestone():
    rig = Rig()
    update = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=H,
                      op_outcomes=[_metric_outcome(executed=True,
                                                   detail="默认出口已改为 Wi-Fi")])
    assert tim.KIND_ROUTE_SWITCH_DONE in rig.kinds(update)
    done = [e for e in update.events if e.kind == tim.KIND_ROUTE_SWITCH_DONE][0]
    assert done.detail == "默认出口切换已执行：默认出口已改为 Wi-Fi"
    assert done.level == tim.LEVEL_INFO


def test_l19_executed_route_switch_is_not_repeated_every_tick():
    rig = Rig()
    outcome = _metric_outcome(executed=True)
    rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=H,
             op_outcomes=[outcome])
    update = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=H,
                      op_outcomes=[outcome])
    assert tim.KIND_ROUTE_SWITCH_DONE not in rig.kinds(update)


def test_l20_planned_but_unexecuted_switch_is_reported_once_as_a_warning():
    rig = Rig()
    open_episode(rig)
    plan = [OpRequest(OpKind.METRIC_WRITE,
                      "以太网确认故障且 Wi-Fi 路径已证实可用：将 Wi-Fi 设为优先出口",
                      {"target": PATH_WIFI})]
    update = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=D, op_plan=plan)
    assert rig.kinds(update) == [tim.KIND_ROUTE_SWITCH]
    event = update.events[0]
    assert event.level == tim.LEVEL_WARNING
    assert "目标 Wi-Fi" in event.detail and "未执行" in event.detail
    assert "将 Wi-Fi 设为优先出口" in event.detail
    repeat = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=D, op_plan=plan)
    assert tim.KIND_ROUTE_SWITCH not in rig.kinds(repeat)


def test_l21_non_metric_write_actions_are_not_route_events():
    rig = Rig()
    update = rig.tick(
        5.0, offline=True, ethernet_state=IU, wifi_state=C,
        op_plan=[OpRequest(OpKind.PORTAL_POST, "Portal 认证", {"target": PATH_WIFI})],
        op_outcomes=[OpOutcome(OpKind.PORTAL_POST, gate_passed=True,
                               attempted=True, executed=True, detail="已提交")])
    assert tim.KIND_ROUTE_SWITCH not in rig.kinds(update)
    assert tim.KIND_ROUTE_SWITCH_DONE not in rig.kinds(update)
    assert tim.KIND_NETWORK_DOWN in rig.kinds(update)


# --------------------------------------------------------------------------- #
# L22–L24：可信度 / 恢复事件 / 恢复阶段
# --------------------------------------------------------------------------- #
def test_l22_untrusted_snapshot_is_reported_once_and_only_once():
    rig = Rig()
    update = rig.tick(5.0, offline=True, trusted=False, detail="快照过期")
    assert tim.KIND_SNAPSHOT_UNTRUSTED in rig.kinds(update)
    assert "快照过期" in update.events[-1].detail
    assert rig.tick(5.0, offline=True, trusted=False).events == ()
    assert rig.tick(5.0, offline=True, trusted=False).events == ()


def test_l23_unobserved_trust_does_not_reset_the_dedupe():
    rig = Rig()
    rig.tick(5.0, offline=True, trusted=False)
    rig.tick(5.0, offline=True)                       # trusted=None：不更新
    assert rig.tick(5.0, offline=True, trusted=False).events == ()
    assert rig.tick(5.0, offline=True, trusted=True).events == ()
    update = rig.tick(5.0, offline=True, trusted=False)
    assert tim.KIND_SNAPSHOT_UNTRUSTED in rig.kinds(update)


def test_l24_recovery_event_and_stage_are_recorded_once_per_change():
    rig = Rig()
    rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=H,
             active_path=PATH_ETHERNET, recovery_event="STANDBY_CONNECTED",
             recovery_steps=("detection", "backup_ready"))
    update = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=H,
                      active_path=PATH_ETHERNET, recovery_event="STANDBY_CONNECTED",
                      recovery_steps=("detection", "backup_ready"))
    assert update.events == ()
    update = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=H,
                      active_path=PATH_ETHERNET, recovery_event="FAILOVER_COMPLETED",
                      recovery_steps=("detection", "backup_ready", "route_switch"))
    assert rig.kinds(update) == [tim.KIND_RECOVERY_EVENT, tim.KIND_RECOVERY_STAGE]
    assert update.events[0].detail == "恢复事件 FAILOVER_COMPLETED（主链路 以太网）"
    assert "route_switch" in update.events[1].detail


# --------------------------------------------------------------------------- #
# L25–L28：UU 恢复（流水线最后一步）
# --------------------------------------------------------------------------- #
class _Request:
    """``UuRestartRequest`` 的最小鸭子替身（时间线只读字段，不判定）。"""

    def __init__(self, active_path: str = PATH_WIFI, epoch: int = 3,
                 reason: str = "恢复事件 + 接口受限验证通过") -> None:
        self.active_path = active_path
        self.network_epoch = epoch
        self.reason = reason


def test_l25_uu_started_and_restarted_attach_to_the_open_episode():
    rig = Rig()
    episode = open_episode(rig)
    rig.timeline.note_uu_started(_Request(), now=rig.now + 1.0)
    rig.timeline.note_uu_outcome(cra.UuRecoveryResult(
        ok=True, action="restart", detail="已清退 2 个用户态进程并按自启项重启",
        restarted=True), now=rig.now + 3.0)
    kinds = [event.kind for event in episode.milestones]
    assert kinds[-2:] == [tim.KIND_UU_RESTART_STARTED, tim.KIND_UU_RESTARTED]
    started = episode.milestones[-2]
    assert "主链路 Wi-Fi" in started.detail and "epoch=3" in started.detail
    assert started.offset == 1.0 and started.epoch == episode.index
    assert episode.milestones[-1].offset == 3.0
    assert rig.sink.levels[-1] == tim.LEVEL_INFO


def test_l26_uu_failure_and_skip_carry_their_own_levels():
    rig = Rig()
    open_episode(rig)
    failure = rig.timeline.note_uu_outcome(cra.UuRecoveryResult(
        ok=False, action="recheck-failed", detail="清退前轻量再确认未通过"))
    assert failure.kind == tim.KIND_UU_RESTART_FAILED
    assert failure.level == tim.LEVEL_WARNING and "recheck-failed" in failure.detail
    skipped = rig.timeline.note_uu_outcome(cra.UuRecoveryResult(
        ok=True, action="noop", detail="UU 未在运行"))
    assert skipped.kind == tim.KIND_UU_SKIPPED
    assert skipped.level == tim.LEVEL_INFO and "无需重启" in skipped.detail


def test_l27_late_uu_outcome_still_lands_in_the_episode_recap():
    rig = Rig()
    open_episode(rig)                        # 断网
    rig.tick(10.0, offline=False, active_path=PATH_WIFI)   # 恢复（20s 处）
    episode = rig.timeline.last_episode()
    assert episode.ended_at is not None
    rig.timeline.note_uu_outcome(cra.UuRecoveryResult(
        ok=True, action="restart", detail="已清退重启", restarted=True),
        now=rig.now + 4.0)
    late = [event for event in episode.milestones
            if event.kind == tim.KIND_UU_RESTARTED]
    assert len(late) == 1
    assert late[0].epoch == episode.index
    assert late[0].offset == episode.duration + 4.0
    assert any("GAMEVIEWER_RESTARTED" in line
               for line in rig.timeline.report_lines())


def test_l28_late_attachment_is_bounded_by_the_window():
    rig = Rig()
    open_episode(rig)
    rig.tick(10.0, offline=False, active_path=PATH_WIFI)
    episode = rig.timeline.last_episode()
    rig.now += tim.LATE_ATTACH_WINDOW + 1.0
    event = rig.timeline.note_reconnect(True, detail="很久以后的手动重连", now=rig.now)
    assert event.epoch == 0 and event.offset is None
    assert event not in episode.milestones


# --------------------------------------------------------------------------- #
# L29–L33：复盘输出 / 日志出口 / 暂停
# --------------------------------------------------------------------------- #
def test_l29_recap_lines_answer_when_it_broke_and_how_long():
    rig = Rig()
    open_episode(rig)
    rig.tick(6.0, offline=True, failover_state=FailoverState.WIFI_PREPARING,
             ethernet_state=IU, wifi_state=H)
    rig.tick(9.0, offline=False, failover_state=FailoverState.WIFI_BACKUP_ACTIVE,
             active_path=PATH_WIFI, ethernet_state=IU, wifi_state=H)
    lines = rig.timeline.report_lines()
    assert all(line.startswith("【故障事件 #1】") for line in lines)
    assert "断网时刻：" in lines[0] and wall_of(EPOCH) in lines[0]
    assert "恢复时刻：" in lines[1] and "恢复耗时 15s" in lines[1]
    assert "主链路：Wi-Fi" in lines[1]
    body = "\n".join(lines[2:])
    assert "T+00:00 NETWORK_DOWN" in body
    assert "T+00:00 FAILOVER_STATE" in body
    assert "T+00:06 FAILOVER_STATE" in body
    assert "T+00:15 NETWORK_UP" in body
    assert "T+00:15 RECOVERY_COMPLETE" in body
    assert lines[-2].endswith("") and "NETWORK_UP" in lines[-2]
    assert "RECOVERY_COMPLETE" in lines[-1]


def test_l30_emit_sink_receives_every_event_and_the_recap():
    rig = Rig()
    open_episode(rig)
    rig.tick(10.0, offline=False, active_path=PATH_WIFI)
    texts = rig.sink.texts
    assert any("NETWORK_DOWN" in text for text in texts)
    assert any("NETWORK_UP" in text for text in texts)
    assert any(text.startswith("【故障事件 #1】断网时刻：") for text in texts)
    assert rig.sink.levels[0] == tim.LEVEL_WARNING          # 断网 = 警告级
    assert tim.LEVEL_INFO in rig.sink.levels


def test_l31_a_broken_sink_never_breaks_the_timeline_state():
    rig = Rig()
    rig.sink.fail = True
    episode = rig.timeline.observe(now=5.0, offline=True, ethernet_state=IU,
                                   wifi_state=D, active_path=PATH_ETHERNET)
    assert episode.started is True and rig.timeline.current() is not None
    assert rig.timeline.recent(), "事件仍必须入台账"
    assert any("时间线日志写入失败" in message for message in rig.messages)


def test_l32_pausing_aborts_the_episode_without_faking_a_recovery():
    rig = Rig()
    open_episode(rig)
    rig.timeline.note_paused(now=rig.now)
    episode = rig.timeline.last_episode()
    assert episode.aborted is True and episode.ongoing is False
    assert episode.end_reason == "监测暂停"
    assert episode.recovered_path is None
    assert rig.timeline.current() is None
    lines = rig.timeline.report_lines()
    assert "记录结束：" in lines[1] and "未能观测到恢复时刻" in lines[1]
    assert not any("NETWORK_UP" in line for line in lines)


def test_l33_paused_is_reported_once_and_resume_is_recorded():
    rig = Rig()
    rig.timeline.observe(now=1.0, paused=True)
    rig.timeline.observe(now=2.0, paused=True)
    rig.timeline.observe(now=3.0, paused=True)
    paused = [event for event in rig.timeline.recent()
              if event.kind == tim.KIND_MONITOR_PAUSED]
    assert len(paused) == 1
    update = rig.timeline.observe(now=4.0, offline=False)
    assert tim.KIND_MONITOR_RESUMED in [event.kind for event in update.events]
    assert rig.timeline.current() is None


def test_l34_pause_while_offline_then_resume_does_not_invent_a_recovery():
    rig = Rig()
    open_episode(rig)
    rig.timeline.note_paused(now=rig.now)
    update = rig.timeline.observe(now=rig.now, offline=False, active_path=PATH_WIFI)
    kinds = [event.kind for event in update.events]
    assert tim.KIND_MONITOR_RESUMED in kinds
    assert tim.KIND_NETWORK_UP not in kinds and tim.KIND_RECOVERY_COMPLETE not in kinds
    assert rig.timeline.last_episode().aborted is True


# --------------------------------------------------------------------------- #
# L35–L42：摘要 / 确认次数 / 边界 / 接线契约
# --------------------------------------------------------------------------- #
def test_l35_status_lines_and_outage_summary_show_the_times():
    empty = Rig()
    assert empty.timeline.status_lines() == []
    assert empty.timeline.outage_summary() == "尚无网络故障事件记录"

    rig = Rig()
    open_episode(rig)
    live = rig.timeline.status_lines(limit=2)
    assert live[0].startswith("⚠ 断网中：") and "已持续" in live[0]
    assert rig.timeline.outage_summary().startswith("故障事件 #1：")
    assert "尚未恢复" in rig.timeline.outage_summary()

    rig.tick(25.0, offline=False, active_path=PATH_WIFI)
    done = rig.timeline.status_lines(limit=1)
    assert done[0].startswith("最近一次断网：") and "耗时 25s" in done[0]
    assert "主链路 Wi-Fi" in done[0]
    assert "耗时 25s" in rig.timeline.outage_summary()


def test_l36_offline_confirmations_require_repeated_failures():
    rig = Rig(confirmations=2)
    first = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=D)
    assert first.started is False and rig.timeline.current() is None
    assert rig.kinds(first) == [tim.KIND_DEGRADED]
    assert rig.kinds(first)[0] == tim.KIND_DEGRADED
    second = rig.tick(5.0, offline=True, ethernet_state=IU, wifi_state=D)
    assert second.started is True and rig.timeline.index == 1
    assert rig.kinds(second) == [tim.KIND_NETWORK_DOWN]


def test_l37_recent_and_episodes_are_bounded_rings():
    rig = Rig(max_events=12, max_episodes=2)
    for index in range(6):
        rig.tick(5.0, offline=True, failover_state="STATE_%d" % index)
        rig.tick(5.0, offline=False)
    assert len(rig.timeline.episodes()) == 2
    assert len(rig.timeline.recent()) <= 12
    assert rig.timeline.recent(3) == rig.timeline.recent()[-3:]


def test_l38_to_dict_is_plain_json_and_carries_no_credentials():
    rig = Rig()
    open_episode(rig)
    rig.tick(10.0, offline=False, active_path=PATH_WIFI)
    payload = rig.timeline.to_dict()
    blob = json.dumps(payload)
    assert payload["index"] == 1 and payload["episodes"] == 1
    assert payload["open"] is None and payload["last"]["duration"] == 10.0
    for forbidden in ("password", "Cookie", "Cookie:", "password="):
        assert forbidden not in blob


def test_l39_standby_path_is_informational_only():
    rig = Rig()
    rig.tick(5.0, offline=False, active_path=PATH_ETHERNET, standby_path=PATH_WIFI)
    update = rig.tick(5.0, offline=False, active_path=PATH_ETHERNET,
                      standby_path=PATH_WIFI)
    assert update.events == ()


def test_l40_reconnect_success_is_a_milestone_but_not_a_recovery():
    rig = Rig()
    episode = open_episode(rig)
    event = rig.timeline.note_reconnect(True, detail="Portal 认证完成", elapsed=12.3,
                                        now=rig.now + 2.0)
    assert event.kind == tim.KIND_RECONNECT_OK and event.level == tim.LEVEL_INFO
    assert "用时 12s" in event.detail
    assert rig.timeline.current() is episode, "认证成功不等于能上网（方案 §1）"
    rig.tick(10.0, offline=False, active_path=PATH_ETHERNET)
    assert rig.timeline.current() is None


def test_l41_reconnect_failure_is_a_warning_milestone():
    rig = Rig()
    open_episode(rig)
    event = rig.timeline.note_reconnect(False, detail="Portal 未识别账号", elapsed=9.0)
    assert event.kind == tim.KIND_RECONNECT_FAILED
    assert event.level == tim.LEVEL_WARNING and "未识别账号" in event.detail


def test_l42_offline_none_never_opens_or_closes_an_episode():
    rig = Rig()
    update = rig.tick(5.0, offline=None, failover_state=FailoverState.ETHERNET_PRIMARY)
    assert update.started is False and rig.timeline.current() is None
    open_episode(rig)
    update = rig.tick(5.0, offline=None, active_path=PATH_ETHERNET)
    assert update.finished is False and rig.timeline.current() is not None


# --------------------------------------------------------------------------- #
# K01–K08：托盘接线
# --------------------------------------------------------------------------- #
def _make_tray(monkeypatch, **overrides: Any) -> tray.TrayApp:
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    monkeypatch.setattr(app, "notify", lambda *a, **k: None)
    return app


def _collect(app: tray.TrayApp, monkeypatch: Any) -> Sink:
    """给托盘换一条**记录型**时间线（同样真实实现，只换出口）。"""
    sink = Sink()
    app.timeline = tim.NetworkTimeline(
        clock=lambda: tray.time.monotonic(), wall_clock=lambda: EPOCH, emit=sink,
        log=lambda message: None)
    return sink


def test_k01_tray_timeline_records_the_outage_moment(monkeypatch):
    app = _make_tray(monkeypatch)
    sink = _collect(app, monkeypatch)
    monkeypatch.setattr(app, "_build_failover_snapshot",
                        lambda online: snapshot(D, D, egress_if=None))
    app._failover_tick(10.0, online=False)
    episode = app.timeline.current()
    assert episode is not None and episode.index == 1
    assert episode.started_at == 10.0
    assert any("NETWORK_DOWN" in text for text in sink.texts)
    assert any("SNAPSHOT_UNTRUSTED" in text for text in sink.texts), \
        "外部注入的快照未经新鲜度校验 ⇒ 如实标注不可信"


def test_k02_tray_timeline_receives_real_state_and_active_path(monkeypatch):
    app = _make_tray(monkeypatch)
    calls: List[Dict[str, Any]] = []

    class FakeTimeline:
        def observe(self, **kwargs):
            calls.append(kwargs)
            return None

    app.timeline = FakeTimeline()
    app.failover_status = type("Status", (), {
        "state": FailoverState.WIFI_BACKUP_ACTIVE, "primary": PATH_WIFI,
        "backup": PATH_ETHERNET, "warnings": (), "remote_warnings": ()})()
    snap = snapshot(IU, H, egress_if=WIFI_IF)
    app.last_observation = ObservationResult(snapshot=snap, trusted=True,
                                             reason="fresh", complete=True)
    app.dual_path_tick = None
    app._timeline_observe(10.0, online=True, snapshot=snap, result=None)

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["offline"] is False and kwargs["trusted"] is True
    assert kwargs["failover_state"] is FailoverState.WIFI_BACKUP_ACTIVE
    assert kwargs["ethernet_state"] is IU and kwargs["wifi_state"] is H
    assert kwargs["active_path"] == PATH_WIFI
    assert kwargs["standby_path"] == PATH_ETHERNET
    assert kwargs["final_state"] == "WIFI_BACKUP_ACTIVE"
    assert kwargs["op_plan"] == () and kwargs["op_outcomes"] == ()


def test_k03_tray_timeline_survives_a_broken_controller(monkeypatch):
    app = _make_tray(monkeypatch)
    sink = _collect(app, monkeypatch)

    class Boom:
        def status(self, **kwargs):
            raise RuntimeError("controller down")

    app.dual_path = Boom()
    app._timeline_observe(10.0, online=False)
    assert app.timeline.current() is not None, "整合层异常不得阻断时间线记录"
    assert any("NETWORK_DOWN" in text for text in sink.texts)
    app.dual_path = None


def test_k04_tray_pausing_aborts_the_episode_without_fake_recovery(monkeypatch):
    app = _make_tray(monkeypatch)
    sink = _collect(app, monkeypatch)
    monkeypatch.setattr(app, "_build_failover_snapshot",
                        lambda online: snapshot(IU, D, egress_if=None))
    app._failover_tick(5.0, online=False)
    assert app.timeline.current() is not None

    app.config["paused"] = True
    app._probe_tick(0.0)                      # 暂停分支：不探测、不伪造恢复
    assert app.timeline.current() is None
    episode = app.timeline.last_episode()
    assert episode.aborted is True and episode.recovered_path is None
    assert any("MONITOR_PAUSED" in text for text in sink.texts)
    assert not any("NETWORK_UP" in text for text in sink.texts)


def test_k05_tray_reconnect_milestones_never_close_the_episode(monkeypatch):
    app = _make_tray(monkeypatch)
    sink = _collect(app, monkeypatch)
    monkeypatch.setattr(app, "_maybe_verify_online", lambda: None)
    monkeypatch.setattr(app, "_build_failover_snapshot",
                        lambda online: snapshot(IU, D, egress_if=None))
    app._failover_tick(5.0, online=False)
    episode = app.timeline.current()
    assert episode is not None

    app._after_repair(True, "Portal 认证完成", 12.0)
    assert app.timeline.current() is episode, "认证成功 ≠ 能上网（方案 §1）"
    app._after_repair(False, "Portal 认证失败", 3.0)
    assert app.timeline.current() is episode
    kinds = [event.kind for event in episode.milestones]
    assert tim.KIND_RECONNECT_OK in kinds and tim.KIND_RECONNECT_FAILED in kinds
    assert any(level == tim.LEVEL_WARNING and "RECONNECT_FAILED" in text
               for level, text in sink.items)


def test_k06_tray_exposes_the_timeline_to_the_ui(monkeypatch):
    app = _make_tray(monkeypatch)
    assert app.timeline_report_lines() == ["尚无网络故障事件记录"]
    assert app.timeline_status_lines() == []

    _collect(app, monkeypatch)
    monkeypatch.setattr(app, "_build_failover_snapshot",
                        lambda online: snapshot(IU, D, egress_if=None))
    app._failover_tick(5.0, online=False)
    report = app.timeline_report_lines()
    assert "断网时刻：" in report[0] and "尚未恢复" in report[1]
    status = app.timeline_status_lines(limit=2)
    assert status[0].startswith("⚠ 断网中：")
    assert any("NETWORK_DOWN" in line for line in status)


def test_k07_tray_uu_recovery_outcome_lands_in_the_timeline(monkeypatch):
    app = _make_tray(monkeypatch)
    sink = _collect(app, monkeypatch)
    app._path_verifier = ScriptedVerifier()
    app._get_remote_app_ops = lambda: FakeRemoteAppOps(
        processes=UU_PROCESSES, run_entries=UU_RUN_ENTRIES)
    app._get_restart_guard = lambda: cra.RestartGuard(cooldown=0.0)
    app._uu_recovery_ledger = lambda: cra.RecoveryRestartLedger()

    def boom(*args, **kwargs):
        raise RuntimeError("清退失败")

    monkeypatch.setattr(cra, "restart_after_verified_recovery", boom)
    app._uu_recovery_async(_Request(), verified())
    assert wait_until(lambda: any("UU_RESTART_STARTED" in text for text in sink.texts))
    assert wait_until(lambda: any("UU_RESTART_FAILED" in text for text in sink.texts))


def test_k08_tray_manual_uu_restart_fail_closed_is_recorded(monkeypatch):
    app = _make_tray(monkeypatch)
    sink = _collect(app, monkeypatch)
    app.last_observation = ObservationResult()
    app.failover_status = type("Status", (), {
        "state": FailoverState.ETHERNET_PRIMARY, "primary": PATH_ETHERNET,
        "backup": PATH_WIFI, "warnings": (), "remote_warnings": ()})()
    app._manual_uu_restart_run()          # 缺接口事实 ⇒ fail-closed，不动作
    assert any("UU_RESTART_FAILED" in text for text in sink.texts), sink.texts
    assert any("fail-closed" in text for text in sink.texts)


def test_k09_ui_state_lines_still_work_with_the_timeline_wired(monkeypatch):
    app = _make_tray(monkeypatch)
    lines = app.ui_state_lines()
    assert lines and any("UI 状态映射失败" not in line for line in lines)
    assert app.timeline_status_lines(limit=1) == []
    assert isinstance(app._path_roles(), tuple)
