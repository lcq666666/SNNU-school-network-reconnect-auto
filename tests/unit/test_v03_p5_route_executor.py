# -*- coding: utf-8 -*-
"""V0.3 P5 执行器协议与 AUTO 闭合回路离线测试（U-5-20..25 / I-5-01..03）。

覆盖：

- P3 冻结的执行器协议在路由侧（``METRIC_WRITE``）与组合侧的正确落地；
  **``str`` 一律视为失败详情**，避免"失败被记为已执行"（P5 发现并规避的歧义）；
- ``CLASH_TOUCH`` 被显式拒绝（方案 §9：核心链路管理独立于 Clash）；
- AUTO 模式下的**完整闭合剧本**：拔线 → 接管（真实 metric 写入 + 出口验证）
  → 有线恢复观察（≥120s 且 ≥5 次成功且切换冷却已过）→ 切回；
- 单一副作用门看守（每轮至多 1 个动作）；有线正常时零动作；双断不抖动 Metric；
- **执行态必须"动作已执行"才能进入**：切换失败时不得谎报进入 ``WIFI_SWITCHING``。

全部用例**零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实 sleep**。
"""
from __future__ import annotations

from campus_failover import (FailoverMode, FailoverPolicy, FailoverState,
                             NetworkFailoverManager, OpKind, OpRequest)
from campus_network import (EgressInfo, HealthAxis, HealthState, InterfaceHealth,
                            NetworkHealthSnapshot, SystemHealth)
from campus_route_guard import (CompositeExecutor, MetricExecutor,
                                EGRESS_TARGET_V4, EGRESS_TARGET_V6, RouteGuard)
from tests.fakes import FakeRouteOps

ETH_IF, WIFI_IF = 10, 5
STEP = 60.0

_ETH_ROW = {"alias": "以太网", "ipv4_metric": 10, "ipv6_metric": 10,
            "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"}
_WIFI_ROW = {"alias": "WLAN", "ipv4_metric": 50, "ipv6_metric": 50,
             "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"}


def _ops(**kwargs) -> FakeRouteOps:
    metrics = kwargs.pop("metrics", None) or {ETH_IF: dict(_ETH_ROW),
                                             WIFI_IF: dict(_WIFI_ROW)}
    egress = kwargs.pop("egress", None) or {EGRESS_TARGET_V4: ETH_IF,
                                           EGRESS_TARGET_V6: ETH_IF}
    return FakeRouteOps(metrics=metrics, egress=egress, **kwargs)


def _guard(ops=None, **kwargs) -> RouteGuard:
    kwargs.setdefault("authorized", True)
    return RouteGuard(ops=ops if ops is not None else _ops(),
                      ethernet_if=ETH_IF, wifi_if=WIFI_IF, **kwargs)


def _request(kind: OpKind, **payload: object) -> OpRequest:
    return OpRequest(kind=kind, reason="test", payload=dict(payload))


def _snap(eth: HealthState, wifi: HealthState, egress_if: int
          ) -> NetworkHealthSnapshot:
    """直接构造三轴快照（不经 P1 探测管道，聚焦状态机与执行器行为）。"""
    ethernet = InterfaceHealth(HealthAxis.ETHERNET_HEALTH, "以太网", ETH_IF, eth)
    wireless = InterfaceHealth(HealthAxis.WIFI_HEALTH, "WLAN", WIFI_IF, wifi)
    system = SystemHealth(axis=HealthAxis.SYSTEM_HEALTH, online=True,
                          egress=EgressInfo(target=EGRESS_TARGET_V4,
                                            if_index=egress_if))
    return NetworkHealthSnapshot(ethernet=ethernet, wifi=wireless, system=system)


class _StubExecutor:
    """记录调用的假执行器（P4 侧替身）。"""

    def __init__(self, result: object = True) -> None:
        self.result = result
        self.calls: list = []

    def execute(self, request: object) -> object:
        self.calls.append(getattr(request, "kind", None))
        return self.result


# --------------------------------------------------------------------------- #
# U-5-20 MetricExecutor：METRIC_WRITE 的成功/失败/未授权三态
# --------------------------------------------------------------------------- #
def test_u5_20_metric_executor_three_states():
    ok = MetricExecutor(_guard(_ops())).execute(
        _request(OpKind.METRIC_WRITE, target="wifi"))
    assert ok.executed is True and ok.attempted is True

    failed = MetricExecutor(_guard(_ops(apply_egress=False))).execute(
        _request(OpKind.METRIC_WRITE, target="wifi"))
    assert failed.executed is False and failed.attempted is True
    assert "未成功" in failed.detail

    unauthorized = MetricExecutor(
        _guard(_ops(), authorized=False)).execute(
            _request(OpKind.METRIC_WRITE, target="wifi"))
    assert unauthorized.executed is False and unauthorized.attempted is False


# --------------------------------------------------------------------------- #
# U-5-21 CLASH_TOUCH 被显式拒绝
# --------------------------------------------------------------------------- #
def test_u5_21_clash_touch_is_refused():
    outcome = MetricExecutor(_guard(_ops())).execute(_request(OpKind.CLASH_TOUCH))
    assert outcome.executed is False and outcome.attempted is False
    assert "Clash" in outcome.detail


