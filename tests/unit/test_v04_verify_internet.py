# -*- coding: utf-8 -*-
"""V0.4-1 连接后**真实上网校验**（防「真假死」）。

需求原文（2026-10-16 用户）：连接上（有线或无线 SNNU）之后做一次真实上网校验，
例如以 ``www.baidu.com`` 为准，用其它网站交叉验证，防止「连着 Wi-Fi 却不能用」的假死。

判据（方案 §6 Q2 默认档：闸门 + 交叉 ≥2）：
- **闸门**：``www.baidu.com`` 的 ``<title>`` 严格匹配（百度一下，你就知道）；
- **交叉**：腾讯 / 网易 / 搜狐 三站中至少 **2** 站严格匹配；
- 闸门不过 ⇒ 直接判失败（交叉全过也不算通过）。

真机取证（2026-10-16，只读）：
- ``http://www.baidu.com/`` / ``http://www.qq.com/`` / ``http://www.sohu.com/``
  裸 URL 返回 **3xx 跳转**，跟随重定向后均 200；``http://www.163.com/`` 直接 200。
  ⇒ 校验**必须跟随重定向**，否则四站全部误判失败。

约束：全程假 Session，零真实网络、零真实 sleep。
"""
from __future__ import annotations

import argparse
import logging

import requests

import campus_reconnect as engine
from campus_core import PROBE_HTML_TITLE
from tests.fakes import FakeResponse, FakeSession

LOG = logging.getLogger("campus")


def _title_page(spec) -> FakeResponse:
    """按探测点造一个**跟随重定向之后**的 200 页面（正文含期望标题）。"""
    body = ("<HTML><HEAD><TITLE>%s</TITLE></HEAD><BODY>%s</BODY></HTML>"
            % (spec.expect, spec.expect))
    return FakeResponse(200, body, url=spec.url)


def _host(spec) -> str:
    from urllib.parse import urlsplit

    return urlsplit(spec.url).hostname


def _route_ok(session: FakeSession, specs) -> None:
    for spec in specs:
        session.route(spec.url, _title_page(spec))


# --------------------------------------------------------------------------- #
# 判据：闸门 + 交叉 ≥2
# --------------------------------------------------------------------------- #
def test_v04_gate_and_two_cross_pass():
    """闸门通过 + 交叉命中 ≥2 ⇒ 校验通过。"""
    session = FakeSession()
    _route_ok(session, [engine.VERIFY_GATE] + list(engine.VERIFY_CROSS[:2]))

    ok, detail = engine.verify_internet(session)

    assert ok is True, detail
    assert _host(engine.VERIFY_GATE) in detail, detail


def test_v04_gate_fail_blocks_even_when_all_cross_pass():
    """核心回归：闸门（百度）不过 ⇒ 即便交叉三站全过也判**失败**。"""
    session = FakeSession()
    _route_ok(session, engine.VERIFY_CROSS)          # 交叉三站全部命中
    session.route(engine.VERIFY_GATE.url,
                  requests.ConnectTimeout("timed out"))  # 闸门超时

    ok, detail = engine.verify_internet(session)

    assert ok is False, "闸门未过却判通过（假死会漏判）：%s" % detail
    assert _host(engine.VERIFY_GATE) in detail, detail
    assert "timeout" in detail, detail


def test_v04_cross_below_minimum_fails():
    """交叉命中数 < ``VERIFY_MIN_CROSS`` ⇒ 失败（只有百度、其它站都不通 = 疑似假死）。"""
    session = FakeSession()
    _route_ok(session, [engine.VERIFY_GATE, engine.VERIFY_CROSS[0]])  # 仅 1 站交叉
    for spec in engine.VERIFY_CROSS[1:]:
        session.route(spec.url, requests.ConnectionError("connection refused"))

    ok, detail = engine.verify_internet(session)

    assert ok is False, detail
    for spec in engine.VERIFY_CROSS[1:]:
        assert _host(spec) in detail, detail


