# -*- coding: utf-8 -*-
"""V0.3 P6 托盘三模式与手动锁定（离线集成 / 闭合回路）。

以假件串联 ``campus_tray_failover`` 与 ``campus_failover`` 状态机，模拟完整故障
剧本（拔线 → 接管 → 恢复 → 切回）并断言**状态序列**与**副作用清单**：

- I-6-01 默认仅监测：全程零副作用（动作只计划不执行）；
- I-6-02 智能自动（未授权）：零执行，计划仅预测；
- I-6-03 智能自动（授权 + 假执行器）：完整接管 → 切回闭合剧本；
- I-6-04 手动锁定：锁定以太网时全程零切换 + 健康告警；
- I-6-05 Clash 保持原样：剧本内无任何 ``clash_touch``；
- I-6-06 托盘探测循环集成：MONITOR 下 V0.2 自动重连照常，AUTO 下让位；
- I-6-07 单一副作用门：全剧本每轮至多一类动作（执行计划 §0.4）。

约束：零真实网络 / 零真实路由写入 / 零真实注册表写入 / 零真实凭据读取 /
零真实浏览器启动 / 零真实 sleep。
"""
from __future__ import annotations

import logging

import campus_reconnect_tray as tray
import campus_tray_failover as tf
from campus_failover import FailoverState, OpKind
from campus_network import HealthAxis, HealthState, InterfaceHealth
from tests.fakes import FakeOpExecutor

LOG = logging.getLogger("campus")

ETH_IF = 10
WIFI_IF = 5
INTERVAL = 15.0


# --------------------------------------------------------------------------- #
# 假件与驱动
# --------------------------------------------------------------------------- #
class _EgressOps:
    """假执行器：记录动作，并按 ``METRIC_WRITE`` 目标更新"当前出口"。

    模拟 Windows 依 InterfaceMetric 选择出口，使状态机的**出口验证**可闭合；
    零真实路由/Metric 写入、零 PowerShell。
    """

    def __init__(self, eth_if=ETH_IF, wifi_if=WIFI_IF):
        self.eth_if = eth_if
        self.wifi_if = wifi_if
        self.egress = eth_if
        self.calls = []
        self.writes = []

    def execute(self, request):
        self.calls.append(request.kind)
        if request.kind is OpKind.METRIC_WRITE:
            target = request.payload.get("target")
            self.writes.append(target)
            self.egress = self.wifi_if if target == "wifi" else self.eth_if
        return True


def _health(axis, state, if_index, name):
    return InterfaceHealth(axis, name, if_index, state)


def _frame(bridge, ops, t, eth_state=HealthState.HEALTHY,
           wifi_state=HealthState.HEALTHY):
    snapshot = tf.light_snapshot(
        system_online=True,
        ethernet=_health(HealthAxis.ETHERNET_HEALTH, eth_state, ETH_IF, "以太网"),
        wifi=_health(HealthAxis.WIFI_HEALTH, wifi_state, WIFI_IF, "WLAN"),
        egress_if=ops.egress)
    return bridge.tick(t, snapshot)


def _drive(bridge, ops, eth_sequence, wifi_state=HealthState.HEALTHY):
    """按每帧以太网健康序列推进，返回每帧 TickResult。"""
    results = []
    for index, eth_state in enumerate(eth_sequence):
        results.append(_frame(bridge, ops, index * INTERVAL, eth_state,
                              wifi_state))
    return results


def _eth_sequence(down_frames=6, up_frames=21):
    """前 ``down_frames`` 帧以太网故障，其余健康。"""
    return ([HealthState.INTERNET_UNREACHABLE] * down_frames
            + [HealthState.HEALTHY] * up_frames)


def _make_app(monkeypatch, **overrides):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "save_config", lambda cfg: True)
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app.session = object()
    app.failover.executor_factory = lambda: FakeOpExecutor()
    return app


# --------------------------------------------------------------------------- #
# I-6-01 默认仅监测：全程零副作用
# --------------------------------------------------------------------------- #
def test_i6_01_monitor_closed_loop_is_fully_readonly():
    ops = _EgressOps()
    bridge = tf.TrayFailoverBridge(mode="MONITOR")   # 未注入执行器
    results = _drive(bridge, ops, _eth_sequence())
    assert all(r.readonly for r in results)
    assert all(r.executed == [] for r in results)
    # 计划中确实出现切换意图，但从未执行
    assert any(any(p.kind is OpKind.METRIC_WRITE for p in r.plan)
               for r in results)
    assert ops.calls == []
    # 未执行 → 不登记切换冷却；出口始终未变
    assert bridge.manager.timers.switch_ready_at == 0.0
    assert ops.egress == ETH_IF


# --------------------------------------------------------------------------- #
# I-6-02 智能自动（未授权）：零执行
# --------------------------------------------------------------------------- #
def test_i6_02_auto_unauthorized_never_executes():
    executor = FakeOpExecutor()
    bridge = tf.TrayFailoverBridge(mode="AUTO",
                                   executor_factory=lambda: executor)
    results = _drive(bridge, _EgressOps(), _eth_sequence())
    assert all(r.executed == [] for r in results)
    assert executor.requests == []
    assert bridge.executor_attached is False
    assert bridge.manager.timers.switch_ready_at == 0.0


