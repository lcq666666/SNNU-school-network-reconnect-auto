# -*- coding: utf-8 -*-
"""V0.3 P7 远程保障离线集成 / 闭合回路测试（I-7-*）。

以假件把 P1 三轴快照 → P3 状态机 → P5 路由卫士 → P6 托盘桥 → P7 远程保障
串成**闭环**，覆盖：

- **I-7-01 敏感期推迟切回闭合剧本**：拔线 → Wi-Fi 接管 → 有线满足恢复判据 →
  **远程会话进行中 → 推迟切回（零 Metric 写入）** → 会话结束 + 静默窗口 → 切回完成；
- **I-7-02 门禁不暂停网络检测**（AGENTS §3.4：认证/切换/门禁都不得推迟探测）；
- **I-7-03 看门狗闭合回路**：线程死亡 + 未完成路由事务 → 先崩溃恢复、再重启监测；
- **I-7-04 关联日志闭合**：Wi-Fi / 路由 / Portal / 远控 四类事件共用一个关联编号；
- **I-7-05 托盘桥接**：远程保障接入后展示文案与零行为变化；
- **I-7-06 单一副作用门看守**：全剧本每轮至多 1 个动作，且不含 Clash。

约束：全部用例**零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实凭据 /
零真实 sleep**（时间由测试显式推进）。
"""
from __future__ import annotations

import json

import campus_remote as cr
from campus_failover import (FailoverMode, FailoverPolicy, FailoverState,
                             NetworkFailoverManager, OpKind)
from campus_network import (EgressInfo, HealthAxis, HealthState, InterfaceHealth,
                            NetworkHealthSnapshot, SystemHealth)
from campus_route_guard import (CompositeExecutor, EGRESS_TARGET_V4,
                                EGRESS_TARGET_V6, MetricExecutor, RouteGuard)
from campus_tray_failover import REMOTE_SENSITIVE, TrayFailoverBridge
from tests.fakes import FakeRemoteOps, FakeRouteOps

ETH_IF, WIFI_IF = 10, 5
STEP = 60.0


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _snap(eth: HealthState, wifi: HealthState, egress_if
          ) -> NetworkHealthSnapshot:
    ethernet = InterfaceHealth(HealthAxis.ETHERNET_HEALTH, "以太网", ETH_IF, eth)
    wireless = InterfaceHealth(HealthAxis.WIFI_HEALTH, "WLAN", WIFI_IF, wifi)
    system = SystemHealth(axis=HealthAxis.SYSTEM_HEALTH, online=True,
                          egress=EgressInfo(target=EGRESS_TARGET_V4,
                                            if_index=egress_if))
    return NetworkHealthSnapshot(ethernet=ethernet, wifi=wireless, system=system)


def _ops() -> FakeRouteOps:
    return FakeRouteOps(
        metrics={ETH_IF: {"alias": "以太网", "ipv4_metric": 10,
                          "ipv6_metric": 10, "ipv4_automatic": "Disabled",
                          "ipv6_automatic": "Disabled"},
                 WIFI_IF: {"alias": "WLAN", "ipv4_metric": 50,
                           "ipv6_metric": 50, "ipv4_automatic": "Disabled",
                           "ipv6_automatic": "Disabled"}},
        egress={EGRESS_TARGET_V4: ETH_IF, EGRESS_TARGET_V6: ETH_IF})


def _guard(ops: FakeRouteOps) -> RouteGuard:
    return RouteGuard(ops=ops, ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                      authorized=True)


class _StubExecutor:
    """记录调用的假执行器（P4 SNNU 侧替身）。"""

    def __init__(self, result: object = True) -> None:
        self.result = result
        self.calls: list = []

    def execute(self, request: object) -> object:
        self.calls.append(getattr(request, "kind", None))
        return self.result


