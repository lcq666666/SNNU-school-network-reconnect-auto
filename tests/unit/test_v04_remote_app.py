# -*- coding: utf-8 -*-
"""V0.4-2 UU 远程（网易 GameViewer）识别规则 + **只读**发现。

需求原文（2026-10-16 用户）：UU 远程 app 在「断网 → 重连」后会卡在「正在重新连接」
而连不上本机，导致无法远控；希望确认网络真连上后把 UU 远程后台清退重启。

本阶段（V0.4-2）只交付**识别 + 只读发现**，**零副作用**；清退/重启在 V0.4-3。

真机取证（2026-10-16，只读）：UU 远程 = 网易 GameViewer
- 用户态：``GameViewer.exe``、``GameViewerLauncher.exe``、``uuyc-cli.exe``、``uuyc-mux.exe``
- 随服务生命期：``GameViewerServer.exe``、``GameViewerHealthd.exe``（**不清退**）
- Windows 服务：``GameViewerService``（**不重启**，需 UAC）
- 自启项：``HKCU\\...\\Run`` → ``GameViewer --auto-run``

约束：零真实进程操作（全部经 :class:`tests.fakes.FakeRemoteAppOps`）。
"""
from __future__ import annotations

import re

import campus_remote_app as cra
from campus_remote import DEFAULT_REMOTE_CLIENTS
from tests.fakes import FakeRemoteAppOps

#: 真机取证到的进程名（含大小写，验证大小写不敏感匹配）。
USER_PROCS = ("GameViewer.exe", "GameViewerLauncher.exe")
SERVICE_PROCS = ("GameViewerServer.exe", "GameViewerHealthd.exe")
OTHER_PROCS = ("uuyc-cli.exe", "uuyc-mux.exe")


def _ops(**kwargs) -> FakeRemoteAppOps:
    rows = [{"pid": 100, "process_name": "GameViewer.exe",
             "path": r"C:\_study\GameViewer\GameViewer.exe"},
            {"pid": 200, "process_name": "GameViewerLauncher.exe",
             "path": r"C:\_study\GameViewer\GameViewerLauncher.exe"},
            {"pid": 300, "process_name": "GameViewerServer.exe",
             "path": r"C:\_study\GameViewer\GameViewerServer.exe"},
            {"pid": 400, "process_name": "GameViewerHealthd.exe",
             "path": r"C:\_study\GameViewer\GameViewerHealthd.exe"}]
    kwargs.setdefault("processes", rows)
    kwargs.setdefault("service", {"name": "GameViewerService", "status": "Running",
                                  "start_type": "Automatic"})
    kwargs.setdefault("run_entries", [{"name": "GameViewer",
                                       "command": r'"C:\_study\GameViewer\GameViewer.exe" --auto-run'}])
    return FakeRemoteAppOps(**kwargs)


# --------------------------------------------------------------------------- #
# 识别规则
# --------------------------------------------------------------------------- #
def test_v04_gameviewer_spec_exists_and_matches_real_processes():
    """``DEFAULT_REMOTE_CLIENTS`` 须含 gameviewer，且命中真机取证的全部进程名。"""
    specs = [spec for spec in DEFAULT_REMOTE_CLIENTS if spec.key == "gameviewer"]
    assert len(specs) == 1, "未登记 gameviewer 远控规则（P7 远控探测将不认识 UU 远程）"
    spec = specs[0]

    for name in USER_PROCS + SERVICE_PROCS + OTHER_PROCS:
        assert spec.matches(name), "规则未命中真机进程 %s" % name

    assert not spec.matches("chrome"), "gameviewer 规则误命中 chrome"
    assert not spec.matches("gamebar"), "gameviewer 规则误命中 gamebar"


def test_v04_netdiag_pattern_covers_gameviewer():
    """P0 只读管道 ``remote_processes`` 的正则须覆盖 GameViewer / uuyc。"""
    from tools import netdiag

    pattern = re.compile(netdiag.REMOTE_CLIENT_PATTERN, re.IGNORECASE)
    for name in USER_PROCS + SERVICE_PROCS + OTHER_PROCS:
        assert pattern.search(name), "netdiag 正则未覆盖 %s（P7 探测看不到 UU）" % name
    assert pattern.search("GameViewerLauncher")
    assert not pattern.search("chrome")
    assert not pattern.search("explorer")


def test_v04_scope_constants_are_explicit():
    """清退范围必须与方案 §3.2 一致（只动用户态两进程）。"""
    assert set(cra.GAMEVIEWER_USER_PROCESSES) == set(USER_PROCS)
    assert set(cra.GAMEVIEWER_SERVICE_PROCESSES) == set(SERVICE_PROCS)
    assert cra.GAMEVIEWER_SERVICE == "GameViewerService"