# --------------------------------------------------------------------------- #
# I-6-03 智能自动（授权 + 假执行器）：接管 → 切回闭合剧本
# --------------------------------------------------------------------------- #
def test_i6_03_auto_authorized_takeover_and_failback():
    ops = _EgressOps()
    bridge = tf.TrayFailoverBridge(mode="AUTO", authorized=True,
                                   executor_factory=lambda: ops)
    results = _drive(bridge, ops, _eth_sequence())

    states = [r.state for r in results]
    # 关键状态可达：疑似 → 准备 → 接管 → 恢复观察 → 切回 → 主用
    assert FailoverState.ETHERNET_SUSPECT in states
    assert FailoverState.WIFI_PREPARING in states
    assert FailoverState.WIFI_BACKUP_ACTIVE in states
    assert FailoverState.ETHERNET_RECOVERY_CHECK in states
    assert FailoverState.ETHERNET_FAILBACK in states
    assert states[-1] is FailoverState.ETHERNET_PRIMARY

    # 副作用清单：仅两次 metric_write（接管一次、切回一次），顺序正确
    assert ops.calls == [OpKind.METRIC_WRITE, OpKind.METRIC_WRITE]
    assert ops.writes == ["wifi", "ethernet"]
    # 出口最终回到以太网
    assert ops.egress == ETH_IF
    # 最近一次切换已记录
    status = bridge.status()
    assert status.last_switch_at is not None
    assert status.primary == "ethernet"


# --------------------------------------------------------------------------- #
# I-6-04 手动锁定：全程零切换 + 健康告警
# --------------------------------------------------------------------------- #
def test_i6_04_locked_closed_loop_never_switches():
    ops = _EgressOps()
    bridge = tf.TrayFailoverBridge(mode="LOCKED", lock_target="ethernet")
    results = _drive(bridge, ops, _eth_sequence())

    assert all(r.executed == [] for r in results)
    assert all(r.plan == [] for r in results)          # 连计划都没有 → 不禁用另一网卡
    assert ops.calls == []
    assert all(r.preferred == "ethernet" for r in results)
    assert bridge.manager.state is FailoverState.ETHERNET_PRIMARY
    assert bridge.manager.timers.switch_ready_at == 0.0
    status = bridge.status()
    assert status.locked is True
    # 锁定路径故障期间必须给出健康告警（逐帧 notes）
    assert any(any("健康告警" in n for n in r.notes) for r in results)


# --------------------------------------------------------------------------- #
# I-6-05 Clash 保持原样：剧本内无 clash_touch
# --------------------------------------------------------------------------- #
def test_i6_05_clash_untouched_during_switch():
    ops = _EgressOps()
    bridge = tf.TrayFailoverBridge(mode="AUTO", authorized=True,
                                   executor_factory=lambda: ops)
    _drive(bridge, ops, _eth_sequence())
    assert OpKind.CLASH_TOUCH not in ops.calls
    assert bridge.clash_keep is True
    assert bridge.status().clash_keep is True


# --------------------------------------------------------------------------- #
# I-6-06 托盘探测循环集成：MONITOR 照常 / AUTO 让位
# --------------------------------------------------------------------------- #
def test_i6_06_tray_probe_never_runs_legacy_auto_repair(monkeypatch,
                                                        virtual_clock):
    """I-6-06（V0.3 P7.5-1 修订）：**任何模式**下后台都不跑 V0.2 自动重连。

    P7.5-1 前：MONITOR 下 ``_probe_tick`` 达阈值即 ``_run_repair()``
    （Portal 认证 + Wi-Fi 重连），与 V0.3 调度器形成第二个副作用源（F75-02）。
    P7.5-1 后：MONITOR / LOCKED / AUTO 三种模式的后台自动路径均为**零**，
    只有显式手动命令（``reconnect``）才会走到 ``_run_repair``。
    """
    app = _make_app(monkeypatch, interval=int(INTERVAL), threshold=3,
                    cooldown=180)
    virtual_clock.install(monkeypatch, tray)
    monkeypatch.setattr(tray, "internet_ok", lambda session: (False, ""))
    app._failover_snapshot_provider = lambda: tf.light_snapshot(
        system_online=True,
        ethernet=_health(HealthAxis.ETHERNET_HEALTH,
                         HealthState.INTERNET_UNREACHABLE, ETH_IF, "以太网"),
        wifi=_health(HealthAxis.WIFI_HEALTH, HealthState.HEALTHY,
                     WIFI_IF, "WLAN"),
        egress_if=WIFI_IF)
    repairs = []
    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: repairs.append(virtual_clock.monotonic()))

    # MONITOR：连续 3 次失败 → 后台**零**认证
    nxt = app._probe_tick(0.0)
    for _ in range(2):
        virtual_clock.advance(INTERVAL)
        nxt = app._probe_tick(nxt)
    assert repairs == [], repairs
    assert app.failover_status is not None

    # LOCKED：同样零后台副作用
    app.apply_mode("LOCKED")
    for _ in range(3):
        virtual_clock.advance(INTERVAL)
        nxt = app._probe_tick(nxt)
    assert repairs == [], repairs

    # AUTO：副作用统一由 P3 单一副作用门调度 → 仍不经由 V0.2 自动重连
    app.apply_mode("AUTO")
    for _ in range(3):
        virtual_clock.advance(INTERVAL)
        nxt = app._probe_tick(nxt)
    assert repairs == [], repairs
    assert app._failover_owns_actions() is True

    # 唯一合法入口：用户显式手动重连（携带当前配置版本）
    app._dispatch(("reconnect", True, app.config_version()), nxt)
    assert len(repairs) == 1, repairs


# --------------------------------------------------------------------------- #
# I-6-07 单一副作用门：每轮至多一类动作（全剧本看守）
# --------------------------------------------------------------------------- #
def test_i6_07_single_side_effect_gate_across_script():
    ops = _EgressOps()
    bridge = tf.TrayFailoverBridge(mode="AUTO", authorized=True,
                                   executor_factory=lambda: ops)
    results = _drive(bridge, ops, _eth_sequence())
    for result in results:
        assert len(result.plan) <= 1
        assert len(result.executed) <= 1
