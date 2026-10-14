# -*- coding: utf-8 -*-
"""V0.3 P3 故障切换状态机单元测试（U-3-01..U-3-17）。

被测对象：``campus_failover.py`` 的 11 态状态机、滞回/冷却、超时与失败处理。

全部输入为假件（真实 P1 契约对象 + :class:`tests.fakes.FakeOpExecutor`），
零真实网络、零真实路由/注册表写入、零真实凭据读取、零真实 sleep。

状态缩写：H=HEALTHY，D=LINK_DOWN，C=CAPTIVE_PORTAL，U=PROBE_UNRELIABLE。
"""
from __future__ import annotations

import pytest

from campus_failover import (CONTRACT_VERSION, EXECUTION_STATES,
                             FailoverMode, FailoverPolicy, FailoverState,
                             NetworkFailoverManager, OpKind,
                             RELIABLE_FAILURE_STATES, UNRELIABLE_STATES)
from campus_network import (EgressInfo, HealthAxis, HealthState,
                            InterfaceHealth, NetworkHealthSnapshot, SystemHealth)
from tests.fakes import FakeOpExecutor

H = HealthState.HEALTHY
D = HealthState.LINK_DOWN
C = HealthState.CAPTIVE_PORTAL
U = HealthState.PROBE_UNRELIABLE

ETH_IF = 10
WIFI_IF = 5
STEP = 15.0


def snapshot(eth: HealthState | None = H, wifi: HealthState | None = U,
             egress_if: int | None = ETH_IF,
             *, online: bool = True) -> NetworkHealthSnapshot:
    """构造真实 P1 契约快照（ifIndex 固定为以太网 10 / Wi-Fi 5）。"""
    ethernet = None if eth is None else InterfaceHealth(
        HealthAxis.ETHERNET_HEALTH, "以太网", ETH_IF, eth)
    wifi_h = None if wifi is None else InterfaceHealth(
        HealthAxis.WIFI_HEALTH, "WLAN", WIFI_IF, wifi)
    egress = None if egress_if is None else EgressInfo(
        target="1.1.1.1", source_ip="192.168.124.9", if_index=egress_if)
    return NetworkHealthSnapshot(
        generated_at="2026-10-14T00:00:00+08:00", host="test-host",
        ethernet=ethernet, wifi=wifi_h,
        system=SystemHealth(online=online, source="fake", egress=egress))


def auto_manager(executor=None, **policy_kwargs):
    """AUTO 模式 + 假执行器（离线；真实执行层由 P5 接入）。"""
    policy = FailoverPolicy(**policy_kwargs) if policy_kwargs else FailoverPolicy()
    return NetworkFailoverManager(policy=policy, mode=FailoverMode.AUTO,
                                  executor=executor or FakeOpExecutor())


def monitor_manager(**policy_kwargs):
    policy = FailoverPolicy(**policy_kwargs) if policy_kwargs else FailoverPolicy()
    return NetworkFailoverManager(policy=policy, mode=FailoverMode.MONITOR)


def run_frames(manager, frames, *, t0=0.0, step=STEP):
    """按帧驱动，返回 (最后一个 TickResult, 状态序列, 下一时刻)。"""
    results = []
    t = t0
    for frame in frames:
        results.append(manager.tick(t, snapshot(*frame)))
        t += step
    return results[-1], [r.state for r in results], t


def drive_to_backup(manager, *, t0=0.0):
    """把 AUTO 管理器从初始态驱动到 WIFI_BACKUP_ACTIVE，返回下一时刻。"""
    frames = [(H, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF),
              (D, H, WIFI_IF), (D, H, WIFI_IF), (D, H, WIFI_IF)]
    last, states, t = run_frames(manager, frames, t0=t0)
    assert last.state is FailoverState.WIFI_BACKUP_ACTIVE, states
    return t


