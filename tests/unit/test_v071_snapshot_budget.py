# -*- coding: utf-8 -*-
"""V0.7.1 阶段 A：观测预算自洽（补丁 A）离线回归。

复现并锁死 2026-11-12 两次 ~10 分钟长断连的**放大器根因**：
旧默认 ``(max_age=8, timeout=12, interval=3)`` 与真机实测一次三轴采集
~21.6s 矛盾 —— 每轮必然超时、快照永不发布，叠加「快照不可信 ⇒ 零网络副作用」
形成长期 fail-closed 自锁。证据见
``docs/P8真实环境验收手册_2026-10-15.md`` §2026-10-16 量化诊断 / 方案 A。

本文件同时是**安全回归**：预算放宽后，超时 / 采集失败 / 真过期 / 结构不完整
仍必须 ``trusted=False``，``RefusingExecutor`` 仍必须零副作用（attempted=False）。

约束：零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实凭据 / 零真实 sleep，
时钟一律用 :class:`tests.fakes.VirtualClock`，采集线程用队列假件。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import campus_reconnect_tray as tray
import campus_tray_observer as tso
from campus_failover import OpKind
from campus_network import (EgressInfo, HealthState, InterfaceHealth,
                            NetworkHealthSnapshot, Recommendation, SystemHealth)
from tests.fakes import VirtualClock

ETH_IF = 12
WIFI_IF = 14
MEASURED = 21.6          # P8 受控实验实测中位耗时（秒）
NEW_BUDGET = {"max_age": 30.0, "collect_timeout": 45.0, "min_interval": 3.0}
LEGACY_BUDGET = {"max_age": 8.0, "collect_timeout": 12.0, "min_interval": 3.0}


def _health(axis, state, if_index, source_ip):
    return InterfaceHealth(axis=axis, interface="", state=state,
                           if_index=if_index, source_ip=source_ip)


def _snapshot():
    """完整三轴快照（含 recommendation / generated_at，可通过 AUTO 契约）。"""
    return NetworkHealthSnapshot(
        generated_at="2026-11-12T16:00:00",
        host="offline-test",
        ethernet=_health("ETHERNET_HEALTH", HealthState.HEALTHY, ETH_IF,
                         "192.168.1.2"),
        wifi=_health("WIFI_HEALTH", HealthState.HEALTHY, WIFI_IF, "10.8.0.5"),
        system=SystemHealth(online=True, source="test",
                            egress=EgressInfo(target="8.8.8.8", if_index=ETH_IF)),
        recommendation=Recommendation(preferred="ethernet", reason="test",
                                      would_switch=False))


def _source(clock, jobs, budget, *, collect=None, max_inflight=8):
    return tso.TraySnapshotSource(
        collect=collect or _snapshot,
        clock=clock.monotonic,
        spawn=jobs.append,
        max_inflight=max_inflight,
        **budget)


def _simulate_slow_cycle(budget, *, duration=MEASURED,
                         tick_times=(8.0, 15.0, 20.0, 24.0)):
    """按虚拟时钟重放「一次预热发布 + 一次 21.6s 慢采集」。

    返回 ``(source, ticks, final)``；``ticks`` 为慢采集在途期间各节拍的观测，
    ``final`` 为采集完成后的观测。全程零真实 sleep / 零真实线程。
    """
    clock = VirtualClock()
    jobs = []
    source = _source(clock, jobs, budget)

    # t=0：首次采集立即完成（模拟启动后已有一份可信快照）。
    assert source.request(0.0) is True
    jobs.pop(0)()
    assert source.collect_count == 1
    assert source.observe(0.0).trusted

    # min_interval 后发起慢采集；期间按节拍 request + observe。
    clock.advance(budget["min_interval"])
    source.request(clock.now)
    ticks = []
    for moment in tick_times:
        source.request(moment)
        ticks.append(source.observe(moment))

    clock.advance(duration)
    if jobs:                              # 只完成第一个在途任务
        jobs.pop(0)()
    return source, ticks, source.observe(clock.now)


# --------------------------------------------------------------------------- #
# 1. 预算自洽性规格（纯函数）
# --------------------------------------------------------------------------- #
def test_u71_a_01_module_defaults_are_coherent():
    coherent, reasons = tso.check_snapshot_budget(
        tso.DEFAULT_MAX_AGE, tso.DEFAULT_COLLECT_TIMEOUT,
        tso.DEFAULT_MIN_INTERVAL)
    assert coherent, reasons
    assert reasons == ()
    assert (tso.DEFAULT_MAX_AGE, tso.DEFAULT_COLLECT_TIMEOUT,
            tso.DEFAULT_MIN_INTERVAL) == (30.0, 45.0, 3.0)


def test_u71_a_02_tray_defaults_match_observer_and_are_coherent():
    cfg = tray.DEFAULT_CONFIG
    assert (cfg["snapshot_max_age"], cfg["snapshot_timeout"],
            cfg["snapshot_interval"]) == (30, 45, 3)
    assert float(cfg["snapshot_max_age"]) == tso.DEFAULT_MAX_AGE
    assert float(cfg["snapshot_timeout"]) == tso.DEFAULT_COLLECT_TIMEOUT
    assert float(cfg["snapshot_interval"]) == tso.DEFAULT_MIN_INTERVAL
    coherent, reasons = tso.check_snapshot_budget(
        cfg["snapshot_max_age"], cfg["snapshot_timeout"],
        cfg["snapshot_interval"])
    assert coherent, reasons


def test_u71_a_03_legacy_budget_is_provably_incoherent():
    coherent, reasons = tso.check_snapshot_budget(8.0, 12.0, 3.0)
    assert coherent is False
    assert len(reasons) == 2                 # 预算不足 + 有效期不足
    assert any("必然超时" in text for text in reasons)
    assert any("STALE" in text for text in reasons)


@pytest.mark.parametrize("budget, coherent_expected", [
    ((30.0, 45.0, 3.0), True),
    ((24.6, 45.0, 3.0), True),             # 恰好等于实测周期，边界含等号
    ((24.5, 45.0, 3.0), False),
    ((30.0, 21.5, 3.0), False),            # 预算低于实测
    ((2.0, 45.0, 3.0), False),             # 有效期短于重采间隔
])
def test_u71_a_04_checker_boundaries(budget, coherent_expected):
    coherent, reasons = tso.check_snapshot_budget(*budget)
    assert coherent is coherent_expected, reasons


def test_u71_a_05_checker_is_pure_and_supports_custom_measurement():
    first = tso.check_snapshot_budget(30.0, 45.0, 3.0)
    assert tso.check_snapshot_budget(30.0, 45.0, 3.0) == first
    # 更快的真机 ⇒ 预算要求随之下降（注入式实测值，零 I/O）
    coherent, _ = tso.check_snapshot_budget(10.0, 12.0, 1.0,
                                            measured_collect=5.0)
    assert coherent is True


# --------------------------------------------------------------------------- #
# 2. 慢采集回归：旧预算自锁 / 新预算发布且全程可信
# --------------------------------------------------------------------------- #
def test_u71_a_06_new_budget_publishes_slow_collect_without_stale_window():
    source, ticks, final = _simulate_slow_cycle(NEW_BUDGET)
    assert source.timeout_count == 0, "21.6s 采集不应触发 45s 超时"
    assert source.collect_count == 2, "慢采集结果必须被发布"
    assert source.error_count == 0
    assert all(obs.trusted for obs in ticks), \
        "慢采集在途期间不得出现 STALE 空窗：%r" % [o.reason for o in ticks]
    assert final.trusted is True
    assert final.reason == tso.SNAPSHOT_OK
    assert final.age == 0.0


def test_u71_a_07_legacy_budget_reproduces_permanent_untrusted():
    source, ticks, final = _simulate_slow_cycle(LEGACY_BUDGET)
    assert source.timeout_count >= 1, "旧 12s 预算必须复现采集超时"
    assert source.collect_count == 1, "旧预算下快照永远发布不出来（P8 实测）"
    assert tso.SNAPSHOT_STALE in [obs.reason for obs in ticks], \
        "旧 8s 有效期必须复现 STALE 空窗：%r" % [o.reason for o in ticks]
    assert final.trusted is False
    assert final.reason in (tso.SNAPSHOT_STALE, tso.SNAPSHOT_GENERATION,
                            tso.SNAPSHOT_COLLECT_TIMEOUT)


# --------------------------------------------------------------------------- #
# 3. fail-closed 安全回归：放宽预算不等于放宽门禁
# --------------------------------------------------------------------------- #
def test_u71_a_08_genuinely_stale_snapshot_is_still_untrusted():
    clock = VirtualClock()
    jobs = []
    source = _source(clock, jobs, NEW_BUDGET)
    assert source.request(0.0) is True
    jobs.pop(0)()
    assert source.observe(0.0).trusted
    observation = source.observe(31.0)      # > max_age(30)
    assert observation.trusted is False
    assert observation.reason == tso.SNAPSHOT_STALE


def test_u71_a_09_collect_timeout_is_still_fail_closed():
    # max_inflight=1：超时后不允许立刻再起一轮，隔离出确定的 COLLECT_TIMEOUT 结论。
    clock = VirtualClock()
    jobs = []
    source = _source(clock, jobs, NEW_BUDGET, max_inflight=1)
    assert source.request(0.0) is True
    clock.advance(46.0)                     # > collect_timeout(45)
    source.request(clock.now)
    observation = source.observe(clock.now)
    assert source.timeout_count == 1
    assert source.collect_count == 0
    assert "超时" in source._error
    assert observation.trusted is False
    assert observation.reason == tso.SNAPSHOT_COLLECT_TIMEOUT


def test_u71_a_09b_timeout_then_relaunch_still_reports_untrusted():
    # 生产形态（max_inflight=3+）：超时会立刻重发一轮，observe 给 COLLECTING，
    # 但**仍然不可信** —— 阶段 A 不放宽这一点。
    clock = VirtualClock()
    jobs = []
    source = _source(clock, jobs, NEW_BUDGET)
    assert source.request(0.0) is True
    clock.advance(46.0)
    assert source.request(clock.now) is True
    observation = source.observe(clock.now)
    assert source.timeout_count == 1
    assert source.collect_count == 0
    assert observation.trusted is False
    assert observation.reason in (tso.SNAPSHOT_COLLECTING,
                                  tso.SNAPSHOT_COLLECT_TIMEOUT)


def test_u71_a_10_collect_error_is_still_fail_closed():
    def broken():
        raise RuntimeError("netdiag 假故障：adapters")

    clock = VirtualClock()
    jobs = []
    source = _source(clock, jobs, NEW_BUDGET, collect=broken)
    assert source.request(0.0) is True
    jobs.pop(0)()
    observation = source.observe(0.0)
    assert source.collect_count == 0
    assert source.error_count == 1
    assert observation.trusted is False
    assert observation.reason == tso.SNAPSHOT_COLLECT_ERROR


def test_u71_a_11_refusing_executor_still_has_zero_side_effects():
    executor = tso.RefusingExecutor("三轴快照不可信：回归测试")
    outcome = executor.execute(SimpleNamespace(kind=OpKind.METRIC_WRITE))
    assert executor.refused == [OpKind.METRIC_WRITE]
    assert outcome.gate_passed is True
    assert outcome.attempted is False       # 不消耗冷却
    assert outcome.executed is False        # 零网络副作用


def test_u71_a_12_light_snapshot_is_still_never_trusted_with_new_budget():
    from campus_tray_failover import light_snapshot

    clock = VirtualClock()
    jobs = []
    source = _source(clock, jobs, NEW_BUDGET,
                     collect=lambda: light_snapshot(system_online=True,
                                                    system_source="占位"))
    assert source.request(0.0) is True
    jobs.pop(0)()
    observation = source.observe(0.0)
    assert observation.trusted is False
    assert observation.reason == tso.SNAPSHOT_INCOMPLETE


# --------------------------------------------------------------------------- #
# 4. 配置迁移：已落盘的旧默认值必须升级，用户显式配置必须保留
# --------------------------------------------------------------------------- #
def test_u71_a_13_legacy_on_disk_config_is_migrated():
    raw = {"snapshot_max_age": 8, "snapshot_timeout": 12,
           "snapshot_interval": 3}
    values = tray._read_snapshot_keys(json.dumps(raw))
    assert (values["snapshot_max_age"], values["snapshot_timeout"]) == (30, 45)
    coherent, reasons = tso.check_snapshot_budget(
        values["snapshot_max_age"], values["snapshot_timeout"],
        values["snapshot_interval"])
    assert coherent, reasons


@pytest.mark.parametrize("raw", [
    {"snapshot_max_age": 9, "snapshot_timeout": 12, "snapshot_interval": 3},
    {"snapshot_max_age": 8, "snapshot_timeout": 20, "snapshot_interval": 3},
    {"snapshot_max_age": 30, "snapshot_timeout": 45, "snapshot_interval": 3},
])
def test_u71_a_14_explicit_user_config_is_not_rewritten(raw):
    values = tray._read_snapshot_keys(json.dumps(raw))
    assert tuple(values.values()) == (raw["snapshot_max_age"],
                                      raw["snapshot_timeout"],
                                      raw["snapshot_interval"])
