# -*- coding: utf-8 -*-
"""V0.3 P2 计时器分离与 V0.2 冷却修复。

缺陷（P0-T6 取证，基线 ``4900ca9``）：V0.2 把「Portal 认证冷却」错误地叠加
到「网络检测」上，认证冷却期（默认 180s）内公网探测被整体暂停，公网一旦在
冷却期内恢复，发现被推迟最多一个冷却周期。

本文件验证 P2 修复后的**三表分离**（AGENTS §3.4 / 执行计划 §0.2 P2）：

- **网络检测计时器**：持续运行，探测节奏恒为 ``interval``，任何冷却都不得推迟；
- **Portal 认证冷却**：只限制新的认证尝试，不限制探测；
- **网卡切换冷却**：只限制路由变更（P3 消费）。

测试分级：``U-2-*`` 单元 / 离线 Mock；``I-2-*`` 离线集成闭合回路。
约束：零真实网络、零真实注册表写入、零真实凭据读取、零真实 sleep
（用 :class:`tests.fakes.VirtualClock`）。
"""
from __future__ import annotations

import argparse
import logging

import campus_reconnect as engine
import campus_reconnect_tray as tray
from campus_core import TimerTable

LOG = logging.getLogger("campus")

INTERVAL = 15.0
COOLDOWN = 180.0


# --------------------------------------------------------------------------- #
# 辅助：watch 运行器（函数式探测剧本，虚拟时钟）
# --------------------------------------------------------------------------- #
def _args(threshold=1, interval=INTERVAL, cooldown=COOLDOWN):
    return argparse.Namespace(ssid="SNNU", yys="mobile", interval=int(interval),
                              threshold=threshold, cooldown=int(cooldown),
                              force=False)


def _run_watch(monkeypatch, clock, probe_fn, max_probes=64):
    """运行 ``watch`` 并返回 ``(认证时刻列表, 探测时刻列表)``。

    ``probe_fn(t)`` 按探测时刻给出"是否在线"；探测次数达到 ``max_probes``
    时抛 ``KeyboardInterrupt`` 结束 watch 的无限循环（防跑飞）。
    """
    clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    attempts: list = []
    probes: list = []

    def fake_internet_ok(session, probes_=None):
        if len(probes) >= max_probes:
            raise KeyboardInterrupt
        t = clock.monotonic()
        probes.append(t)
        return bool(probe_fn(t)), ""

    def fake_attempt(session, args, log):
        attempts.append(clock.monotonic())
        return False

    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair", fake_attempt)
    engine.watch(object(), _args(), LOG)
    return attempts, probes


def _make_app(monkeypatch, **overrides):
    """构造托盘应用，只替换配置来源，不触碰任何真实副作用。"""
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app.session = object()
    return app


def _all_steps_equal(times, step):
    return all(abs((b - a) - step) < 1e-9 for a, b in zip(times, times[1:]))


# --------------------------------------------------------------------------- #
# U-2-01..04：TimerTable 三表分离原语
# --------------------------------------------------------------------------- #
def test_u2_01_timer_table_is_exported_and_starts_ready():
    """TimerTable 为 campus_core 公开契约，初始三个计时器均"到点"。"""
    import campus_core

    assert "TimerTable" in campus_core.__all__
    timers = TimerTable()
    assert timers.detection_due(0.0) is True
    assert timers.auth_allowed(0.0) is True
    assert timers.switch_allowed(0.0) is True


def test_u2_02_auth_cooldown_only_gates_auth_not_detection():
    """认证冷却只限制认证：检测/切换计时器完全不受影响。"""
    timers = TimerTable()
    timers.note_auth(0.0, COOLDOWN)
    assert timers.auth_ready_at == COOLDOWN
    assert timers.auth_allowed(COOLDOWN - 0.001) is False
    assert timers.auth_allowed(COOLDOWN) is True
    # 检测与切换计时器未受影响
    assert timers.detection_due(0.0) is True
    assert timers.switch_allowed(0.0) is True
    # 冷却期内仍可推进检测计时器，且与冷却无关
    assert timers.schedule_detection(5.0, INTERVAL) == 5.0 + INTERVAL


