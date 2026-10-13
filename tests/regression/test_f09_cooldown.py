# -*- coding: utf-8 -*-
"""F09 回归用例：冷却不统一，短暂恢复绕过最短间隔（对应 T19 / T20）。

依据《审查方案》第 10 节离线复现：
``watch_attempt_times_with_180s_cooldown= [0, 30]`` —— 虚拟时钟、threshold=1 /
interval=15 / cooldown=180，失败→恢复→失败序列中，原 CLI 在 0 秒与 30 秒各尝试一次，
即「短暂恢复把上次尝试的冷却清掉了」。

**本文件在修复前必须失败**（阶段 3 修复后转绿）；全程使用虚拟时钟，不真实 sleep。
"""
from __future__ import annotations

import argparse
import logging

import pytest

import campus_reconnect as engine

pytestmark = pytest.mark.regression

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
    """T19：失败→恢复→失败，两次尝试间隔必须 >= cooldown（当前 0s/30s）。"""
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
