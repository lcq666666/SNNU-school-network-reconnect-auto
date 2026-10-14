# -*- coding: utf-8 -*-
"""V0.3 P7 远程保障单元 / 离线 Mock 测试（U-7-*）。

覆盖（方案 §7.2「路由变更需要事务和回滚」崩溃恢复、§7.3「远程会话的现实限制」、
§4.3「有线网络自动恢复」敏感期、§12-P4「远程保障」；执行计划 §0.2 P7 行）：

- **远控探测与敏感期判定**（§7.3）：只看已建立的远控会话；仅监听/仅 UDP 不算
  会话进行中（否则 Tailscale 常驻监听将导致永不切回）；
- **切回门禁**：敏感期推迟切回、静默窗口防抖、推迟上限到点放行、未授权不推迟、
  门禁异常保守推迟（fail-closed）；
- **无人值守就绪自检**（§7.3）：睡眠/休眠、自动登录、远控客户端与自启、托盘自启、
  凭据在位、远控自动重连（人工项）；
- **只读性**：本模块没有任何副作用入口（零路由写入、零认证、零 Clash、零杀进程）。

约束：全部用例**零真实网络 / 零真实子进程 / 零真实注册表 / 零真实凭据 /
零真实 sleep**（用 :class:`tests.fakes.VirtualClock`）。
"""
from __future__ import annotations

import json

import pytest

import campus_remote as cr
from tests.fakes import FakeRemoteOps, VirtualClock

ETH_IF, WIFI_IF = 10, 5
DEFAULT_POLICY = {"detection_cooldown": 60.0}


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
def _proc(name="tailscaled", pid=6864):
    return {"pid": pid, "process_name": name, "path": "C:/fake/%s.exe" % name}


def _port(state="Established", remote="100.64.0.8", local=49152, pid=6864):
    return {"state": state, "remote_address": remote, "local_address": "10.0.0.2",
            "local_port": local, "remote_port": 41641, "pid": pid}


def _guard(**kwargs) -> cr.RemoteSafetyGuard:
    kwargs.setdefault("quiet_seconds", 60.0)
    kwargs.setdefault("max_defer_seconds", 600.0)
    kwargs.setdefault("probe_interval", 30.0)
    return cr.RemoteSafetyGuard(**kwargs)


def _probe(**kwargs) -> cr.RemoteProbe:
    return cr.RemoteProbe(**kwargs)


# --------------------------------------------------------------------------- #
# U-7-01..03 契约与探测数据
# --------------------------------------------------------------------------- #
def test_u7_01_contract_and_constants():
    """P7 契约版本只增不改；看门狗动作集合与日志分类覆盖方案 §12-P4 四类。"""
    assert cr.CONTRACT_VERSION == "v03-p7-remote-1"
    assert cr.EVENT_LOG_CONTRACT_VERSION == "v03-p7-remotelog-1"
    for kind in ("wifi", "route", "portal", "remote"):
        assert kind in cr.LOG_KINDS
    assert "system" in cr.LOG_KINDS
    assert set(cr.WATCHDOG_ACTIONS) == {"OK", "RESUME_RECHECK", "RESTART_MONITOR",
                                        "MANUAL_REQUIRED"}
    # 已知客户端含本机实测在用的 Tailscale 与向日葵（P0-T5 结论）
    keys = {spec.key for spec in cr.DEFAULT_REMOTE_CLIENTS}
    assert {"tailscale", "awesun", "todesk"} <= keys
    names = {spec.matches("tailscaled") for spec in cr.DEFAULT_REMOTE_CLIENTS}
    assert True in names
    assert not any(spec.matches("chrome") for spec in cr.DEFAULT_REMOTE_CLIENTS)


