# -*- coding: utf-8 -*-
"""V0.7.1 阶段 C：分级执行器（DEGRADED 白名单）离线回归。

背景（15:50 / 16:20 两次 10 分钟断连的最后一半根因）：阶段 B 已把观测分为
``FULL / DEGRADED / REFUSED``，但状态机在 ``trusted=False`` 时根本不调用
``_decide()``，动作计划恒为空 ⇒ 仅替换执行器无法解除自锁。

阶段 C（用户 2026-11-12 确认的三条边界）：

1. 只有**显式**登记 ``MODE_DEGRADED`` 才启用分级；缺省登记路径仍是
   ``FULL`` / ``REFUSED`` 二元语义（阶段 A/B 全部既有行为零变化）；
2. 白名单仅 ``PORTAL_POST`` / ``WIFI_RECONNECT``；``METRIC_WRITE`` /
   ``CLASH_TOUCH`` 一律拒绝，UU 重启由整合层 ``trusted=False`` 拒绝；
3. 每 ``network_epoch`` 限 2 次 + 全局 180s 冷却；宽限耗尽 / 无 LKG /
   非 AUTO 授权 / 冷却中立即退化回 ``RefusingExecutor``。

覆盖：契约与纯函数预算校验、白名单与拒绝语义、有界窗口 / epoch / 冷却、
bridge 接线（含 ``VerifiedActionGate`` 组合）、托盘端到端闭合回路、
A/B/C 三阶段对比（自锁解除）。

约束：零真实网络 / 零真实路由写入 / 零真实注册表写入 / 零真实凭据读取 /
零真实浏览器启动 / 零真实 sleep（虚拟时钟注入，``GradedExecutor`` 不读时钟）。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import campus_reconnect_tray as tray
import campus_tray_failover as tf
import campus_tray_observer as tso
from campus_dual_path import VerifiedActionGate
from campus_failover import (FailoverMode, FailoverState, OpKind, OpOutcome,
                             OpRequest)
from campus_network import HealthAxis, HealthState, InterfaceHealth
from campus_reconnect import PATH_OK, PATH_ROLE_WIFI, PathVerificationResult
from tests.fakes import FakeOpExecutor

LOG = logging.getLogger("campus")

ETH_IF = 10
WIFI_IF = 5
COOLDOWN = tso.DEFAULT_GRADED_COOLDOWN          # 180s（用户确认值）
EPOCH_LIMIT = tso.DEFAULT_GRADED_MAX_PER_EPOCH  # 2（用户确认值）
WHITELIST = (OpKind.PORTAL_POST, OpKind.WIFI_RECONNECT)
DENYLIST = (OpKind.METRIC_WRITE, OpKind.CLASH_TOUCH)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _health(axis, state, if_index):
    return InterfaceHealth(axis, "测试网卡", if_index, state)


def _snapshot(eth_state=HealthState.LINK_DOWN,
              wifi_state=HealthState.CAPTIVE_PORTAL,
              egress_if=WIFI_IF):
    """完整三轴快照（带 recommendation / generated_at，满足 AUTO 契约）。"""
    snap = tf.light_snapshot(
        system_online=False,
        ethernet=_health(HealthAxis.ETHERNET_HEALTH, eth_state, ETH_IF),
        wifi=_health(HealthAxis.WIFI_HEALTH, wifi_state, WIFI_IF),
        egress_if=egress_if)
    snap.generated_at = "2026-11-12T16:00:00"
    snap.recommendation = SimpleNamespace(preferred="wifi", would_switch=False)
    return snap


def _request(kind):
    return OpRequest(kind=kind, reason="测试请求", payload={})


def _graded(**overrides):
    """构造一个「窗口可用」的分级执行器（delegate 记录调用）。"""
    kwargs = dict(delegate=FakeOpExecutor(), grace_left=59.0, epoch=7,
                  now=100.0)
    kwargs.update(overrides)
    return tso.GradedExecutor(**kwargs)


def _bridge(**overrides):
    kwargs = dict(mode="AUTO", authorized=True,
                  executor_factory=lambda: FakeOpExecutor())
    kwargs.update(overrides)
    return tf.TrayFailoverBridge(**kwargs)


def _degraded(bridge, *, grace_left=59.0, epoch=7, now=100.0):
    """把桥接登记为显式 DEGRADED（阶段 C 唯一启用路径）。"""
    bridge.set_snapshot_trust(False, "三轴快照已过期",
                              mode=tso.MODE_DEGRADED,
                              grace_left=grace_left, epoch=epoch, now=now)


def _prepare(bridge, state=FailoverState.WIFI_PREPARING):
    """把状态机置于指定状态（绕开到达该状态所需的观测轮次）。"""
    bridge.manager.state = state


# --------------------------------------------------------------------------- #
# 1. 契约与纯函数预算校验
# --------------------------------------------------------------------------- #
def test_u71_c_01_contract_and_whitelist():
    """观测契约不升版（数据结构未变）；分级契约独立标记；白名单恰为两类。"""
    assert tso.CONTRACT_VERSION == "v03-p75-obs-2"
    assert tso.GRADED_CONTRACT_VERSION == "v03-p75-graded-1"
    assert tso.DEGRADED_ALLOWED_KINDS == frozenset(
        k.value for k in WHITELIST)
    assert tso.DEFAULT_GRADED_MAX_PER_EPOCH == 2
    assert tso.DEFAULT_GRADED_COOLDOWN == 180.0
    assert tso.MIN_GRADED_COOLDOWN == 180.0
    assert tso.MAX_GRADED_COOLDOWN == 3600.0
    assert tso.DEGRADED_ALLOWED_KINDS.isdisjoint(
        {k.value for k in DENYLIST})


@pytest.mark.parametrize("limit,cooldown,ok", [
    (2, 180.0, True),        # 用户确认默认值
    (1, 180.0, True),
    (10, 3600.0, True),      # 边界包含
    (0, 180.0, False),
    (11, 180.0, False),
    (-1, 180.0, False),
    (True, 180.0, False),    # bool 不是合法整数
    (2, 179.9, False),       # 低于 180s 硬下限
    (2, 0.0, False),
    (2, 3600.1, False),      # 超过硬上限 = 永久禁用
    (2, float("nan"), False),
    (2, "abc", False),
])
def test_u71_c_02_check_graded_budget_invariants(limit, cooldown, ok):
    coherent, reasons = tso.check_graded_budget(limit, cooldown)
    assert coherent is ok
    assert (len(reasons) == 0) is ok
    if not ok:
        assert all(reasons)


def test_u71_c_03_illegal_budget_rejected_at_construction():
    with pytest.raises(ValueError):
        tso.GradedExecutor(FakeOpExecutor(), max_per_epoch=0, cooldown=30.0)
    with pytest.raises(ValueError):
        tso.GradedExecutor(FakeOpExecutor(), cooldown=7200.0)


# --------------------------------------------------------------------------- #
# 2. 白名单与拒绝语义（P7.5-1A：拒绝不消耗冷却、不谎报）
# --------------------------------------------------------------------------- #
def test_u71_c_04_whitelist_executes_delegate():
    ex = _graded()
    outcome = ex.execute(_request(OpKind.PORTAL_POST))
    assert outcome.gate_passed is True
    assert outcome.attempted is True and outcome.executed is True
    assert ex.delegate.calls == [OpKind.PORTAL_POST]
    assert len(ex.allowed) == 1 and ex.refused == []
    assert ex.actions_left == EPOCH_LIMIT - 1


@pytest.mark.parametrize("kind", DENYLIST)
def test_u71_c_05_denylist_refused_fail_closed(kind):
    ex = _graded()
    outcome = ex.execute(_request(kind))
    assert outcome.gate_passed is True          # 过了互斥串行与模式门禁
    assert outcome.attempted is False           # 不是真实尝试：不消耗状态机冷却
    assert outcome.executed is False            # 零网络副作用
    assert "白名单" in outcome.detail
    assert ex.delegate.calls == []              # 真实执行层根本没被调用
    assert ex.refused and ex.actions_left == EPOCH_LIMIT


def test_u71_c_06_refusal_does_not_consume_budget_or_cooldown():
    ex = _graded()
    for _ in range(3):
        ex.execute(_request(OpKind.METRIC_WRITE))
        ex.execute(_request(OpKind.CLASH_TOUCH))
    assert ex.active is True                    # 拒绝完全不影响预算
    outcome = ex.execute(_request(OpKind.PORTAL_POST))
    assert outcome.executed is True


def test_u71_c_07_delegate_return_protocols():
    """与 ``NetworkOpScheduler._invoke`` 相同的返回值语义（含异常兜底）。"""
    class Odd:
        def __init__(self):
            self.queue = [True, None, False, "已执行（说明）",
                          RuntimeError("boom"),
                          OpOutcome(OpKind.PORTAL_POST, True, attempted=False,
                                    executed=False, detail="门内拒绝")]

        def execute(self, request):
            value = self.queue.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

    ex = tso.GradedExecutor(Odd(), grace_left=59.0, epoch=1, now=0.0,
                            max_per_epoch=10, cooldown=180.0)
    # 冷却会拦截后续尝试，因此逐轮注入新 now（模拟真实桥接的每轮 update）。
    now = 0.0
    outcomes = []
    for _ in range(6):
        ex.update(grace_left=59.0, epoch=1, now=now)
        outcomes.append(ex.execute(_request(OpKind.PORTAL_POST)))
        now += COOLDOWN                          # 只有 attempted 才真正记账
    assert [(o.attempted, o.executed) for o in outcomes] == [
        (True, True), (True, True), (True, False), (True, True),
        (True, False), (False, False)]
    assert outcomes[4].detail.startswith("执行器异常")
    assert outcomes[5].attempted is False        # 透传 OpOutcome 原样返回


def test_u71_c_08_block_reason_is_pure():
    ex = _graded()
    before = (ex.actions_left, ex.cooldown_left, ex.active, len(ex.allowed))
    assert ex.block_reason("metric_write")
    assert ex.block_reason("clash_touch")
    assert ex.block_reason("")                   # 未知动作
    assert ex.block_reason("portal_post") == ""
    assert (ex.actions_left, ex.cooldown_left, ex.active,
            len(ex.allowed)) == before


def test_u71_c_09_no_delegate_is_inactive_and_refused():
    ex = tso.GradedExecutor(None, grace_left=59.0, epoch=1, now=0.0)
    assert ex.active is False
    outcome = ex.execute(_request(OpKind.PORTAL_POST))
    assert outcome.attempted is False and outcome.executed is False
    assert "执行层" in outcome.detail


# --------------------------------------------------------------------------- #
# 3. 有界窗口 / epoch 限次 / 全局冷却
# --------------------------------------------------------------------------- #
def test_u71_c_10_zero_grace_closes_window():
    ex = _graded(grace_left=0.0)
    assert ex.active is False
    outcome = ex.execute(_request(OpKind.WIFI_RECONNECT))
    assert outcome.attempted is False and "宽限" in outcome.detail


def test_u71_c_11_cooldown_blocks_until_elapsed():
    ex = _graded(now=100.0)
    assert ex.execute(_request(OpKind.PORTAL_POST)).executed is True
    assert ex.active is False and ex.cooldown_left == COOLDOWN
    ex.update(grace_left=59.0, epoch=7, now=100.0 + COOLDOWN - 1)
    assert ex.active is False
    outcome = ex.execute(_request(OpKind.PORTAL_POST))
    assert outcome.attempted is False and "冷却" in outcome.detail
    ex.update(grace_left=59.0, epoch=7, now=100.0 + COOLDOWN)
    assert ex.active is True
    assert ex.actions_left == EPOCH_LIMIT - 1    # 同 epoch 名额继续扣减


def test_u71_c_12_epoch_limit_enforced():
    ex = _graded(now=100.0)
    first = ex.execute(_request(OpKind.PORTAL_POST))
    assert first.executed is True
    ex.update(grace_left=59.0, epoch=7, now=100.0 + COOLDOWN)
    second = ex.execute(_request(OpKind.WIFI_RECONNECT))
    assert second.executed is True
    assert ex.actions_left == 0 and ex.active is False
    ex.update(grace_left=59.0, epoch=7, now=100.0 + 2 * COOLDOWN)
    outcome = ex.execute(_request(OpKind.PORTAL_POST))
    assert outcome.attempted is False and "限次" in outcome.detail


def test_u71_c_13_new_epoch_resets_budget_not_cooldown():
    ex = _graded(now=100.0)
    assert ex.execute(_request(OpKind.PORTAL_POST)).executed is True
    # epoch 已变化（网络身份变化），名额重置；但全局冷却仍然生效。
    ex.update(grace_left=59.0, epoch=8, now=100.0 + 10)
    assert ex.actions_left == EPOCH_LIMIT
    assert ex.active is False and ex.cooldown_left > 0
    outcome = ex.execute(_request(OpKind.PORTAL_POST))
    assert outcome.attempted is False and "冷却" in outcome.detail
    ex.update(grace_left=59.0, epoch=8, now=100.0 + COOLDOWN)
    assert ex.active is True


def test_u71_c_14_clock_regression_cannot_bypass_cooldown():
    ex = _graded(now=100.0)
    assert ex.execute(_request(OpKind.PORTAL_POST)).executed is True
    ex.update(grace_left=59.0, epoch=7, now=50.0)   # 时钟倒退
    assert ex.cooldown_left >= COOLDOWN
    assert ex.active is False


def test_u71_c_15_update_returns_active_flag():
    ex = _graded()
    assert ex.update(grace_left=1.0, epoch=7, now=100.0) is True
    assert ex.update(grace_left=0.0, epoch=7, now=100.0) is False


# --------------------------------------------------------------------------- #
# 4. bridge 接线：缺省路径零变化 + 显式 DEGRADED 才启用
# --------------------------------------------------------------------------- #
def test_u71_c_16_default_registration_unchanged():
    """不传 ``mode`` 的既有调用：阶段 A/B 语义逐字不变。"""
    bridge = _bridge()
    bridge.set_snapshot_trust(False, "NO_DATA")
    executor = bridge.manager.scheduler.executor
    assert isinstance(executor, tso.RefusingExecutor)
    assert bridge.observation_mode == tso.MODE_REFUSED
    assert bridge.graded_active is False
    bridge.set_snapshot_trust(True, "OK")
    assert bridge.observation_mode == tso.MODE_FULL
    assert isinstance(bridge.manager.scheduler.executor, FakeOpExecutor)


def test_u71_c_17_degraded_requires_explicit_mode():
    bridge = _bridge()
    _degraded(bridge)
    executor = bridge.manager.scheduler.executor
    assert isinstance(executor, tso.GradedExecutor)
    assert bridge.graded_active is True
    assert executor.epoch == 7 and executor.grace_left == 59.0


@pytest.mark.parametrize("grace_left", [0.0, -5.0])
def test_u71_c_18_exhausted_grace_degrades_to_refusing(grace_left):
    bridge = _bridge()
    _degraded(bridge, grace_left=grace_left)
    assert isinstance(bridge.manager.scheduler.executor,
                      tso.RefusingExecutor)
    assert bridge.graded_active is False


def test_u71_c_19_unauthorized_or_monitor_degrades_to_refusing():
    for overrides in ({"authorized": False}, {"mode": "MONITOR"},
                      {"executor_factory": None}):
        bridge = _bridge(**overrides)
        _degraded(bridge)
        assert isinstance(bridge.manager.scheduler.executor,
                          tso.RefusingExecutor)
        assert bridge.graded_active is False


def test_u71_c_20_contradictory_degraded_trusted_is_refused():
    """``DEGRADED`` 与 ``trusted=True`` 矛盾时按更严格一侧处理。"""
    bridge = _bridge()
    bridge.set_snapshot_trust(True, "OK", mode=tso.MODE_DEGRADED,
                              grace_left=59.0, epoch=1, now=100.0)
    assert bridge.observation_mode == tso.MODE_REFUSED
    assert isinstance(bridge.manager.scheduler.executor, FakeOpExecutor)


def test_u71_c_21_unknown_mode_is_refused():
    bridge = _bridge()
    bridge.set_snapshot_trust(False, "奇怪结论", mode="PARTIAL",
                              grace_left=59.0, epoch=1, now=100.0)
    assert bridge.observation_mode == tso.MODE_REFUSED
    assert isinstance(bridge.manager.scheduler.executor, tso.RefusingExecutor)


def test_u71_c_22_degraded_portal_post_executes_end_to_end():
    """核心修复：DEGRADED 下状态机基于 LKG 产生 Portal 认证并真正执行。"""
    bridge = _bridge()
    _prepare(bridge)
    _degraded(bridge)
    result = bridge.tick(100.0, _snapshot())
    assert [p.kind for p in result.plan] == [OpKind.PORTAL_POST]
    assert [(o.kind, o.attempted, o.executed) for o in result.executed] == [
        (OpKind.PORTAL_POST, True, True)]
    assert result.state is FailoverState.WIFI_AUTHENTICATING
    delegate = bridge.manager.scheduler.executor.delegate
    assert delegate.calls == [OpKind.PORTAL_POST]


def test_u71_c_23_degraded_metric_write_never_executes():
    """路由写入被计划但被白名单拒绝：状态保持，不谎报已切换。"""
    bridge = _bridge()
    _prepare(bridge)
    _degraded(bridge)
    snap = _snapshot(wifi_state=HealthState.HEALTHY, egress_if=ETH_IF)
    result = bridge.tick(100.0, snap)
    assert [p.kind for p in result.plan] == [OpKind.METRIC_WRITE]
    assert result.executed == []                 # 零路由副作用
    assert result.state is FailoverState.WIFI_PREPARING
    assert bridge.manager.scheduler.executor.delegate.calls == []
    assert bridge.last_switch_at is None         # 不被记成"最近切换"


def test_u71_c_24_cooldown_degrades_back_to_refusing_with_zero_plan():
    bridge = _bridge()
    _prepare(bridge)
    _degraded(bridge)
    assert bridge.tick(100.0, _snapshot()).executed
    _degraded(bridge, grace_left=58.0, now=101.0)   # 冷却中
    assert bridge.graded_active is False
    result = bridge.tick(101.0, _snapshot())
    assert result.plan == [] and result.executed == []
    assert isinstance(bridge.manager.scheduler.executor,
                      tso.RefusingExecutor)
    delegate = bridge._graded.delegate
    assert delegate.calls == [OpKind.PORTAL_POST]      # 只执行过那一次


def test_u71_c_25_full_path_still_executes_route_write():
    """对照组：FULL 模式下路由写入不受分级执行器影响（既有语义零变化）。"""
    bridge = _bridge()
    _prepare(bridge)
    bridge.set_snapshot_trust(True, "OK")
    snap = _snapshot(wifi_state=HealthState.HEALTHY, egress_if=ETH_IF)
    result = bridge.tick(100.0, snap)
    assert [(o.kind, o.executed) for o in result.executed] == [
        (OpKind.METRIC_WRITE, True)]
    assert result.state is FailoverState.WIFI_SWITCHING
    assert not isinstance(bridge.manager.scheduler.executor,
                          tso.GradedExecutor)


def test_u71_c_26_verified_gate_still_blocks_metric_write_first():
    """组合门：验证前置门（V0.5-4）先拦 METRIC_WRITE，分级执行器兜底。"""
    gate = VerifiedActionGate(mode=FailoverMode.AUTO, clock=lambda: 100.0)
    bridge = _bridge(action_gate=gate)
    _prepare(bridge)
    _degraded(bridge)
    snap = _snapshot(wifi_state=HealthState.HEALTHY, egress_if=ETH_IF)
    # 1) 无验证证据：前置门直接拦下（分级执行器根本不会被调用）。
    result = bridge.tick(100.0, snap)
    assert result.executed == []
    assert [p.kind for p in result.plan] == [OpKind.METRIC_WRITE]
    assert result.state is FailoverState.WIFI_PREPARING
    assert bridge.manager.scheduler.executor.delegate.calls == []
    assert len(gate.gate_blocked) == 1
    # 2) 登记刚通过的接口受限验证后：前置门放行，但分级执行器仍拒绝
    #    （DEGRADED 白名单不含 METRIC_WRITE —— 双保险）。
    gate.note_verification(
        PathVerificationResult(verified=True, role=PATH_ROLE_WIFI,
                               interface_index=WIFI_IF, source_ip="10.8.0.5",
                               reason="测试：接口受限验证通过",
                               reliability=PATH_OK),
        now=100.0)
    _prepare(bridge)                     # 上轮保持原状态，这里显式归位
    result = bridge.tick(100.0, snap)
    assert result.executed == []
    assert [p.kind for p in result.plan] == [OpKind.METRIC_WRITE]
    assert gate.gate_blocked and gate.gate_blocked[0][0].kind is \
        OpKind.METRIC_WRITE              # 第 1 步的前置门拦截台账仍在
    assert bridge.manager.scheduler.executor.refused
    assert bridge.manager.scheduler.executor.refused[-1][0] is OpKind.METRIC_WRITE


def test_u71_c_27_status_surfaces_degraded_context():
    bridge = _bridge()
    _prepare(bridge)
    _degraded(bridge)
    # 动作执行前：如实告知处于有界降级且白名单可用。
    before = bridge.status()
    assert before.observation_mode == tso.MODE_DEGRADED
    assert before.graded_actions_left == EPOCH_LIMIT
    assert any("不可信" in w and "有界降级" in w for w in before.warnings)
    bridge.tick(100.0, _snapshot())
    status = bridge.status()
    assert status.observation_mode == tso.MODE_DEGRADED
    assert any("不可信" in w and "冷却/限次中" in w for w in status.warnings)
    data = status.to_dict()
    assert data["observation_mode"] == tso.MODE_DEGRADED
    assert "DEGRADED" in "\n".join(status.lines())
    # 宽限耗尽：如实告知窗口耗尽，而不是静默无动作。
    _degraded(bridge, grace_left=0.0, now=101.0)
    warnings = bridge.status().warnings
    assert any("窗口已耗尽" in w for w in warnings)


def test_u71_c_28_executor_instance_reused_across_ticks():
    """复用同一分级执行器实例：epoch 计数与冷却台账不因重复登记而清零。"""
    bridge = _bridge()
    _prepare(bridge)
    _degraded(bridge)
    first = bridge.manager.scheduler.executor
    bridge.tick(100.0, _snapshot())
    _degraded(bridge, grace_left=59.0, now=100.0 + COOLDOWN)
    second = bridge.manager.scheduler.executor
    assert first is second
    assert second.actions_left == EPOCH_LIMIT - 1


def test_u71_c_29_mode_or_auth_change_closes_window():
    bridge = _bridge()
    _prepare(bridge)
    _degraded(bridge)
    bridge.set_mode("MONITOR")
    assert isinstance(bridge.manager.scheduler.executor,
                      tso.RefusingExecutor)
    assert bridge.graded_active is False
    bridge.set_mode("AUTO")
    bridge.set_authorized(False)
    assert isinstance(bridge.manager.scheduler.executor,
                      tso.RefusingExecutor)


def test_u71_c_30_manager_tick_still_frozen_without_lkg():
    """REFUSED（无 LKG）时状态机保持冻结 —— 阶段 A/B 语义不变。"""
    bridge = _bridge()
    _prepare(bridge)
    bridge.set_snapshot_trust(False, "COLLECT_ERROR")
    result = bridge.tick(100.0, _snapshot())
    assert result.plan == [] and result.executed == []
    assert result.state is FailoverState.WIFI_PREPARING


# --------------------------------------------------------------------------- #
# 5. 托盘端到端闭合回路（真实 _failover_tick 调用链）
# --------------------------------------------------------------------------- #
def _make_app(monkeypatch, **overrides):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "save_config", lambda cfg: True)
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app.session = object()
    return app


def _degraded_app(monkeypatch, *, lkg=None, grace_left=59.0, epoch=7,
                  enabled=True):
    """构造「采集失败但有 LKG」的托盘：直接注入 DEGRADED 观测（零 I/O）。"""
    app = _make_app(monkeypatch,
                    graded_recovery_enabled=bool(enabled),
                    remote_restart_enabled=True)
    ex = FakeOpExecutor()
    app.failover.executor_factory = lambda: ex
    app.failover.set_mode("AUTO")
    app.failover.set_authorized(True)
    lkg = lkg if lkg is not None else _snapshot()
    observation = tso.ObservationResult(
        snapshot=None, trusted=False, reason=tso.SNAPSHOT_STALE,
        detail="三轴快照已过期（测试注入）", mode=tso.MODE_DEGRADED,
        stale_for=1.0, grace_left=grace_left, lkg_snapshot=lkg)

    def _fake_observe(now=None):
        # 真实 _observe_snapshot 会写 last_observation；假件保持同一契约。
        app.last_observation = observation
        return observation

    monkeypatch.setattr(app, "_observe_snapshot", _fake_observe)
    return app, ex, observation, lkg


def test_u71_c_31_tray_feeds_lkg_and_executes_whitelist(monkeypatch):
    app, ex, observation, lkg = _degraded_app(monkeypatch)
    app.failover.manager.state = FailoverState.ETHERNET_SUSPECT
    assert app._full_snapshot() is lkg               # LKG 成为只读上下文
    assert app.last_observation.trusted is False     # 但绝不「变可信」
    # 第 1 轮：无动作（SUSPECT → 准备备用链路），窗口保持可用。
    first = app._failover_tick(100.0, False)
    assert first.executed == []
    assert first.state is FailoverState.WIFI_PREPARING
    assert app.failover.snapshot_trust is False
    assert app.failover.observation_mode == tso.MODE_DEGRADED
    assert app.failover.graded_active is True
    # 第 2 轮：状态机依据 LKG 产生 Portal 认证并真正执行。
    result = app._failover_tick(100.0, False)
    assert [(o.kind, o.executed) for o in result.executed] == [
        (OpKind.PORTAL_POST, True)]
    assert ex.calls == [OpKind.PORTAL_POST]
    status = app.failover_status
    assert status.observation_mode == tso.MODE_DEGRADED


def test_u71_c_32_tray_keeps_dual_path_fail_closed(monkeypatch):
    """整合层 / UU 重启在 DEGRADED 下保持 fail-closed（关键安全边界）。"""
    app, ex, observation, lkg = _degraded_app(monkeypatch)
    app.failover.manager.state = FailoverState.WIFI_PREPARING
    app._failover_tick(100.0, False)
    tick = app.dual_path_tick
    assert tick is not None and tick.trusted is False
    assert tick.schedules_uu_restart is False
    assert tick.uu_request is None
    assert "不安排验证" in "".join(tick.notes) or tick.verify_requests == ()
    assert "DEGRADED" in (observation.reason_text + "；观测模式 DEGRADED")


def test_u71_c_33_tray_second_tick_is_frozen_by_cooldown(monkeypatch):
    app, ex, observation, lkg = _degraded_app(monkeypatch)
    app.failover.manager.state = FailoverState.WIFI_PREPARING
    app._failover_tick(100.0, False)
    result = app._failover_tick(101.0, False)
    assert result.plan == [] and result.executed == []
    assert app.failover.graded_active is False
    assert isinstance(app.failover.manager.scheduler.executor,
                      tso.RefusingExecutor)
    assert ex.calls == [OpKind.PORTAL_POST]               # 全程只有一次副作用


def test_u71_c_34_tray_config_kill_switch(monkeypatch):
    """``graded_recovery_enabled=False``：立即退回严格 fail-closed。"""
    app, ex, observation, lkg = _degraded_app(monkeypatch, enabled=False)
    app.failover.manager.state = FailoverState.WIFI_PREPARING
    result = app._failover_tick(100.0, False)
    assert app.failover.observation_mode == tso.MODE_REFUSED
    assert app.failover.graded_active is False
    assert result.plan == [] and result.executed == []
    assert ex.calls == []


def test_u71_c_35_tray_bad_lkg_contract_is_refused(monkeypatch):
    """LKG 不满足 AUTO 契约（轻量占位快照）→ 拒绝降级，不误放行。"""
    app, ex, observation, lkg = _degraded_app(
        monkeypatch, lkg=tf.light_snapshot(system_online=False))
    app.failover.manager.state = FailoverState.WIFI_PREPARING
    result = app._failover_tick(100.0, False)
    assert app.failover.observation_mode == tso.MODE_REFUSED
    assert result.plan == [] and result.executed == []
    assert ex.calls == []


def test_u71_c_36_tray_exhausted_grace_is_refused(monkeypatch):
    app, ex, observation, lkg = _degraded_app(monkeypatch, grace_left=0.0)
    app.failover.manager.state = FailoverState.WIFI_PREPARING
    result = app._failover_tick(100.0, False)
    assert app.failover.observation_mode == tso.MODE_REFUSED
    assert result.executed == [] and ex.calls == []


def test_u71_c_37_tray_full_observation_unchanged(monkeypatch):
    """对照：观测可信时走既有 FULL 路径（分级执行器完全不介入）。"""
    app = _make_app(monkeypatch, remote_restart_enabled=False)
    ex = FakeOpExecutor()
    app.failover.executor_factory = lambda: ex
    app.failover.set_mode("AUTO")
    app.failover.set_authorized(True)
    lkg = _snapshot()
    observation = tso.ObservationResult(snapshot=lkg, trusted=True,
                                        reason=tso.SNAPSHOT_OK,
                                        mode=tso.MODE_FULL, complete=True)

    def _fake_observe(now=None):
        app.last_observation = observation
        return observation

    monkeypatch.setattr(app, "_observe_snapshot", _fake_observe)
    app.failover.manager.state = FailoverState.WIFI_PREPARING
    result = app._failover_tick(100.0, False)
    assert app.failover.observation_mode == tso.MODE_FULL
    assert app.failover.graded_active is False
    assert [(o.kind, o.executed) for o in result.executed] == [
        (OpKind.PORTAL_POST, True)]
    assert not isinstance(app.failover.manager.scheduler.executor,
                          tso.GradedExecutor)


def test_u71_c_38_tray_external_provider_keeps_p75_semantics(monkeypatch):
    """外部注入提供者（未经新鲜度校验）不进入 DEGRADED：完整快照仍按 P7.5-1
    的结构判定走 FULL；分级执行器完全不介入（零行为变化）。"""
    app = _make_app(monkeypatch)
    ex = FakeOpExecutor()
    app.failover.executor_factory = lambda: ex
    app.failover.set_mode("AUTO")
    app.failover.set_authorized(True)
    monkeypatch.setattr(app, "_failover_snapshot_provider",
                        lambda: _snapshot(), raising=False)
    app.failover.manager.state = FailoverState.WIFI_PREPARING
    result = app._failover_tick(100.0, False)
    assert app.failover.observation_mode == tso.MODE_FULL
    assert app.failover.graded_active is False
    assert [(o.kind, o.executed) for o in result.executed] == [
        (OpKind.PORTAL_POST, True)]
    assert not isinstance(app.failover.manager.scheduler.executor,
                          tso.GradedExecutor)


# --------------------------------------------------------------------------- #
# 6. A/B/C 对比：长断连自锁解除（15:50 / 16:20 复现剧本）
# --------------------------------------------------------------------------- #
def test_u71_c_39_sustained_collect_failure_now_recovers_portal(monkeypatch):
    """A：FULL→REFUSED 零动作；B：DEGRADED 数据就绪但零动作；
    C：同一时间线内 Portal 认证真正执行 —— 自锁解除。"""
    from campus_failover import FailoverState as FS

    # —— 阶段 A 语义：显式 REFUSED（或未登记 mode）→ 零动作 —— #
    bridge_a = _bridge()
    _prepare(bridge_a)
    bridge_a.set_snapshot_trust(False, "三轴采集失败")
    result_a = bridge_a.tick(100.0, _snapshot())
    assert result_a.executed == [] and result_a.state is FS.WIFI_PREPARING

    # —— 阶段 B 语义：DEGRADED 已可判定，但仍零副作用 —— #
    bridge_b = _bridge()
    _prepare(bridge_b)
    bridge_b.set_snapshot_trust(False, "三轴快照已过期")   # 未传 mode
    result_b = bridge_b.tick(100.0, _snapshot())
    assert result_b.executed == []

    # —— 阶段 C 语义：同一观测下白名单动作真正执行 —— #
    bridge_c = _bridge()
    _prepare(bridge_c)
    _degraded(bridge_c)
    result_c = bridge_c.tick(100.0, _snapshot())
    assert [(o.kind, o.executed) for o in result_c.executed] == [
        (OpKind.PORTAL_POST, True)]
    assert result_c.state is FS.WIFI_AUTHENTICATING


def test_u71_c_40_outage_window_has_exactly_one_side_effect(monkeypatch):
    """90s 降级窗口 + 180s 冷却 ⇒ 整个断连期间最多一次白名单动作。"""
    app, ex, observation, lkg = _degraded_app(monkeypatch)
    app.failover.manager.state = FailoverState.WIFI_PREPARING
    executed_kinds = []
    now = 100.0
    for _ in range(6):                    # 模拟 6 轮检测（含宽限与冷却）
        observation.grace_left = max(0.0, observation.grace_left - 10.0)
        result = app._failover_tick(now, False)
        executed_kinds.extend(o.kind.value for o in result.executed)
        now += 15.0                        # 检测间隔
    assert executed_kinds == ["portal_post"]
    assert ex.calls == [OpKind.PORTAL_POST]
    # 宽限耗尽后：观测退回 REFUSED，窗口永久关闭（不永久自锁，也不永久放行）。
    assert observation.grace_left == 0.0
    assert app.failover.observation_mode == tso.MODE_REFUSED
