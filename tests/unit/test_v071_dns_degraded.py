"""V0.7.1 阶段 D：IP + 域名双通道校验分类（DNS_DEGRADED）单元测试。

零真实网络：域名通道（``verify_internet``）与 IP 通道（``ip_channel_ok``）
全部注入假探针；只验证**三态分类语义、告警文案与证据日志**，
不改变任何门禁 / 状态机 / UU 行为（2026-11-12 用户确认的边界）。
"""

import logging
import time
import types

import campus_clash_health as clash
import campus_reconnect as engine
import campus_reconnect_tray as tray

LOG = logging.getLogger("campus")


# ---------- 假件 ----------

class _FakeLog:
    """记录 app.log 调用的假日志（消息已做 %-format 展开）。"""

    def __init__(self):
        self.records = []

    def _rec(self, level, msg, *args):
        try:
            self.records.append((level, msg % args if args else msg))
        except TypeError:
            self.records.append((level, msg))

    def info(self, msg, *args):
        self._rec(logging.INFO, msg, *args)

    def warning(self, msg, *args):
        self._rec(logging.WARNING, msg, *args)

    def error(self, msg, *args):
        self._rec(logging.ERROR, msg, *args)

    def lines(self, level=logging.WARNING):
        return [m for lvl, m in self.records if lvl >= level]


def _app(**cfg):
    app = tray.TrayApp(LOG, config=dict(tray.DEFAULT_CONFIG))
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(cfg)
    return app