# --------------------------------------------------------------------------- #
# U-3-01 契约与初始态
# --------------------------------------------------------------------------- #
def test_u3_01_contract_and_initial_state():
    assert CONTRACT_VERSION == "v03-p3-failover-1"
    # 方案 §5 的 11 个状态全部存在，且无多余状态
    assert len(list(FailoverState)) == 11
    assert {s.value for s in FailoverState} == {
        "ETHERNET_PRIMARY", "ETHERNET_SUSPECT", "WIFI_PREPARING",
        "WIFI_AUTHENTICATING", "WIFI_SWITCHING", "WIFI_BACKUP_ACTIVE",
        "ETHERNET_RECOVERY_CHECK", "ETHERNET_FAILBACK", "BOTH_OFFLINE",
        "MANUAL_REQUIRED", "PAUSED"}
    # 只读监测是 P3 默认模式，且不执行任何动作
    assert FailoverMode.MONITOR.executes is False
    assert FailoverMode.LOCKED.executes is False
    assert FailoverMode.AUTO.executes is True
    manager = monitor_manager()
    assert manager.state is FailoverState.ETHERNET_PRIMARY
    assert manager.mode is FailoverMode.MONITOR
    assert manager.timers.next_detection_at == 0.0
    # 分类集合契约：可靠失败 / 不可判定 / 需认证互不重叠
    assert not (RELIABLE_FAILURE_STATES & UNRELIABLE_STATES)
    assert HealthState.CAPTIVE_PORTAL not in RELIABLE_FAILURE_STATES
    assert EXECUTION_STATES == {FailoverState.WIFI_AUTHENTICATING,
                               FailoverState.WIFI_SWITCHING,
                               FailoverState.ETHERNET_FAILBACK}


# --------------------------------------------------------------------------- #
# U-3-02 以太网健康：零动作、零认证（方案 §6.1）
# --------------------------------------------------------------------------- #
def test_u3_02_ethernet_healthy_stays_primary_without_any_action():
    executor = FakeOpExecutor()
    manager = auto_manager(executor)
    for wifi_state in (H, U, C, D, None):
        last, states, _ = run_frames(
            manager, [(H, wifi_state, ETH_IF)] * 3)
        assert last.state is FailoverState.ETHERNET_PRIMARY
        assert last.plan == []
        assert last.executed == []
        assert last.would_switch is False
    assert executor.calls == []          # 有线正常时禁止自动重登 SNNU


# --------------------------------------------------------------------------- #
# U-3-03 滞回：连续 3 次可靠失败才进入疑似异常
# --------------------------------------------------------------------------- #
def test_u3_03_failure_hysteresis_requires_three_reliable_failures():
    manager = monitor_manager(failure_threshold=3)
    first = manager.tick(0.0, snapshot(H, U, ETH_IF))
    assert first.state is FailoverState.ETHERNET_PRIMARY
    # 第 1、2 次可靠失败：仍保持以太网优先（不切换、不认证）
    for index, t in ((1, 15.0), (2, 30.0)):
        result = manager.tick(t, snapshot(D, U, ETH_IF))
        assert result.state is FailoverState.ETHERNET_PRIMARY
        assert result.counters["eth_failure_streak"] == index
        assert result.plan == []
    # 第 3 次可靠失败才进入疑似异常
    result = manager.tick(45.0, snapshot(D, U, ETH_IF))
    assert result.state is FailoverState.ETHERNET_SUSPECT
    assert result.previous_state is FailoverState.ETHERNET_PRIMARY
    assert result.counters["eth_failure_streak"] == 3
    assert "疑似异常" in "".join(result.notes)


def test_u3_03b_single_transient_failure_does_not_trigger_anything():
    """单次失败即恢复：不得触发切换（滞回窗口内计数被清零）。"""
    executor = FakeOpExecutor()
    manager = auto_manager(executor, failure_threshold=3)
    run_frames(manager, [(H, U, ETH_IF), (D, U, ETH_IF), (H, U, ETH_IF),
                         (H, U, ETH_IF)])
    assert manager.state is FailoverState.ETHERNET_PRIMARY
    assert manager.eth_failure_streak == 0
    assert executor.calls == []


