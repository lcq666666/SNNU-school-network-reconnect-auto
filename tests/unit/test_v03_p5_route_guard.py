# -*- coding: utf-8 -*-
"""V0.3 P5 路由卫士离线单元测试（U-5-01..17）。

覆盖主方案 §7.2「路由变更需要事务和回滚」与 AGENTS §3.7/§3.11/§3.12：

- 目标 Metric 计算（最小变更、目标接口严格优先、不臆断未解析接口）；
- 快照只读；未授权零写入；授权后事务式切换（快照 → 最小变更 → 出口验证 → 回滚）；
- 出口验证三态（UNCHANGED / IPV6_STALE / UNKNOWN）**均须回滚**（不得只切 IPv4）；
- 写入失败按快照回滚；AutomaticMetric 的写入与恢复；
- 变更日志持久化与崩溃恢复（已生效则提交、否则按快照回滚）；
- 危险操作黑名单与写入白名单 fail-closed；切换冷却与网络检测计时器互不影响。

全部用例**零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实 sleep**。
"""
from __future__ import annotations

import pytest

from campus_core import TimerTable
from campus_failover import FailoverPolicy
from campus_route_guard import (ALLOWED_OPERATIONS, ALLOWED_SET_PARAMETERS,
                                CONTRACT_VERSION, DANGEROUS_OPERATIONS,
                                EGRESS_TARGET_V4, EGRESS_TARGET_V6,
                                TAKEOVER_METRIC_GAP, EgressVerdict,
                                RouteChangeRecord, RouteGuard, RouteGuardError,
                                RouteGuardRefused, RouteJournal, RouteOps,
                                RouteStatus, WindowsMetricWriter,
                                assert_operation_allowed, assert_write_allowed,
                                desired_metrics)
from tests.fakes import FakeMetricWriter, FakeRouteOps, VirtualClock

ETH_IF, WIFI_IF = 10, 5

_ETH_ROW = {"alias": "以太网", "ipv4_metric": 10, "ipv6_metric": 10,
            "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"}
_WIFI_ROW = {"alias": "WLAN", "ipv4_metric": 50, "ipv6_metric": 50,
             "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"}


def _ops(metrics=None, egress=None, **kwargs) -> FakeRouteOps:
    metrics = metrics if metrics is not None else {ETH_IF: dict(_ETH_ROW),
                                                  WIFI_IF: dict(_WIFI_ROW)}
    egress = egress if egress is not None else {EGRESS_TARGET_V4: ETH_IF,
                                                EGRESS_TARGET_V6: ETH_IF}
    return FakeRouteOps(metrics=metrics, egress=egress, **kwargs)


def _guard(ops=None, *, authorized=True, journal=None, timers=None,
           policy=None) -> RouteGuard:
    return RouteGuard(ops=ops if ops is not None else _ops(),
                      ethernet_if=ETH_IF, wifi_if=WIFI_IF, policy=policy,
                      journal=journal, timers=timers, authorized=authorized)


# --------------------------------------------------------------------------- #
# U-5-01 目标 Metric 计算
# --------------------------------------------------------------------------- #
def test_u5_01_desired_metrics_prefers_target_strictly():
    policy = FailoverPolicy()
    eth = desired_metrics("ethernet", ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                          policy=policy)
    assert eth == {ETH_IF: policy.ethernet_interface_metric,
                   WIFI_IF: policy.wifi_interface_metric}
    wifi = desired_metrics("wifi", ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                           policy=policy)
    assert wifi == {WIFI_IF: policy.wifi_interface_metric,
                    ETH_IF: policy.wifi_interface_metric + TAKEOVER_METRIC_GAP}
    assert wifi[WIFI_IF] < wifi[ETH_IF]        # 目标接口 Metric 严格更小（优先）
    assert desired_metrics("ethernet", ethernet_if=ETH_IF, wifi_if=None,
                           policy=policy) == {ETH_IF: policy.ethernet_interface_metric}
    assert desired_metrics("wifi", ethernet_if=None, wifi_if=WIFI_IF,
                           policy=policy) == {WIFI_IF: policy.wifi_interface_metric}
    with pytest.raises(RouteGuardRefused):
        desired_metrics("vpn", ethernet_if=ETH_IF, wifi_if=WIFI_IF)