# --------------------------------------------------------------------------- #
# 只读发现
# --------------------------------------------------------------------------- #
def test_v04_discover_classifies_processes():
    """发现结果须区分「清退范围」与「随服务生命期」两类进程。"""
    status = cra.discover(_ops())

    assert {p.name for p in status.user_processes} == set(USER_PROCS)
    assert {p.name for p in status.service_processes} == set(SERVICE_PROCS)
    assert status.running is True
    assert status.error == ""


def test_v04_discover_classifies_extensionless_names_from_get_process():
    """**真机取证回归**：``Get-Process`` 返回的名字**不带 .exe**，仍须正确归类。

    V0.4-2 真机只读跑出「进程 4 个（清退范围 0 个）」的假阴性——根因正是
    ``GameViewer`` 与本模块 ``.exe`` 常量直接比较永不相等；本用例把该缺口钉死。
    """
    ops = _ops(processes=[{"pid": 11, "process_name": "GameViewer"},
                          {"pid": 12, "process_name": "GameViewerLauncher"},
                          {"pid": 13, "process_name": "GameViewerServer"},
                          {"pid": 14, "process_name": "GameViewerHealthd"},
                          {"pid": 15, "process_name": "uuyc-cli"},
                          {"pid": 16, "process_name": "uuyc-mux"}])

    status = cra.discover(ops)

    assert {p.name for p in status.user_processes} == {"GameViewer",
                                                       "GameViewerLauncher"}
    assert {p.name for p in status.service_processes} == {"GameViewerServer",
                                                          "GameViewerHealthd"}
    assert status.running is True


def test_v04_discover_is_read_only():
    """**核心边界**：只读发现绝不产生任何副作用（不清退、不重启）。"""
    ops = _ops()
    cra.discover(ops)

    assert ops.killed == [], "只读发现清退了进程：%r" % (ops.killed,)
    assert ops.launched == [], "只读发现启动了进程：%r" % (ops.launched,)
    assert ops.list_calls == 1


def test_v04_discover_reports_service_and_autostart():
    """发现须报告服务状态与自启项（重启需用自启项的原始命令）。"""
    status = cra.discover(_ops())

    assert status.service_state.get("status") == "Running"
    assert cra.launch_command(status), "未解析出可用于重启的命令"
    assert "GameViewer" in cra.launch_command(status)


def test_v04_discover_tolerates_partial_probe_failure():
    """任一探测源失败只记 error，其余结果仍返回，绝不抛异常。"""
    ops = _ops(probe_error=OSError("wmi unavailable"))

    status = cra.discover(ops)

    assert status.processes == []
    assert status.error, "探测失败未如实记录"
    assert status.service_state.get("status") == "Running"   # 其余源仍可用


def test_v04_discover_when_not_running():
    """UU 远程没在跑时：进程为空、running=False，且仍能给出启动命令（自启项）。"""
    status = cra.discover(_ops(processes=[], service={}, run_entries=[]))

    assert status.processes == []
    assert status.running is False
    assert status.error == ""


def test_v04_discover_ignores_rows_without_pid():
    """脏数据（缺 pid/名字）不得进入结果，也不得抛错。"""
    ops = _ops(processes=[{"process_name": "GameViewer.exe"},
                          {"pid": "abc", "process_name": "GameViewer.exe"},
                          {"pid": 1, "process_name": ""}])

    status = cra.discover(ops)

    assert all(isinstance(p.pid, int) and p.pid > 0 for p in status.processes)
    assert all(p.name for p in status.processes)


def test_v04_format_status_lists_everything():
    """人类可读报告须含服务/进程/自启项要点（供 CLI --status）。"""
    text = cra.format_status(cra.discover(_ops()))

    assert "GameViewerService" in text
    assert "GameViewer.exe" in text
    assert "GameViewerServer.exe" in text


def test_v04_discover_lists_untouched_auxiliary_processes():
    """同族辅助进程（uuyc-*）须**如实列出**且明确归入"不动"一类，不留盲区。"""
    ops = _ops(processes=[{"pid": 21, "process_name": "GameViewer"},
                          {"pid": 22, "process_name": "uuyc-cli"},
                          {"pid": 23, "process_name": "uuyc-mux"}])

    status = cra.discover(ops)

    assert {p.name for p in status.other_processes} == {"uuyc-cli", "uuyc-mux"}
    assert status.user_processes == [p for p in status.processes
                                     if p.name == "GameViewer"]
    text = cra.format_status(status)
    assert "uuyc-cli" in text and "uuyc-mux" in text


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_v04_cli_remote_app_defaults_to_status():
    """``remote-app`` 须存在；**不带参数即只读**（默认 --status）。"""
    import campus_reconnect as engine

    args = engine.build_parser().parse_args(["remote-app"])

    assert args.command == "remote-app"
    assert getattr(args, "restart", False) is False, "remote-app 默认不得启用作副作用动作"