# --------------------------------------------------------------------------- #
# U-5-22 组合执行器分流（四类动作）+ 组合侧归一化
# --------------------------------------------------------------------------- #
def test_u5_22_composite_routes_every_kind():
    snnu = _StubExecutor(result=True)
    route = _StubExecutor(result=True)
    composite = CompositeExecutor(snnu=snnu, route=route)
    assert composite.execute(_request(OpKind.PORTAL_POST)).executed is True
    assert composite.execute(_request(OpKind.WIFI_RECONNECT)).executed is True
    assert composite.execute(_request(OpKind.METRIC_WRITE)).executed is True
    clash = composite.execute(_request(OpKind.CLASH_TOUCH))
    assert clash.executed is False and clash.attempted is False
    assert snnu.calls == [OpKind.PORTAL_POST, OpKind.WIFI_RECONNECT]
    assert route.calls == [OpKind.METRIC_WRITE]


def test_u5_22b_string_result_is_failure_not_success():
    """P4 执行器以 ``str`` 表示失败：组合侧必须翻译为 ``executed=False``。"""
    composite = CompositeExecutor(snnu=_StubExecutor(
        result="SNNU 认证未成功：E_SNNU_AUTH_FAILED"))
    outcome = composite.execute(_request(OpKind.PORTAL_POST))
    assert outcome.executed is False and outcome.attempted is True
    assert "SNNU 认证未成功" in outcome.detail


def test_u5_22c_composite_without_delegates_reports_failure():
    composite = CompositeExecutor()
    assert composite.execute(_request(OpKind.PORTAL_POST)).executed is False
    assert composite.execute(_request(OpKind.METRIC_WRITE)).executed is False


# --------------------------------------------------------------------------- #
# U-5-24 AUTO 无执行器：绝不进入执行态
# --------------------------------------------------------------------------- #
def test_u5_24_auto_without_executor_never_enters_execution_state():
    manager = NetworkFailoverManager(mode=FailoverMode.AUTO, executor=None)
    results = []
    now = 0.0
    for eth in [HealthState.HEALTHY] + [HealthState.LINK_DOWN] * 6:
        results.append(manager.tick(now, _snap(eth, HealthState.HEALTHY, ETH_IF)))
        now += 15.0
    assert all(not r.state.is_execution for r in results)
    assert all(r.executed == [] for r in results)
    assert results[-1].state is FailoverState.WIFI_PREPARING


# --------------------------------------------------------------------------- #
# U-5-25 切换失败：不得谎报进入 WIFI_SWITCHING（P5 修复的状态机缺口）
# --------------------------------------------------------------------------- #
def test_u5_25_failed_switch_never_enters_switching_state():
    ops = _ops(apply_egress=False)          # 写入成功但出口不变 → 回滚 → 未执行
    manager = NetworkFailoverManager(
        mode=FailoverMode.AUTO,
        executor=CompositeExecutor(route=MetricExecutor(_guard(ops))))
    results = []
    now = 0.0
    for eth in [HealthState.HEALTHY] + [HealthState.LINK_DOWN] * 5:
        results.append(manager.tick(now, _snap(eth, HealthState.HEALTHY, ETH_IF)))
        now += 15.0
    assert results[4].state is FailoverState.WIFI_PREPARING   # 进入准备
    assert results[5].state is FailoverState.WIFI_PREPARING   # 失败 → 不进入切换态
    assert all(not r.state.is_execution for r in results)
    assert [o.kind for r in results for o in r.executed] == []
    # 已"尝试"写入 → 登记切换冷却，避免每轮重试（限频）
    assert manager.timers.switch_ready_at == 75.0 + manager.policy.switch_min_interval
    # 回滚把以太网 Metric 复原
    assert ops.metrics[ETH_IF]["ipv4_metric"] == 10