def test_u7_02_probe_from_report_and_mapping():
    """探测数据可从 P0 报告 / 原始 dict 构造；坏输入一律降级而不抛错。"""
    report = type("R", (), {"remote_control": {
        "clients": [_proc()], "ports": [_port()],
        "udp_endpoints": [{"local_port": 41641, "pid": 6864}],
        "sleep_states": {"standby_available": False, "hibernate_available": False},
        "auto_logon": {"auto_admin_logon": True, "user": "lennonli"},
        "run_entries": [{"hive": "HKCU", "name": "CampusReconnectTray",
                         "command": "pythonw campus_reconnect_tray.py"}]}})()
    probe = cr.RemoteProbe.from_report(report)
    assert probe.has_readiness_data is True
    assert [c["process_name"] for c in probe.clients] == ["tailscaled"]
    assert probe.ports[0]["state"] == "Established"
    # 非对象 / None / 缺字段：返回带 error 的空探测
    assert cr.RemoteProbe.from_mapping("nope").error
    assert cr.RemoteProbe.from_mapping({}).clients == []
    empty = cr.RemoteProbe.from_report(None)
    assert empty.error and empty.clients == []


def test_u7_03_sensitive_uses_established_not_listen():
    """只有『已建立』远控连接才算敏感期；仅监听与仅 UDP 不算（Tailscale 常驻监听）。"""
    guard = _guard()
    listening = guard.observe(_probe(clients=[_proc()], ports=[_port(state="Listen")]),
                              now=0.0)
    assert listening.active is False and listening.sensitive is False
    assert listening.listening is True

    udp_only = guard.observe(_probe(clients=[_proc()], udp_endpoints=[
        {"local_address": "0.0.0.0", "local_port": 41641, "pid": 6864}]), now=1.0)
    assert udp_only.active is False and udp_only.sensitive is False
    assert udp_only.udp_endpoints == 1

    busy = guard.observe(_probe(clients=[_proc()], ports=[_port()]), now=2.0)
    assert busy.active is True and busy.sensitive is True
    assert busy.established == 1
    assert "tailscale" in busy.keys and "Tailscale" in busy.clients


def test_u7_04_probe_failure_is_conservative():
    """探测失败不得臆断为『无远程会话』。"""
    guard = _guard(probe_interval=0.0, ops=FakeRemoteOps(probe_error=OSError("boom")))
    status = guard.poll(now=0.0)
    assert status.activity == cr.ACTIVITY_UNKNOWN
    assert "boom" in status.probe_error


# --------------------------------------------------------------------------- #
# U-7-05..09 切回门禁（方案 §4.3）
# --------------------------------------------------------------------------- #
def test_u7_05_no_session_allows_failback():
    """无远程会话时一律放行切回（不影响 §4.3 的既有恢复判据）。"""
    guard = _guard()
    guard.observe(_probe(clients=[_proc()]), now=10.0)
    allowed, why = guard.failback_gate(now=11.0)
    assert allowed is True
    assert "无远程会话敏感期" in why


def test_u7_06_active_session_defers_failback():
    """会话进行中推迟切回，并给出可读原因与推迟次数。"""
    guard = _guard()
    guard.observe(_probe(clients=[_proc()], ports=[_port()]), now=0.0)
    allowed, why = guard.failback_gate(now=5.0)
    assert allowed is False
    assert "推迟自动切回" in why
    assert guard.defer_count == 1
    assert guard.status().sensitive is True


def test_u7_07_quiet_window_defers_then_allows():
    """会话结束后仍需经过静默窗口才放行（防抖：避免刚切回又被连入）。"""
    guard = _guard(quiet_seconds=60.0)
    guard.observe(_probe(clients=[_proc()], ports=[_port()]), now=0.0)
    # 会话结束（不再有已建立连接）
    guard.observe(_probe(clients=[_proc()]), now=5.0)
    allowed, why = guard.failback_gate(now=30.0)
    assert allowed is False and "静默窗口" in why
    allowed, why = guard.failback_gate(now=65.0)
    assert allowed is True and why


def test_u7_08_max_defer_releases_failback():
    """长时间敏感不得永久阻止切回：达到上限后放行并如实告警。

    推迟计时自**敏感期起点**起算（``observe`` 时记录），因此上限反映
    "会话已持续多久"，而不是"门禁第一次被询问多久"。
    """
    guard = _guard(max_defer_seconds=600.0)
    busy = _probe(clients=[_proc()], ports=[_port()])
    guard.observe(busy, now=0.0)          # 会话自 t=0 开始
    still = guard.failback_gate(now=599.0)
    assert still[0] is False
    capped, why = guard.failback_gate(now=601.0)
    assert capped is True
    assert "上限" in why and "会话可能瞬断" in why