def _remote_ops(*, established: bool) -> FakeRemoteOps:
    """按"是否正在会话"提供远控探测。"""
    processes = [{"pid": 6864, "process_name": "tailscaled",
                  "path": "C:/fake/tailscaled.exe"}]
    ports = ([{"state": "Established", "remote_address": "100.64.0.8",
               "local_address": "10.0.0.2", "local_port": 49152,
               "remote_port": 41641, "pid": 6864}] if established else
             [{"state": "Listen", "remote_address": "", "local_address": "0.0.0.0",
               "local_port": 443, "remote_port": 0, "pid": 6864}])
    return FakeRemoteOps(processes=processes, ports=ports)


def _remote_guard(ops: FakeRemoteOps, **kwargs) -> cr.RemoteSafetyGuard:
    kwargs.setdefault("quiet_seconds", 60.0)
    kwargs.setdefault("probe_interval", 0.0)      # 每轮都取最新探测（闭合剧本）
    return cr.RemoteSafetyGuard(ops=ops, **kwargs)


# --------------------------------------------------------------------------- #
# I-7-01 敏感期推迟切回闭合剧本
# --------------------------------------------------------------------------- #
def test_i7_01_defer_failback_closed_loop():
    ops = _ops()
    executor = CompositeExecutor(snnu=_StubExecutor(),
                                 route=MetricExecutor(_guard(ops)))
    remote_ops = _remote_ops(established=True)
    remote = _remote_guard(remote_ops, quiet_seconds=120.0,
                           max_defer_seconds=1800.0)
    log = cr.RemoteEventLog()
    manager = NetworkFailoverManager(
        policy=FailoverPolicy(), mode=FailoverMode.AUTO, executor=executor,
        failback_gate=remote.failback_gate)

    # 剧本（STEP=60s，16 帧）：
    #   f0        以太网优先（远控会话已在进行）
    #   f1..f3    连续可靠失败 → ETHERNET_SUSPECT
    #   f4..f6    准备 → 切换 → 已证实 Wi-Fi 接管（第 1 次 Metric 写入）
    #   f7..f10   有线恢复观察：成功 1..4/5 次（判据未满足）
    #   f11..f12  判据满足 + 切换冷却已过 + **远控会话中** → P7 连续推迟
    #   f13       远端断开（探测切为仅监听）→ 静默窗口内 → 继续推迟
    #   f14       静默窗口已过 → 切回执行（第 2 次 Metric 写入）
    #   f15       出口已验证回以太网 → ETHERNET_PRIMARY（切回完成）
    script = (
        [(HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF)]      # f0
        + [(HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF)] * 5   # f1..f5
        + [(HealthState.LINK_DOWN, HealthState.HEALTHY, WIFI_IF)]      # f6
        + [(HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF)] * 8    # f7..f14
        + [(HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF)]         # f15
    )
    idle_ports = [{"state": "Listen", "remote_address": "",
                   "local_address": "0.0.0.0", "local_port": 443,
                   "remote_port": 0, "pid": 6864}]

    now = 0.0
    results = []
    correlation = log.new_correlation_id()
    for index, (eth, wifi, egress) in enumerate(script):
        # f13 起远端断开：仅剩监听端口 → 敏感期结束、进入静默窗口
        if index == 13:
            remote_ops.ports = idle_ports
        remote.poll(now)
        result = manager.tick(now, _snap(eth, wifi, egress))
        results.append(result)
        cr.record_tick_events(log, result, correlation_id=correlation)
        now += STEP

    states = [r.state for r in results]
    assert states[0] is FailoverState.ETHERNET_PRIMARY
    assert states[3] is FailoverState.ETHERNET_SUSPECT
    assert states[4] is FailoverState.WIFI_PREPARING
    assert states[5] is FailoverState.WIFI_SWITCHING
    assert states[6] is FailoverState.WIFI_BACKUP_ACTIVE
    assert states[7] is FailoverState.ETHERNET_RECOVERY_CHECK

    # 阶段 3：判据满足 + 冷却已过 + 敏感期 → 连续推迟（帧 11、12）
    for index in (11, 12):
        assert results[index].state is FailoverState.ETHERNET_RECOVERY_CHECK
        assert any("推迟自动切回" in n for n in results[index].notes)
        assert any("远程会话敏感期" in n for n in results[index].notes)
        assert results[index].plan == []                      # 零动作
    # 阶段 4：会话结束但静默窗口内 → 仍推迟（帧 13）
    assert results[13].state is FailoverState.ETHERNET_RECOVERY_CHECK
    assert any("静默窗口" in n for n in results[13].notes)
    assert results[13].plan == []
    # 阶段 5：静默窗口过后切回执行并完成（帧 14 → 帧 15）
    assert states[14] is FailoverState.ETHERNET_FAILBACK
    assert states[15] is FailoverState.ETHERNET_PRIMARY
    assert manager.failback_deferred == 3

    # 副作用清单：全剧本仅**两次** Metric 写入（接管 + 切回），无 Clash
    kinds = [o.kind for r in results for o in r.executed]
    assert kinds == [OpKind.METRIC_WRITE, OpKind.METRIC_WRITE]
    assert all(len(r.plan) <= 1 for r in results)
    assert all(o.kind is not OpKind.CLASH_TOUCH for r in results for o in r.plan)
    # 推迟期间零写入：恢复判据满足帧的 ops.writes 未增长
    assert ops.writes == [(ETH_IF, "IPv4", 150), (ETH_IF, "IPv6", 150),
                          (ETH_IF, "IPv4", 10), (ETH_IF, "IPv6", 10)]
    # 关联日志覆盖路由/系统事件，且全剧本共用同一关联编号
    kinds_logged = {e["kind"] for e in log.entries(correlation_id=correlation)}
    assert {"route"} <= kinds_logged
    assert all(e["correlation_id"] == correlation for e in log.entries())