# --------------------------------------------------------------------------- #
# U-5-02 快照只读
# --------------------------------------------------------------------------- #
def test_u5_02_snapshot_is_readonly():
    ops = _ops()
    guard = _guard(ops, authorized=False)      # 未授权同样可读快照
    snapshot = guard.snapshot()
    assert snapshot.contract_version == CONTRACT_VERSION
    assert snapshot.primary_egress_if_index == ETH_IF
    assert snapshot.metric_state(ETH_IF).ipv4_metric == 10
    assert snapshot.metric_state(WIFI_IF).ipv6_metric == 50
    assert snapshot.interface_ids == {"ethernet": ETH_IF, "wifi": WIFI_IF}
    assert ops.writes == [] and ops.restores == []


# --------------------------------------------------------------------------- #
# U-5-03 未授权：零写入、零日志
# --------------------------------------------------------------------------- #
def test_u5_03_unauthorized_never_writes_or_journals():
    ops = _ops()
    journal = RouteJournal()
    guard = _guard(ops, authorized=False, journal=journal)
    record = guard.apply("wifi", "未授权测试")
    assert record.status is RouteStatus.REFUSED
    assert record.error == RouteGuardError.NOT_AUTHORIZED.value
    assert record.changes == []
    assert ops.writes == [] and ops.restores == []
    assert journal.pending() is None and journal.history() == []


# --------------------------------------------------------------------------- #
# U-5-04 授权后 Wi-Fi 接管：最小变更 + 出口验证 + 日志收尾
# --------------------------------------------------------------------------- #
def test_u5_04_wifi_takeover_minimal_change_and_verified():
    ops = _ops()
    journal = RouteJournal()
    guard = _guard(ops, journal=journal)
    record = guard.apply("wifi", "拔线接管")
    assert record.ok is True and record.status is RouteStatus.COMPLETED
    target = 50 + TAKEOVER_METRIC_GAP
    # 最小变更：Wi-Fi 已是 50（跳过），只抬高以太网
    assert [(c["if_index"], c["family"], c["old"], c["new"])
            for c in record.changes] == [(ETH_IF, "IPv4", 10, target),
                                        (ETH_IF, "IPv6", 10, target)]
    assert ops.writes == [(ETH_IF, "IPv4", target), (ETH_IF, "IPv6", target)]
    assert ops.restores == []
    assert ops.egress[EGRESS_TARGET_V4] == WIFI_IF
    assert ops.egress[EGRESS_TARGET_V6] == WIFI_IF
    assert record.verification.verdict is EgressVerdict.CONFIRMED
    assert record.rolled_back is False
    assert record.after.primary_egress_if_index == WIFI_IF
    assert journal.pending() is None and len(journal.history()) == 1


# --------------------------------------------------------------------------- #
# U-5-05 切回以太网：恢复基线
# --------------------------------------------------------------------------- #
def test_u5_05_failback_restores_baseline():
    metrics = {ETH_IF: {"alias": "以太网", "ipv4_metric": 150, "ipv6_metric": 150,
                        "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"},
               WIFI_IF: dict(_WIFI_ROW)}
    ops = _ops(metrics=metrics, egress={EGRESS_TARGET_V4: WIFI_IF,
                                        EGRESS_TARGET_V6: WIFI_IF})
    guard = _guard(ops)
    record = guard.apply("ethernet", "有线恢复")
    assert record.ok is True and record.status is RouteStatus.COMPLETED
    assert ops.writes == [(ETH_IF, "IPv4", 10), (ETH_IF, "IPv6", 10)]
    assert ops.egress[EGRESS_TARGET_V4] == ETH_IF
    assert ops.egress[EGRESS_TARGET_V6] == ETH_IF