# --------------------------------------------------------------------------- #
# U-3-04 UNKNOWN / PROBE_UNRELIABLE：不计数、不动作（方案 §3.3）
# --------------------------------------------------------------------------- #
def test_u3_04_unreliable_states_never_count_or_act():
    executor = FakeOpExecutor()
    manager = auto_manager(executor)
    for _ in range(6):
        result = manager.tick(0.0, snapshot(U, U, None))
    assert result.state is FailoverState.ETHERNET_PRIMARY
    assert manager.eth_failure_streak == 0
    assert result.plan == [] and result.executed == []
    # 角色未解析（None）同样不动作
    result = manager.tick(15.0, snapshot(None, None, None))
    assert result.state is FailoverState.ETHERNET_PRIMARY
    assert result.plan == []
    assert executor.calls == []


def test_u3_04b_reliable_failure_counts_every_declared_state():
    """可靠失败集合内的每一种结论都必须计入连续失败（不漏判）。"""
    for state in sorted(RELIABLE_FAILURE_STATES, key=lambda s: s.value):
        manager = monitor_manager(failure_threshold=1)
        manager.tick(0.0, snapshot(state, U, ETH_IF))
        result = manager.tick(15.0, snapshot(state, U, ETH_IF))
        assert manager.eth_failure_streak == 1
        assert result.state is FailoverState.ETHERNET_SUSPECT


def test_u3_04c_captive_portal_counts_as_ethernet_failure():
    """有线被 Portal 拦截（CAPTIVE_PORTAL）同样不可用，计入可靠失败。"""
    manager = monitor_manager(failure_threshold=1)
    manager.tick(0.0, snapshot(C, U, ETH_IF))
    result = manager.tick(15.0, snapshot(C, U, ETH_IF))
    assert result.state is FailoverState.ETHERNET_SUSPECT


# --------------------------------------------------------------------------- #
# U-3-05 只读监测：不进入执行态，仅上报预测状态
# --------------------------------------------------------------------------- #
def test_u3_05_monitor_reports_prediction_without_executing():
    manager = monitor_manager()
    last, states, _ = run_frames(
        manager, [(H, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF),
                  (D, C, WIFI_IF), (D, C, WIFI_IF)])
    assert last.state is FailoverState.WIFI_PREPARING
    assert last.predicted_state is FailoverState.WIFI_AUTHENTICATING
    assert [p.kind for p in last.plan] == [OpKind.PORTAL_POST]
    assert last.executed == []
    assert "只读监测" in "".join(last.notes)
    # 预测不改变真实状态：后续帧仍停留在准备阶段
    again = manager.tick(120.0, snapshot(D, C, WIFI_IF))
    assert again.state is FailoverState.WIFI_PREPARING


def test_u3_05b_monitor_plans_switch_but_never_executes():
    executor = FakeOpExecutor()
    manager = monitor_manager()
    last, states, _ = run_frames(
        manager, [(H, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF),
                  (D, H, WIFI_IF), (D, H, WIFI_IF)])
    assert last.state is FailoverState.WIFI_PREPARING
    assert last.predicted_state is FailoverState.WIFI_SWITCHING
    assert last.would_switch is True
    assert [p.kind for p in last.plan] == [OpKind.METRIC_WRITE]
    assert last.executed == [] and last.readonly is True
    assert manager.timers.switch_ready_at == 0.0   # 未执行 → 不登记切换冷却
    assert executor.calls == []
    # 只读监测下执行态一律不可达
    assert not any(s in EXECUTION_STATES for s in states)


# --------------------------------------------------------------------------- #
# U-3-06 双断：BOTH_OFFLINE 且限频恢复尝试
# --------------------------------------------------------------------------- #
def test_u3_06_both_offline_after_suspect_and_limited_retry():
    executor = FakeOpExecutor()
    manager = auto_manager(executor, wifi_reconnect_cooldown=60.0)
    last, states, t = run_frames(
        manager, [(H, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF),
                  (D, D, None)])
    assert last.state is FailoverState.BOTH_OFFLINE
    assert last.plan == []            # 本轮刚进入，尚未到恢复尝试窗口
    # 第一轮：计划一次无线重连（限频）
    result = manager.tick(t, snapshot(D, D, None))
    assert result.state is FailoverState.BOTH_OFFLINE
    assert [p.kind for p in result.plan] == [OpKind.WIFI_RECONNECT]
    assert len(executor.calls) == 1
    # 冷却期内：不再重复尝试（禁止在两张网卡之间来回切换）
    for _ in range(3):
        t += STEP
        result = manager.tick(t, snapshot(D, D, None))
        assert result.plan == []
    assert len(executor.calls) == 1
    # 冷却过后再尝试一次
    t += 60.0
    result = manager.tick(t, snapshot(D, D, None))
    assert [p.kind for p in result.plan] == [OpKind.WIFI_RECONNECT]
    assert len(executor.calls) == 2