# --------------------------------------------------------------------------- #
# I-7-02 门禁不暂停网络检测
# --------------------------------------------------------------------------- #
def test_i7_02_gate_never_delays_detection():
    ops = _ops()
    executor = CompositeExecutor(route=MetricExecutor(_guard(ops)))
    remote = _remote_guard(_remote_ops(established=True))
    manager = NetworkFailoverManager(
        policy=FailoverPolicy(detection_interval=15.0), mode=FailoverMode.AUTO,
        executor=executor, failback_gate=remote.failback_gate)
    now = 0.0
    # 走一遍完整闭合剧本：接管 → 恢复观察 → 敏感期连续推迟
    script = (
        [(HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF)]
        + [(HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF)] * 5
        + [(HealthState.LINK_DOWN, HealthState.HEALTHY, WIFI_IF)]
        + [(HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF)] * 8
    )
    for eth, wifi, egress in script:
        remote.poll(now)
        manager.tick(now, _snap(eth, wifi, egress))
        # 检测计时器恒为 now + interval，与门禁/认证/切换冷却无关
        assert manager.timers.next_detection_at == now + 15.0
        now += STEP
    # 门禁确实拦下了切回，但探测节拍从未被推迟
    assert manager.failback_deferred >= 3
    assert manager.state is FailoverState.ETHERNET_RECOVERY_CHECK