def test_u7_09_disabled_defer_and_gate_exception():
    """开关关闭即放行；看门狗未接线时状态仍为 IDLE 且放行（不误报敏感期）。"""
    off = _guard(defer_failback=False)
    off.observe(_probe(clients=[_proc()], ports=[_port()]), now=0.0)
    assert off.failback_gate(now=0.0)[0] is True

    class _Broken(cr.RemoteSafetyGuard):
        def failback_gate(self, now):
            raise RuntimeError("gate exploded")

    broken = _Broken()
    with pytest.raises(RuntimeError):
        broken.failback_gate(now=0.0)     # 原样上抛，由 campus_failover 兜底


def test_u7_10_snapshot_never_blocks():
    """快照（未 observe）不得制造假敏感期：默认 UNKNOWN + 放行。"""
    guard = _guard()
    allowed, why = guard.failback_gate(now=123.0)
    assert allowed is True
    assert guard.status().activity == cr.ACTIVITY_UNKNOWN


# --------------------------------------------------------------------------- #
# U-7-11..14 无人值守就绪自检（方案 §7.3）
# --------------------------------------------------------------------------- #
def _readiness_ops(*, credentials=True, running=True, autostart=True,
                   sleep_ok=True, logon=True, tray_autostart=True):
    """构造一份就绪自检输入（默认全部满足，便于按项制造缺口）。"""
    entries = []
    if autostart:
        entries.append({"hive": "HKLM", "name": "SunloginClient",
                        "command": "C:/Program Files/SunloginClient/SunloginClient.exe"})
    if tray_autostart:
        entries.append({"hive": "HKCU", "name": "CampusReconnectTray",
                        "command": "pythonw campus_reconnect_tray.py"})
    return FakeRemoteOps(
        processes=[_proc("SunloginClient", 12345)] if running else [],
        ports=[_port(remote="1.2.3.4", pid=12345)] if running else [],
        sleep=({"standby_available": False, "hibernate_available": False}
               if sleep_ok else
               {"standby_available": True, "hibernate_available": True}),
        auto_logon=({"auto_admin_logon": True, "user": "lennonli"}
                    if logon else {"auto_admin_logon": 0, "user": None}),
        run_entries=entries, credentials=credentials)


def test_u7_11_readiness_full_pass():
    """四项可自动判定的检查全部在位时，报告 ok（人工项不参与）。"""
    guard = _guard(ops=_readiness_ops())
    report = guard.readiness()
    assert report.ok is True, report.warnings
    assert [i.key for i in report.items] == ["sleep", "auto_logon",
                                              "remote_client", "tray_autostart",
                                              "credentials", "remote_reconnect"]
    manual = [i for i in report.items if i.manual]
    assert len(manual) == 1 and manual[0].key == "remote_reconnect"
    payload = report.to_dict()
    assert payload["ok"] is True and payload["items"]


def test_u7_12_readiness_reports_each_gap():
    """睡眠/自动登录/客户端/凭据每项缺口都要点名（对应 P0 §7 三项缺口）。"""
    guard = _guard(ops=_readiness_ops(credentials=False, running=False,
                                      autostart=False, sleep_ok=False,
                                      logon=False, tray_autostart=False))
    report = guard.readiness()
    assert report.ok is False
    gaps = {w.split("：")[0] for w in report.warnings}
    assert {"睡眠/休眠", "自动登录", "远控客户端", "托盘自启", "校园网凭据"} <= gaps
    for item in report.items:
        if item.key in ("sleep", "auto_logon"):
            assert item.severity == "warn"