def test_v04_cli_remote_app_restart_is_explicit():
    """清退重启必须**显式** ``--restart``；冷却值可配且默认 300s。"""
    import campus_reconnect as engine

    args = engine.build_parser().parse_args(["remote-app", "--restart"])

    assert args.restart is True
    assert args.cooldown == 300, "默认冷却须为 300s（方案 §3.3）"


# --------------------------------------------------------------------------- #
# V0.4-3 清退 / 重启动作（有副作用；全部经假件，零真实进程操作）
# --------------------------------------------------------------------------- #
def _restart_ops(**kwargs):
    return _ops(**kwargs)


def test_v04_restart_kills_only_user_scope_processes():
    """**核心边界**：只清退用户态两进程，绝不碰 Server/Healthd/uuyc/服务。"""
    ops = _restart_ops(processes=[{"pid": 100, "process_name": "GameViewer"},
                                  {"pid": 200, "process_name": "GameViewerLauncher"},
                                  {"pid": 300, "process_name": "GameViewerServer"},
                                  {"pid": 400, "process_name": "GameViewerHealthd"},
                                  {"pid": 500, "process_name": "uuyc-cli"}])

    result = cra.restart_user_app(ops)

    assert result.ok is True
    assert result.action == "restart"
    assert ops.killed == [100, 200], "清退范围越界：%r" % (ops.killed,)


def test_v04_restart_relaunches_with_autostart_command():
    """重启须用**用户原本的启动方式**（自启项命令）。"""
    ops = _restart_ops()

    result = cra.restart_user_app(ops)

    assert ops.launched == [r'"C:\_study\GameViewer\GameViewer.exe" --auto-run']
    assert result.launched == ops.launched[0]


def test_v04_restart_noop_when_app_not_running():
    """没在跑 ⇒ **什么都不做**（尤其不得替用户把已关闭的 UU 启动起来）。"""
    ops = _restart_ops(processes=[{"pid": 300, "process_name": "GameViewerServer"}])

    result = cra.restart_user_app(ops)

    assert result.ok is True and result.action == "noop"
    assert ops.killed == [] and ops.launched == []


def test_v04_restart_fails_closed_without_launch_command():
    """找不到重启命令 ⇒ **一个进程都不清退**（fail-closed，不做半截动作）。"""
    ops = _restart_ops(processes=[{"pid": 100, "process_name": "GameViewer"}],
                       run_entries=[])

    result = cra.restart_user_app(ops)

    assert result.ok is False
    assert ops.killed == [], "无重启命令却已清退进程（会造成 UU 彻底掉线）"
    assert ops.launched == []


def test_v04_restart_aborts_when_kill_fails():
    """清退失败 ⇒ 立即中止且**不启动新实例**（避免两个实例抢会话）。"""
    ops = _restart_ops(processes=[{"pid": 100, "process_name": "GameViewer"},
                                  {"pid": 200, "process_name": "GameViewerLauncher"}],
                       kill_results=[True, False])

    result = cra.restart_user_app(ops)

    assert result.ok is False
    assert ops.killed == [100, 200]        # 记录已尝试的
    assert ops.launched == [], "清退未全部成功却已启动新实例"
    assert "200" in result.detail or "GameViewerLauncher" in result.detail


def test_v04_restart_reports_launch_failure_honestly():
    """重启命令失败 ⇒ 如实报告（不静默重试、不谎报成功）。"""
    ops = _restart_ops(launch_results=False)

    result = cra.restart_user_app(ops)

    assert result.ok is False
    assert ops.killed == [100, 200], "应先清退再重启"
    assert result.detail and "失败" in result.detail


def test_v04_restart_swallows_kill_exception_and_reports():
    """清退调用抛异常 ⇒ 记失败结果，绝不向上抛（不炸掉调用方线程）。"""
    ops = _restart_ops(kill_results=OSError("access denied"))

    result = cra.restart_user_app(ops)

    assert result.ok is False
    assert ops.launched == []