def _wait_verify_done(app, timeout=3.0):
    """校验线程结束的确定性等待（无真实 sleep 依赖）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not app._verify_busy.is_set():
            return True
        time.sleep(0.01)
    return not app._verify_busy.is_set()


def _verify_app(monkeypatch, verify_result=None, verify_raises=None,
                ip_ok=False):
    """构建只测 ``_verify_online_async`` 的最小托盘应用。"""
    app = _app(clash_auto_proxy=False)
    app.log = _FakeLog()
    notices = []
    monkeypatch.setattr(app, "notify", lambda *a: notices.append("".join(a)))
    clash_calls = []
    monkeypatch.setattr(app, "_start_clash_check_async",
                        lambda ok: clash_calls.append(ok))
    if verify_raises is not None:
        def _boom(session):
            raise verify_raises
        monkeypatch.setattr(tray, "verify_internet", _boom)
    else:
        monkeypatch.setattr(tray, "verify_internet",
                            lambda session: verify_result)
    monkeypatch.setattr(tray, "ip_channel_ok", lambda **kwargs: ip_ok)
    return app, notices, clash_calls


def _clash_app(monkeypatch, proxy_ok, proxy_error=None):
    """构建只测 ``_dual_axis_clash_check`` 的最小托盘应用。"""
    app = _app(proxy_endpoint="127.0.0.1:10999", clash_auto_proxy=False)
    app.log = _FakeLog()
    monkeypatch.setattr(
        tray, "probe_via_proxy",
        lambda session, probes: clash.ProxyAxisResult(
            direct_ok=None, proxy_ok=proxy_ok, proxy_error=proxy_error))
    monkeypatch.setattr(
        tray.engine, "direct_session",
        lambda: types.SimpleNamespace(proxies={}))
    healed = []
    monkeypatch.setattr(app, "_clash_heal_execute",
                        lambda plan: healed.append(plan) or True)
    return app, healed


# ---------- 1. 三态常量契约 ----------

def test_verdict_constants_contract():
    assert engine.VERIFY_VERDICTS == ("OK", "DNS_DEGRADED", "OFFLINE")
    assert len(set(engine.VERIFY_VERDICTS)) == 3, "三态取值必须互异"
    assert engine.VERIFY_OK == "OK"
    assert engine.VERIFY_DNS_DEGRADED == "DNS_DEGRADED"
    assert engine.VERIFY_OFFLINE == "OFFLINE"


# ---------- 2. ip_channel_ok：免 DNS 的 IP 级通道 ----------

def test_ip_channel_ok_first_target_success_short_circuits():
    calls = []

    def probe(host, port, timeout):
        calls.append((host, port, timeout))
        return True

    assert engine.ip_channel_ok(tcp_probe=probe) is True
    assert len(calls) == 1, "任一点成功即短路，不得继续探测"


def test_ip_channel_ok_all_fail():
    assert engine.ip_channel_ok(tcp_probe=lambda *a: False) is False


def test_ip_channel_ok_exception_falls_through_to_next_target():
    calls = []

    def probe(host, port, timeout):
        calls.append(host)
        if host == engine.DEFAULT_TCP_TARGETS[0][0]:
            raise OSError("boom")
        return True

    assert engine.ip_channel_ok(tcp_probe=probe) is True
    assert calls == [h for h, _ in engine.DEFAULT_TCP_TARGETS], \
        "探测异常只算该点不可达，继续下一目标"


def test_ip_channel_ok_custom_targets_used_in_order():
    seen = []

    def probe(host, port, timeout):
        seen.append((host, port))
        return port == 8080

    ok = engine.ip_channel_ok(
        tcp_probe=probe, targets=[("1.1.1.1", 80), ("2.2.2.2", 8080)])
    assert ok is True
    assert seen == [("1.1.1.1", 80), ("2.2.2.2", 8080)]


def test_ip_channel_ok_default_probe_targets_and_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(
        engine, "IP_TCP_PROBE",
        lambda host, port, timeout: seen.append((host, port, timeout)) or False)

    assert engine.ip_channel_ok() is False
    assert seen == [(h, p, engine.IP_PROBE_TIMEOUT)
                    for h, p in engine.DEFAULT_TCP_TARGETS], \
        "默认复用 DEFAULT_TCP_TARGETS + IP_TCP_PROBE + IP_PROBE_TIMEOUT"


# ---------- 3. verify_internet_classified：三态分类 ----------

def test_classified_ok_when_verify_passes(monkeypatch):
    seen = []

    def fake_verify(session):
        seen.append(session)
        return True, "网关 ok"

    monkeypatch.setattr(engine, "verify_internet", fake_verify)
    ok, verdict, detail = engine.verify_internet_classified(object())

    assert (ok, verdict) == (True, engine.VERIFY_OK)
    assert detail == "网关 ok"
    assert len(seen) == 1, "verify_internet 契约不变：仍收到原 session"


def test_classified_dns_degraded_when_ip_channel_reachable(monkeypatch):
    monkeypatch.setattr(
        engine, "verify_internet",
        lambda session: (False, "闸门 www.baidu.com(timeout)"))
    ok, verdict, detail = engine.verify_internet_classified(
        object(), tcp_probe=lambda *a: True)

    assert ok is False
    assert verdict == engine.VERIFY_DNS_DEGRADED
    assert "闸门 www.baidu.com(timeout)" in detail, "域名明细保留"
    assert "IP 通道可达" in detail, "补 IP 通道证据"


def test_classified_offline_when_both_channels_fail(monkeypatch):
    monkeypatch.setattr(
        engine, "verify_internet",
        lambda session: (False, "闸门 www.baidu.com(timeout)"))
    ok, verdict, detail = engine.verify_internet_classified(
        object(), tcp_probe=lambda *a: False)

    assert (ok, verdict) == (False, engine.VERIFY_OFFLINE)
    assert detail == "闸门 www.baidu.com(timeout)", "OFFLINE 明细与旧契约一致"


def test_classified_probe_exception_means_offline(monkeypatch):
    monkeypatch.setattr(engine, "verify_internet",
                        lambda session: (False, "x"))

    def boom(*a):
        raise OSError("no socket")

    ok, verdict, _ = engine.verify_internet_classified(
        object(), tcp_probe=boom)
    assert (ok, verdict) == (False, engine.VERIFY_OFFLINE)


# ---------- 4. 托盘 _verify_online_async：三态分支 ----------

def test_tray_verify_dns_degraded_branch(monkeypatch):
    """域名失败 + IP 通道可达 ⇒ DNS_DEGRADED 告警（非假死），Clash 检查照常。"""
    app, notices, clash_calls = _verify_app(
        monkeypatch, verify_result=(False, "闸门 www.baidu.com(timeout)"),
        ip_ok=True)

    app._verify_online_async()
    assert _wait_verify_done(app)

    assert clash_calls == [False], "分类只改告警，Clash 检查触发点不变"
    assert len(notices) == 1
    assert "DNS_DEGRADED" in notices[0]
    assert "非假死" in notices[0]
    assert "疑似假死" not in notices[0], "DNS 降级不得再误报「疑似假死」"
    assert any("DNS_DEGRADED" in m and "非假死" in m for m in app.log.lines())


def test_tray_verify_offline_branch_keeps_legacy_text(monkeypatch):
    """两路都失败 ⇒ 旧「疑似假死」文案逐字保留。"""
    app, notices, clash_calls = _verify_app(
        monkeypatch, verify_result=(False, "闸门 www.baidu.com(timeout)"),
        ip_ok=False)

    app._verify_online_async()
    assert _wait_verify_done(app)

    assert clash_calls == [False]
    assert "已连上校园网，但真实网页访问失败（疑似假死）" in notices[0]
    assert all("DNS_DEGRADED" not in n for n in notices)


def test_tray_verify_ok_no_notify(monkeypatch):
    app, notices, clash_calls = _verify_app(
        monkeypatch, verify_result=(True, "网关 ok"))

    app._verify_online_async()
    assert _wait_verify_done(app)

    assert notices == []
    assert clash_calls == [True]


def test_tray_verify_exception_swallowed_no_clash_check(monkeypatch):
    """校验自身异常不得影响工作线程，也不触发 Clash 检查（ok 为 None）。"""
    app, notices, clash_calls = _verify_app(
        monkeypatch, verify_raises=OSError("no socket"))

    app._verify_online_async()
    assert _wait_verify_done(app)

    assert notices == []
    assert clash_calls == []
    assert any("异常" in m for m in app.log.lines())


# ---------- 5. _dual_axis_clash_check：矛盾结论的证据日志 ----------

def test_clash_contradiction_logs_dns_evidence(monkeypatch):
    """直连失败 + 代理通（unknown）+ IP 通道可达 ⇒ 补证据日志，verdict 不变。"""
    app, healed = _clash_app(monkeypatch, proxy_ok=True)
    monkeypatch.setattr(tray, "ip_channel_ok", lambda **kwargs: True)

    app._dual_axis_clash_check(False)

    joined = "\n".join(app.log.lines())
    assert "证据补充" in joined
    assert "DNS 降级" in joined
    assert "远端解析" in joined
    assert healed == [], "verdict 保持 unknown：不触发 Clash 自愈"


def test_clash_contradiction_no_evidence_when_ip_unreachable(monkeypatch):
    app, healed = _clash_app(monkeypatch, proxy_ok=True)
    monkeypatch.setattr(tray, "ip_channel_ok", lambda **kwargs: False)

    app._dual_axis_clash_check(False)

    assert all("证据补充" not in m for m in app.log.lines())
    assert healed == []


def test_clash_healthy_no_evidence_even_with_ip_ok(monkeypatch):
    """双轴 healthy ⇒ 提前返回，连证据日志都不出现。"""
    app, healed = _clash_app(monkeypatch, proxy_ok=True)
    monkeypatch.setattr(tray, "ip_channel_ok", lambda **kwargs: True)

    app._dual_axis_clash_check(True)

    assert all("证据补充" not in m for m in app.log.lines())
    assert app._clash_heal_attempts == 0


def test_clash_offline_verdict_no_evidence(monkeypatch):
    """直连失败 + 代理不通 ⇒ offline 判定（非 unknown），不补矛盾证据。"""
    app, healed = _clash_app(monkeypatch, proxy_ok=False,
                             proxy_error="timeout")
    monkeypatch.setattr(tray, "ip_channel_ok", lambda **kwargs: True)

    app._dual_axis_clash_check(False)

    assert all("证据补充" not in m for m in app.log.lines())
    assert healed == []


def test_clash_evidence_ip_probe_exception_swallowed(monkeypatch):
    """证据复核自身异常只算「IP 不可达」，绝不影响双轴自愈主流程。"""
    app, healed = _clash_app(monkeypatch, proxy_ok=True)

    def boom(**kwargs):
        raise OSError("x")

    monkeypatch.setattr(tray, "ip_channel_ok", boom)

    app._dual_axis_clash_check(False)   # 不得抛出

    assert all("证据补充" not in m for m in app.log.lines())
    assert healed == []