# --------------------------------------------------------------------------- #
# U-3-07 超时：疑似异常/备用准备超时 → 人工处理或双断
# --------------------------------------------------------------------------- #
def test_u3_07_suspect_timeout_goes_manual_when_unjudgeable():
    manager = monitor_manager(suspect_timeout=60.0)
    last, states, t = run_frames(
        manager, [(H, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF)])
    assert last.state is FailoverState.ETHERNET_SUSPECT
    # 有线仍失败且无线不可判定：等待到超时 → 人工处理（不臆断、不切换）
    while manager.state is FailoverState.ETHERNET_SUSPECT and t < 600:
        result = manager.tick(t, snapshot(D, U, ETH_IF))
        t += STEP
    assert manager.state is FailoverState.MANUAL_REQUIRED
    assert "不可判定" in manager.manual_reason
    assert result.plan == []


def test_u3_07b_prepare_timeout_falls_to_both_offline():
    """备用链路准备超时（无线仍不可用）→ 两条路径均不可用。"""
    manager = monitor_manager(prepare_timeout=30.0)
    last, states, t = run_frames(
        manager, [(H, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF),
                  (D, C, WIFI_IF), (D, D, WIFI_IF)])
    assert last.state is FailoverState.WIFI_PREPARING
    result = manager.tick(t, snapshot(D, D, WIFI_IF))
    assert result.state is FailoverState.BOTH_OFFLINE
    assert "准备超时" in "".join(result.notes)


def test_u3_07c_prepare_timeout_manual_when_wifi_unjudgeable():
    """备用链路准备期无线状态不可判定且超时 → 人工处理（不臆断、不动作）。"""
    manager = monitor_manager(prepare_timeout=30.0)
    last, states, t = run_frames(
        manager, [(H, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF),
                  (D, C, WIFI_IF), (D, U, WIFI_IF)])
    assert last.state is FailoverState.WIFI_PREPARING
    result = manager.tick(t, snapshot(D, U, WIFI_IF))
    assert result.state is FailoverState.MANUAL_REQUIRED
    assert result.plan == []


# --------------------------------------------------------------------------- #
# U-3-08 认证冷却：只限制认证、不限制探测（AGENTS §3.4）
# --------------------------------------------------------------------------- #
def test_u3_08_auth_cooldown_only_limits_auth_and_never_pauses_detection():
    executor = FakeOpExecutor()
    manager = auto_manager(executor, auth_cooldown=180.0)
    frames = [(H, U, ETH_IF)] + [(D, U, ETH_IF)] * 3 + [(D, C, WIFI_IF)] * 8
    last, states, t = run_frames(manager, frames)
    assert FailoverState.WIFI_AUTHENTICATING in states
    # 冷却窗口（180s）内只提交过一次认证
    assert executor.calls.count(OpKind.PORTAL_POST) == 1
    assert manager.timers.auth_ready_at == 75.0 + 180.0
    # 探测计时器恒为 now + interval：认证冷却不得推迟网络检测
    probe = manager.tick(t, snapshot(D, C, WIFI_IF))
    assert manager.timers.next_detection_at == t + manager.policy.detection_interval
    assert probe.counters["auth_ready_in"] > 0