# --------------------------------------------------------------------------- #
# U-5-06 已是目标状态：不写入但仍须验证
# --------------------------------------------------------------------------- #
def test_u5_06_already_at_target_verified_without_write():
    metrics = {ETH_IF: {"ipv4_metric": 150, "ipv6_metric": 150,
                        "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"},
               WIFI_IF: dict(_WIFI_ROW)}
    ops = _ops(metrics=metrics, egress={EGRESS_TARGET_V4: WIFI_IF,
                                        EGRESS_TARGET_V6: WIFI_IF})
    guard = _guard(ops)
    record = guard.apply("wifi", "重复切换")
    assert record.ok is True and record.status is RouteStatus.COMPLETED
    assert record.changes == [] and ops.writes == []
    assert "无需写入" in record.detail


def test_u5_06b_already_at_target_but_egress_stale_is_refused():
    metrics = {ETH_IF: {"ipv4_metric": 150, "ipv6_metric": 150,
                        "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"},
               WIFI_IF: dict(_WIFI_ROW)}
    ops = _ops(metrics=metrics, egress={EGRESS_TARGET_V4: ETH_IF,
                                        EGRESS_TARGET_V6: ETH_IF})
    guard = _guard(ops)
    record = guard.apply("wifi", "状态一致但出口未变")
    assert record.ok is False and record.status is RouteStatus.REFUSED
    assert record.error == RouteGuardError.EGRESS_UNCHANGED.value
    assert ops.writes == [] and ops.restores == []


# --------------------------------------------------------------------------- #
# U-5-07..09 出口验证失败一律回滚（IPv4 未变 / IPv6 未变 / 无法确认）
# --------------------------------------------------------------------------- #
def test_u5_07_egress_unchanged_triggers_rollback():
    ops = _ops(apply_egress=False)
    guard = _guard(ops)
    record = guard.apply("wifi", "出口未变")
    assert record.ok is False and record.status is RouteStatus.ROLLED_BACK
    assert record.error == RouteGuardError.EGRESS_UNCHANGED.value
    assert record.rolled_back is True
    assert record.verification.verdict is EgressVerdict.UNCHANGED
    assert len(ops.writes) == 4                 # 2 笔写入 + 2 笔回滚
    assert ops.metrics[ETH_IF]["ipv4_metric"] == 10
    assert ops.metrics[ETH_IF]["ipv6_metric"] == 10
    assert ops.egress[EGRESS_TARGET_V4] == ETH_IF


def test_u5_08_ipv6_stale_triggers_rollback():
    ops = _ops(egress={EGRESS_TARGET_V4: WIFI_IF, EGRESS_TARGET_V6: ETH_IF},
               apply_egress=False)
    guard = _guard(ops)
    record = guard.apply("wifi", "只切了 IPv4")
    assert record.error == RouteGuardError.EGRESS_IPV6_STALE.value
    assert record.verification.verdict is EgressVerdict.IPV6_STALE
    assert record.rolled_back is True
    assert ops.metrics[ETH_IF]["ipv4_metric"] == 10


def test_u5_09_egress_unknown_triggers_rollback():
    ops = _ops(egress={EGRESS_TARGET_V4: None, EGRESS_TARGET_V6: None},
               apply_egress=False)
    guard = _guard(ops)
    record = guard.apply("wifi", "出口无法确认")
    assert record.error == RouteGuardError.EGRESS_UNKNOWN.value
    assert record.verification.verdict is EgressVerdict.UNKNOWN
    assert record.rolled_back is True