# --------------------------------------------------------------------------- #
# I-5-01 AUTO 完整闭合剧本：拔线 → 接管 → 有线恢复 → 切回
# --------------------------------------------------------------------------- #
def test_i5_01_closed_loop_takeover_and_failback():
    ops = _ops()
    guard = _guard(ops)
    snnu = _StubExecutor(result=True)
    executor = CompositeExecutor(snnu=snnu, route=MetricExecutor(guard))
    manager = NetworkFailoverManager(policy=FailoverPolicy(),
                                     mode=FailoverMode.AUTO, executor=executor)
    script = [
        (HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF),      # 0 双网正常
        (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),    # 1 拔线
        (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),    # 2
        (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),    # 3 疑似异常
        (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),    # 4 准备备用链路
        (HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF),    # 5 执行切换
        (HealthState.LINK_DOWN, HealthState.HEALTHY, WIFI_IF),   # 6 Wi-Fi 接管
        (HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF),     # 7 恢复观察
        (HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF),     # 8
        (HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF),     # 9
        (HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF),     # 10
        (HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF),     # 11 执行切回
        (HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF),      # 12 切回完成
    ]
    results = []
    now = 0.0
    for eth, wifi, egress in script:
        results.append(manager.tick(now, _snap(eth, wifi, egress)))
        now += STEP

    states = [r.state for r in results]
    assert states[0] is FailoverState.ETHERNET_PRIMARY
    assert states[3] is FailoverState.ETHERNET_SUSPECT
    assert states[4] is FailoverState.WIFI_PREPARING
    assert states[5] is FailoverState.WIFI_SWITCHING
    assert states[6] is FailoverState.WIFI_BACKUP_ACTIVE
    assert states[7] is FailoverState.ETHERNET_RECOVERY_CHECK
    assert states[11] is FailoverState.ETHERNET_FAILBACK
    assert states[12] is FailoverState.ETHERNET_PRIMARY

    # 副作用清单：仅两次路由变更，且都通过出口验证（executed=True）
    assert [o.kind for r in results for o in r.executed] == [
        OpKind.METRIC_WRITE, OpKind.METRIC_WRITE]
    # 单一副作用门：每轮至多 1 个动作，且从未触碰 Clash
    assert all(len(r.plan) <= 1 for r in results)
    assert all(o.kind is not OpKind.CLASH_TOUCH
               for r in results for o in r.plan)
    # 真实写入序列：接管抬高以太网、切回恢复基线
    assert ops.writes == [(ETH_IF, "IPv4", 150), (ETH_IF, "IPv6", 150),
                          (ETH_IF, "IPv4", 10), (ETH_IF, "IPv6", 10)]
    assert ops.egress[EGRESS_TARGET_V4] == ETH_IF
    assert ops.egress[EGRESS_TARGET_V6] == ETH_IF
    # 切换冷却只在真正执行变更时登记（t=300 → 600；t=660 → 960）
    assert manager.timers.switch_ready_at == 11 * STEP + 300.0
    # 网络检测计时器恒为 now + interval（任何冷却不得推迟，AGENTS §3.4）
    assert manager.timers.next_detection_at == 12 * STEP \
        + manager.policy.detection_interval
    # 未触发任何 SNNU 认证（本剧本 Wi-Fi 始终健康）
    assert snnu.calls == []


# --------------------------------------------------------------------------- #
# I-5-02 有线正常：零动作、零写入
# --------------------------------------------------------------------------- #
def test_i5_02_ethernet_healthy_zero_side_effects():
    ops = _ops()
    snnu = _StubExecutor(result=True)
    manager = NetworkFailoverManager(
        mode=FailoverMode.AUTO,
        executor=CompositeExecutor(snnu=snnu,
                                   route=MetricExecutor(_guard(ops))))
    now = 0.0
    for _ in range(6):
        result = manager.tick(now, _snap(HealthState.HEALTHY,
                                         HealthState.HEALTHY, ETH_IF))
        now += 15.0
        assert result.state is FailoverState.ETHERNET_PRIMARY
        assert result.plan == [] and result.executed == []
        assert result.would_switch is False
    assert ops.writes == [] and ops.restores == []
    assert snnu.calls == []


# --------------------------------------------------------------------------- #
# I-5-03 双断：不调整 Metric、不来回切换，恢复尝试限频
# --------------------------------------------------------------------------- #
def test_i5_03_both_offline_never_flaps_metrics():
    ops = _ops()
    snnu = _StubExecutor(result=True)
    manager = NetworkFailoverManager(
        mode=FailoverMode.AUTO,
        executor=CompositeExecutor(snnu=snnu,
                                   route=MetricExecutor(_guard(ops))))
    results = []
    now = 0.0
    for _ in range(8):
        results.append(manager.tick(
            now, _snap(HealthState.LINK_DOWN, HealthState.LINK_DOWN, ETH_IF)))
        now += 15.0
    assert results[-1].state is FailoverState.BOTH_OFFLINE
    assert all(r.state is not FailoverState.WIFI_SWITCHING for r in results)
    assert ops.writes == [] and ops.restores == []     # 从不调整 Metric
    kinds = [o.kind for r in results for o in r.executed]
    assert all(kind is OpKind.WIFI_RECONNECT for kind in kinds)
    assert 1 <= len(kinds) <= 2                        # 重连尝试受冷却限频


# --------------------------------------------------------------------------- #
# U-5-26 组合执行器接入真实守卫后，AUTO 仍受单一副作用门约束
# --------------------------------------------------------------------------- #
def test_u5_26_auto_with_real_guard_respects_single_side_effect_gate():
    ops = _ops()
    guard = _guard(ops)
    manager = NetworkFailoverManager(
        mode=FailoverMode.AUTO,
        executor=CompositeExecutor(route=MetricExecutor(guard)))
    now = 0.0
    executed_rounds = 0
    for eth in [HealthState.LINK_DOWN] * 8:
        result = manager.tick(now, _snap(eth, HealthState.HEALTHY, ETH_IF))
        now += 15.0
        assert len(result.plan) <= 1
        executed_rounds += len(result.executed)
    assert executed_rounds == 1        # 只成功切换一次，之后受切换冷却约束
    assert ops.writes == [(ETH_IF, "IPv4", 150), (ETH_IF, "IPv6", 150)]
