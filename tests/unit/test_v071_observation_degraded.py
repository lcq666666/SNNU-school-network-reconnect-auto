# -*- coding: utf-8 -*-
"""V0.7.1 阶段 B：有界降级观测（FULL / DEGRADED / REFUSED）离线回归。

复现 2026-11-12 两次 ~10 分钟长断连的**另一半根因**：阶段 A 放宽预算后，
真实断网时 ``netdiag`` 的 ``adapters`` / ``default_routes`` 仍会抛
``RuntimeError``，没有新快照发布 ⇒ 旧快照 30s 后过期 ⇒ 重新进入
「零网络副作用」自锁。阶段 B 把一刀切不可信细分为三态，并在**有界宽限**
（用户 2026-11-12 确认 60s，总窗口 90s）内保留 last-known-good 依据。

本文件同时是**安全回归**：阶段 B 不改变 ``trusted`` 语义 —— ``DEGRADED``
仍 ``trusted=False``，``RefusingExecutor`` 仍 ``attempted=False /
executed=False``，所有既有消费方零行为变化；结构不完整 / 代次变化 / 接口
身份变化 / 宽限耗尽一律 ``REFUSED``。真正放行白名单留给阶段 C。

约束：零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实凭据 / 零真实 sleep，
时钟一律用 :class:`tests.fakes.VirtualClock`，采集线程用队列假件。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import campus_tray_observer as tso
from campus_failover import OpKind
from campus_network import (EgressInfo, HealthState, InterfaceHealth,
                            NetworkHealthSnapshot, Recommendation, SystemHealth)
from campus_tray_failover import TrayFailoverBridge
from tests.fakes import VirtualClock

ETH_IF = 12
WIFI_IF = 14
WINDOW = 90.0            # max_age 30 + degraded_grace 60


def _health(axis, state, if_index, source_ip):
    return InterfaceHealth(axis=axis, interface="", state=state,
                           if_index=if_index, source_ip=source_ip)


def _snapshot(*, complete=True, eth_ip="192.168.1.2", wifi_ip="10.8.0.5"):
    """完整三轴快照；``complete=False`` 模拟缺少 recommendation 的占位快照。

    两种形态的**接口签名相同**，便于单独测试结构判定而不误触发身份变化。
    """
    return NetworkHealthSnapshot(
        generated_at="2026-11-12T16:00:00" if complete else "",
        host="offline-test",
        ethernet=_health("ETHERNET_HEALTH", HealthState.HEALTHY, ETH_IF, eth_ip),
        wifi=_health("WIFI_HEALTH", HealthState.HEALTHY, WIFI_IF, wifi_ip),
        system=SystemHealth(online=True, source="test",
                            egress=EgressInfo(target="8.8.8.8", if_index=ETH_IF)),
        recommendation=(Recommendation(preferred="ethernet", reason="test",
                                       would_switch=False) if complete else None))


def _primed(clock, *, grace=60.0, generation=None, collect=None):
    """构造「已发布过一份可信 LKG」的观测源，返回 ``(source, jobs)``。"""
    jobs = []
    source = tso.TraySnapshotSource(
        collect=collect or _snapshot,
        clock=clock.monotonic,
        generation=generation,
        spawn=jobs.append,
        max_inflight=8,
        degraded_grace=grace)
    assert source.request(0.0) is True
    jobs.pop(0)()                       # 采集立即完成，发布 FULL 快照 + LKG
    assert source.observe(0.0).mode == tso.MODE_FULL
    return source, jobs


def _degraded(clock, *, grace=60.0, at=31.0):
    """推进到过期但仍在宽限内，返回 ``(source, observation)``。"""
    source, jobs = _primed(clock, grace=grace)
    clock.advance(at)
    observation = source.observe(clock.now)
    assert observation.mode == tso.MODE_DEGRADED
    return source, observation


# --------------------------------------------------------------------------- #
# 1. 契约、纯函数与模型兼容
# --------------------------------------------------------------------------- #
def test_u71_b_01_contract_and_mode_constants():
    assert tso.CONTRACT_VERSION == "v03-p75-obs-2"
    assert tso.OBSERVATION_MODES == (tso.MODE_FULL, tso.MODE_DEGRADED,
                                     tso.MODE_REFUSED)
    assert tso.DEFAULT_DEGRADED_GRACE == 60.0
    assert tso.MAX_DEGRADED_GRACE == 300.0
    # 硬拒绝结论绝不在软失败白名单里
    for hard in (tso.SNAPSHOT_NO_DATA, tso.SNAPSHOT_INCOMPLETE,
                 tso.SNAPSHOT_EXTERNAL, tso.SNAPSHOT_GENERATION,
                 tso.SNAPSHOT_OK):
        assert hard not in tso.DEGRADABLE_REASONS


def test_u71_b_02_default_mode_derivation_keeps_legacy_construction():
    """既有 ``ObservationResult(...)`` 关键字构造语义不变。"""
    empty = tso.ObservationResult()
    assert empty.trusted is False and empty.mode == tso.MODE_REFUSED
    assert empty.usable_snapshot is None and empty.is_degraded is False
    snap = _snapshot()
    full = tso.ObservationResult(snapshot=snap, trusted=True)
    assert full.mode == tso.MODE_FULL and full.usable_snapshot is snap
    timeout = tso.ObservationResult(reason="COLLECT_TIMEOUT")
    assert timeout.mode == tso.MODE_REFUSED
    assert timeout.usable_snapshot is None


def test_u71_b_03_to_dict_exposes_degradation_fields():
    obs = tso.ObservationResult(mode=tso.MODE_DEGRADED, stale_for=1.0,
                                grace_left=59.0, lkg_snapshot=_snapshot())
    data = obs.to_dict()
    assert data["mode"] == tso.MODE_DEGRADED
    assert data["stale_for"] == 1.0 and data["grace_left"] == 59.0
    assert data["has_lkg"] is True
    assert "reason_text" in data and data["trusted"] is False


@pytest.mark.parametrize("max_age,grace,expect_ok", [
    (30.0, 60.0, True),
    (30.0, 18.0, True),        # 30+18 == 45+3，恰好覆盖病态采集周期
    (30.0, 17.0, False),
    (30.0, 0.0, False),
    (30.0, -1.0, False),
    (30.0, 301.0, False),
    (30.0, float("inf"), False),
    (30.0, float("nan"), False),
])
def test_u71_b_04_check_degraded_window(max_age, grace, expect_ok):
    ok, reasons = tso.check_degraded_window(max_age, grace)
    assert ok is expect_ok
    assert bool(reasons) == (not expect_ok)


def test_u71_b_05_grace_is_clamped_at_construction():
    """构造期即钳到硬上限，运行时不可能被配成事实上的永久宽限。"""
    clock = VirtualClock()
    jobs = []
    source = tso.TraySnapshotSource(
        collect=_snapshot, clock=clock.monotonic, spawn=jobs.append,
        degraded_grace=1e9)
    assert source.degraded_grace == tso.MAX_DEGRADED_GRACE


@pytest.mark.parametrize("reason,expected", [
    (tso.SNAPSHOT_STALE, True),
    (tso.SNAPSHOT_COLLECT_ERROR, True),
    (tso.SNAPSHOT_COLLECT_TIMEOUT, True),
    (tso.SNAPSHOT_COLLECTING, True),
    (tso.SNAPSHOT_GENERATION, True),
    (tso.SNAPSHOT_NO_DATA, False),
    (tso.SNAPSHOT_INCOMPLETE, False),
    (tso.SNAPSHOT_EXTERNAL, False),
    (tso.SNAPSHOT_OK, False),
])
def test_u71_b_06_soft_failure_whitelist(reason, expected):
    assert tso.TraySnapshotSource._is_soft_failure(
        reason, lkg_external="cfg-1", current_external="cfg-1",
        lkg_signature=(("eth",), ("wifi",)),
        signature=(("eth",), ("wifi",))) is expected


@pytest.mark.parametrize("external,signature", [
    ("cfg-2", (("eth",), ("wifi",))),         # 配置 / SSID 真的变了
    ("cfg-1", (("eth-new",), ("wifi",))),     # 接口身份真的变了
])
def test_u71_b_07_generation_mismatch_is_hard_when_identity_changed(external,
                                                                    signature):
    assert tso.TraySnapshotSource._is_soft_failure(
        tso.SNAPSHOT_GENERATION, lkg_external="cfg-1",
        current_external=external,
        lkg_signature=(("eth",), ("wifi",)), signature=signature) is False


# --------------------------------------------------------------------------- #
# 2. FULL / DEGRADED / REFUSED 三态与有界窗口
# --------------------------------------------------------------------------- #
def test_u71_b_08_fresh_snapshot_is_full():
    clock = VirtualClock()
    source, _ = _primed(clock)
    obs = source.observe(0.0)
    assert obs.mode == tso.MODE_FULL and obs.trusted is True
    assert obs.reason == tso.SNAPSHOT_OK
    assert obs.usable_snapshot is obs.snapshot
    assert obs.stale_for == 0.0 and obs.grace_left == 0.0


def test_u71_b_09_stale_within_window_is_degraded():
    clock = VirtualClock()
    source, obs = _degraded(clock, at=31.0)
    assert obs.reason == tso.SNAPSHOT_STALE
    assert obs.trusted is False                 # 阶段 B 不改变 trusted
    assert obs.is_degraded is True
    assert obs.lkg_snapshot is not None
    assert obs.usable_snapshot is obs.lkg_snapshot
    assert obs.lkg_snapshot is source.snapshot()
    assert obs.stale_for == pytest.approx(1.0)
    assert obs.grace_left == pytest.approx(59.0)


def test_u71_b_10_window_boundary_is_inclusive():
    clock = VirtualClock()
    _, obs = _degraded(clock, at=WINDOW)
    assert obs.mode == tso.MODE_DEGRADED
    assert obs.stale_for == pytest.approx(60.0)
    assert obs.grace_left == pytest.approx(0.0)


def test_u71_b_11_window_exhausted_is_refused():
    clock = VirtualClock()
    source, _ = _primed(clock)
    clock.advance(WINDOW + 1.0)
    obs = source.observe(clock.now)
    assert obs.mode == tso.MODE_REFUSED
    assert obs.trusted is False and obs.reason == tso.SNAPSHOT_STALE
    assert obs.lkg_snapshot is None and obs.usable_snapshot is None
    assert obs.grace_left == 0.0


def test_u71_b_12_collect_error_is_degraded_even_before_expiry():
    """真实日志里 ``RuntimeError: adapters`` 的场景：采集失败但快照仍新鲜。"""
    clock = VirtualClock()
    source, jobs = _primed(clock)
    source._collect = lambda: (_ for _ in ()).throw(
        RuntimeError("网络采集查询失败：adapters"))
    clock.advance(3.0)
    assert source.request(clock.now) is True
    jobs.pop(0)()
    obs = source.observe(clock.now)
    assert obs.reason == tso.SNAPSHOT_COLLECT_ERROR
    assert obs.mode == tso.MODE_DEGRADED and obs.trusted is False
    assert obs.stale_for == 0.0
    assert obs.grace_left == pytest.approx(WINDOW - 3.0)


def test_u71_b_13_internal_generation_bump_from_timeout_is_soft():
    """采集超时会自增内部任务代次，但网络身份未变 ⇒ 仍可有界降级。"""
    clock = VirtualClock()
    source, jobs = _primed(clock)
    clock.advance(3.0)
    assert source.request(clock.now) is True       # 在途任务，不执行
    clock.advance(46.0)                           # > collect_timeout 45
    assert source.request(clock.now) is True       # 判超时 + 自增代次 + 重发
    obs = source.observe(clock.now)
    assert obs.reason == tso.SNAPSHOT_GENERATION
    assert obs.mode == tso.MODE_DEGRADED
    assert source.timeout_count == 1
    assert jobs                                    # 重发的任务未执行，无真实线程


def test_u71_b_14_no_lkg_never_degrades():
    clock = VirtualClock()
    jobs = []
    source = tso.TraySnapshotSource(
        collect=lambda: (_ for _ in ()).throw(RuntimeError("adapters")),
        clock=clock.monotonic, spawn=jobs.append, max_inflight=8)
    assert source.request(0.0) is True
    jobs.pop(0)()
    for at in (0.0, 10.0, 100.0):
        clock.now = at
        obs = source.observe(at)
        assert obs.mode == tso.MODE_REFUSED and obs.trusted is False
        assert obs.lkg_snapshot is None and obs.usable_snapshot is None


def test_u71_b_15_incomplete_snapshot_is_hard_refused():
    clock = VirtualClock()
    source, jobs = _primed(clock)
    source._collect = lambda: _snapshot(complete=False)
    clock.advance(3.0)
    assert source.request(clock.now) is True
    jobs.pop(0)()
    obs = source.observe(clock.now)
    assert obs.reason == tso.SNAPSHOT_INCOMPLETE
    assert obs.mode == tso.MODE_REFUSED and obs.usable_snapshot is None


def test_u71_b_16_broken_lkg_never_degrades():
    """LKG 被损坏成结构不完整时，即使落在宽限内也必须硬拒绝。"""
    clock = VirtualClock()
    source, _ = _primed(clock)
    source._lkg_snapshot = _snapshot(complete=False)
    clock.advance(31.0)
    obs = source.observe(clock.now)
    assert obs.reason == tso.SNAPSHOT_STALE
    assert obs.mode == tso.MODE_REFUSED and obs.usable_snapshot is None


# --------------------------------------------------------------------------- #
# 3. 硬拒绝边界：配置代次 / 网络身份 / 显式作废
# --------------------------------------------------------------------------- #
def test_u71_b_17_invalidate_clears_lkg():
    clock = VirtualClock()
    source, _ = _primed(clock)
    clock.advance(31.0)
    source.invalidate("SSID 变化")
    obs = source.observe(clock.now)
    assert obs.mode == tso.MODE_REFUSED
    assert obs.lkg_snapshot is None and obs.usable_snapshot is None
    assert source._lkg_at is None


def test_u71_b_18_external_generation_change_is_hard_refused():
    state = {"cfg": 1}
    clock = VirtualClock()
    source, _ = _primed(clock, generation=lambda: state["cfg"])
    state["cfg"] = 2                                  # 配置 / SSID / 模式变化
    clock.advance(31.0)
    obs = source.observe(clock.now)
    assert obs.reason == tso.SNAPSHOT_GENERATION
    assert obs.mode == tso.MODE_REFUSED and obs.usable_snapshot is None


def test_u71_b_19_interface_identity_change_replaces_lkg():
    """DHCP / 出口接口变化后，旧 LKG 不得继续作为当前依据。"""
    clock = VirtualClock()
    source, jobs = _primed(clock)
    old_signature = source._lkg_signature
    source._collect = lambda: _snapshot(wifi_ip="10.8.0.99")
    clock.advance(3.0)
    assert source.request(clock.now) is True
    jobs.pop(0)()
    obs = source.observe(clock.now)
    assert obs.mode == tso.MODE_FULL                 # 新身份下重新可信
    assert source._lkg_signature != old_signature
    assert source._lkg_snapshot is source.snapshot()
    source._lkg_signature = old_signature           # 模拟旧 LKG 残留
    clock.advance(31.0)
    refused = source.observe(clock.now)
    assert refused.mode == tso.MODE_REFUSED


# --------------------------------------------------------------------------- #
# 4. 零副作用契约 + 既有消费方零行为变化
# --------------------------------------------------------------------------- #
def test_u71_b_20_degraded_still_zero_side_effects():
    clock = VirtualClock()
    _, obs = _degraded(clock)
    assert obs.trusted is False
    executor = tso.RefusingExecutor("阶段 B 不应改变零副作用语义")
    for kind in OpKind:
        outcome = executor.execute(SimpleNamespace(kind=kind))
        assert outcome.attempted is False and outcome.executed is False
    assert len(executor.refused) == len(list(OpKind))


def test_u71_b_21_bridge_still_refuses_degraded_observation():
    """托盘桥接按 ``trusted`` 门控：DEGRADED 仍换成 RefusingExecutor。"""
    clock = VirtualClock()
    _, obs = _degraded(clock)
    bridge = TrayFailoverBridge(mode="AUTO", authorized=True,
                                executor_factory=lambda: object())
    bridge.set_snapshot_trust(obs.trusted, obs.reason_text)
    assert isinstance(bridge.manager.scheduler.executor, tso.RefusingExecutor)
    assert bridge.snapshot_trust is False


@pytest.mark.parametrize("reason", [
    tso.SNAPSHOT_NO_DATA, tso.SNAPSHOT_COLLECTING,
    tso.SNAPSHOT_COLLECT_ERROR, tso.SNAPSHOT_COLLECT_TIMEOUT,
    tso.SNAPSHOT_STALE, tso.SNAPSHOT_GENERATION,
    tso.SNAPSHOT_INCOMPLETE, tso.SNAPSHOT_EXTERNAL,
])
def test_u71_b_22_stage_a_fail_closed_contrast_unchanged(reason):
    """阶段 A 的全部不可信结论，其 ``trusted=False`` 语义一字未改。"""
    obs = tso.ObservationResult(reason=reason, trusted=False)
    assert obs.trusted is False
    assert obs.mode == tso.MODE_REFUSED
    assert obs.usable_snapshot is None
    assert obs.reason_text


def test_u71_b_23_format_observation_shows_bounded_grace():
    clock = VirtualClock()
    _, obs = _degraded(clock)
    text = tso.format_observation(obs)
    assert tso.MODE_DEGRADED in text
    assert "剩余宽限" in text
    assert "可信      : 否" in text


# --------------------------------------------------------------------------- #
# 5. A/B 对比：采集持续失败的自锁场景（15:50 / 16:20 复现）
# --------------------------------------------------------------------------- #
def test_u71_b_24_sustained_collect_failure_is_bounded_not_permanent():
    """阶段 A 只有 FULL→REFUSED；阶段 B 在 90s 内给出 DEGRADED，随后硬退出。"""
    clock = VirtualClock()
    calls = {"n": 0}

    def collect():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("网络采集查询失败：adapters")
        return _snapshot()

    source, jobs = _primed(clock, collect=collect)
    timeline = [(0.0, source.observe(0.0))]
    for at in (3.0, 10.0, 31.0, 60.0, 90.0, 91.0, 120.0):
        clock.now = at
        if source.request(at):
            jobs.pop(0)()                        # 采集立即抛错，无新快照发布
        timeline.append((at, source.observe(at)))

    assert timeline[0][1].mode == tso.MODE_FULL
    assert all(obs.mode == tso.MODE_DEGRADED
               for _, obs in timeline[1:6]), [m for _, o in timeline for m in [o.mode]]
    assert all(obs.mode == tso.MODE_REFUSED for _, obs in timeline[6:])
    assert all(obs.trusted is False for _, obs in timeline[1:])
    # DEGRADED 段有可用 LKG 依据；REFUSED 段严格清空
    assert all(obs.usable_snapshot is not None for _, obs in timeline[1:6])
    assert all(obs.usable_snapshot is None for _, obs in timeline[6:])
    # 宽限单调递减且严格有界
    graces = [obs.grace_left for _, obs in timeline[1:6]]
    assert graces == sorted(graces, reverse=True)
    assert max(graces) <= WINDOW
    assert calls["n"] > 1                         # 确实重放了持续采集失败
