# -*- coding: utf-8 -*-
"""F09 统一冷却与失败退避（对应 T19 / T20）。

阶段 3 修复后由回归集合并入默认集合。修复要点：
- 恢复**不抹掉**最短重连间隔（``next_attempt`` 保持）；
- 冷却只禁止新的认证，不禁止探测；
- CLI ``watch`` 与托盘 ``_probe_tick`` 语义一致；
- 全程使用虚拟时钟，不真实 sleep。
"""
from __future__ import annotations

import argparse
import logging

import campus_reconnect as engine
import campus_reconnect_tray as tray

LOG = logging.getLogger("campus")


def _args(threshold=1, interval=15, cooldown=180):
    return argparse.Namespace(ssid="SNNU", yys="mobile", interval=interval,
                              threshold=threshold, cooldown=cooldown, force=False)


def _run_watch(monkeypatch, clock, sequence):
    """用虚拟时钟与脚本化探测运行 watch，返回每次尝试的单调时刻。"""
    clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    attempts = []
    cursor = {"i": 0}

    def fake_internet_ok(session, probes=None):
        i = cursor["i"]
        cursor["i"] += 1
        if i < len(sequence):
            return sequence[i], ""
        raise KeyboardInterrupt  # 结束 watch 的无限循环

    def fake_attempt(session, args, log):
        attempts.append(clock.monotonic())
        return False

    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair", fake_attempt)
    engine.watch(object(), _args(), LOG)
    return attempts


def test_t19_short_recovery_must_not_bypass_cooldown(monkeypatch, virtual_clock):
    """T19：失败→恢复→失败，两次尝试间隔必须 >= cooldown。"""
    attempts = _run_watch(monkeypatch, virtual_clock, [False, True, False])
    assert len(attempts) >= 2, "未复现出两次尝试：%r" % attempts
    gap = attempts[1] - attempts[0]
    assert gap >= 180, "短暂恢复绕过了最短重连间隔，尝试时刻=%r（间隔 %ss）" % (attempts, gap)


def test_t20_recovery_does_not_reset_min_interval(monkeypatch, virtual_clock):
    """T20：恢复探测成功后，下一次认证仍不得早于 cooldown。"""
    attempts = _run_watch(monkeypatch, virtual_clock, [False, True, True, False])
    assert len(attempts) >= 2, "未复现出两次尝试：%r" % attempts
    gap = attempts[1] - attempts[0]
    assert gap >= 180, "恢复抹掉了最短间隔，尝试时刻=%r" % (attempts,)


def test_watch_needs_user_stops_automatic_retry(monkeypatch, virtual_clock):
    """密码错误/验证码等需人工处理时，停止自动频繁重试。"""
    clock = virtual_clock
    clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    attempts = []
    cursor = {"i": 0}
    sequence = [False, False, False, False, False, False]

    def fake_internet_ok(session, probes=None):
        i = cursor["i"]
        cursor["i"] += 1
        if i < len(sequence):
            return False, ""
        raise KeyboardInterrupt

    class _Result:
        needs_user = True

    def fake_attempt(session, args, log):
        attempts.append(clock.monotonic())
        return _Result()

    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair", fake_attempt)
    engine.watch(object(), _args(), LOG)
    assert len(attempts) == 1, "需人工处理后仍自动重试：%r" % attempts


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
    """托盘：探测恢复后仍不得早于 cooldown 再次认证。"""
    app = _make_app(monkeypatch, interval=15, threshold=1, cooldown=180)
    virtual_clock.install(monkeypatch, tray)
    attempts = []
    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: attempts.append(virtual_clock.monotonic()))
    monkeypatch.setattr(tray, "internet_ok", lambda session: (False, ""))
    nxt = app._probe_tick(0.0)          # t=0 失败 → 触发一次重连
    assert attempts == [0.0]

    monkeypatch.setattr(tray, "internet_ok", lambda session: (True, ""))
    virtual_clock.advance(15)
    assert app._probe_tick(nxt) >= 180, "恢复在冷却期内重置了最短间隔"

    virtual_clock.advance(165)          # t=180
    nxt2 = app._probe_tick(180.0)
    assert nxt2 == 195.0                # 恢复探测刷新，但不早于冷却

    virtual_clock.advance(15)           # t=195
    monkeypatch.setattr(tray, "internet_ok", lambda session: (False, ""))
    app._probe_tick(nxt2)
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] >= 180
