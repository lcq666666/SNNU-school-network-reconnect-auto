# -*- coding: utf-8 -*-
"""V0.3 P7.5-0 集成修复缺陷复现用例（``regression`` 标记）。

本文件记录《CampusReconnect V0.3 — P7.5 集成修复与 P8 真实环境验收实施计划》
第 3 节列出的 8 项核心缺陷（F75-01 .. F75-08）在**基线 cebc622** 上的复现。

约定（AGENTS §2.3 / 计划 §4 P7.5-0）：

- 复现用例**默认排除**（``pytest.ini`` 的 ``-m "not regression"``），单独运行：
  ``.venv/Scripts/python.exe -m pytest -m regression``；
- 修复前**必须失败**（每条断言都指向"期望的正确行为"，当前实现为错误行为）；
- 对应阶段修复完成后，用例必须**转绿并移出** ``regression`` 标记，进入默认回归；
- 零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实凭据 / 零真实 sleep。

复现用例编号：``R75-<缺陷号>``；源码证据见
``docs/P7.5-0缺陷复现报告_2026-10-15.md``。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import campus_network as cn
import campus_reconnect as engine
import campus_reconnect_tray as tray
import campus_remote as cr
import campus_route_guard as rg
import campus_snnu as snnu_mod
from campus_core import TimerTable
from campus_failover import OpKind, OpRequest
from campus_network import (HealthAxis, HealthState, InterfaceHealthChecker)
from tests.fakes import FakeProbeEngine, FakeRouteOps

pytestmark = pytest.mark.regression

LOG = logging.getLogger("campus")

ETH_IF = 10
WIFI_IF = 5


# --------------------------------------------------------------------------- #
# 通用假件
# --------------------------------------------------------------------------- #
class _FakeObserver:
    """假网络观察器：``observe()`` 返回**当前**报告（可模拟 DHCP 变更后新 IP）。"""

    def __init__(self, report: SimpleNamespace, egress_if: int = WIFI_IF) -> None:
        self.report = report
        self.egress_if = egress_if
        self.observe_calls: list = []

    def observe(self, target_ssid: str = "SNNU"):
        self.observe_calls.append(target_ssid)
        return self.report

    def route_lookup(self, target: str):
        return SimpleNamespace(target=target, if_index=self.egress_if,
                               source_ip="10.8.0.9", error=None)


def _report(wifi_ip: str, eth_ip: str = "10.20.30.40") -> SimpleNamespace:
    return SimpleNamespace(
        host="HOST",
        adapters=[
            SimpleNamespace(name="以太网", if_index=ETH_IF, status="Up",
                            category="physical-ethernet"),
            SimpleNamespace(name="WLAN", if_index=WIFI_IF, status="Up",
                            category="physical-wifi"),
        ],
        ip_addresses=[
            {"if_index": ETH_IF, "address_family": "IPv4", "ip_address": eth_ip},
            {"if_index": WIFI_IF, "address_family": "IPv4", "ip_address": wifi_ip},
        ],
        default_routes=[
            {"if_index": ETH_IF, "address_family": "IPv4",
             "destination_prefix": "0.0.0.0/0", "next_hop": "10.20.30.1",
             "route_metric": 0, "interface_metric": 10},
            {"if_index": WIFI_IF, "address_family": "IPv4",
             "destination_prefix": "0.0.0.0/0", "next_hop": "10.8.0.1",
             "route_metric": 0, "interface_metric": 50},
        ],
        egress={},
        remote_control={},
    )


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


def _fake_snnu_ops(observer: _FakeObserver):
    """假 SNNU 能力集合：链路已关联、Portal 可达、认证引擎返回成功。"""
    return snnu_mod.SnnuOps(
        wifi_state=lambda: ("SNNU", "connected"),
        wifi_reconnect=lambda ssid: True,
        ip_ready=lambda: True,
        route_lookup=observer.route_lookup,
        resolve_port=lambda session, port: (8601, "fake"),
        portal_reachable=lambda session, port: True,
        read_status=lambda session, port: None,
        repair=lambda session, options: SimpleNamespace(
            ok=True, network_ok=True, auth_ok=True, status=None,
            error_code=None, port=8601, needs_user=False,
            already_authenticated=False, detail="假认证成功"),
    )


# --------------------------------------------------------------------------- #
# F75-01 托盘缺少真实三轴健康快照
# --------------------------------------------------------------------------- #
def test_r75_01_tray_must_feed_complete_three_axis_snapshot(monkeypatch):
    """R75-01：AUTO 只能消费完整三轴快照；轻量快照不得授权路由切换。

    基线证据：``campus_reconnect_tray.py:507`` 把 ``_failover_snapshot_provider``
    固定为 ``None``，``_build_failover_snapshot``（:594）因此永远回退到
    ``light_snapshot``（只有 ``system.online``，以太网/Wi-Fi 两轴恒为 ``None``）。
    """
    app = _make_app(monkeypatch, failover_mode="AUTO",
                    auto_switch_authorized=True)
    assert app._failover_snapshot_provider is not None, \
        "托盘必须接入真实三轴快照提供者（基线为 None，只有轻量探测）"
    snapshot = app._build_failover_snapshot(True)
    assert snapshot.ethernet is not None, "缺少 ETHERNET_HEALTH 轴"
    assert snapshot.wifi is not None, "缺少 WIFI_HEALTH 轴"
    assert snapshot.system is not None, "缺少 SYSTEM_HEALTH 轴"


# --------------------------------------------------------------------------- #
# F75-02 MONITOR / LOCKED 仍可能运行旧认证流程
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["MONITOR", "LOCKED"])
def test_r75_02_monitor_and_locked_must_not_run_legacy_repair(monkeypatch, mode):
    """R75-02：仅监测 / 手动锁定下背景不得自动认证、重连 Wi-Fi 或改 Metric。

    基线证据：``_failover_owns_actions``（:584）**仅在 AUTO** 返回 ``True``；
    ``_probe_tick``（:1235-1246）因此让 V0.2 的 ``_run_repair()`` 在
    MONITOR / LOCKED 下照常触发 Portal 认证与 Wi-Fi 重连。
    """
    app = _make_app(monkeypatch, failover_mode=mode, threshold=1)
    calls: list = []
    monkeypatch.setattr(app, "_run_repair", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(tray, "internet_ok", lambda session: (False, "假离线"))
    app.state["failures"] = 0
    app._probe_tick(0.0)
    assert calls == [], "MONITOR/LOCKED 下不得发起 V0.2 自动重连（含 Portal 认证）"


# --------------------------------------------------------------------------- #
# F75-03 非默认出口 Wi-Fi 无法可靠确认健康
# --------------------------------------------------------------------------- #
def test_r75_03_wifi_not_default_route_must_be_confirmable():
    """R75-03：非默认出口的备用 Wi-Fi 必须能被独立证明可用。

    基线证据：``campus_network.InterfaceHealthChecker.check`` 第 4 步（:520-534）
    用 ``route_lookup``（``Find-NetRoute`` = 系统**最佳**路由）判断出口是否等于
    被测接口；以太网主用时该值恒为以太网 ifIndex，于是 Wi-Fi 轴恒被判
    ``PROBE_UNRELIABLE`` —— 备用链路永远无法被确认为健康。
    """
    wifi = SimpleNamespace(name="WLAN", if_index=WIFI_IF, status="Up")
    report = _report(wifi_ip="10.8.0.5")
    fake = FakeProbeEngine()
    fake.tcp["default"] = True
    fake.http["default"] = {"status_code": 200, "redirected": False,
                            "body": "Microsoft Connect Test", "error": ""}
    fake.dns["default"] = True
    # 系统最佳路由仍是以太网（Wi-Fi 非默认出口），但 Wi-Fi 自身完全可用
    fake.routes["default"] = SimpleNamespace(if_index=ETH_IF,
                                             source_ip="10.20.30.40", error=None)
    # 期望实现：接口约束连接的出口证明能力（基线无此概念，此处先埋点）
    fake.proof = {"default": True}

    checker = InterfaceHealthChecker(fake)
    health = checker.check(HealthAxis.WIFI_HEALTH, wifi, report,
                           fake.route_lookup)
    assert health.state is HealthState.HEALTHY, \
        "以太网主用时 Wi-Fi 备用链路被误判为 %s（无法证明出口）" % health.state.value
    assert health.egress_confirmed is True, "缺少可核实的接口约束出口证明"


def test_r75_03_dns_probe_must_not_leak_across_interfaces():
    """R75-03b：DNS 判定不得用**全局**解析结果冒充被测接口的结果。

    基线证据：``WindowsProbeEngine.dns_resolve``（:373）调用全局
    ``socket.getaddrinfo``，与被测接口无关；``InterfaceHealthChecker`` 第 7 步
    （:567-574）直接用它判 ``DNS_FAILURE``，导致"系统默认 DNS 通"被算作
    "本接口 DNS 通"。
    """
    import inspect

    fake = FakeProbeEngine()
    fake.dns["default"] = True
    # 期望：dns_resolve 必须接受源地址/接口约束（基线签名无该参数）
    params = inspect.signature(fake.dns_resolve).parameters
    assert "source_address" in params, \
        "DNS 探测必须可按接口约束（基线为全局 getaddrinfo）"


# --------------------------------------------------------------------------- #
# F75-04 Wi-Fi 重连后使用旧 IP
# --------------------------------------------------------------------------- #
def test_r75_04_portal_auth_must_rebind_after_dhcp_change(monkeypatch):
    """R75-04：每次认证前必须重新读取无线 IPv4，不得复用 DHCP 变更前的源地址。

    基线证据：``campus_reconnect._build_composite_executor``（:889-920）在**构造时**
    一次性算出 ``source_ip = _wifi_source_ip(report, wifi_if)`` 并闭包捕获，
    之后每次 ``PORTAL_POST`` 都用它绑定会话；Wi-Fi 重连拿到新 IP 后仍绑定旧地址。
    """
    bound: list = []

    def _record(session, source_ip):
        bound.append(source_ip)
        return bool(source_ip)

    monkeypatch.setattr(snnu_mod, "bind_source_address", _record)
    monkeypatch.setattr(engine, "_snnu_ops",
                        lambda session, obs, log, iface: _fake_snnu_ops(obs))

    wifi = SimpleNamespace(name="WLAN", interface="WLAN", if_index=WIFI_IF)
    observer = _FakeObserver(_report(wifi_ip="10.8.0.5"))
    args = SimpleNamespace(ssid="SNNU", port=None, yys="mobile",
                           account_format="plain")
    executor = engine._build_composite_executor(
        SimpleNamespace(), observer, observer.report, wifi, args, LOG,
        object())

    # Wi-Fi 重新关联后 DHCP 给出新地址
    observer.report = _report(wifi_ip="10.8.0.9")
    executor.execute(OpRequest(OpKind.PORTAL_POST, reason="P7.5-0 复现"))

    assert bound, "未发生任何源地址绑定"
    assert bound[-1] == "10.8.0.9", \
        "认证仍绑定 DHCP 变更前的旧源地址 %s" % bound[-1]


# --------------------------------------------------------------------------- #
# F75-05 路由事务日志与回滚不够安全
# --------------------------------------------------------------------------- #
def test_r75_05_corrupted_journal_must_error_not_silently_empty():
    """R75-05a：损坏的路由日志必须**明确报错**，不得当作空日志。

    基线证据：``RouteJournal._read``（:414-429）在 ``OSError``/``ValueError`` 时
    一律 ``return self._empty()`` —— 崩溃恢复会把"日志损坏"误判为"无待恢复项"。
    """
    import shutil
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="p75-journal-"))
    try:
        path = root / "route_journal.json"
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        journal = rg.RouteJournal(path)
        journal.pending()                       # 触发读取
        assert getattr(journal, "corrupted", False) is True, \
            "日志损坏未被标记：崩溃恢复会误判为「无待恢复项」"
        assert getattr(journal, "last_error", ""), "损坏日志必须给出可读错误"
    finally:
        shutil.rmtree(str(root), ignore_errors=True)


def test_r75_05_failed_rollback_must_keep_recovery_record():
    """R75-05b：回滚失败必须保留 pending / RECOVERY_REQUIRED，不能只改状态。

    基线证据：``RouteGuard.apply`` 失败分支（:692-702、:708-718）调用
    ``_finish`` → ``journal.record_finished`` 清空 ``pending``；回滚失败时
    恢复信息随之丢失，系统停在未知路由状态却被记为 ``ROLLED_BACK``。
    """
    ops = FakeRouteOps(metrics={}, egress={"8.8.8.8": ETH_IF,
                                           "2606:4700:4700::1111": ETH_IF},
                       set_ok=True, restore_ok=False, apply_egress=False)
    journal = rg.RouteJournal()                 # 内存日志（零真实文件写入）
    guard = rg.RouteGuard(ops=ops, ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                          journal=journal, authorized=True,
                          timers=TimerTable(), now=lambda: 100.0)
    record = guard.apply("wifi")
    assert record.rolled_back is False, "本用例构造的前提：回滚失败"
    assert journal.pending() is not None, \
        "回滚失败后必须保留恢复信息（pending/RECOVERY_REQUIRED）"


def test_r75_05_recover_failure_must_keep_recovery_record():
    """R75-05c：崩溃恢复回滚失败时同样必须保留恢复信息。

    基线证据：``RouteGuard.recover``（:774-784）在 ``rollback_ok=False`` 时仍把
    ``status`` 置为 ``ROLLED_BACK`` 并 ``_finish`` 清空 pending。
    """
    journal = rg.RouteJournal()                 # 内存日志（零真实文件写入）
    journal.record_pending(rg.RouteChangeRecord(
        operation_id="op-repro", target="wifi", status=rg.RouteStatus.PENDING,
        changes=[{"if_index": WIFI_IF, "family": "IPv4", "old": 50, "new": 50,
                  "old_automatic": "Disabled"}]))
    ops = FakeRouteOps(metrics={}, egress={"8.8.8.8": ETH_IF,
                                           "2606:4700:4700::1111": ETH_IF},
                       restore_ok=False)
    guard = rg.RouteGuard(ops=ops, ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                          journal=journal, authorized=False)
    guard.recover()
    assert journal.pending() is not None, \
        "崩溃恢复回滚失败后不得清空 pending（否则无可靠恢复条件）"


# --------------------------------------------------------------------------- #
# F75-06 远程敏感期判定不充分
# --------------------------------------------------------------------------- #
def test_r75_06_unknown_remote_state_must_defer_failback():
    """R75-06a：远控状态 UNKNOWN 时必须**保守**推迟切回。

    基线证据：``RemoteSafetyGuard.observe``（:362-365）在探测失败时令
    ``sensitive = bool(self._last_sensitive_at is not None)``；从未观测到敏感期时
    ``sensitive=False``，``failback_gate``（:430-437）随后返回 ``True``
    （"无远程会话敏感期：允许切回"）—— UNKNOWN 被当成"确认无会话"。
    """
    guard = cr.RemoteSafetyGuard(ops=None, defer_failback=True)
    guard.observe(cr.RemoteProbe(error="PowerShell 失败"), 0.0)
    assert str(guard.status().activity) == "UNKNOWN"
    allowed, reason = guard.failback_gate(1.0)
    assert allowed is False, "远控状态 UNKNOWN 必须保守推迟切回（当前：%s）" % reason


def test_r75_06_udp_only_remote_client_must_be_conservative():
    """R75-06b：Tailscale 等 UDP 型远控不能仅因"无 Established TCP"判为空闲。

    基线证据：``observe``（:356-379）只统计 ``state == "Established"`` 的 TCP 行，
    ``udp_endpoints`` 仅计数不判定，于是"客户端在跑 + 有 UDP 端点 + 仅监听"
    被判 ``IDLE`` 并放行切回。
    """
    guard = cr.RemoteSafetyGuard(defer_failback=True)
    probe = cr.RemoteProbe(
        clients=[{"pid": 7, "process_name": "tailscaled"}],
        ports=[{"state": "Listen", "local_port": 0}],
        udp_endpoints=[{"local_port": 41641}, {"local_port": 3478}])
    guard.observe(probe, 0.0)
    assert guard.status().sensitive is True, \
        "无法确认远控会话状态时必须保守处理（当前判为 %s）" % guard.status().activity


def test_r75_06_active_session_must_not_be_force_cut():
    """R75-06c：活跃远程会话不得因到达 1800s 上限被强制切断。

    基线证据：``failback_gate``（:424-427）在 ``deferred >= max_defer_seconds``
    时一律放行，硬编码默认值 1800s（:313），策略不可配且优先"强制放行"。
    计划要求：默认改为**等待空闲**策略，强制放行可配且需显式告警。
    """
    guard = cr.RemoteSafetyGuard(defer_failback=True, max_defer_seconds=1800.0)
    probe = cr.RemoteProbe(
        clients=[{"pid": 11, "process_name": "ToDesk.exe"}],
        ports=[{"state": "Established", "remote_address": "1.2.3.4:5555"}])
    guard.observe(probe, 0.0)
    allowed, reason = guard.failback_gate(3600.0)
    assert allowed is False, \
        "健康 Wi-Fi 正承载远程会话时不得为恢复有线优先强制切断（当前：%s）" % reason


# --------------------------------------------------------------------------- #
# F75-07 Windows 路由写权限
# --------------------------------------------------------------------------- #
def test_r75_07_route_writer_must_enforce_interface_allowlist():
    """R75-07a：受限写入器必须有**物理网卡允许清单**，越界 ifIndex 一律拒绝。

    基线证据：``WindowsMetricWriter``（:999-1034）只做命令白名单自检，
    没有任何接口允许清单，构造时不绑定角色 ifIndex —— AUTO 授权即等于
    "可向任意接口写 Metric"。
    """
    writer = rg.WindowsMetricWriter(runner=lambda script: (0, "", ""))
    assert writer.set_metric(9999, "IPv4", 10) is False, \
        "越界 ifIndex 必须被允许清单拒绝（基线无任何清单）"


def test_r75_07_permission_failure_must_not_retry_every_cycle():
    """R75-07b：权限不足时必须提示并**停止**，不得每轮重复尝试写入。

    基线证据：``RouteGuard.apply`` 写入失败分支（:691-702）不调用
    ``_note_switch()``，切换冷却未登记；下一轮检测 ``switch_allowed`` 仍为真，
    于是每 15s 重试一次写 Metric（违反 AGENTS §3.11）。
    """
    ops = FakeRouteOps(metrics={}, egress={"8.8.8.8": ETH_IF,
                                           "2606:4700:4700::1111": ETH_IF},
                       set_ok=False)
    attempts: list = []
    real_set_metric = ops.set_metric

    def _counting(if_index, family, metric):
        attempts.append((if_index, family, metric))
        return real_set_metric(if_index, family, metric)

    ops.set_metric = _counting                  # 统计**写入尝试**次数（含失败）
    guard = rg.RouteGuard(ops=ops, ethernet_if=ETH_IF, wifi_if=WIFI_IF,
                          journal=rg.RouteJournal(), authorized=True,
                          timers=TimerTable(), now=lambda: 100.0)
    first = guard.apply("wifi")
    second = guard.apply("wifi")
    assert first.ok is False
    assert len(attempts) == 1, \
        "权限失败后仍重复尝试写入 %d 次（应只尝试 1 次并登记冷却/停止）" % len(attempts)
    assert second.status is rg.RouteStatus.REFUSED, \
        "权限失败后的下一轮应被冷却门拒绝，而非继续尝试写入"


# --------------------------------------------------------------------------- #
# F75-08 看门狗与无人值守判定
# --------------------------------------------------------------------------- #
def test_r75_08_live_thread_with_stale_heartbeat_must_not_be_ok():
    """R75-08a：线程存活但心跳长期不更新（卡死）必须被识别。

    基线证据：``MonitorWatchdog.check``（:643-644）在 ``alive`` 为真时**直接返回
    OK**，心跳年龄判定（:645-650）只在 ``alive`` 为假时可达 —— 活线程卡死
    永远不会被发现。
    """
    watchdog = cr.MonitorWatchdog(heartbeat_timeout=90.0)
    decision = watchdog.check(500.0, alive=True, heartbeat_age=500.0)
    assert decision.action != "OK", \
        "活线程但心跳已停滞 %.0fs 仍被判为 %s" % (500.0, decision.action)


def test_r75_08_sleep_readiness_must_read_configured_timeout():
    """R75-08b：就绪自检必须读**实际电源计划超时**，不能只看"是否支持睡眠"。

    基线证据：``RemoteSafetyGuard._sleep_item``（:477-492）仅依据
    ``standby_available`` / ``hibernate_available``（= 系统**是否支持**）判定；
    "支持但配置为从不睡眠"被误报为无人值守缺口。
    """
    guard = cr.RemoteSafetyGuard()
    probe = cr.RemoteProbe(sleep_states={
        "standby_available": True, "hibernate_available": False,
        "sleep_timeout_ac": 0, "sleep_timeout_dc": 0})
    item = guard._sleep_item(probe)
    assert item.ok is True, \
        "已配置为从不自动睡眠，却仍报缺口：%s" % item.detail


def test_r75_08_readiness_must_cover_process_level_recovery():
    """R75-08c：就绪自检必须覆盖"进程整体崩溃"的独立恢复机制（当前缺失）。

    基线证据：``readiness``（:440-475）只检查睡眠/自动登录/远控/托盘自启/凭据/
    人工项；进程内看门狗无法应对整个进程崩溃，但清单里既无该项也无提示。
    """
    guard = cr.RemoteSafetyGuard()
    probe = cr.RemoteProbe(
        clients=[{"pid": 3, "process_name": "tailscaled"}],
        sleep_states={"standby_available": False, "hibernate_available": False},
        auto_logon={"auto_admin_logon": True, "user": "tester"},
        run_entries=[{"name": "CampusReconnectTray",
                      "command": "pythonw campus_reconnect_tray.py"}])
    report = guard.readiness(probe)
    keys = [item.key for item in report.items]
    assert "external_recovery" in keys, \
        "无人值守清单缺少「进程级独立恢复机制」项（当前仅：%s）" % ", ".join(keys)


# --------------------------------------------------------------------------- #
# 契约守卫：本文件不得引入真实副作用
# --------------------------------------------------------------------------- #
def test_r75_zz_repro_suite_has_no_real_network_symbols():
    """守卫：复现用例不得依赖真实 Portal 目标或真实写入入口。"""
    assert cn.PORTAL_HINTS and snnu_mod.PORTAL_TARGET == "202.117.144.205"
    assert rg.DANGEROUS_OPERATIONS and "disable_adapter" in rg.DANGEROUS_OPERATIONS