def test_u3_08b_auth_retry_after_cooldown_then_manual_required():
    executor = FakeOpExecutor()
    manager = auto_manager(executor, auth_cooldown=180.0, auth_max_attempts=2)
    frames = [(H, U, ETH_IF)] + [(D, U, ETH_IF)] * 3 + [(D, C, WIFI_IF)] * 2
    last, states, t = run_frames(manager, frames)
    assert manager.auth_attempts == 1
    # 冷却过后再试一次（第 2 次）
    t = 75.0 + 180.0
    manager.tick(t, snapshot(D, C, WIFI_IF))
    assert manager.auth_attempts == 2
    assert executor.calls.count(OpKind.PORTAL_POST) == 2
    # 超过重试上限：停止自动认证，转人工（不无限重试）
    t += STEP
    result = manager.tick(t, snapshot(D, C, WIFI_IF))
    assert result.state is FailoverState.MANUAL_REQUIRED
    assert "认证重复失败" in manager.manual_reason
    assert executor.calls.count(OpKind.PORTAL_POST) == 2


# --------------------------------------------------------------------------- #
# U-3-09 切换冷却：300s 内不重复改变路由（方案 §4.3）
# --------------------------------------------------------------------------- #
def test_u3_09_switch_cooldown_blocks_repeated_metric_write():
    executor = FakeOpExecutor()
    manager = auto_manager(executor, switch_min_interval=300.0)
    t = drive_to_backup(manager)
    assert executor.calls.count(OpKind.METRIC_WRITE) == 1
    assert manager.timers.switch_ready_at == 75.0 + 300.0
    # 无线掉线 → 双断 → 无线恢复 → 准备切换，但冷却未过 → 只等待不切换
    result = manager.tick(t, snapshot(D, D, WIFI_IF))
    assert result.state is FailoverState.BOTH_OFFLINE
    t += STEP
    result = manager.tick(t, snapshot(D, H, WIFI_IF))
    assert result.state is FailoverState.WIFI_PREPARING
    for _ in range(3):
        t += STEP
        result = manager.tick(t, snapshot(D, H, WIFI_IF))
        assert result.state is FailoverState.WIFI_PREPARING
        assert "切换冷却未过" in "".join(result.notes)
    assert executor.calls.count(OpKind.METRIC_WRITE) == 1
    # 冷却过后才允许再次切换
    t = 75.0 + 300.0
    result = manager.tick(t, snapshot(D, H, WIFI_IF))
    assert result.state is FailoverState.WIFI_SWITCHING
    assert executor.calls.count(OpKind.METRIC_WRITE) == 2


# --------------------------------------------------------------------------- #
# U-3-10 有线恢复判据：连续稳定 ≥120s 且 ≥5 次成功（方案 §4.3）
# --------------------------------------------------------------------------- #
def test_u3_10_recovery_requires_stable_window_and_success_count():
    executor = FakeOpExecutor()
    manager = auto_manager(executor)
    t = drive_to_backup(manager)
    result = manager.tick(t, snapshot(H, H, WIFI_IF))
    assert result.state is FailoverState.ETHERNET_RECOVERY_CHECK
    assert manager.recovery_successes == 1
    # 成功次数先到 5 次，但稳定时长不足 120s → 不切回
    for _ in range(4):
        t += STEP
        result = manager.tick(t, snapshot(H, H, WIFI_IF))
        assert result.state is FailoverState.ETHERNET_RECOVERY_CHECK
    assert manager.recovery_successes >= 5
    assert t - manager.recovery_since < 120.0
    assert executor.calls.count(OpKind.METRIC_WRITE) == 1
    # 稳定窗口满足且切换冷却已过 → 切回
    t = 75.0 + 300.0
    result = manager.tick(t, snapshot(H, H, WIFI_IF))
    assert result.state is FailoverState.ETHERNET_FAILBACK
    assert executor.calls.count(OpKind.METRIC_WRITE) == 2
    assert result.plan[0].payload["target"] == "ethernet"
    # 出口已切回以太网 → 完成
    result = manager.tick(t + STEP, snapshot(H, H, ETH_IF))
    assert result.state is FailoverState.ETHERNET_PRIMARY


def test_u3_10b_recovery_window_resets_when_ethernet_fails_again():
    manager = auto_manager()
    t = drive_to_backup(manager)
    manager.tick(t, snapshot(H, H, WIFI_IF))
    assert manager.state is FailoverState.ETHERNET_RECOVERY_CHECK
    t += STEP
    result = manager.tick(t, snapshot(D, H, WIFI_IF))
    assert result.state is FailoverState.WIFI_BACKUP_ACTIVE
    assert manager.recovery_successes == 0


