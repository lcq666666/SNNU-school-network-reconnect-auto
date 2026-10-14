# -*- coding: utf-8 -*-
"""V0.3 P4 离线集成闭合回路（I-4-*）：SNNU 备用链路 + 接口绑定认证。

用 **V0.2 真实认证引擎** ``campus_core.repair_service``（不重写、只封装调用，
AGENTS §3.1）配合 :class:`tests.fakes.FakeSession` 的 Portal 路由，验证：

- 出口已确认为 SNNU 接口后，认证请求确实**经绑定会话**发出（``session.mounts``），
  账号与本次提交**完整一致**（F06/F17），成功后公网复核通过；
- 出口**未**确认为 SNNU 接口时，全程**零认证 POST、零凭据**（AGENTS §3.6）；
- 已有会话（状态页显示本账号在线）时不重复 POST，如实标记 ``already_authenticated``；
- 验证码/失败场景进入 ``NEEDS_USER``，不无限重试。

全程零真实网络 / 零真实凭据 / 零真实注册表 / 零真实 sleep（``VirtualClock``）。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import campus_reconnect as engine
from campus_core import PortalStatus, RepairAdapters, repair_service
from campus_snnu import (BackupStage, SnnuAuthError, SnnuBackupLink, SnnuOps)
from tests.fakes import (FakeResponse, FakeSession, FakeSnnuLink, VirtualClock)

LOG = logging.getLogger("campus")
PORT = 8601
WIFI_IF = 5
WIFI_SRC = "10.252.37.88"
ACCOUNT = "20230001"
LOGIN_JSP = "http://202.117.144.205:8601/snnuportal/login.jsp"
LOGIN_ACTION = "http://202.117.144.205:8601/snnuportal/login"
USERSTATUS = "http://202.117.144.205:8601/snnuportal/userstatus.jsp"


class _ScriptedProbe:
    """按序返回公网探测结果的假探测（脚本化"认证前离线 / 认证后在线"）。"""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self, session):
        self.calls += 1
        if not self.values:
            return True, "msftconnecttest"
        return bool(self.values.pop(0)), "msftconnecttest"


def _session(*, pre_body="<html>请先登录</html>", post_body=None,
             encoding="gb2312") -> FakeSession:
    session = FakeSession()
    session.route(LOGIN_JSP, FakeResponse(200, "<html>login</html>"))
    session.route(USERSTATUS, FakeResponse(200, pre_body, encoding=encoding))
    if post_body is not None:
        session.route(LOGIN_ACTION,
                      FakeResponse(200, post_body, encoding=encoding))
    return session


def _link(session, clock, probe, *, allow_auth=True, if_index=WIFI_IF,
          source_ip=WIFI_SRC, route=None, repair=None,
          status=None) -> SnnuBackupLink:
    adapters = RepairAdapters(
        wifi_state=lambda: ("SNNU", "connected"),
        wifi_reconnect=lambda ssid: True,
        resolve_port=engine.resolve_port,
        portal_reachable=engine.portal_reachable,
        probe_online=probe,
        login=engine.portal_login,
    )

    def _default_repair(bound_session, options):
        return repair_service(bound_session, options, clock.monotonic, adapters,
                              lambda: (ACCOUNT, "secret-pw"),
                              sleep=clock.sleep, log=LOG)

    fake = FakeSnnuLink()
    ops = SnnuOps(
        wifi_state=fake.wifi_state,
        wifi_reconnect=fake.wifi_reconnect,
        ip_ready=fake.ip_ready,
        route_lookup=route or (lambda target: SimpleNamespace(
            if_index=WIFI_IF, source_ip=WIFI_SRC)),
        resolve_port=lambda s, requested: (PORT, "测试端口"),
        portal_reachable=lambda s, port: True,
        read_status=lambda s, port: status,
        repair=repair if repair is not None else _default_repair,
    )
    return SnnuBackupLink(
        ops=ops, session=session, target_ssid="SNNU", if_index=if_index,
        source_ip=source_ip, allow_auth=allow_auth, reconnect_wifi=False,
        port=PORT, sleep=fake.sleep)


# --------------------------------------------------------------------------- #
# I-4-01 闭合回路：关联 → 出口确认 → 需认证 → 受控认证成功
# --------------------------------------------------------------------------- #
def test_i4_01_controlled_auth_success_through_bound_session():
    session = _session(pre_body="<html>请先登录</html>",
                       post_body="当前登录账号 20230001")
    clock = VirtualClock()
    probe = _ScriptedProbe([False, True])       # 认证前离线 → 认证后在线
    result = _link(session, clock, probe).run()

    assert result.ok is True
    assert result.stage is BackupStage.DONE
    assert result.credentials_sent is True
    assert result.repair is not None and result.repair.auth_ok is True
    assert result.repair.network_ok is True
    assert result.repair.status is PortalStatus.ONLINE
    assert result.repair.already_authenticated is False

    # 认证确实经"绑定到 SNNU 接口"的会话发出（方案 §6.2）
    assert len(session.mounts) == 2
    posts = session.posts()
    assert len(posts) == 1
    assert posts[0]["url"] == LOGIN_ACTION
    payload = posts[0]["kwargs"]["data"]
    assert payload["account"] == ACCOUNT          # 完整一致（F06）
    assert payload["password"] == "secret-pw"
    assert payload["yys"] == "mobile"
    # 所有 GET 均指向 Portal（无外泄）
    assert all(call["url"].startswith("http://202.117.144.205:8601/")
               for call in session.calls)
    # 零真实 sleep：仅公网复核前的一次虚拟等待
    assert clock.sleeps == [3.0]


# --------------------------------------------------------------------------- #
# I-4-02 出口未确认：全程零凭据、零 POST
# --------------------------------------------------------------------------- #
def test_i4_02_unconfirmed_egress_zero_credentials():
    session = _session(pre_body="<html>请先登录</html>",
                       post_body="当前登录账号 20230001")
    clock = VirtualClock()
    probe = _ScriptedProbe([False, True])
    result = _link(session, clock, probe,
                   route=lambda target: SimpleNamespace(if_index=10,
                                                        source_ip="192.168.124.9")
                   ).run()

    assert result.error == SnnuAuthError.AUTH_PATH_UNCONFIRMED.value
    assert result.stage is BackupStage.EGRESS
    assert result.credentials_sent is False
    assert session.posts() == []
    assert session.calls == []          # 连只读 Portal 请求都未发出
    assert session.mounts == []         # 未绑定
    assert probe.calls == 0
    assert clock.sleeps == []


# --------------------------------------------------------------------------- #
# I-4-03 已有会话：不重复 POST，如实标记 already_authenticated
# --------------------------------------------------------------------------- #
def test_i4_03_existing_session_no_post():
    session = _session(pre_body="当前登录账号：20230001")   # 状态页显示本账号在线
    clock = VirtualClock()
    probe = _ScriptedProbe([False, True])
    result = _link(session, clock, probe).run()

    assert result.ok is True
    assert result.repair is not None
    assert result.repair.already_authenticated is True
    assert session.posts() == []          # 复用已有会话，未重复认证


# --------------------------------------------------------------------------- #
# I-4-05 验证码：需人工处理，单次 POST，不无限重试
# --------------------------------------------------------------------------- #
def test_i4_05_captcha_needs_user_single_post():
    session = _session(pre_body="<html>请先登录</html>", post_body="请输入验证码")
    clock = VirtualClock()
    probe = _ScriptedProbe([False])
    result = _link(session, clock, probe).run()

    assert result.ok is False
    assert result.error == SnnuAuthError.AUTH_FAILED.value
    assert result.credentials_sent is True
    assert result.repair is not None
    assert result.repair.needs_user is True
    assert result.repair.status is PortalStatus.NEEDS_USER
    assert len(session.posts()) == 1      # 只提交一次，不无限重试


# --------------------------------------------------------------------------- #
# I-4-06 认证引擎异常：如实报告，不让调度器崩溃
# --------------------------------------------------------------------------- #
def test_i4_06_repair_engine_exception_is_reported():
    def _boom(bound_session, options):
        raise RuntimeError("engine down")

    session = _session(pre_body="<html>请先登录</html>")
    clock = VirtualClock()
    result = _link(session, clock, _ScriptedProbe([False]),
                   repair=_boom).run()

    assert result.error == SnnuAuthError.AUTH_FAILED.value
    assert result.stage is BackupStage.AUTH
    assert result.ok is False
    assert session.posts() == []
    assert "engine down" in result.detail


# --------------------------------------------------------------------------- #
# I-4-07 只读识别（allow_auth=False）：即便出口已确认也不提交凭据
# --------------------------------------------------------------------------- #
def test_i4_07_readonly_identify_never_posts_even_when_confirmed():
    session = _session(pre_body="当前登录账号 20230001")
    clock = VirtualClock()
    result = _link(session, clock, _ScriptedProbe([False]), allow_auth=False,
                   status=SimpleNamespace(status=PortalStatus.ONLINE)).run()

    assert result.credentials_sent is False
    assert result.status is PortalStatus.ONLINE   # 经只读状态识别
    assert session.posts() == []
    assert len(session.mounts) == 2               # 已绑定，但未认证
