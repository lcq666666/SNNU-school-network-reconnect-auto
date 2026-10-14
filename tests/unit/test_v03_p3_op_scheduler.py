# -*- coding: utf-8 -*-
"""V0.3 P3 单一副作用门（``NetworkOpScheduler``）永久看守测试（U-3-20..U-3-25）。

依据：执行计划 §0.4 —— 全系统只允许**一个**副作用执行点，Wi-Fi 重连 /
Portal POST / Metric 写入 / Clash 触碰四类动作**互斥串行**，任何一轮检测不允许
同时触发两类以上动作；该规则从 P3 起由本文件永久看守（AGENTS §3.3）。

全部为离线测试：执行器为 :class:`tests.fakes.FakeOpExecutor`（零真实副作用）。
"""
from __future__ import annotations

import pytest

from campus_failover import (FailoverMode, NetworkFailoverManager, OpKind,
                             NetworkOpScheduler, OpRequest)
from campus_network import (EgressInfo, HealthAxis, HealthState,
                            InterfaceHealth, NetworkHealthSnapshot, SystemHealth)
from tests.fakes import FakeOpExecutor

ALL_KINDS = tuple(OpKind)

H = HealthState.HEALTHY
D = HealthState.LINK_DOWN
C = HealthState.CAPTIVE_PORTAL
U = HealthState.PROBE_UNRELIABLE
ETH_IF = 10
WIFI_IF = 5
STEP = 15.0


def request(kind: OpKind, reason: str = "test") -> OpRequest:
    return OpRequest(kind=kind, reason=reason)


def snapshot(eth, wifi, egress_if):
    """真实 P1 契约快照（ifIndex 固定为以太网 10 / Wi-Fi 5）。"""
    ethernet = None if eth is None else InterfaceHealth(
        HealthAxis.ETHERNET_HEALTH, "以太网", ETH_IF, eth)
    wifi_h = None if wifi is None else InterfaceHealth(
        HealthAxis.WIFI_HEALTH, "WLAN", WIFI_IF, wifi)
    egress = None if egress_if is None else EgressInfo(
        target="1.1.1.1", source_ip="192.168.124.9", if_index=egress_if)
    return NetworkHealthSnapshot(
        generated_at="2026-10-14T00:00:00+08:00", host="test-host",
        ethernet=ethernet, wifi=wifi_h,
        system=SystemHealth(online=True, source="fake", egress=egress))


# --------------------------------------------------------------------------- #
# U-3-20 四类动作两两互斥：一轮最多 1 个动作
# --------------------------------------------------------------------------- #
def test_u3_20_one_action_per_round_for_every_kind_pair():
    for first in ALL_KINDS:
        for second in ALL_KINDS:
            scheduler = NetworkOpScheduler(FailoverMode.AUTO, FakeOpExecutor())
            scheduler.begin_round(1)
            ok = scheduler.submit(request(first))
            assert ok.gate_passed is True
            assert ok.executed is True
            blocked = scheduler.submit(request(second))
            assert blocked.gate_passed is False, (first, second)
            assert blocked.executed is False
            assert "互斥串行" in blocked.detail
            assert len(scheduler.plan) == 1
            assert len(scheduler.executed) == 1
            assert len(scheduler.rejected) == 1


def test_u3_20b_same_kind_twice_in_one_round_is_also_rejected():
    scheduler = NetworkOpScheduler(FailoverMode.AUTO, FakeOpExecutor())
    scheduler.begin_round(1)
    assert scheduler.submit(request(OpKind.METRIC_WRITE)).executed is True
    duplicate = scheduler.submit(request(OpKind.METRIC_WRITE))
    assert duplicate.gate_passed is False
    assert len(scheduler.executed) == 1


# --------------------------------------------------------------------------- #
# U-3-21 只读监测 / 手动锁定：动作只登记为计划，绝不执行
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", [FailoverMode.MONITOR, FailoverMode.LOCKED])
def test_u3_21_readonly_modes_never_execute(mode):
    executor = FakeOpExecutor()
    scheduler = NetworkOpScheduler(mode, executor)
    for round_id, kind in enumerate(ALL_KINDS, start=1):
        scheduler.begin_round(round_id)
        outcome = scheduler.submit(request(kind))
        assert outcome.gate_passed is True
        assert outcome.attempted is False
        assert outcome.executed is False
        assert "不执行" in outcome.detail
    assert executor.calls == []
    assert scheduler.executed == []


