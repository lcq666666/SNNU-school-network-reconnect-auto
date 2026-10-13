# -*- coding: utf-8 -*-
"""F14/F08 共享重连服务行为（``repair_service``）。

验证「无认证需要不 POST」、目标网络判定、端口/Portal 失败分类、凭据缺失、
取消令牌、需人工处理与公网复核（已认证但公网异常）等分支。全程假件、零真实 I/O。
"""
from __future__ import annotations

from campus_core import (E_AUTH_CAPTCHA, E_CANCELLED, E_CRED_MISSING,
                         E_NETS_FAILED, E_PORTAL_UNREACHABLE, PortalStatus,
                         LoginOutcome, RepairAdapters, RepairOptions,
                         repair_service)


def _probe_from(sequence):
    """按序弹出的 probe_online（最后一项重复）。"""
    state = {"i": 0}

    def probe(_session):
        i = state["i"]
        state["i"] += 1
        if i < len(sequence):
            return sequence[i]
        return sequence[-1]

    return probe


def _run(*, wifi=("SNNU", "已连接"), port=(8601, "fake"), reachable=True,
         probe=(False, ""), creds=("123456", "pw"), login=(False, "stop"),
         cancelled=False, force=False, reconnect_wifi=False):
    captured = {"logins": [], "sleeps": []}
    probe_fn = _probe_from(probe) if isinstance(probe, list) else (lambda s: probe)
    adapters = RepairAdapters(
        wifi_state=lambda: wifi,
        wifi_reconnect=lambda ssid: True,
        resolve_port=lambda session, requested: port,
        portal_reachable=lambda session, p: reachable,
        probe_online=probe_fn,
        login=lambda *a, **k: (captured["logins"].append(a), login)[1],
    )
    options = RepairOptions(ssid="SNNU", operator="mobile", account_format="plain",
                            reconnect_wifi=reconnect_wifi, browser_fallback=False,
                            force=force)
    result = repair_service(object(), options, lambda: 0.0, adapters,
                            lambda: creds, cancelled=lambda: cancelled,
                            sleep=lambda s: captured["sleeps"].append(s))
    return result, captured


def test_service_no_post_when_already_online():
    """F08/T13：公网已连通时零认证请求。"""
    result, captured = _run(probe=(True, "url"))
    assert result.network_ok is True
    assert result.status is PortalStatus.ONLINE
    assert captured["logins"] == [], "已在线仍提交了认证"


def test_service_force_skips_precheck():
    result, captured = _run(probe=(True, "url"), force=True, login=(False, "x"))
    assert len(captured["logins"]) == 1


def test_service_non_target_network():
    result, captured = _run(wifi=("OtherWiFi", "已连接"))
    assert result.status is PortalStatus.NON_TARGET
    assert result.error_code == E_NETS_FAILED
    assert captured["logins"] == []


def test_service_wifi_disconnected():
    result, _ = _run(wifi=(None, "未连接"))
    assert result.status is PortalStatus.WIFI_DISCONNECTED
    assert result.error_code == E_NETS_FAILED


def test_service_port_resolution_failure():
    result, captured = _run(port=(None, "无法确定端口"))
    assert result.error_code == E_PORTAL_UNREACHABLE
    assert captured["logins"] == []


def test_service_portal_unreachable():
    result, captured = _run(reachable=False)
    assert result.error_code == E_PORTAL_UNREACHABLE
    assert captured["logins"] == []


def test_service_credentials_missing_needs_user():
    result, captured = _run(creds=None)
    assert result.error_code == E_CRED_MISSING
    assert result.needs_user is True
    assert result.retryable is False
    assert captured["logins"] == []


def test_service_cancelled_before_auth():
    result, captured = _run(cancelled=True)
    assert result.error_code == E_CANCELLED
    assert captured["logins"] == []


def test_service_captcha_needs_user_no_retry():
    login = LoginOutcome(False, "验证码", status=PortalStatus.NEEDS_USER,
                         error_code=E_AUTH_CAPTCHA)
    result, _ = _run(login=login)
    assert result.needs_user is True
    assert result.retryable is False
    assert result.status is PortalStatus.NEEDS_USER


def test_service_post_check_success():
    """认证成功且公网复核通过。"""
    result, captured = _run(probe=[(False, ""), (True, "url")], login=(True, "ok"))
    assert result.network_ok is True and result.auth_ok is True
    assert result.status is PortalStatus.ONLINE
    assert captured["sleeps"] == [3]


def test_service_authenticated_but_degraded():
    """Portal 已认证但公网仍不通 ⇒ 独立报告为降级状态。"""
    result, captured = _run(probe=(False, ""), login=(True, "ok"))
    assert result.auth_ok is True
    assert result.network_ok is False
    assert result.status is PortalStatus.AUTHENTICATED_DEGRADED
    assert captured["sleeps"] == [3, 5, 8]