def test_v04_min_cross_boundary_is_two():
    """边界：交叉命中恰好 ``VERIFY_MIN_CROSS`` 站即通过（不多不少）。"""
    assert engine.VERIFY_MIN_CROSS == 2
    assert len(engine.VERIFY_CROSS) >= engine.VERIFY_MIN_CROSS

    for hits in range(0, len(engine.VERIFY_CROSS) + 1):
        session = FakeSession()
        _route_ok(session, [engine.VERIFY_GATE] + list(engine.VERIFY_CROSS[:hits]))
        for spec in engine.VERIFY_CROSS[hits:]:
            session.route(spec.url, requests.ConnectionError("connection refused"))

        ok, _ = engine.verify_internet(session)

        assert ok is (hits >= engine.VERIFY_MIN_CROSS), (
            "交叉命中 %d 站时判定错误" % hits)


# --------------------------------------------------------------------------- #
# 必须跟随重定向（真机四站裸 URL 均 3xx 跳转）
# --------------------------------------------------------------------------- #
def test_v04_site_request_follows_redirects():
    """校验请求须显式 ``allow_redirects=True``（真机裸 URL 全为 3xx，不跟随即全败）。"""
    session = FakeSession()
    spec = engine.VERIFY_GATE
    session.route(spec.url, _title_page(spec))

    engine.verify_site(session, spec)

    assert session.calls, "未发出任何请求"
    assert session.calls[0]["kwargs"].get("allow_redirects") is True, (
        "校验未跟随重定向：真机 baidu/qq/sohu 裸 URL 均 3xx，将四站全判失败")


def test_v04_unfollowed_redirect_is_not_success():
    """未跟随的 3xx 响应**不得**判为成功（防把跳转页当在线证据）。"""
    session = FakeSession()
    spec = engine.VERIFY_GATE
    session.route(spec.url, FakeResponse(302, "", url=spec.url))

    result = engine.verify_site(session, spec)

    assert result.ok is False


def test_v04_verify_internet_uses_redirect_following_for_every_site():
    """整轮校验的**每个**站点都要跟随重定向。"""
    session = FakeSession()
    _route_ok(session, [engine.VERIFY_GATE] + list(engine.VERIFY_CROSS))

    engine.verify_internet(session)

    expected = {engine.VERIFY_GATE.url} | {s.url for s in engine.VERIFY_CROSS}
    assert {c["url"] for c in session.calls} == expected
    for call in session.calls:
        assert call["kwargs"].get("allow_redirects") is True, call


def test_v04_probe_uses_strict_title_match():
    """标题不符（例如 Portal 劫持页）不得算命中。"""
    session = FakeSession()
    spec = engine.VERIFY_GATE
    session.route(spec.url, FakeResponse(200, "<HTML><TITLE>校园网认证</TITLE></HTML>",
                                         url=spec.url))

    result = engine.verify_site(session, spec)

    assert result.ok is False
    assert spec.mode == PROBE_HTML_TITLE


# --------------------------------------------------------------------------- #
# 明细：逐站可读、异常不中断
# --------------------------------------------------------------------------- #
def test_v04_detail_lists_every_site_with_reason():
    """失败明细须逐站列出「主机(错误类型/状态码)」，供日志定位。"""
    session = FakeSession()
    session.route(engine.VERIFY_GATE.url, requests.ConnectTimeout("timed out"))
    session.route(engine.VERIFY_CROSS[0].url,
                  requests.ConnectionError("getaddrinfo failed"))
    session.route(engine.VERIFY_CROSS[1].url, FakeResponse(403, "Forbidden"))
    session.route(engine.VERIFY_CROSS[2].url, requests.ConnectionError("reset"))

    ok, detail = engine.verify_internet(session)

    assert ok is False
    assert "%s(timeout)" % _host(engine.VERIFY_GATE) in detail, detail
    assert "%s(dns)" % _host(engine.VERIFY_CROSS[0]) in detail, detail
    assert "%s(403)" % _host(engine.VERIFY_CROSS[1]) in detail, detail
    assert "%s(connection)" % _host(engine.VERIFY_CROSS[2]) in detail, detail