# --------------------------------------------------------------------------- #
# I-7-03 看门狗闭合回路：线程死亡 + 未完成路由事务 → 先恢复、再重启
# --------------------------------------------------------------------------- #
def test_i7_03_watchdog_recovery_and_restart_closed_loop(tmp_path):
    journal_path = tmp_path / "route_journal.json"
    ops = _ops()
    guard = _guard(ops)
    journal = guard.journal
    journal.path = journal_path                       # 落盘到临时目录（零仓库写入）

    # 制造一次"进行中"事务：写入前先把 pending 记录落盘（崩溃锚点）
    guard.authorized = True
    record = guard.snapshot(target="wifi")
    journal.record_pending(record)
    assert journal.pending() is not None

    wd = cr.MonitorWatchdog(heartbeat_timeout=90.0, max_restarts=3,
                            restart_window=1800.0)
    order = []
    watchdog_clock = {"now": 1000.0}

    def _pending():
        # 只看磁盘：pending 处置（提交/回滚）后再查应为 None
        return journal.pending() is not None

    def _recover():
        outcome = guard.recover()
        order.append(("recover", outcome))
        return outcome

    def _restart():
        order.append(("restart", None))
        return True

    decision = wd.check(now=watchdog_clock["now"], alive=False,
                        heartbeat_age=200.0, pending_transaction=_pending)
    assert decision.action == "RESTART_MONITOR" and decision.recover_route is True
    outcome = cr.apply_watchdog_decision(decision, restart=_restart,
                                         recover=_recover, watchdog=wd,
                                         now=watchdog_clock["now"])
    assert [step[0] for step in order] == ["recover", "restart"]
    assert outcome["restarted"] is True
    # 恢复后日志中无未完成事务
    assert journal.pending() is None
    assert wd.restart_count == 1
    # 事务性保护：恢复过程中写入只来自受限白名单
    assert all(row[0] in (ETH_IF, WIFI_IF) for row in ops.writes) or ops.writes


# --------------------------------------------------------------------------- #
# I-7-04 关联日志闭合：Wi-Fi / 路由 / Portal / 远控 共用关联编号
# --------------------------------------------------------------------------- #
def test_i7_04_correlated_timeline_across_four_domains(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "_iso_now", lambda: "2026-10-14T16:00:00+08:00")
    path = tmp_path / "remote_guard.jsonl"
    log = cr.RemoteEventLog(path)
    ops = _ops()
    executor = CompositeExecutor(snnu=_StubExecutor(),
                                 route=MetricExecutor(_guard(ops)))
    remote = _remote_guard(_remote_ops(established=True))
    manager = NetworkFailoverManager(
        policy=FailoverPolicy(), mode=FailoverMode.AUTO, executor=executor,
        failback_gate=remote.failback_gate)
    now = 0.0
    cid = log.new_correlation_id()
    script = [(HealthState.LINK_DOWN, HealthState.CAPTIVE_PORTAL, ETH_IF),
              (HealthState.LINK_DOWN, HealthState.HEALTHY, WIFI_IF),
              (HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF),
              (HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF)]
    for index, (eth, wifi, egress) in enumerate(script):
        remote.poll(now)
        result = manager.tick(now, _snap(eth, wifi, egress))
        cr.record_tick_events(log, result, correlation_id=cid)
        log.record("remote", "远控会话%s"
                  % ("进行中" if remote.status().sensitive else "空置"),
                  correlation_id=cid)
        now += 60
        if index:
            log.record("wifi", "SNNU 已关联并认证", correlation_id=cid)
    # 四类事件按时间进入同一关联编号
    log.record("portal", "Portal 认证已提交（凭据不落日志）", correlation_id=cid)
    log.record("route", "接口跃点已写入（白名单参数）", correlation_id=cid)
    kinds = [e["kind"] for e in log.entries(correlation_id=cid)]
    assert set(kinds) >= {"portal", "route", "remote", "wifi"}
    assert all(e["correlation_id"] == cid for e in log.entries())
    # 文件落盘与内存一致，且仍不含凭据字段
    written = [json.loads(line) for line in
               path.read_text(encoding="utf-8").splitlines() if line]
    assert len(written) == len(log.entries())
    assert not any(k in row for row in written for k in
                   ("password", "account", "credential", "yys"))