# --------------------------------------------------------------------------- #
# U-5-10 写入失败按快照回滚（权限不足不反复重试）
# --------------------------------------------------------------------------- #
def test_u5_10_write_failure_rolls_back_applied_changes():
    ops = _ops(set_ok=[True, False])
    guard = _guard(ops)
    record = guard.apply("wifi", "第二次写入失败")
    assert record.ok is False and record.status is RouteStatus.ROLLED_BACK
    assert record.error == RouteGuardError.PERMISSION_DENIED.value
    assert record.rolled_back is True
    assert ops.metrics[ETH_IF]["ipv4_metric"] == 10     # 第一笔已按快照回滚
    assert ops.metrics[ETH_IF]["ipv6_metric"] == 10


# --------------------------------------------------------------------------- #
# U-5-11 各类拒绝：非法目标 / 接口未解析 / 冷却 / 前置条件
# --------------------------------------------------------------------------- #
def test_u5_11_refusals_never_write():
    ops = _ops()
    assert _guard(ops).apply("vpn").error \
        == RouteGuardError.INVALID_TARGET.value

    unresolved = RouteGuard(ops=_ops(), ethernet_if=ETH_IF, wifi_if=None,
                            authorized=True)
    assert unresolved.apply("wifi").error \
        == RouteGuardError.INTERFACE_UNRESOLVED.value

    timers = TimerTable()
    timers.note_switch(0.0, 300.0)
    cooling = RouteGuard(ops=_ops(), ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                         authorized=True, timers=timers, now=lambda: 100.0)
    refused = cooling.apply("wifi")
    assert refused.error == RouteGuardError.SWITCH_COOLDOWN.value
    assert "冷却" in refused.detail

    precheck = _guard(_ops()).apply("wifi", precheck=lambda: (False, "有线仍健康"))
    assert precheck.error == RouteGuardError.PRECHECK_FAILED.value

    assert ops.writes == [] and ops.restores == []


# --------------------------------------------------------------------------- #
# U-5-12 危险操作黑名单 + 写入命令白名单（fail-closed）
# --------------------------------------------------------------------------- #
def test_u5_12_dangerous_blacklist_and_write_whitelist():
    assert set(ALLOWED_OPERATIONS).isdisjoint(DANGEROUS_OPERATIONS)
    for operation in DANGEROUS_OPERATIONS:
        with pytest.raises(RouteGuardRefused):
            assert_operation_allowed(operation)
    for allowed in ALLOWED_OPERATIONS:
        assert_operation_allowed(allowed)
    with pytest.raises(RouteGuardRefused):
        assert_operation_allowed("create_default_route")

    assert_write_allowed(WindowsMetricWriter.SET_METRIC.format(
        if_index=5, family="IPv4", metric=50))
    assert_write_allowed(WindowsMetricWriter.RESTORE_AUTOMATIC.format(
        if_index=5, family="IPv6"))
    for bad in ("Disable-NetAdapter -Name WLAN",
                "Restart-NetAdapter -Name WLAN",
                "Reset-NetAdapter -Name WLAN",
                "Remove-NetRoute -InterfaceIndex 5 -Confirm:$false",
                "Set-NetIPInterface -InterfaceIndex 5 -AddressFamily IPv4 "
                "-InterfaceMetric 50 -NextHop 1.1.1.1",
                "netsh wlan disconnect",
                "ipconfig /flushdns",
                "Restart-Service -Name WlanSvc"):
        with pytest.raises(ValueError):
            assert_write_allowed(bad)
    assert "-NextHop" not in ALLOWED_SET_PARAMETERS
    assert "-InterfaceMetric" in ALLOWED_SET_PARAMETERS


# --------------------------------------------------------------------------- #
# U-5-13 路由卫士只会发出白名单内的写入命令
# --------------------------------------------------------------------------- #
def test_u5_13_guard_only_emits_whitelisted_writes():
    writer = FakeMetricWriter()
    base = _ops(apply_egress=False)
    ops = RouteOps(read_metrics=base.read_metrics,
                   read_default_routes=base.read_default_routes,
                   read_primary_egress=base.read_primary_egress,
                   set_metric=writer.set_metric,
                   restore_automatic=writer.restore_automatic,
                   egress_check=base.egress_check)
    record = _guard(ops).apply("wifi", "白名单看守")
    assert record.ok is False                   # 出口未变 → 回滚
    assert writer.scripts, "应至少发出一次写入"
    assert all(script.startswith("Set-NetIPInterface") for script in writer.scripts)
    assert writer.calls[0] == ("set_metric", ETH_IF, "IPv4")