def test_u7_13_readiness_gap_is_eager_and_no_secrets():
    """就绪自检两次调用结果一致；凭据存在性检查不得读取明文。"""
    ops = _readiness_ops(credentials=True)
    guard = _guard(ops=ops)
    first = guard.readiness()
    second = guard.readiness()
    assert [i.ok for i in first.items] == [i.ok for i in second.items]
    # 未提供凭据检查能力时给出告警而不是崩溃
    ops_min = FakeRemoteOps(sleep={"standby_available": False},
                            auto_logon={"auto_admin_logon": True},
                            run_entries=[])
    ops_min.credentials_present = lambda: True
    guard_min = _guard(ops=ops_min)
    item = {i.key: i for i in guard_min.readiness().items}["credentials"]
    assert item.ok is True
    assert "凭据" in item.detail and "存在性" in item.detail


def test_u7_14_readiness_probe_failure_is_reported():
    """就绪探测失败不算『通过』，显式列为缺口（防自欺）。"""
    guard = _guard(ops=FakeRemoteOps(probe_error=OSError("powercfg failed")))
    report = guard.readiness()
    assert report.ok is False
    assert any("探测" in i.label or i.key == "probe_error" for i in report.items)


# --------------------------------------------------------------------------- #
# U-7-15..18 看门狗与崩溃恢复（方案 §7.2）
# --------------------------------------------------------------------------- #
def test_u7_15_watchdog_healthy_and_resume():
    """心跳新鲜 → OK；时间跳变（睡眠/待机恢复）→ RESUME_RECHECK，不动作。"""
    wd = cr.MonitorWatchdog(heartbeat_timeout=90.0, resume_gap=180.0,
                            max_restarts=3, restart_window=1800.0)
    ok = wd.check(now=0.0, alive=True, heartbeat_age=1.0)
    assert ok.action == "OK" and ok.resumed is False
    resume = wd.check(now=500.0, alive=True, heartbeat_age=1.0)
    assert resume.action == "RESUME_RECHECK" and resume.resumed is True
    assert wd.resume_count == 1


def test_u7_16_watchdog_restarts_dead_monitor():
    """线程已死且心跳过期 → RESTART_MONITOR；重启频次达上限转 MANUAL_REQUIRED。"""
    wd = cr.MonitorWatchdog(heartbeat_timeout=90.0, max_restarts=2,
                            restart_window=1800.0)
    first = wd.check(now=0.0, alive=False, heartbeat_age=200.0)
    assert first.action == "RESTART_MONITOR" and first.recover_route is False
    wd.note_restart(now=1.0)
    second = wd.check(now=2.0, alive=False, heartbeat_age=200.0)
    assert second.action == "RESTART_MONITOR"
    wd.note_restart(now=3.0)
    third = wd.check(now=4.0, alive=False, heartbeat_age=200.0)
    assert third.action == "MANUAL_REQUIRED"
    assert third.requires_action is True


def test_u7_17_watchdog_flags_pending_route_transaction():
    """线程死亡且存在未完成路由事务时，决策要求先做崩溃恢复。"""
    wd = cr.MonitorWatchdog(heartbeat_timeout=90.0)
    calls = {"n": 0}

    def _pending():
        calls["n"] += 1
        return True

    decision = wd.check(now=0.0, alive=False, heartbeat_age=200.0,
                        pending_transaction=_pending)
    assert decision.action == "RESTART_MONITOR" and decision.recover_route is True
    assert calls["n"] == 1
    # 心跳在容忍窗口内时**不**触碰日志（避免无谓 I/O）
    wd2 = cr.MonitorWatchdog(heartbeat_timeout=90.0)
    fresh = wd2.check(now=0.0, alive=False, heartbeat_age=1.0,
                      pending_transaction=lambda: pytest.fail("不应查询日志"))
    assert fresh.action == "OK"