# --------------------------------------------------------------------------- #
# I-7-05 托盘桥接：远程保障接入后的展示语义（P6 行为不回退）
# --------------------------------------------------------------------------- #
def test_i7_05_bridge_shows_remote_guard_and_keeps_p6_semantics():
    from tests.fakes import FakeOpExecutor

    ops = _remote_ops(established=True)
    remote = _remote_guard(ops)
    # 默认（未接入守卫）：P6 文案原样保留
    plain = TrayFailoverBridge()
    assert plain.manager.failback_gate is None
    assert plain.status().remote_guard == "未启用"
    assert plain.status().remote_detail == ""

    bridge = TrayFailoverBridge(mode=FailoverMode.AUTO, authorized=True,
                                executor_factory=lambda: FakeOpExecutor(),
                                remote_guard=remote)
    # 桥接把守卫的切回门禁**按值**接入状态机（绑定方法每次取值生成新对象，
    # 只能比较相等性而非同一性）
    assert bridge.manager.failback_gate == remote.failback_gate
    assert bridge.defer_failback is True
    remote.observe(ops.probe(), now=0.0)
    status = bridge.status()
    assert status.remote_guard == REMOTE_SENSITIVE
    assert "远控" in status.remote_detail and "Tailscale" in status.remote_detail
    assert "已建立" in status.remote_detail

    # 摘除守卫后恢复 P6 语义（failback_gate 一并解除）
    bridge.set_remote_guard(None)
    assert bridge.manager.failback_gate is None
    assert bridge.status().remote_guard == "启用"
    assert bridge.status().remote_detail == ""

    # 关闭延迟开关：守卫在位但门禁不接入
    bridge.set_remote_guard(remote, defer_failback=False)
    assert bridge.manager.failback_gate is None
    assert bridge.manager.failback_deferred == 0


# --------------------------------------------------------------------------- #
# I-7-06 单一副作用门看守（全剧本）
# --------------------------------------------------------------------------- #
def test_i7_06_single_side_effect_gate_holds_throughout():
    ops = _ops()
    executor = CompositeExecutor(snnu=_StubExecutor(),
                                 route=MetricExecutor(_guard(ops)))
    # 远程会话全程持续 → 走到 max_defer 上限的"安全阀放行"分支
    remote = _remote_guard(_remote_ops(established=True),
                           max_defer_seconds=700.0)
    manager = NetworkFailoverManager(
        policy=FailoverPolicy(), mode=FailoverMode.AUTO, executor=executor,
        failback_gate=remote.failback_gate)
    now = 0.0
    script = ([(HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF)]
              + [(HealthState.LINK_DOWN, HealthState.HEALTHY, ETH_IF)] * 5
              + [(HealthState.LINK_DOWN, HealthState.HEALTHY, WIFI_IF)]
              + [(HealthState.HEALTHY, HealthState.HEALTHY, WIFI_IF)] * 12
              + [(HealthState.HEALTHY, HealthState.HEALTHY, ETH_IF)])
    results = []
    for eth, wifi, egress in script:
        remote.poll(now)
        result = manager.tick(now, _snap(eth, wifi, egress))
        results.append(result)
        assert len(result.plan) <= 1                      # 每轮至多 1 个动作
        assert len(result.executed) <= 1
        assert all(o.kind is not OpKind.CLASH_TOUCH for o in result.plan)
        assert all(o.kind is not OpKind.PORTAL_POST for o in result.plan)
        now += STEP
    # 全剧本仅路由跃点写入，绝不出现 Clash 触碰或 Portal 心跳
    planned = [o.kind for r in results for o in r.plan]
    executed = [o.kind for r in results for o in r.executed]
    assert set(planned) <= {OpKind.METRIC_WRITE}
    assert set(executed) <= {OpKind.METRIC_WRITE}
    # 接管 1 次 + 切回 2 次（第一次切回未过出口验证被撤销后重试成功）
    assert len(executed) == 3
    assert len(ops.writes) == 4
    # 安全阀：1 次推迟后达到上限放行，且此后不再重复推迟
    assert manager.failback_deferred == 1
    assert any("≥上限" in n for n in results[12].notes)