# --------------------------------------------------------------------------- #
# U-5-14 变更日志持久化 + 崩溃恢复
# --------------------------------------------------------------------------- #
def test_u5_14_journal_persistence(tmp_path):
    path = tmp_path / "route_journal.json"
    ops = _ops(apply_egress=False)
    journal = RouteJournal(path)
    guard = _guard(ops, journal=journal)
    record = guard.apply("wifi", "崩溃前失败回滚")
    assert record.status is RouteStatus.ROLLED_BACK
    assert journal.pending() is None
    assert len(journal.history()) == 1
    assert journal.history()[0]["operation_id"] == record.operation_id


def _pending_record(guard: RouteGuard) -> RouteChangeRecord:
    return RouteChangeRecord(
        operation_id="crash-0001", created_at="2026-10-14T00:00:00+08:00",
        target="wifi", reason="模拟崩溃", status=RouteStatus.PENDING,
        before=guard.snapshot("wifi"),
        changes=[{"if_index": ETH_IF, "family": "IPv4", "old": 10, "new": 150,
                  "old_automatic": "Disabled"},
                 {"if_index": ETH_IF, "family": "IPv6", "old": 10, "new": 150,
                  "old_automatic": "Disabled"}])


def test_u5_14b_recover_rolls_back_unfinished_transaction(tmp_path):
    path = tmp_path / "route_journal.json"
    ops = _ops()
    guard = _guard(ops, journal=RouteJournal(path))
    journal = RouteJournal(path)
    journal.record_pending(_pending_record(guard))
    # 模拟"崩溃前 Metric 已写入但出口未变"
    ops.metrics[ETH_IF]["ipv4_metric"] = 150
    ops.metrics[ETH_IF]["ipv6_metric"] = 150
    recovered = _guard(ops, journal=RouteJournal(path)).recover()
    assert recovered is not None and recovered.rolled_back is True
    assert recovered.status is RouteStatus.ROLLED_BACK
    assert recovered.error == RouteGuardError.RECOVERED.value
    assert ops.metrics[ETH_IF]["ipv4_metric"] == 10
    assert ops.metrics[ETH_IF]["ipv6_metric"] == 10
    assert RouteJournal(path).pending() is None


def test_u5_14c_recover_commits_when_egress_already_target(tmp_path):
    path = tmp_path / "route_journal.json"
    ops = _ops(egress={EGRESS_TARGET_V4: WIFI_IF, EGRESS_TARGET_V6: WIFI_IF})
    guard = _guard(ops, journal=RouteJournal(path))
    RouteJournal(path).record_pending(_pending_record(guard))
    recovered = _guard(ops, journal=RouteJournal(path)).recover()
    assert recovered is not None
    assert recovered.status is RouteStatus.COMPLETED and recovered.ok is True
    assert ops.restores == []
    assert RouteJournal(path).pending() is None


def test_u5_14d_recover_without_pending_returns_none():
    guard = _guard(_ops(), journal=RouteJournal())
    assert guard.recover() is None


# --------------------------------------------------------------------------- #
# U-5-15 切换冷却登记，且不推迟网络检测
# --------------------------------------------------------------------------- #
def test_u5_15_switch_registers_cooldown_without_touching_detection():
    timers = TimerTable()
    clock = VirtualClock(1000.0)
    guard = RouteGuard(ops=_ops(), ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                       authorized=True, timers=timers, now=clock.monotonic)
    record = guard.apply("wifi", "冷却登记")
    assert record.ok is True
    assert timers.switch_ready_at == 1000.0 + guard.policy.switch_min_interval
    assert timers.next_detection_at == 0.0     # 切换冷却不得推迟网络检测
    assert timers.auth_ready_at == 0.0         # 也不影响认证冷却


