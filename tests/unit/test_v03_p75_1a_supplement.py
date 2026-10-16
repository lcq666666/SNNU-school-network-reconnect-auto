# -*- coding: utf-8 -*-
"""P7.5-1A 边界补强离线测试。

覆盖三个边界问题：
1. 超时后单飞失效（并发采集安全 / 任务 ID 匹配）；
2. 拒绝执行的 attempted 语义（不消耗冷却）；
3. AUTO 快照契约收紧（三轴 + 有效 if_index）。

约束：零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实凭据 / 零真实 sleep。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import campus_tray_failover as tf
import campus_tray_observer as tso
from campus_failover import FailoverMode, OpKind
from campus_network import (EgressInfo, HealthState, InterfaceHealth,
                            NetworkHealthSnapshot, SystemHealth)
from tests.fakes import FakeOpExecutor, VirtualClock, seed_path_verification

LOG = logging.getLogger("campus")

ETH_IF = 12
WIFI_IF = 14


def _health(axis, state, if_index, source_ip="",
            interface=""):
    return InterfaceHealth(
        axis=axis, interface=interface, state=state, if_index=if_index,
        source_ip=source_ip)


def _snapshot(eth_state, wifi_state, egress_if=ETH_IF):
    """构造完整三轴快照（含 recommendation / generated_at）。"""
    from campus_network import Recommendation
    return NetworkHealthSnapshot(
        generated_at="2026-10-15T10:00:00",
        host="testhost",
        ethernet=_health("ETHERNET_HEALTH", eth_state, ETH_IF,
                         "192.168.1.2", interface="以太网"),
        wifi=_health("WIFI_HEALTH", wifi_state, WIFI_IF,
                     "10.8.0.5", interface="WLAN"),
        system=SystemHealth(
            online=(eth_state is HealthState.HEALTHY
                    or wifi_state is HealthState.HEALTHY),
            source="test",
            egress=EgressInfo(target="8.8.8.8", if_index=egress_if)),
        recommendation=Recommendation(
            preferred="wifi" if eth_state in (HealthState.LINK_DOWN,
                                               HealthState.INTERNET_UNREACHABLE)
            and wifi_state is HealthState.HEALTHY else "ethernet",
            reason="test",
            would_switch=False))


def _queued_source(**kwargs):
    clock = VirtualClock()
    jobs = []
    source = tso.TraySnapshotSource(
        collect=lambda: _snapshot(HealthState.HEALTHY, HealthState.HEALTHY),
        clock=clock.monotonic, spawn=jobs.append,
        max_age=10.0, collect_timeout=1.0, min_interval=0.0, **kwargs)
    return source, clock, jobs


def test_u75_1a_01_old_task_with_mismatched_id_is_dropped():
    source, clock, jobs = _queued_source()
    assert source.request()
    clock.advance(2)
    assert source.request()
    jobs[0]()
    assert source._collecting is True
    assert source._inflight_count == 1
    assert source.collect_count == 0
    assert source.dropped_task_mismatch == 1
    jobs[1]()
    assert source.observe().trusted


def test_u75_1a_02_old_task_error_must_not_override_new_snapshot():
    source, clock, jobs = _queued_source()
    source.request()
    clock.advance(2)
    source.request()
    jobs[1]()
    published = source.snapshot()
    source._collect = None
    jobs[0]()
    assert source.snapshot() is published
    assert source._error == ""
    assert source.error_count == 0
    assert source.observe().trusted
    assert source._inflight_count == 0


def test_u75_1a_03_timeout_bumps_task_id_and_generation():
    source, clock, jobs = _queued_source()
    source.request()
    old_id = source._active_task_id
    clock.advance(2)
    assert source.request()
    assert source.timeout_count == 1
    assert source.generation == 1
    assert source._active_task_id > old_id
    jobs[0]()
    assert source.dropped_task_mismatch == 1


def test_u75_1a_04_start_time_zero_is_not_falsy_timeout():
    source, clock, jobs = _queued_source()
    source.request()
    assert source._collect_started_at == 0.0
    clock.advance(2)
    assert source.request()
    assert source.timeout_count == 1


def test_u75_1a_05_consecutive_timeouts_then_recovery():
    source, clock, jobs = _queued_source(max_inflight=2)
    assert source.request()
    clock.advance(2)
    assert source.request()
    clock.advance(2)
    assert not source.request()
    assert source.timeout_count == 2
    assert source.rejected_inflight_limit == 1
    assert len(jobs) == 2
    assert source.collect_now() is None, "同步入口不得绕过并发上限"
    jobs[0]()
    assert source.request()
    jobs[2]()
    jobs[1]()
    assert source.observe().trusted
    assert source._inflight_count == 0


def test_late_completion_without_retry_is_not_published():
    source, clock, jobs = _queued_source()
    source.request()
    clock.advance(2)
    jobs[0]()
    assert not source.observe().trusted
    assert source.collect_count == 0
    assert source.timeout_count == 1


def test_collect_now_obeys_single_flight_and_counts_itself():
    source, clock, jobs = _queued_source()
    source.request()
    assert source.collect_now() is None
    assert source._inflight_count == 1
    jobs[0]()
    counts = []
    original = source._collect
    def collect():
        counts.append(source._inflight_count)
        return original()
    source._collect = collect
    assert source.collect_now() is not None
    assert counts == [1]
    assert source._inflight_count == 0

# --------------------------------------------------------------------------- #
# U-75-1A-06 .. 07：拒绝执行语义（不消耗冷却）
# --------------------------------------------------------------------------- #
def test_u75_1a_06_refusing_executor_attempted_is_false():
    """RefusingExecutor 返回 attempted=False，状态机不应记录冷却。"""
    executor = tso.RefusingExecutor("快照不可信")
    outcome = executor.execute(SimpleNamespace(kind=OpKind.PORTAL_POST))
    assert outcome.attempted is False, "P7.5-1A：attempted 必须为 False"
    assert outcome.executed is False
    assert outcome.gate_passed is True


@pytest.mark.parametrize("wifi_state, kind", [
    (HealthState.HEALTHY, OpKind.METRIC_WRITE),
    (HealthState.CAPTIVE_PORTAL, OpKind.PORTAL_POST),
    (HealthState.LINK_DOWN, OpKind.WIFI_RECONNECT),
])
def test_u75_1a_07_untrusted_then_trusted_does_not_consume_cooldown(wifi_state, kind):
    """快照先不可信（拒绝）再可信 → 不会因为虚假冷却记录而被阻止执行。"""
    from campus_core import TimerTable

    timers = TimerTable()
    now = 0.0
    # 构造桥：AUTO + 已授权 + 真实执行器
    fake_exec = FakeOpExecutor({kind: True})
    bridge = tf.TrayFailoverBridge(
        mode=FailoverMode.AUTO, authorized=True,
        executor_factory=lambda: fake_exec,
        timer_table=timers)
    # 第 1 轮：快照不可信 → 拒绝执行
    bad_snap = _snapshot(HealthState.LINK_DOWN, wifi_state)
    bridge.set_snapshot_trust(False, "NO_DATA")
    refusing = bridge.manager.scheduler.executor
    for now in range(0, 61, 5):
        r1 = bridge.tick(float(now), bad_snap)
        assert r1.executed == []
    assert kind in refusing.refused, "必须到达对应动作的拒绝执行器"
    assert bridge.manager.last_switch_at is None
    assert bridge.manager._last_wifi_reconnect_at is None
    # 第 2 轮：快照变为可信（完整三轴）
    good_snap = _snapshot(HealthState.LINK_DOWN, wifi_state)
    bridge.set_snapshot_trust(True, "OK")
    # 推进多轮直到触发切换（需要满足连续失败阈值 + 冷却窗口）
    executed = []
    for i in range(30):
        now += 5.0
        r2 = bridge.tick(now, good_snap)
        executed.extend(r2.executed)
    # 可信后应能执行（没有被虚假冷却阻止）
    assert any(o.kind is kind for o in executed), \
        "重新可信后因虚假冷却被阻止：%r" % executed


# --------------------------------------------------------------------------- #
# U-75-1A-08 .. 09：AUTO 快照契约收紧
# --------------------------------------------------------------------------- #
def test_u75_1a_08_auto_requires_both_axes_with_ifindex():
    """缺少任一轴的 if_index 时，is_auto_executable_snapshot 返回 False。"""
    from campus_network import Recommendation
    # 完整快照
    full = _snapshot(HealthState.LINK_DOWN, HealthState.HEALTHY)
    ok, reason = tso.is_auto_executable_snapshot(full)
    assert ok is True, "完整快照应可执行：%s" % reason
    # 缺少 ethernet.if_index
    bad_eth = NetworkHealthSnapshot(
        generated_at="t", host="h",
        ethernet=_health("ETHERNET_HEALTH", HealthState.LINK_DOWN, None),
        wifi=_health("WIFI_HEALTH", HealthState.HEALTHY, WIFI_IF),
        system=SystemHealth(online=False, source="t"),
        recommendation=Recommendation(preferred="wifi", reason="test", would_switch=True))
    ok2, reason2 = tso.is_auto_executable_snapshot(bad_eth)
    assert ok2 is False and "Ethernet" in reason2, reason2
    # 缺少 wifi.if_index
    bad_wifi = NetworkHealthSnapshot(
        generated_at="t", host="h",
        ethernet=_health("ETHERNET_HEALTH", HealthState.LINK_DOWN, ETH_IF),
        wifi=_health("WIFI_HEALTH", HealthState.HEALTHY, None),
        system=SystemHealth(online=False, source="t"),
        recommendation=Recommendation(preferred="wifi", reason="test", would_switch=True))
    ok3, reason3 = tso.is_auto_executable_snapshot(bad_wifi)
    assert ok3 is False and "Wi-Fi" in reason3, reason3


def test_u75_1a_09_external_snapshot_cannot_bypass_freshness():
    """外部直接注入的快照仍须经过结构验证（不能仅因内容完整就绕过）。"""
    from campus_network import Recommendation
    # 外部快照结构完整但缺少 generated_at → 结构不完整
    external_bad = NetworkHealthSnapshot(
        generated_at="", host="h",
        ethernet=_health("ETHERNET_HEALTH", HealthState.LINK_DOWN, ETH_IF),
        wifi=_health("WIFI_HEALTH", HealthState.HEALTHY, WIFI_IF),
        system=SystemHealth(online=False, source="ext"),
        recommendation=Recommendation(preferred="wifi", reason="test", would_switch=True))
    ok, reason = tso.is_auto_executable_snapshot(external_bad)
    assert ok is False, "外部注入的不完整快照不应通过 AUTO 契约：%s" % reason


@pytest.mark.parametrize("index", [None, 0, -1, True, False, "14", 1.5])
@pytest.mark.parametrize("axis", ["ethernet", "wifi"])
def test_auto_rejects_invalid_interface_indices(axis, index):
    from dataclasses import replace
    snap = _snapshot(HealthState.LINK_DOWN, HealthState.HEALTHY)
    snap = replace(snap, **{axis: replace(getattr(snap, axis), if_index=index)})
    assert not tso.is_auto_executable_snapshot(snap)[0]


def test_auto_rejects_same_interface_for_both_roles():
    from dataclasses import replace
    snap = _snapshot(HealthState.LINK_DOWN, HealthState.HEALTHY)
    snap = replace(snap, wifi=replace(snap.wifi, if_index=ETH_IF))
    assert not tso.is_auto_executable_snapshot(snap)[0]


@pytest.mark.parametrize("raw, expected", [
    ({}, (8, 12, 3)),
    ({"snapshot_max_age": True, "snapshot_timeout": "12",
      "snapshot_interval": None}, (8, 12, 3)),
    ({"snapshot_max_age": -1, "snapshot_timeout": 0,
      "snapshot_interval": 0}, (1, 3, 1)),
    ({"snapshot_max_age": 100, "snapshot_timeout": 100,
      "snapshot_interval": 100}, (30, 60, 30)),
])
def test_snapshot_config_defaults_types_and_bounds(raw, expected):
    import json
    import campus_reconnect_tray as tray
    values = tray._read_snapshot_keys(json.dumps(raw))
    assert tuple(values.values()) == expected


@pytest.mark.parametrize("first, second", [
    ("sync", "sync"), ("sync", "async"), ("async", "sync")])
def test_sync_and_async_collection_share_throttle(first, second):
    clock = VirtualClock()
    calls = []
    source = tso.TraySnapshotSource(
        collect=lambda: calls.append(1) or _snapshot(
            HealthState.HEALTHY, HealthState.HEALTHY),
        clock=clock.monotonic, spawn=lambda fn: fn(), min_interval=3)
    def run(kind):
        return source.collect_now() if kind == "sync" else source.request()
    run(first)
    run(second)
    assert len(calls) == 1
    clock.advance(3)
    run(second)
    assert len(calls) == 2


@pytest.mark.parametrize("variant", ["valid", "missing_axis", "invalid", "same"])
def test_tray_auto_contract_is_enforced_in_actual_tick(monkeypatch, variant):
    from dataclasses import replace
    import campus_reconnect_tray as tray
    clock = VirtualClock()
    clock.install(monkeypatch, tray)
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "save_config", lambda cfg: True)
    app = tray.TrayApp(LOG)
    app.config.update(failover_mode="AUTO", auto_switch_authorized=True)
    snap = _snapshot(HealthState.LINK_DOWN, HealthState.HEALTHY)
    if variant == "missing_axis":
        snap = replace(snap, ethernet=None)
    elif variant == "invalid":
        snap = replace(snap, wifi=replace(snap.wifi, if_index=0))
    elif variant == "same":
        snap = replace(snap, wifi=replace(snap.wifi, if_index=ETH_IF))
    executor = FakeOpExecutor({OpKind.METRIC_WRITE: True})
    app._snapshot_collector = lambda: snap
    app._snapshot_spawn = lambda fn: fn()
    app.failover.executor_factory = lambda: executor
    app._sync_failover_from_config()
    executed = []
    for _ in range(20):
        # V0.5-4：路由切换前必须先有「新鲜的接口受限验证通过」证据；
        # 真机由后台验证线程产出，这里离线登记（零真实网络）。
        seed_path_verification(app.dual_path, "wifi",
                               now=clock.monotonic(), source_ip="10.8.0.5")
        result = app._failover_tick(clock.monotonic(), False)
        executed.extend(result.executed)
        clock.advance(5)
    assert app.last_observation.trusted
    assert app.failover.snapshot_trust is (variant == "valid")
    assert bool(executed) is (variant == "valid")
