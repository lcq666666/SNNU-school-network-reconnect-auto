# -*- coding: utf-8 -*-
"""F09 统一冷却与失败退避（对应 T19 / T20）。

阶段 3 修复后由回归集合并入默认集合。修复要点：

- 恢复**不抹掉**最短重连间隔（认证冷却不被成功探测重置）；
- **V0.3 P2 三表分离**：冷却只禁止**新的认证**，不禁止探测 —— 探测节奏恒为
  ``interval``，冷却期内公网探测持续进行（修复 P0-T6 取证缺陷）；
- CLI ``watch`` 与托盘 ``_probe_tick`` 语义一致；
- 全程使用虚拟时钟，不真实 sleep。
"""
from __future__ import annotations

import argparse
import logging

import campus_reconnect as engine
import campus_reconnect_tray as tray

LOG = logging.getLogger("campus")

INTERVAL = 15.0


def _args(threshold=1, interval=int(INTERVAL), cooldown=180):
    return argparse.Namespace(ssid="SNNU", yys="mobile", interval=interval,
                              threshold=threshold, cooldown=cooldown, force=False)


def _run_watch(monkeypatch, clock, probe_fn, max_probes=64):
    """用虚拟时钟与函数式探测剧本运行 watch，返回 ``(认证时刻, 探测时刻)``。

    ``probe_fn(t)`` 按探测时刻给出"是否在线"；探测次数达 ``max_probes``
    时抛 ``KeyboardInterrupt`` 结束 watch 的无限循环。
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


def test_t19_short_recovery_must_not_bypass_cooldown(monkeypatch, virtual_clock):
    """T19：失败→短暂恢复→失败，两次认证间隔必须 >= cooldown。

    P2：短暂恢复期间的探测按 interval 持续（不再被冷却暂停），但第二次
    认证仍被认证冷却挡在 cooldown 之后。
    """
    attempts, probes = _run_watch(
        monkeypatch, virtual_clock, lambda t: INTERVAL <= t < 2 * INTERVAL,
        max_probes=24)
    assert len(attempts) >= 2, "未复现出两次尝试：%r" % attempts
    gap = attempts[1] - attempts[0]
    assert gap >= 180, "短暂恢复绕过了最短重连间隔，尝试时刻=%r（间隔 %ss）" % (attempts, gap)
    # 探测节奏严格等于 interval —— 冷却期探测未被暂停（P2）
    assert all(b - a == INTERVAL for a, b in zip(probes, probes[1:])), probes


def test_t20_recovery_does_not_reset_min_interval(monkeypatch, virtual_clock):
    """T20：恢复探测成功后，下一次认证仍不得早于 cooldown。"""
    attempts, probes = _run_watch(
        monkeypatch, virtual_clock, lambda t: INTERVAL <= t < 3 * INTERVAL,
        max_probes=24)
    assert len(attempts) >= 2, "未复现出两次尝试：%r" % attempts
    gap = attempts[1] - attempts[0]
    assert gap >= 180, "恢复抹掉了最短间隔，尝试时刻=%r" % (attempts,)
    assert all(b - a == INTERVAL for a, b in zip(probes, probes[1:])), probes


def test_watch_needs_user_stops_automatic_retry(monkeypatch, virtual_clock):
    """密码错误/验证码等需人工处理时，停止自动频繁重试（探测仍继续）。"""
    clock = virtual_clock
    clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    attempts: list = []
    probes: list = []

    class _Result:
        needs_user = True

    def fake_internet_ok(session, probes_=None):
        if len(probes) >= 6:
            raise KeyboardInterrupt
        probes.append(clock.monotonic())
        return False, ""

    def fake_attempt(session, args, log):
        attempts.append(clock.monotonic())
        return _Result()

    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair", fake_attempt)
    engine.watch(object(), _args(), LOG)
    assert len(attempts) == 1, "需人工处理后仍自动重试：%r" % attempts
    assert len(probes) == 6, probes


# --------------------------------------------------------------------------- #
# 托盘冷却语义与 CLI 一致（F09）
# --------------------------------------------------------------------------- #
def _make_app(monkeypatch, **overrides):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app.session = object()
    return app


def test_t20_tray_recovery_does_not_reset_cooldown(monkeypatch, virtual_clock):
    """托盘：后台不再自动认证（P7.5-1 F75-02）；冷却期探测不暂停（P2）。

    P7.5-1 前后对照：
    - 修复前：``_probe_tick`` 在失败达到阈值后直接 ``_run_repair()``，
      并把 ``max(now+interval, auth_ready_at)`` 叠加到探测节奏上（P0 取证缺陷）；
    - 修复后：``_probe_tick`` **永不**自动认证，认证只可能来自
      ① 用户**显式**手动重连（:meth:`on_reconnect`）或
      ② P3 单一副作用门（AUTO 且已授权且快照可信）；
      探测节奏恒为 ``now + interval``，与认证冷却无关。
    """
    app = _make_app(monkeypatch, interval=int(INTERVAL), threshold=1, cooldown=180)
    virtual_clock.install(monkeypatch, tray)
    attempts: list = []
    probed: list = []

    def fake_internet_ok(session):
        probed.append(virtual_clock.monotonic())
        return False, ""

    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: attempts.append(virtual_clock.monotonic()))
    monkeypatch.setattr(tray, "internet_ok", fake_internet_ok)

    nxt = app._probe_tick(0.0)          # t=0 失败：后台**不**自动认证
    assert attempts == [], "MONITOR 下不得后台自动认证：%r" % attempts
    assert nxt == INTERVAL, "认证冷却推迟了探测：%r" % nxt

    # 唯一合法的托盘认证入口：用户显式手动重连（登记认证冷却）
    app._dispatch(("reconnect", False, app.config_version()), nxt)
    assert attempts == [0.0]
    assert app._timers.auth_ready_at == 180.0, app._timers.auth_ready_at

    # 冷却期内：探测每 interval 一次照常进行，但不再自动认证
    for expected_next in (2 * INTERVAL, 3 * INTERVAL, 4 * INTERVAL):
        virtual_clock.advance(INTERVAL)
        nxt = app._probe_tick(nxt)
        assert nxt == expected_next, nxt
    assert probed == [0.0, 15.0, 30.0, 45.0], probed
    assert attempts == [0.0], "冷却期内重复认证：%r" % attempts

    virtual_clock.advance(135.0)        # t=180：冷却结束
    app._probe_tick(180.0)
    assert attempts == [0.0], "后台自动认证入口已移除：%r" % attempts
    assert probed[-1] == 180.0, probed


# --------------------------------------------------------------------------- #
# P8 / K-4：确认窗口内自愈 ⇒ 抑制重复告警与重复修复
# --------------------------------------------------------------------------- #
def test_watch_self_heal_in_confirmation_window_resets_failures(monkeypatch,
                                                               virtual_clock):
    """达阈值后修复预检确认「公网已连通」⇒ 判定为探测抖动：

    - 清零失败计数（下一次抖动需重新累积阈值，不再逐轮重复修复）；
    - 因此 12 次全失败探测、阈值 3、无冷却时，修复次数应为 4（每 3 连败一次），
      而不是旧行为的 10（阈值之后每轮都修）。
    """
    clock = virtual_clock
    clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    probes: list = []
    attempts: list = []

    class _OnlineRepair:
        needs_user = False
        network_ok = True

    def fake_internet_ok(session, probes_=None):
        if len(probes) >= 12:
            raise KeyboardInterrupt
        probes.append(clock.monotonic())
        return False, ""

    def fake_attempt(session, args, log):
        attempts.append(clock.monotonic())
        return _OnlineRepair()

    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair", fake_attempt)
    engine.watch(object(), _args(threshold=3, interval=int(INTERVAL), cooldown=0), LOG)

    assert len(probes) == 12, probes
    assert len(attempts) == 4, (
        "确认窗口内自愈后未清零失败计数，出现重复修复：%r" % attempts)