def test_v04_restart_guard_blocks_second_call_within_cooldown():
    """冷却：300s 内的第二次真实清退必须被拒，且零副作用。"""
    clock = {"t": 1000.0}
    guard = cra.RestartGuard(cooldown=300.0, clock=lambda: clock["t"])
    ops = _restart_ops()

    first = cra.restart_user_app(ops, guard=guard)
    assert first.ok is True and first.action == "restart"
    assert len(ops.killed) == 2

    clock["t"] += 30.0                     # 冷却期内
    second = cra.restart_user_app(ops, guard=guard)

    assert second.ok is False and second.action == "skipped"
    assert len(ops.killed) == 2, "冷却期内仍清退了进程"
    assert len(ops.launched) == 1


def test_v04_restart_guard_allows_after_cooldown():
    """冷却期满 ⇒ 允许再次真实清退。"""
    clock = {"t": 1000.0}
    guard = cra.RestartGuard(cooldown=300.0, clock=lambda: clock["t"])
    ops = _restart_ops()

    assert cra.restart_user_app(ops, guard=guard).ok is True

    clock["t"] += 301.0
    again = cra.restart_user_app(ops, guard=guard)

    assert again.ok is True and again.action == "restart"
    assert len(ops.killed) == 4, "冷却期满未重新清退：%r" % (ops.killed,)


def test_v04_restart_failed_attempt_does_not_start_cooldown():
    """失败（未真正完成重启）不计入冷却，否则失败一次要等 5 分钟才能补救。"""
    guard = cra.RestartGuard(cooldown=300.0, clock=lambda: 1000.0)
    ops = _restart_ops(launch_results=False)

    cra.restart_user_app(ops, guard=guard)

    assert guard.last_at is None, "失败也被计入冷却"


# --------------------------------------------------------------------------- #
# Windows 副作用适配层：命令行拼装（不真正执行任何进程操作）
# --------------------------------------------------------------------------- #
def test_v04_kill_process_uses_taskkill_force_without_shell(monkeypatch):
    """清退经 ``taskkill /PID <pid> /F``，**不经 shell**（无注入面）。"""
    import campus_windows as cw

    seen = {}

    class Done:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Done()

    monkeypatch.setattr(cw.subprocess, "run", fake_run)

    ok, detail = cw.kill_process(4321)

    assert ok is True
    assert list(seen["args"])[:3] == ["taskkill", "/PID", "4321"]
    assert "/F" in seen["args"]
    assert seen["kwargs"].get("shell") in (None, False)
    assert detail


def test_v04_kill_process_reports_failure_with_message(monkeypatch):
    """``taskkill`` 非零返回码 ⇒ ``False`` + 如实带上原 stderr。"""
    import campus_windows as cw

    class Done:
        returncode = 128
        stdout = b""
        stderr = "ERROR: 没有找到进程 \"4321\"。".encode("gbk")

    monkeypatch.setattr(cw.subprocess, "run", lambda *a, **k: Done())

    ok, detail = cw.kill_process(4321)

    assert ok is False
    assert "128" in detail or "ERROR" in detail


def test_v04_launch_detached_splits_quoted_and_unquoted_commands(monkeypatch):
    """自启项命令有/无引号两种写法都要正确切分（**真机取证写法有引号**）。

    Windows 语义：**不做** POSIX 反斜杠转义（``posix=False``），否则
    ``C:\\_study\\...`` 会被吃掉反斜杠 —— 这是本机真机自启项的实际形态。
    """
    import campus_windows as cw

    seen = []

    class Proc:
        pid = 1234

    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda args, **kwargs: seen.append((args, kwargs)) or Proc())

    ok, _ = cw.launch_detached(r'"C:\_study\GameViewer\GameViewer.exe" --auto-run')
    assert ok is True
    assert list(seen[0][0]) == [r"C:\_study\GameViewer\GameViewer.exe", "--auto-run"]
    assert seen[0][1].get("cwd") == r"C:\_study\GameViewer"

    ok2, _ = cw.launch_detached(r"C:\_study\GameViewer\GameViewer.exe --auto-run")
    assert ok2 is True
    assert list(seen[1][0]) == [r"C:\_study\GameViewer\GameViewer.exe", "--auto-run"]


def test_v04_launch_detached_fails_closed_on_empty_or_error(monkeypatch):
    """空命令 / 启动异常 ⇒ ``False`` + 说明（不抛异常）。"""
    import campus_windows as cw

    launched = []
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: launched.append(a) or (_ for _ in ()).throw(
                            OSError("not found")))

    ok, detail = cw.launch_detached("")
    assert ok is False and launched == []

    ok2, detail2 = cw.launch_detached(r'"C:\nope\GameViewer.exe"')
    assert ok2 is False and detail2