def test_v04_one_site_exception_does_not_abort_the_round():
    """任一站点抛异常只算该站失败，其余站点仍须被校验（不得中断整轮）。"""
    session = FakeSession()
    session.route(engine.VERIFY_GATE.url, RuntimeError("boom"))   # 非 requests 异常
    _route_ok(session, engine.VERIFY_CROSS)

    ok, detail = engine.verify_internet(session)

    assert ok is False, "闸门爆炸却判通过"
    for spec in engine.VERIFY_CROSS:                 # 交叉三站仍被请求
        assert spec.url in {c["url"] for c in session.calls}
    assert _host(engine.VERIFY_GATE) in detail, detail


def test_v04_verify_internet_never_raises():
    """校验自身**绝不**抛异常（不得把监测循环炸掉）；异常一律转成失败明细。"""
    class _Boom:
        def get(self, *args, **kwargs):
            raise OSError("socket layer unavailable")

    ok, detail = engine.verify_internet(_Boom())

    assert ok is False
    assert isinstance(detail, str) and detail
    assert "OSError" in detail, detail


# --------------------------------------------------------------------------- #
# 取证锁定：站点与标题与 2026-10-16 真机一致
# --------------------------------------------------------------------------- #
def test_v04_sites_match_real_forensics_20261016():
    """锁定真机取证：闸门=百度、交叉=腾讯/网易/搜狐，标题与实测一致。"""
    assert engine.VERIFY_GATE.url == "http://www.baidu.com/"
    assert engine.VERIFY_GATE.expect == "百度一下，你就知道"
    assert engine.VERIFY_GATE.mode == PROBE_HTML_TITLE

    cross = {spec.url: spec.expect for spec in engine.VERIFY_CROSS}
    assert cross == {
        "http://www.qq.com/": "腾讯网",
        "http://www.163.com/": "网易",
        "http://www.sohu.com/": "搜狐",
    }, cross


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_v04_verify_subcommand_parses():
    """``verify`` 子命令须存在且零必需参数（只读校验）。"""
    args = engine.build_parser().parse_args(["verify"])

    assert args.command == "verify"


# --------------------------------------------------------------------------- #
# 触发点：watch 在线跃迁
# --------------------------------------------------------------------------- #
def _args(threshold=3, interval=15, cooldown=180):
    return argparse.Namespace(ssid="SNNU", yys="mobile", interval=interval,
                              threshold=threshold, cooldown=cooldown, force=False)


def _run_watch_once_series(monkeypatch, virtual_clock, online_series):
    """按脚本驱动 ``watch``：消费 ``online_series``，用尽后 ``KeyboardInterrupt`` 收尾。"""
    virtual_clock.install(monkeypatch, engine)
    monkeypatch.setattr(engine, "ensure_single_instance", lambda log: True)
    state = {"i": 0}

    def fake_internet_ok(session, probes=None, **kwargs):
        if state["i"] >= len(online_series):
            raise KeyboardInterrupt
        value = online_series[state["i"]]
        state["i"] += 1
        return (value, "http://probe/") if value else (False, "detail")

    verified: list = []
    monkeypatch.setattr(engine, "internet_ok", fake_internet_ok)
    monkeypatch.setattr(engine, "verify_internet",
                        lambda session, **kwargs: verified.append(1) or (True, "ok"))
    monkeypatch.setattr(engine, "attempt_repair", lambda session, args, log: None)

    engine.watch(object(), _args(), LOG)
    return verified


def test_v04_watch_verifies_once_on_first_online(monkeypatch, virtual_clock):
    """watch 在首次确认在线时**恰好一次**执行连接后校验。"""
    verified = _run_watch_once_series(monkeypatch, virtual_clock, [True, True, True])

    assert len(verified) == 1, "在线期间重复校验：%d 次" % len(verified)


def test_v04_watch_reverifies_after_offline_then_online(monkeypatch, virtual_clock):
    """离线→在线跃迁须重新校验（每次「连上」都校验一次）。"""
    verified = _run_watch_once_series(monkeypatch, virtual_clock,
                                      [True, False, False, True])

    assert len(verified) == 2, "跃迁未重新校验：%d 次" % len(verified)


def test_v04_watch_never_verifies_while_offline(monkeypatch, virtual_clock):
    """一直离线 ⇒ 不执行任何校验。"""
    verified = _run_watch_once_series(monkeypatch, virtual_clock, [False, False])

    assert verified == []
