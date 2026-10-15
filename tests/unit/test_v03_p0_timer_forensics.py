# -*- coding: utf-8 -*-
"""V0.3 P0-T6 计时器缺陷取证 → P2 修复后反向断言。

**P0 取证（基线 ``4900ca9``，2026-10-14）**：V0.2 把「认证冷却」错误地叠加到
「网络检测」上，认证冷却期（默认 180s）内公网探测被整体暂停：

1. ``campus_reconnect.watch()``：一次认证失败后登记
   ``next_attempt = now + cooldown``，随后
   ``wait = max(interval, next_attempt - now)`` 让本轮探测整体睡到冷却结束
   —— 探测时刻 ``[0, 180, 195]``，冷却期内零探测，公网恢复发现被推迟 170s；
2. 托盘 ``_probe_tick()``/``_next_probe_time()`` 同款模式：
   ``_next_attempt_at = now + cooldown``，``now < next_probe`` 时直接跳过探测。

**P2 修复后（本文件现为反向断言）**：三表分离（:class:`campus_core.TimerTable`）
—— 网络检测计时器持续运行、认证冷却只限制认证。P0 取证结论保留于
``docs/阶段0诊断报告_2026-10-14.md`` §6 与 git 历史。

本文件的两个用例断言"冷却期内探测不暂停"，作为该缺陷的常驻回归看守。
"""
from __future__ import annotations

import argparse
import logging

import campus_reconnect as engine
import campus_reconnect_tray as tray

LOG = logging.getLogger("campus")

INTERVAL = 15
COOLDOWN = 180


def _args(threshold=1, interval=INTERVAL, cooldown=COOLDOWN):
    return argparse.Namespace(ssid="SNNU", yys="mobile", interval=interval,
                              threshold=threshold, cooldown=cooldown, force=False)


def test_watch_probes_continue_during_auth_cooldown(monkeypatch, virtual_clock):
    """CLI watch()：认证冷却期内公网探测**不暂停**（P2 修复后断言）。

    P0 取证（缺陷）：探测时刻 = [0, 180, 195]，冷却 180s 内零探测。
    P2 修复后：探测时刻 = [0, 15, 30, ...]（每 interval 一次）；
    公网 t=10 恢复 → 下一个窗口 t=15 即被发现（旧实现要等到 t=180）。
    """
    clock = virtual_clock
    clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    probes: list = []
    attempts: list = []
    recovered_at = 10.0            # 公网真实恢复时刻（剧本设定）
    max_probes = 20

    def fake_internet_ok(session, probes_=None):
        if len(probes) >= max_probes:
            raise KeyboardInterrupt      # 结束 watch 的无限循环
        t = clock.monotonic()
        probes.append(t)
        return (t >= INTERVAL), ""       # t=0 失败；t>=15 公网已恢复

    def fake_attempt(session, args, log):
        attempts.append(clock.monotonic())
        return False

    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair", fake_attempt)
    engine.watch(object(), _args(), LOG)

    # 反向断言：探测每 interval 一次，冷却期（0..180）内一次都没被跳过
    assert probes[0] == 0.0
    assert all(b - a == INTERVAL for a, b in zip(probes, probes[1:])), probes
    assert probes[1] == 15.0, probes
    detection_delay = probes[1] - recovered_at
    assert detection_delay <= INTERVAL, (
        "恢复发现延迟 %s 超过 interval(%s)：%r" % (detection_delay, INTERVAL, probes))
    # 认证只发生在 t=0（公网随后恢复，不再认证）
    assert attempts == [0.0], attempts
    # 证据摘要（对照 P0 取证）：探测时刻=[0,15,30,...]；认证时刻=[0]


def test_tray_probes_continue_during_auth_cooldown(monkeypatch, virtual_clock):
    """托盘 _probe_tick：认证冷却期内探测**不暂停**（P2 修复后断言）。

    P0 取证（缺陷）：t=0 失败→**自动**认证；t=15 的 tick 跳过探测；t=180 才恢复探测。
    P2 修复后：t=15 照常探测，探测节奏与认证冷却解耦。
    P7.5-1 修复后：``_probe_tick`` **不再**自动认证（F75-02），认证只来自显式
    手动重连或 P3 单一副作用门；本用例因此改为先显式触发一次手动重连以登记冷却，
    再断言冷却期内的探测节奏不受影响。
    """
    app = _make_app(monkeypatch, interval=INTERVAL, threshold=1,
                    cooldown=COOLDOWN)
    virtual_clock.install(monkeypatch, tray)
    attempts: list = []
    probed: list = []

    def fake_internet_ok(session):
        probed.append(virtual_clock.monotonic())
        return False, ""

    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: attempts.append(virtual_clock.monotonic()))
    monkeypatch.setattr(tray, "internet_ok", fake_internet_ok)

    nxt = app._probe_tick(0.0)                 # t=0：探测失败，后台**不**认证
    assert attempts == [], "后台自动认证入口已移除（F75-02）：%r" % attempts
    assert probed == [0.0]

    # 显式手动重连（唯一合法入口）→ 登记 180s 认证冷却
    app._dispatch(("reconnect", False, app.config_version()), nxt)
    assert attempts == [0.0]

    virtual_clock.advance(INTERVAL)            # t=15：探测照常进行
    result = app._probe_tick(nxt)
    assert probed == [0.0, 15.0], ("冷却期内探测被暂停：%r" % probed)
    assert result == 30.0, "下一次探测应为一个 interval 之后：%r" % result
    assert attempts == [0.0], ("冷却期内不应重复认证：%r" % attempts)


def _make_app(monkeypatch, **overrides):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app.session = object()
    return app