def test_u5_15b_no_change_success_does_not_register_cooldown():
    metrics = {ETH_IF: {"ipv4_metric": 150, "ipv6_metric": 150,
                        "ipv4_automatic": "Disabled", "ipv6_automatic": "Disabled"},
               WIFI_IF: dict(_WIFI_ROW)}
    ops = _ops(metrics=metrics, egress={EGRESS_TARGET_V4: WIFI_IF,
                                        EGRESS_TARGET_V6: WIFI_IF})
    timers = TimerTable()
    guard = RouteGuard(ops=ops, ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                       authorized=True, timers=timers)
    assert guard.apply("wifi").ok is True
    assert timers.switch_ready_at == 0.0       # 未产生写入 → 不占用切换冷却


# --------------------------------------------------------------------------- #
# U-5-16/17 AutomaticMetric：需要显式写入关闭、回滚时恢复自动
# --------------------------------------------------------------------------- #
def test_u5_16_automatic_metric_forces_explicit_write():
    metrics = {ETH_IF: {"ipv4_metric": 150, "ipv6_metric": 150,
                        "ipv4_automatic": "Enabled", "ipv6_automatic": "Enabled"},
               WIFI_IF: dict(_WIFI_ROW)}
    ops = _ops(metrics=metrics)
    record = _guard(ops).apply("wifi", "自动 Metric 需显式关闭")
    assert record.changes, "AutomaticMetric=Enabled 时即便数值相同也须显式写入"
    assert ops.writes == [(ETH_IF, "IPv4", 150), (ETH_IF, "IPv6", 150)]
    assert record.ok is True


def test_u5_17_rollback_restores_automatic_metric():
    metrics = {ETH_IF: {"ipv4_metric": 10, "ipv6_metric": 10,
                        "ipv4_automatic": "Enabled", "ipv6_automatic": "Enabled"},
               WIFI_IF: dict(_WIFI_ROW)}
    ops = _ops(metrics=metrics, apply_egress=False)
    record = _guard(ops).apply("wifi", "回滚恢复自动 Metric")
    assert record.rolled_back is True
    assert ops.restores, "原 AutomaticMetric=Enabled 的项应恢复为自动"
    assert ops.metrics[ETH_IF]["ipv4_automatic"] == "Enabled"
    assert ops.metrics[ETH_IF]["ipv6_automatic"] == "Enabled"


def test_u5_18_corrupted_journal_is_not_treated_as_empty(tmp_path):
    path = tmp_path / "route_journal.json"
    path.write_text("{ broken", encoding="utf-8")
    journal = RouteJournal(path)
    with pytest.raises(ValueError, match="日志损坏"):
        journal.pending()
    assert journal.corrupted is True

def test_u5_19_failed_recovery_keeps_pending_record(tmp_path):
    path = tmp_path / "route_journal.json"
    ops = _ops(set_ok=False, restore_ok=False)
    guard = _guard(ops, journal=RouteJournal(path))
    RouteJournal(path).record_pending(_pending_record(guard))
    recovered = _guard(ops, journal=RouteJournal(path)).recover()
    assert recovered.status is RouteStatus.RECOVERY_REQUIRED
    assert recovered.error == RouteGuardError.ROLLBACK_FAILED.value
    assert RouteJournal(path).pending() is not None


def test_u5_20_metric_writer_enforces_physical_interface_allowlist():
    calls = []
    writer = WindowsMetricWriter(
        runner=lambda script: calls.append(script) or (0, "", ""),
        allowed_if_indices=[ETH_IF, WIFI_IF])
    assert writer.set_metric(9999, "IPv4", 10) is False
    assert calls == []
    assert writer.set_metric(WIFI_IF, "IPv4", 50) is True
    assert len(calls) == 1
