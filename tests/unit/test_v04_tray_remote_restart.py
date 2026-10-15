# -*- coding: utf-8 -*-
"""V0.4-3 托盘侧接线：**确认真的连上后**清退重启 UU 远程（默认关闭）。

需求原文（2026-10-16 用户）：UU 远程在「断网 → 重连」后卡在「正在重新连接」而连不上
本机；希望确认网络真连上后把 UU 远程后台清退重启。

接线判据（方案 §3.3）：
- 触发点 = **连接后真实上网校验通过**（V0.4-1 的 ``verify_internet`` 返回 ok）；
  「校验通过」本身就是"真的连上"的定义，故不另设判据；
- **默认关闭**（``remote_restart_enabled=False``）：不主动调用即零副作用；
- 冷却 ≥ ``remote_restart_cooldown``（默认 300s），跨在线段仍然生效；
- 失败**如实提示**，不改动状态机、不触发重连/路由动作。

约束：零真实进程操作（假 ops）、零真实网络、零真实 sleep。
"""
from __future__ import annotations

import logging
import time

import campus_reconnect_tray as tray
from campus_remote_app import RestartGuard
from tests.fakes import FakeRemoteAppOps

LOG = logging.getLogger("campus")


def wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _make_app(monkeypatch, online_series=None, **overrides):
    """构造托盘应用：外部副作用全部替换为假件（与 test_v04_tray_verify 同构）。"""
    replies = list(online_series or [True])
    state = {"i": 0}

    def fake_internet_ok(session, probes=None):
        index = min(state["i"], len(replies) - 1)
        state["i"] += 1
        return bool(replies[index]), "via"

    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    monkeypatch.setattr(tray, "wifi_state", lambda: ("SNNU", "已连接"))
    monkeypatch.setattr(tray, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(app, "notify", lambda *a, **k: None)
    return app


def _attach_fake_restart(app, monkeypatch, **ops_kwargs):
    """装上假 UU 清退能力 + 记录 ``verify_internet`` 是否通过。"""
    app.session = object()
    ops = _fake_ops(**ops_kwargs)
    app._remote_app_ops = ops
    monkeypatch.setattr(tray, "verify_internet", lambda session: (True, "ok"))
    return ops


def _fake_ops(**kwargs):
    return FakeRemoteAppOps(
        processes=[{"pid": 100, "process_name": "GameViewer"},
                   {"pid": 200, "process_name": "GameViewerLauncher"},
                   {"pid": 300, "process_name": "GameViewerServer"}],
        service={"name": "GameViewerService", "status": "Running",
                 "start_type": "Automatic"},
        run_entries=[{"name": "GameViewer",
                      "command": r'"C:\_study\GameViewer\GameViewer.exe" --auto-run'}],
        **kwargs)


def _wait_restart_done(app):
    def done():
        busy = getattr(app, "_restart_busy", None)
        return busy is None or not busy.is_set()

    return wait_until(done)


# --------------------------------------------------------------------------- #
# 默认关闭（fail-closed）
# --------------------------------------------------------------------------- #
def test_v04_default_config_has_restart_disabled_and_300s_cooldown():
    """配置默认：**关闭** + 冷却 300s（方案 §3.3）。"""
    assert tray.DEFAULT_CONFIG["remote_restart_enabled"] is False
    assert tray.DEFAULT_CONFIG["remote_restart_cooldown"] == 300


def test_v04_restart_not_triggered_when_disabled(monkeypatch):
    """默认关闭时：即使校验通过，也**不得**触碰任何 UU 进程。"""
    app = _make_app(monkeypatch)          # remote_restart_enabled 默认 False
    ops = _attach_fake_restart(app, monkeypatch)

    app._probe_tick(0.0)
    time.sleep(0.1)

    assert ops.killed == [], "默认关闭却在清退进程"
    assert ops.launched == []


# --------------------------------------------------------------------------- #
# 触发点 = 校验通过
# --------------------------------------------------------------------------- #
def test_v04_restart_triggered_once_after_verified_online(monkeypatch):
    """开启后：**校验通过** ⇒ 清退用户态进程并按自启项重启（各一次）。"""
    app = _make_app(monkeypatch, remote_restart_enabled=True)
    ops = _attach_fake_restart(app, monkeypatch)

    app._probe_tick(0.0)

    assert wait_until(lambda: ops.launched), "校验通过后未触发 UU 远程重启"
    assert _wait_restart_done(app)
    assert ops.killed == [100, 200]
    assert ops.launched == [r'"C:\_study\GameViewer\GameViewer.exe" --auto-run']


def test_v04_restart_not_triggered_when_verify_fails(monkeypatch):
    """校验**未通过**（疑似假死/没真连上）⇒ 不得重启 UU（否则白清一次）。"""
    app = _make_app(monkeypatch, remote_restart_enabled=True)
    ops = FakeRemoteAppOps(processes=[{"pid": 100, "process_name": "GameViewer"}],
                           run_entries=[{"name": "GameViewer", "command": "x.exe"}])
    app._remote_app_ops = ops
    app.session = object()
    monkeypatch.setattr(tray, "verify_internet",
                        lambda session: (False, "闸门 www.baidu.com(timeout)"))

    app._probe_tick(0.0)
    time.sleep(0.1)

    assert ops.killed == [], "校验未通过却清退了 UU 进程"


def test_v04_restart_cooldown_spans_online_segments(monkeypatch):
    """冷却跨在线段生效：掉线再连上（冷却期内）不得二次清退。"""
    app = _make_app(monkeypatch, remote_restart_enabled=True,
                    online_series=[True, False, True])
    ops = _attach_fake_restart(app, monkeypatch)
    clock = {"t": 1000.0}
    app._remote_restart_guard = RestartGuard(cooldown=300.0,
                                             clock=lambda: clock["t"])

    app._probe_tick(0.0)                      # 在线 → 校验通过 → 清退 #1
    assert wait_until(lambda: ops.launched) and _wait_restart_done(app)
    app._probe_tick(0.0)                      # 掉线（重置在线段标记）
    clock["t"] += 20.0
    app._probe_tick(0.0)                      # 再连上 → 校验通过，但冷却中
    time.sleep(0.15)

    assert len(ops.launched) == 1, "冷却期内重复清退：%d 次" % len(ops.launched)


# --------------------------------------------------------------------------- #
# 失败/异常不得影响主流程
# --------------------------------------------------------------------------- #
def test_v04_restart_failure_notifies_and_keeps_state(monkeypatch):
    """清退/重启失败 ⇒ 气泡如实提示 + 只记日志；不改状态机、不触发重连。"""
    app = _make_app(monkeypatch, remote_restart_enabled=True)
    notices = []
    monkeypatch.setattr(app, "notify", lambda *a, **k: notices.append(a))
    ops = _attach_fake_restart(app, monkeypatch, launch_results=False)

    app._probe_tick(0.0)

    assert wait_until(lambda: notices), "重启失败未提示用户"
    assert app.state["mode"] == "online", "UU 重启失败不应改变在线状态"
    assert ops.killed == [100, 200]


def test_v04_restart_exception_is_swallowed(monkeypatch):
    """清退能力自身异常不得炸掉校验/监测线程。"""
    app = _make_app(monkeypatch, remote_restart_enabled=True)
    app.session = object()
    app._remote_app_ops = _fake_ops(probe_error=OSError("wmi down"))
    monkeypatch.setattr(tray, "verify_internet", lambda session: (True, "ok"))

    app._probe_tick(0.0)
    time.sleep(0.15)

    assert app.state["mode"] == "online"
    assert _wait_restart_done(app), "重启线程未正常收尾"


# --------------------------------------------------------------------------- #
# 配置读写与开关
# --------------------------------------------------------------------------- #
def test_v04_restart_keys_are_known_config_keys():
    """新键须登记进 ``campus_core`` 已知键，否则每次读配置都报"未知键"告警。"""
    import json as _json

    from campus_core import load_config_text

    result = load_config_text(_json.dumps(
        {"remote_restart_enabled": True, "remote_restart_cooldown": 600}))

    assert result.unknown_keys == (), "新键被当成未知键隔离：%r" % (result.unknown_keys,)


def test_v04_read_restart_keys_defaults_and_clamps():
    """非法值回退安全默认（关闭）；冷却限幅 60..3600s，防止配置把冷却压没。"""
    assert tray._read_remote_app_keys("{}") == {
        "remote_restart_enabled": False, "remote_restart_cooldown": 300}
    assert tray._read_remote_app_keys("not json")["remote_restart_enabled"] is False
    assert tray._read_remote_app_keys('{"remote_restart_cooldown": 5}')[
        "remote_restart_cooldown"] == 60
    assert tray._read_remote_app_keys('{"remote_restart_cooldown": 99999}')[
        "remote_restart_cooldown"] == 3600
    assert tray._read_remote_app_keys('{"remote_restart_enabled": "yes"}')[
        "remote_restart_enabled"] is False


def test_v04_toggle_restart_flag_persists(monkeypatch):
    """菜单开关：写入配置并保存；默认从 False 切到 True。"""
    app = _make_app(monkeypatch)
    saved = {}
    monkeypatch.setattr(tray, "save_config",
                        lambda cfg: saved.update(cfg) or True)

    value = app.apply_remote_restart(True)

    assert value is True
    assert app.config["remote_restart_enabled"] is True
    assert saved.get("remote_restart_enabled") is True