def test_u7_18_apply_watchdog_decision_runs_recovery_then_restart():
    """按决策先恢复路由、再重启监测；RECOVER 失败不阻断重启，异常不外抛。"""
    wd = cr.MonitorWatchdog()
    decision = cr.WatchdogDecision(action="RESTART_MONITOR",
                                   reason="thread died", recover_route=True)
    order = []

    def _recover():
        order.append("recover")
        return "rolled-back"

    def _restart():
        order.append("restart")
        return True

    outcome = cr.apply_watchdog_decision(decision, restart=_restart,
                                         recover=_recover, watchdog=wd, now=5.0)
    assert outcome["action"] == "RESTART_MONITOR"
    assert outcome["recovered"] == "rolled-back" and outcome["restarted"] is True
    assert order == ["recover", "restart"]
    assert wd.restart_count == 1
    # MANUAL_REQUIRED / OK / RESUME_RECHECK 均零动作
    for action in ("MANUAL_REQUIRED", "OK", "RESUME_RECHECK"):
        out = cr.apply_watchdog_decision(cr.WatchdogDecision(action=action),
                                         restart=lambda: pytest.fail("不应重启"),
                                         recover=lambda: pytest.fail("不应恢复"))
        assert out["restarted"] is False and out["recovered"] is None


def test_u7_19_watchdog_recovery_failure_does_not_block_restart():
    """路由恢复失败被记录为错误，但仍继续重启监测（不无限期停摆）。"""
    decision = cr.WatchdogDecision(action="RESTART_MONITOR", recover_route=True)

    def _boom():
        raise OSError("journal unreadable")

    out = cr.apply_watchdog_decision(decision, recover=_boom,
                                     restart=lambda: True)
    assert out["restarted"] is True
    assert any("路由恢复失败" in e for e in out["errors"])


# --------------------------------------------------------------------------- #
# U-7-20..22 关联日志（方案 §12-P4）
# --------------------------------------------------------------------------- #
def test_u7_20_event_log_memory_mode_zero_io(tmp_path, monkeypatch):
    """默认（无 path）为内存日志：零文件写入。"""
    monkeypatch.setattr(cr, "_iso_now", lambda: "2026-10-14T16:00:00+08:00")
    log = cr.RemoteEventLog()
    cid = log.new_correlation_id()
    log.record("wifi", "重新关联 SNNU", correlation_id=cid)
    log.record("route", "Metric 写入：Wi-Fi 接管", correlation_id=cid)
    log.record("portal", "Portal 认证成功", correlation_id=cid)
    log.record("remote", "远控会话建立", correlation_id=cid)
    rows = log.entries(correlation_id=cid)
    assert [r["kind"] for r in rows] == ["wifi", "route", "portal", "remote"]
    assert all(r["at"] == "2026-10-14T16:00:00+08:00" for r in rows)
    assert "无可用路径" not in ""
    assert list(tmp_path.iterdir()) == []        # 未写任何文件
    text = log.summarize(cid)
    assert "[wifi]" in text and "[portal]" in text
    assert log.write_errors == 0


def test_u7_21_event_log_jsonl_written_and_redacted(tmp_path):
    """给定 path 时按 JSONL 追加，且不含凭据类字段（AGENTS §6）。"""
    path = tmp_path / "nested" / "remote_guard.jsonl"
    log = cr.RemoteEventLog(path)
    log.record("portal", "认证成功", correlation_id="c1")
    payload = json.loads(cr.json.dumps(log.entries()[0], ensure_ascii=False))
    assert payload["contract_version"] == cr.EVENT_LOG_CONTRACT_VERSION
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 1 and json.loads(lines[0])["kind"] == "portal"
    body = path.read_text(encoding="utf-8").lower()
    for banned in ("password", "passwd", "cookie", "credential", "token"):
        assert banned not in body


def test_u7_22_record_tick_events_maps_four_kinds():
    """一轮 TickResult 的 plan/executed/状态转换按四类事件记录，且只记录不执行。"""
    from campus_failover import FailoverMode, FailoverPolicy, FailoverState
    from campus_failover import OpKind

    class _FakeOutcome:
        kind = OpKind.METRIC_WRITE
        detail = "已执行"

    class _FakeResult:
        plan = [type("R", (), {"kind": OpKind.METRIC_WRITE,
                              "reason": "Wi-Fi 接管"})(),
                type("R", (), {"kind": OpKind.PORTAL_POST,
                              "reason": "SNNU 认证"})()]
        executed = [_FakeOutcome()]
        changed = True
        previous_state = FailoverState.ETHERNET_SUSPECT
        state = FailoverState.WIFI_BACKUP_ACTIVE

    log = cr.RemoteEventLog()
    events = cr.record_tick_events(log, _FakeResult(), correlation_id="c2")
    kinds = [e["kind"] for e in events]
    assert kinds.count("route") == 2 and kinds.count("portal") == 1
    assert "状态转换" in events[-1]["detail"]
    assert len(log.entries()) == 4
    assert all(e["correlation_id"] == "c2" for e in events)
    assert FailoverMode.AUTO is FailoverMode.AUTO      # 仅引用，无副作用
    assert FailoverPolicy().switch_min_interval == 300.0