def test_u3_10c_failback_without_verification_rolls_back():
    """切回后若出口未改变（验证失败/超时）→ 撤销，重新进入观察期。"""
    executor = FakeOpExecutor()
    manager = auto_manager(executor)
    t = drive_to_backup(manager)
    manager.tick(t, snapshot(H, H, WIFI_IF))
    # 先满足"连续 5 次成功探测"（稳定时长由 120s 判据约束）
    for _ in range(4):
        t += STEP
        manager.tick(t, snapshot(H, H, WIFI_IF))
    assert manager.recovery_successes >= 5
    t = 75.0 + 300.0
    result = manager.tick(t, snapshot(H, H, WIFI_IF))
    assert result.state is FailoverState.ETHERNET_FAILBACK
    # 出口仍为 Wi-Fi：等待至切换验证超时 → 撤销回 Wi-Fi 接管
    for _ in range(4):
        t += STEP
        result = manager.tick(t, snapshot(H, H, WIFI_IF))
        if manager.state is FailoverState.WIFI_BACKUP_ACTIVE:
            break
    assert manager.state is FailoverState.WIFI_BACKUP_ACTIVE
    assert "撤销" in "".join(result.notes)


# --------------------------------------------------------------------------- #
# U-3-11 全 11 态可达（AUTO 离线假执行器）
# --------------------------------------------------------------------------- #
def test_u3_11_all_eleven_states_are_reachable():
    """AUTO + 离线假执行器：11 个状态在完整剧本中全部可达。"""
    executor = FakeOpExecutor()
    manager = auto_manager(executor)
    script = [
        (0.0, (H, U, ETH_IF)),        # ETHERNET_PRIMARY
        (15.0, (D, U, ETH_IF)),       # 失败 1/3
        (30.0, (D, U, ETH_IF)),       # 失败 2/3
        (45.0, (D, U, ETH_IF)),       # ETHERNET_SUSPECT
        (60.0, (D, C, WIFI_IF)),      # WIFI_PREPARING
        (75.0, (D, C, WIFI_IF)),      # WIFI_AUTHENTICATING（portal_post）
        (90.0, (D, H, WIFI_IF)),      # 认证完成 → 回到准备
        (105.0, (D, H, WIFI_IF)),     # WIFI_SWITCHING（metric_write）
        (120.0, (D, H, WIFI_IF)),     # WIFI_BACKUP_ACTIVE
        (135.0, (H, H, WIFI_IF)),     # ETHERNET_RECOVERY_CHECK
        (150.0, (H, H, WIFI_IF)),
        (165.0, (H, H, WIFI_IF)),
        (180.0, (H, H, WIFI_IF)),
        (195.0, (H, H, WIFI_IF)),     # 成功 5 次，稳定窗口未满
        (405.0, (H, H, WIFI_IF)),     # 稳定 270s 且切换冷却已过 → ETHERNET_FAILBACK
        (420.0, (H, H, ETH_IF)),      # 出口验证通过 → ETHERNET_PRIMARY
        (435.0, (D, D, None)),
        (450.0, (D, D, None)),
        (465.0, (D, D, None)),        # 失败 3/3 → ETHERNET_SUSPECT
        (480.0, (D, D, None)),        # BOTH_OFFLINE
    ]
    seen = {manager.state}
    for at, frame in script:
        seen.add(manager.tick(at, snapshot(*frame)).state)
    assert manager.state is FailoverState.BOTH_OFFLINE
    # 人工处理（外部信号，P4/P6 接入）
    manager.require_manual(495.0, "凭据错误：停止后台自动登录")
    seen.add(manager.tick(510.0, snapshot(D, D, None)).state)
    assert manager.state is FailoverState.MANUAL_REQUIRED
    manager.clear_manual(525.0)
    assert manager.state is FailoverState.ETHERNET_PRIMARY
    # 暂停
    manager.pause(540.0, "用户暂停")
    result = manager.tick(555.0, snapshot(H, H, ETH_IF))
    seen.add(result.state)
    assert result.state is FailoverState.PAUSED
    manager.resume(570.0)
    assert manager.state is FailoverState.ETHERNET_PRIMARY
    assert seen == set(FailoverState), sorted(s.value for s in seen)
    # 剧本全程：认证与切换各执行一次，且互斥串行
    assert executor.calls == [OpKind.PORTAL_POST, OpKind.METRIC_WRITE,
                              OpKind.METRIC_WRITE]