def test_u2_03_switch_cooldown_only_gates_switch_not_detection_or_auth():
    """切换冷却只限制路由变更：检测与认证计时器完全不受影响。"""
    timers = TimerTable()
    timers.note_switch(0.0, 300.0)
    assert timers.switch_allowed(299.0) is False
    assert timers.switch_allowed(300.0) is True
    assert timers.detection_due(0.0) is True
    assert timers.auth_allowed(0.0) is True
    # 切换冷却期内登记认证冷却，两者互不覆盖
    timers.note_auth(10.0, COOLDOWN)
    assert timers.auth_ready_at == 10.0 + COOLDOWN
    assert timers.switch_ready_at == 300.0


def test_u2_04_detection_timer_is_always_now_plus_interval():
    """检测计时器恒为 ``now + interval``；负间隔钳位为 0，冷却不参与。"""
    timers = TimerTable()
    assert timers.schedule_detection(100.0, INTERVAL) == 100.0 + INTERVAL
    # 负/零间隔钳位
    assert timers.schedule_detection(100.0, -5.0) == 100.0
    assert timers.schedule_detection(100.0, 0.0) == 100.0
    # 处于认证 + 切换冷却中，检测计时器依旧恒为 now + interval
    timers.note_auth(100.0, COOLDOWN)
    timers.note_switch(100.0, 300.0)
    assert timers.schedule_detection(100.0, INTERVAL) == 100.0 + INTERVAL
    assert timers.detection_due(100.0 + INTERVAL - 0.001) is False
    assert timers.detection_due(100.0 + INTERVAL) is True


# --------------------------------------------------------------------------- #
# U-2-05..06：CLI watch()
# --------------------------------------------------------------------------- #
def test_u2_05_watch_probes_continue_through_auth_cooldown(monkeypatch,
                                                           virtual_clock):
    """watch()：认证冷却期内探测**不暂停**，每 interval 一次（P0-T6 修复）。"""
    # t=0 失败触发一次认证；t>=15 起公网已恢复
    attempts, probes = _run_watch(monkeypatch, virtual_clock,
                                  lambda t: t >= 15.0, max_probes=20)
    assert attempts == [0.0], attempts
    assert probes[0] == 0.0
    # 探测节奏严格等于 interval —— 冷却期（0..180）内一次都没被跳过
    assert _all_steps_equal(probes, INTERVAL), probes
    # 公网在 t=15 恢复 → 下一个探测窗口（t=15）即被发现
    assert probes[1] == 15.0, probes
    assert (probes[1] - 10.0) <= INTERVAL, "恢复发现被推迟超过一个 interval：%r" % probes


def test_u2_06_watch_needs_user_still_probes(monkeypatch, virtual_clock):
    """watch()：需人工处理只停**认证**，不停**探测**（冷却期探测持续）。"""
    clock = virtual_clock
    clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    probes: list = []
    attempts: list = []

    class _NeedsUser:
        needs_user = True

    def fake_internet_ok(session, probes_=None):
        if len(probes) >= 8:
            raise KeyboardInterrupt
        probes.append(clock.monotonic())
        return False, ""

    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair",
                        lambda session, args, log:
                        attempts.append(clock.monotonic()) or _NeedsUser())
    engine.watch(object(), _args(), LOG)

    assert len(attempts) == 1, "需人工处理后仍自动重试：%r" % attempts
    assert len(probes) == 8, probes
    assert _all_steps_equal(probes, INTERVAL), probes