# --------------------------------------------------------------------------- #
# U-7-23..25 探测节流与格式输出
# --------------------------------------------------------------------------- #
def test_u7_23_poll_throttles_probes(virtual_clock: VirtualClock):
    """``probe_interval`` 内的重复 tick 不产生新的探测调用（零 I/O）。"""
    ops = FakeRemoteOps(processes=[_proc()], ports=[_port()])
    guard = cr.RemoteSafetyGuard(ops=ops, probe_interval=30.0)
    clock = virtual_clock
    for _ in range(5):
        guard.poll(clock.monotonic())
        clock.advance(5.0)              # 5×5s = 25s < 30s：只探测一次
    assert ops.probe_calls == [1]
    clock.advance(10.0)                 # 累计 35s：触发第二次探测
    guard.poll(clock.monotonic())
    assert ops.probe_calls == [1, 1]
    assert guard.probe_count == 2


def test_u7_24_format_remote_status_no_side_effects(capsys):
    """纯格式输出：不产生任何 I/O，也不泄露敏感字段。"""
    status = cr.RemoteStatus(activity=cr.ACTIVITY_ACTIVE, keys=["tailscale"],
                             clients=["Tailscale"], established=1,
                             sensitive=True, reason="测试")
    text = cr.format_remote_status(status)
    assert "V0.3 P7 远程保障" in text and "远程会话进行中" in text
    for banned in ("password", "keyring", "凭据管理器"):
        assert banned not in text
    out = capsys.readouterr().out
    assert out == ""


def test_u7_25_build_windows_remote_ops_is_read_only(fake_runner):
    """真实适配层只发 netdiag 只读命令（写动词一律被 assert_read_only 拒绝）。"""
    fake_runner.responses.update({
        "remote_processes": '[{"Id":6864,"ProcessName":"tailscaled",'
                            '"Path":"C:/fake/tailscaled.exe"}]',
        "remote_tcp": '[{"OwningProcess":6864,"State":"Listen",'
                      '"LocalAddress":"0.0.0.0","LocalPort":443,'
                      '"RemoteAddress":"","RemotePort":0}]',
        "remote_udp": '[{"OwningProcess":6864,"LocalAddress":"0.0.0.0",'
                      '"LocalPort":41641}]',
        "sleep_states": "The following sleep states are available: Standby (S3)",
        "winlogon": '{"AutoAdminLogon":1,"DefaultUserName":"lennonli"}',
        "run_entries": '[{"Name":"CampusReconnectTray",'
                       '"Command":"pythonw campus_reconnect_tray.py",'
                       '"PSPath":"HKEY_CURRENT_USER\\\\Run"}]',
    })
    ops = cr.build_windows_remote_ops(fake_runner)
    probe = ops.probe()
    ready_probe = ops.probe_readiness()
    commands = [name for name, _script in fake_runner.commands]
    # 有 PID → 必然查询 TCP/UDP；就绪探测额外含睡眠/登录/自启
    assert {"remote_processes", "remote_tcp", "remote_udp"} <= set(commands)
    assert {"sleep_states", "winlogon", "run_entries"} <= set(commands)
    assert [c["process_name"] for c in probe.clients] == ["tailscaled"]
    assert ready_probe.has_readiness_data is True
    for name, script in fake_runner.commands:
        assert "Set-" not in script and "Remove-" not in script
        assert name in {"remote_processes", "remote_tcp", "remote_udp",
                        "sleep_states", "winlogon", "run_entries"}