def test_u3_11b_paused_and_manual_are_sticky_and_action_free():
    executor = FakeOpExecutor()
    manager = auto_manager(executor)
    manager.pause(0.0, "用户暂停")
    for t in (STEP, 2 * STEP):
        result = manager.tick(t, snapshot(D, D, None))
        assert result.state is FailoverState.PAUSED
        assert result.plan == [] and result.executed == []
    manager.resume(3 * STEP)
    manager.require_manual(3 * STEP, "Portal 结构未知")
    for t in (4 * STEP, 5 * STEP):
        result = manager.tick(t, snapshot(D, C, WIFI_IF))
        assert result.state is FailoverState.MANUAL_REQUIRED
        assert result.plan == [] and result.executed == []
    assert executor.calls == []


# --------------------------------------------------------------------------- #
# U-3-12 认证过程中有线恢复：中止无线路径（不执行多余认证）
# --------------------------------------------------------------------------- #
def test_u3_12_ethernet_recovery_aborts_auth_path():
    executor = FakeOpExecutor()
    manager = auto_manager(executor)
    last, states, t = run_frames(
        manager, [(H, U, ETH_IF)] + [(D, U, ETH_IF)] * 3
        + [(D, C, WIFI_IF)] * 2)
    assert manager.state is FailoverState.WIFI_AUTHENTICATING
    result = manager.tick(t, snapshot(H, C, ETH_IF))
    assert result.state is FailoverState.ETHERNET_PRIMARY
    assert "中止无线认证路径" in "".join(result.notes)


# --------------------------------------------------------------------------- #
# U-3-13 切换执行失败：撤销并回到以太网优先
# --------------------------------------------------------------------------- #
def test_u3_13_switch_without_verification_rolls_back_to_ethernet():
    """切换执行后出口未改变、且以太网已恢复 → 撤销本次切换。"""
    executor = FakeOpExecutor()
    manager = auto_manager(executor)
    last, states, t = run_frames(
        manager, [(H, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF), (D, U, ETH_IF),
                  (D, H, ETH_IF), (D, H, ETH_IF), (H, H, ETH_IF)])
    assert last.state is FailoverState.ETHERNET_PRIMARY
    assert "撤销" in "".join(last.notes)
    # 回退动作确实以"回退以太网优先"提交
    assert [p.payload.get("target") for p in last.plan] == ["ethernet"]
    assert [o.kind for o in last.executed] == [OpKind.METRIC_WRITE]


# --------------------------------------------------------------------------- #
# U-3-14 策略参数校验：只告警、不阻断
# --------------------------------------------------------------------------- #
def test_u3_14_policy_validation_reports_warnings():
    assert FailoverPolicy().validate() == []
    issues = FailoverPolicy(detection_interval=1.0, failure_threshold=0,
                            auth_cooldown=10.0, switch_min_interval=30.0,
                            recovery_stable_seconds=0.0,
                            recovery_min_successes=0).validate()
    assert len(issues) == 5
    joined = "；".join(issues)
    assert "5–15s" in joined and "180s" in joined and "300s" in joined
    # 非法参数不阻断运行（文档明确这些是建议值）
    manager = monitor_manager(detection_interval=1.0)
    assert manager.tick(0.0, snapshot(H, U, ETH_IF)).state \
        is FailoverState.ETHERNET_PRIMARY


def test_u3_14b_policy_is_frozen_and_rejects_mutation():
    policy = FailoverPolicy()
    with pytest.raises(Exception):
        policy.failure_threshold = 9      # frozen dataclass：策略不可被就地篡改