# --------------------------------------------------------------------------- #
# U-2-07..08：托盘
# --------------------------------------------------------------------------- #
def test_u2_07_tray_probes_continue_through_auth_cooldown(monkeypatch,
                                                          virtual_clock):
    """托盘 _probe_tick：冷却期探测照常进行，认证被单独门控。"""
    app = _make_app(monkeypatch, interval=int(INTERVAL), threshold=1,
                    cooldown=int(COOLDOWN))
    virtual_clock.install(monkeypatch, tray)
    attempts: list = []
    probed: list = []

    def fake_internet_ok(session):
        probed.append(virtual_clock.monotonic())
        return False, ""

    monkeypatch.setattr(tray, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: attempts.append(virtual_clock.monotonic()))

    nxt = app._probe_tick(0.0)                 # t=0：失败 → 认证
    assert attempts == [0.0]
    assert nxt == INTERVAL, "认证冷却推迟了探测：%r" % nxt

    for expected_next in (30.0, 45.0, 60.0):   # t=15 / 30 / 45 照常探测
        virtual_clock.advance(INTERVAL)
        nxt = app._probe_tick(nxt)
        assert nxt == expected_next, nxt
    assert probed == [0.0, 15.0, 30.0, 45.0], probed
    assert attempts == [0.0], "冷却期内重复认证：%r" % attempts

    virtual_clock.advance(135.0)               # t=180：冷却结束 → 允许再次认证
    app._probe_tick(180.0)
    assert len(attempts) == 2, attempts
    assert attempts[1] - attempts[0] >= COOLDOWN, attempts


def test_u2_08_tray_manual_reconnect_registers_auth_cooldown_only(monkeypatch,
                                                                 virtual_clock):
    """托盘 _dispatch：手动重连登记认证冷却，但**不**推迟探测。"""
    app = _make_app(monkeypatch, interval=int(INTERVAL), cooldown=int(COOLDOWN))
    virtual_clock.install(monkeypatch, tray)
    calls: list = []
    monkeypatch.setattr(app, "_run_repair", lambda force=False: calls.append(force))

    nxt = app._dispatch(("reconnect", True), 0.0)
    assert calls == [True]
    assert nxt == INTERVAL, "手动重连推迟了探测：%r" % nxt
    assert app._timers.auth_ready_at == COOLDOWN
    assert app._timers.auth_allowed(INTERVAL) is False
    assert app._timers.auth_allowed(COOLDOWN) is True


# --------------------------------------------------------------------------- #
# I-2-01：离线集成闭合回路
# --------------------------------------------------------------------------- #
def test_i2_01_offline_closed_loop_recovery_within_one_interval(monkeypatch,
                                                               virtual_clock):
    """离线闭合回路：故障→认证→冷却窗口内恢复，探测不中断、认证不重复。

    剧本：t=0..30 全断（连续 3 次达阈值 → t=30 触发一次认证）；
    t=45 起公网恢复。断言：探测每 15s 一次贯穿全程；认证全程仅一次。
    """
    app = _make_app(monkeypatch, interval=int(INTERVAL), threshold=3,
                    cooldown=int(COOLDOWN))
    virtual_clock.install(monkeypatch, tray)
    attempts: list = []
    probed: list = []

    def fake_internet_ok(session):
        t = virtual_clock.monotonic()
        probed.append(t)
        return (t >= 45.0), ""

    monkeypatch.setattr(tray, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: attempts.append(virtual_clock.monotonic()))

    nxt = app._probe_tick(0.0)                 # t=0 失败（1/3）
    assert probed == [0.0] and attempts == []
    for _ in range(2):                         # t=15（2/3）、t=30（3/3 → 认证）
        virtual_clock.advance(INTERVAL)
        nxt = app._probe_tick(nxt)
    assert attempts == [30.0], attempts
    assert probed == [0.0, 15.0, 30.0], probed

    virtual_clock.advance(INTERVAL)            # t=45：冷却期内探测 → 发现恢复
    nxt = app._probe_tick(nxt)
    assert probed == [0.0, 15.0, 30.0, 45.0], probed
    assert app.state["mode"] == "online", app.state
    assert attempts == [30.0], "副作用清单：全程应仅一次认证：%r" % attempts
