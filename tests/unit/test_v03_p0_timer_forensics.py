# -*- coding: utf-8 -*-
"""V0.3 P0-T6 计时器缺陷取证：认证冷却期公网探测被暂停。

取证对象（现行实现，基线 ``4900ca9``）：

1. ``campus_reconnect.watch()``：一次认证失败后登记
   ``next_attempt = now + cooldown``，随后
   ``wait = max(interval, next_attempt - now)`` 让本轮探测整体睡到冷却结束
   —— 冷却期（默认 180s）内**不再探测公网**；
2. 托盘 ``_probe_tick()``/``_next_probe_time()`` 同款模式：修复后
   ``_next_attempt_at = now + cooldown``，``_probe_tick`` 见
   ``now < next_probe`` 直接跳过探测。

两者与各自 docstring 声称的"冷却只禁止新的认证，不禁止探测"相矛盾。

本文件为**行为特征化记录**（characterization）：用例在 P0 全绿，
用于给 P2「计时器分离」提供修复前后的时间戳证据。
**P2 修复通过后，本文件必须改写为断言"冷却期内探测不暂停"。**
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


def test_forensic_watch_probe_paused_during_auth_cooldown(monkeypatch,
                                                          virtual_clock):
    """CLI watch()：修复失败后 180s 冷却内零探测（时间戳证据）。

    剧本：t=0 探测失败 → 认证一次；公网在 t=10 恢复（真实世界）；
    现行实现直到 t=180 才做下一次探测 → 恢复发现被推迟 170 秒。
    """
    clock = virtual_clock
    clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    probes: list = []
    attempts: list = []
    recovered_at = 10.0            # 公网真实恢复时刻（剧本设定）
    cursor = {"i": 0}
    sequence = [False, True]       # 第 1 次探测失败；其后公网已恢复

    def fake_internet_ok(session, probes_=None):
        i = cursor["i"]
        cursor["i"] += 1
        probes.append(clock.monotonic())
        if i < len(sequence):
            return sequence[i], ""
        raise KeyboardInterrupt      # 结束 watch 的无限循环

    def fake_attempt(session, args, log):
        attempts.append(clock.monotonic())
        return False

    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "attempt_repair", fake_attempt)
    engine.watch(object(), _args(), LOG)

    # 取证断言（现行缺陷行为）：
    assert attempts == [0.0], "认证应发生在 t=0：%r" % attempts
    # t=0 失败探测 → t=180 冷却结束探测（恢复）→ t=195 恢复后正常间隔探测
    assert probes == [0.0, 180.0, 195.0], "探测时间表：%r" % probes
    gap = probes[1] - probes[0]
    assert gap == COOLDOWN, ("相邻探测间隔 = cooldown(%s) 而非 interval(%s)，"
                             "即冷却期探测被暂停：%r" % (COOLDOWN, INTERVAL, probes))
    missed_slots = int((probes[1] - INTERVAL - probes[0]) / INTERVAL)
    assert missed_slots == 11, "冷却期内漏掉的探测窗口应为 11 个：%r" % probes
    detection_delay = probes[1] - recovered_at
    assert detection_delay == 170.0
    # 证据摘要（写入 P0 诊断报告）：
    #   探测时刻=[0.0, 180.0, 195.0]；认证时刻=[0.0]；漏探测窗口=11；恢复发现延迟=170s


def test_forensic_tray_probe_skipped_during_auth_cooldown(monkeypatch,
                                                          virtual_clock):
    """托盘 _probe_tick：认证后的下一次 tick 直接跳过探测（时间戳证据）。"""
    app = _make_app(monkeypatch, interval=INTERVAL, threshold=1,
                    cooldown=COOLDOWN)
    virtual_clock.install(monkeypatch, tray)
    attempts = []
    probed: list = []

    def fake_internet_ok(session):
        probed.append(virtual_clock.monotonic())
        return False, ""

    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: attempts.append(virtual_clock.monotonic()))
    monkeypatch.setattr(tray, "internet_ok", fake_internet_ok)

    nxt = app._probe_tick(0.0)                 # t=0：失败 → 认证
    assert attempts == [0.0]
    assert probed == [0.0]

    virtual_clock.advance(INTERVAL)            # t=15：普通间隔到点
    result = app._probe_tick(nxt)              # 现行实现：跳过探测
    assert probed == [0.0], ("冷却期内 tick 仍然执行了探测：%r" % probed)
    assert result >= COOLDOWN, "下一次探测被推迟到冷却结束：%r" % result

    virtual_clock.advance(COOLDOWN - INTERVAL)  # t=180：冷却结束
    app._probe_tick(result)
    assert probed == [0.0, 180.0], "冷却结束后才恢复探测：%r" % probed


def _make_app(monkeypatch, **overrides):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app.session = object()
    return app