# --------------------------------------------------------------------------- #
# U-3-22 AUTO 但无执行器：已批准但未执行（P3 无真实执行层）
# --------------------------------------------------------------------------- #
def test_u3_22_auto_without_executor_is_approved_but_not_executed():
    scheduler = NetworkOpScheduler(FailoverMode.AUTO, executor=None)
    scheduler.begin_round(1)
    outcome = scheduler.submit(request(OpKind.METRIC_WRITE))
    assert outcome.gate_passed is True
    assert outcome.attempted is False and outcome.executed is False
    assert "P5" in outcome.detail


# --------------------------------------------------------------------------- #
# U-3-23 AUTO + 执行器：跨轮串行执行
# --------------------------------------------------------------------------- #
def test_u3_23_auto_executes_serially_across_rounds():
    executor = FakeOpExecutor()
    scheduler = NetworkOpScheduler(FailoverMode.AUTO, executor)
    for round_id, kind in enumerate(ALL_KINDS, start=1):
        scheduler.begin_round(round_id)
        outcome = scheduler.submit(request(kind))
        assert outcome.executed is True
    assert executor.calls == list(ALL_KINDS)


def test_u3_23b_begin_round_clears_per_round_gate_state():
    executor = FakeOpExecutor()
    scheduler = NetworkOpScheduler(FailoverMode.AUTO, executor)
    scheduler.begin_round(1)
    scheduler.submit(request(OpKind.WIFI_RECONNECT))
    assert scheduler.plan and scheduler.executed
    scheduler.begin_round(2)
    assert scheduler.plan == [] and scheduler.executed == []
    assert scheduler.rejected == []
    assert scheduler.round_id == 2
    # 新一轮可再次提交同一类动作
    assert scheduler.submit(request(OpKind.WIFI_RECONNECT)).executed is True
    assert executor.calls == [OpKind.WIFI_RECONNECT, OpKind.WIFI_RECONNECT]


# --------------------------------------------------------------------------- #
# U-3-24 执行器异常/失败不得让调度器崩溃
# --------------------------------------------------------------------------- #
def test_u3_24_executor_failure_is_contained():
    scheduler = NetworkOpScheduler(FailoverMode.AUTO,
                                   FakeOpExecutor(default=False))
    scheduler.begin_round(1)
    outcome = scheduler.submit(request(OpKind.METRIC_WRITE))
    assert outcome.gate_passed is True
    assert outcome.attempted is True and outcome.executed is False
    assert "执行失败" in outcome.detail

    raising = NetworkOpScheduler(FailoverMode.AUTO,
                                 FakeOpExecutor(default=RuntimeError("boom")))
    raising.begin_round(1)
    outcome = raising.submit(request(OpKind.METRIC_WRITE))
    assert outcome.attempted is True and outcome.executed is False
    assert "执行器异常" in outcome.detail


def test_u3_24b_callable_executor_is_supported():
    calls = []

    def executor(req):
        calls.append(req.kind)
        return "done"

    scheduler = NetworkOpScheduler(FailoverMode.AUTO, executor)
    scheduler.begin_round(1)
    outcome = scheduler.submit(request(OpKind.CLASH_TOUCH))
    assert outcome.executed is True and outcome.detail == "done"
    assert calls == [OpKind.CLASH_TOUCH]


# --------------------------------------------------------------------------- #
# U-3-25 管理器全剧本：每轮至多一类动作；只读监测下零执行
# --------------------------------------------------------------------------- #
def test_u3_25_manager_never_submits_two_kinds_in_one_round():
    executor = FakeOpExecutor()
    manager = NetworkFailoverManager(mode=FailoverMode.AUTO, executor=executor)
    frames = ([(H, U, ETH_IF)] + [(D, U, ETH_IF)] * 3
              + [(D, C, WIFI_IF)] * 2 + [(D, D, WIFI_IF)] * 2
              + [(D, H, WIFI_IF)] * 3)
    t = 0.0
    for frame in frames:
        result = manager.tick(t, snapshot(*frame))
        assert len(result.plan) <= 1, result.plan
        assert len(result.executed) <= 1, result.executed
        t += STEP
    # 四类动作严格串行，无同类连发
    assert executor.calls == [OpKind.PORTAL_POST, OpKind.WIFI_RECONNECT,
                              OpKind.METRIC_WRITE]


def test_u3_25b_monitor_manager_never_executes_across_whole_script():
    executor = FakeOpExecutor()
    manager = NetworkFailoverManager(mode=FailoverMode.MONITOR,
                                     executor=executor)
    frames = ([(H, H, ETH_IF)] + [(D, C, WIFI_IF)] * 8
              + [(D, H, WIFI_IF)] * 4 + [(D, D, None)] * 4
              + [(H, H, ETH_IF)] * 2)
    t = 0.0
    for frame in frames:
        result = manager.tick(t, snapshot(*frame))
        assert result.executed == []
        assert result.readonly is True
        assert len(result.plan) <= 1
        t += STEP
    assert executor.calls == []
