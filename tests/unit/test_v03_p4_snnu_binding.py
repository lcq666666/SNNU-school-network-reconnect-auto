# -*- coding: utf-8 -*-
"""V0.3 P4 SNNU 备用链路与接口绑定认证——单元/离线测试（U-4-*）。

覆盖：
- 关联分类（``classify_snnu_link``）与出口证明（``confirm_egress``）纯函数分支；
- 源地址绑定（``bind_source_address``）；
- :class:`campus_snnu.SnnuBackupLink` 编排各失败/成功路径，**重点看守**：
  出口未确认为 SNNU 接口时**零凭据提交、零请求、零绑定**（AGENTS §3.6）；
- P3 执行器协议在 SNNU 侧的实现（``PORTAL_POST`` / ``WIFI_RECONNECT``）。

全程零真实网络 / 零真实凭据 / 零真实注册表 / 零真实 sleep（用
:class:`tests.fakes.FakeSnnuLink` 与 :class:`tests.fakes.FakeSession`）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from campus_core import PortalStatus
from campus_failover import OpKind, OpRequest
from campus_network import bind_source_address
from campus_snnu import (AUTH_PATH_UNCONFIRMED_TEXT, BackupLinkResult,
                         BackupStage, EgressProof, SnnuAuthError,
                         SnnuBackupExecutor, SnnuBackupLink, SnnuLinkState,
                         SnnuOps, classify_snnu_link, confirm_egress,
                         format_backup_result)
from tests.fakes import FakeSession, FakeSnnuLink

PORT = 8601
WIFI_IF = 5
WIFI_SRC = "10.252.37.88"


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #
def _route(if_index: int = WIFI_IF, source_ip: str = WIFI_SRC):
    return lambda target: SimpleNamespace(if_index=if_index, source_ip=source_ip)


def _ops(fake: FakeSnnuLink, *, route=None, resolve=(PORT, "测试端口"),
         reachable=True, status=None, repair=None) -> SnnuOps:
    return SnnuOps(
        wifi_state=fake.wifi_state,
        wifi_reconnect=fake.wifi_reconnect,
        ip_ready=fake.ip_ready,
        route_lookup=_route() if route is None else route,
        resolve_port=lambda session, requested: resolve,
        portal_reachable=lambda session, port: reachable,
        read_status=lambda session, port: status,
        repair=repair,
    )


def _run(fake: FakeSnnuLink, session=None, *, route=None, allow_auth=False,
         if_index=WIFI_IF, source_ip=WIFI_SRC, resolve=(PORT, "测试端口"),
         reachable=True, status=None, repair=None, reconnect_wifi=True,
         reconnect_attempts=3):
    session = session if session is not None else FakeSession()
    ops = _ops(fake, route=route, resolve=resolve, reachable=reachable,
               status=status, repair=repair)
    link = SnnuBackupLink(
        ops=ops, session=session, target_ssid="SNNU", if_index=if_index,
        source_ip=source_ip, allow_auth=allow_auth,
        reconnect_wifi=reconnect_wifi, port=None,
        reconnect_attempts=reconnect_attempts, wait_interval=5.0,
        sleep=fake.sleep)
    return link.run(), session


# --------------------------------------------------------------------------- #
# U-4-01 关联分类
# --------------------------------------------------------------------------- #
def test_u4_01_classify_snnu_link_all_branches():
    assert classify_snnu_link("SNNU", "connected", has_ipv4=True) \
        is SnnuLinkState.ASSOCIATED
    assert classify_snnu_link("SNNU", "connected", has_ipv4=False) \
        is SnnuLinkState.ASSOCIATED_NO_IP
    # 未提供 IP 信息时按"已关联"处理（不臆断无 IP）
    assert classify_snnu_link("SNNU", "connected") is SnnuLinkState.ASSOCIATED
    assert classify_snnu_link(None, "disconnected") is SnnuLinkState.DISCONNECTED
    assert classify_snnu_link(None, "no-wireless") is SnnuLinkState.NO_WIRELESS
    assert classify_snnu_link("Other", "connected", has_ipv4=True) \
        is SnnuLinkState.NON_TARGET
    # F17：大小写不同即非目标（不得把 snnu 误当 SNNU 提交凭据）
    assert classify_snnu_link("snnu", "connected", has_ipv4=True) \
        is SnnuLinkState.NON_TARGET
    assert classify_snnu_link(None, "non-windows") is SnnuLinkState.UNKNOWN
    assert classify_snnu_link(None, "error") is SnnuLinkState.UNKNOWN
    assert classify_snnu_link(None, "unknown") is SnnuLinkState.UNKNOWN
    assert classify_snnu_link(None, "") is SnnuLinkState.UNKNOWN
    assert SnnuLinkState.ASSOCIATED.usable is True
    assert SnnuLinkState.ASSOCIATED_NO_IP.associated is True
    assert SnnuLinkState.ASSOCIATED_NO_IP.usable is False


# --------------------------------------------------------------------------- #
# U-4-02 出口证明分支
# --------------------------------------------------------------------------- #
def test_u4_02_confirm_egress_branches():
    confirmed = confirm_egress(_route(WIFI_IF), WIFI_IF)
    assert confirmed.proof is EgressProof.CONFIRMED and confirmed.ok
    assert confirmed.egress_if_index == WIFI_IF

    other = confirm_egress(_route(10, "192.168.124.9"), WIFI_IF)
    assert other.proof is EgressProof.UNCONFIRMED and not other.ok
    assert other.egress_if_index == 10

    assert confirm_egress(None, WIFI_IF).proof is EgressProof.UNAVAILABLE
    assert confirm_egress(_route(), None).proof is EgressProof.UNAVAILABLE
    assert confirm_egress(lambda target: None, WIFI_IF).proof \
        is EgressProof.UNAVAILABLE
    assert confirm_egress(lambda target: SimpleNamespace(if_index=None),
                          WIFI_IF).proof is EgressProof.UNAVAILABLE

    def _boom(target):
        raise RuntimeError("find-netroute 失败")

    assert confirm_egress(_boom, WIFI_IF).proof is EgressProof.UNAVAILABLE
    assert confirmed.target == "202.117.144.205"


# --------------------------------------------------------------------------- #
# U-4-03 源地址绑定
# --------------------------------------------------------------------------- #
def test_u4_03_bind_source_address_requires_source_ip():
    session = FakeSession()
    assert bind_source_address(session, None) is False
    assert bind_source_address(session, "") is False
    assert session.mounts == []
    assert bind_source_address(session, WIFI_SRC) is True
    # http:// 与 https:// 各挂一个绑定适配器
    assert len(session.mounts) == 2


# --------------------------------------------------------------------------- #
# U-4-04 出口未确认：零凭据、零请求、零绑定（AGENTS §3.6 核心看守）
# --------------------------------------------------------------------------- #
def test_u4_04_unconfirmed_egress_never_sends_credentials():
    fake = FakeSnnuLink()
    result, session = _run(fake, route=_route(10, "192.168.124.9"))
    assert result.error == SnnuAuthError.AUTH_PATH_UNCONFIRMED.value
    assert result.stage is BackupStage.EGRESS
    assert result.credentials_sent is False
    assert AUTH_PATH_UNCONFIRMED_TEXT in result.detail
    assert session.calls == [] and session.posts() == []
    assert session.mounts == []


def test_u4_04b_egress_unavailable_also_blocks_credentials():
    fake = FakeSnnuLink()
    result, session = _run(fake, route=None, if_index=None)
    assert result.error == SnnuAuthError.AUTH_PATH_UNCONFIRMED.value
    assert result.credentials_sent is False
    assert session.calls == []


# --------------------------------------------------------------------------- #
# U-4-05 出口确认 + 只读识别：零凭据
# --------------------------------------------------------------------------- #
def test_u4_05_confirmed_egress_readonly_identify():
    fake = FakeSnnuLink()
    result, session = _run(fake,
                           status=SimpleNamespace(status=PortalStatus.AUTH_REQUIRED))
    assert result.ok is True
    assert result.stage is BackupStage.DONE
    assert result.status is PortalStatus.AUTH_REQUIRED
    assert result.credentials_sent is False
    assert result.egress.proof is EgressProof.CONFIRMED
    assert len(session.mounts) == 2          # 已绑定 SNNU 接口
    assert session.posts() == []             # 只读识别不提交凭据


def test_u4_05b_identify_online_and_needs_user():
    fake = FakeSnnuLink()
    online, _ = _run(fake, status=SimpleNamespace(status=PortalStatus.ONLINE))
    assert online.ok is True and online.status is PortalStatus.ONLINE
    needs, _ = _run(FakeSnnuLink(),
                    status=SimpleNamespace(status=PortalStatus.NEEDS_USER))
    assert needs.status is PortalStatus.NEEDS_USER
    assert needs.credentials_sent is False
    assert "需人工处理" in needs.detail


# --------------------------------------------------------------------------- #
# U-4-06..09 关联/重连路径
# --------------------------------------------------------------------------- #
def test_u4_06_no_wireless_adapter():
    fake = FakeSnnuLink([(None, "no-wireless", False)])
    result, _ = _run(fake)
    assert result.error == SnnuAuthError.NO_WIRELESS.value
    assert result.stage is BackupStage.LINK
    assert fake.reconnect_calls == []


def test_u4_07_non_target_network():
    fake = FakeSnnuLink([("SNNU-Guest", "connected", True)])
    result, _ = _run(fake)
    assert result.error == SnnuAuthError.NON_TARGET.value
    assert "非目标" in result.detail


def test_u4_08_disconnected_then_reconnect_succeeds():
    fake = FakeSnnuLink([(None, "disconnected", False),
                         ("SNNU", "connected", True)])
    result, _ = _run(fake)
    assert result.ok is True
    assert fake.reconnect_calls == ["SNNU"]
    assert result.link.reconnected is True
    assert result.link.state is SnnuLinkState.ASSOCIATED


def test_u4_09_reconnect_failure_is_reported():
    fake = FakeSnnuLink([(None, "disconnected", False)], reconnect_result=False)
    result, _ = _run(fake)
    assert result.error == SnnuAuthError.WIFI_RECONNECT_FAILED.value
    assert fake.reconnect_calls == ["SNNU"]
    assert result.link.reconnected is False


# --------------------------------------------------------------------------- #
# U-4-10..11 IP 就绪等待
# --------------------------------------------------------------------------- #
def test_u4_10_wait_for_ip_then_continue():
    fake = FakeSnnuLink([("SNNU", "connected", False),
                         ("SNNU", "connected", True)])
    result, _ = _run(fake, status=SimpleNamespace(status=PortalStatus.ONLINE))
    assert result.ok is True
    assert fake.sleeps == [5.0]              # 有界等待一次（零真实 sleep）
    assert result.link.state is SnnuLinkState.ASSOCIATED


def test_u4_11_no_ip_exhausts_bounded_wait():
    fake = FakeSnnuLink([("SNNU", "connected", False)])
    result, _ = _run(fake, reconnect_attempts=3)
    assert result.error == SnnuAuthError.NO_IP.value
    assert len(fake.sleeps) == 3
    assert result.credentials_sent is False


# --------------------------------------------------------------------------- #
# U-4-12..14 绑定要素缺失 / 未授权重连 / 未接入认证引擎
# --------------------------------------------------------------------------- #
def test_u4_12_missing_source_ip_blocks_binding():
    fake = FakeSnnuLink()
    result, session = _run(fake, source_ip=None,
                           route=_route(WIFI_IF, None))
    assert result.error == SnnuAuthError.AUTH_PATH_UNCONFIRMED.value
    assert result.credentials_sent is False
    assert session.mounts == []


def test_u4_13_no_reconnect_flag_does_not_attempt():
    fake = FakeSnnuLink([(None, "disconnected", False)])
    result, _ = _run(fake, reconnect_wifi=False)
    assert result.error == SnnuAuthError.LINK_NOT_ASSOCIATED.value
    assert fake.reconnect_calls == []


def test_u4_14_auth_without_engine_is_rejected():
    fake = FakeSnnuLink()
    result, session = _run(fake, allow_auth=True, repair=None)
    assert result.error == SnnuAuthError.AUTH_FAILED.value
    assert result.credentials_sent is False
    assert session.posts() == []


# --------------------------------------------------------------------------- #
# U-4-15..16 Portal 端口解析 / 可达
# --------------------------------------------------------------------------- #
def test_u4_15_portal_port_unresolved():
    fake = FakeSnnuLink()
    result, _ = _run(fake, resolve=(None, "根页面未发现可识别跳转"))
    assert result.error == SnnuAuthError.PORTAL_UNREACHABLE.value
    assert result.stage is BackupStage.PORTAL


def test_u4_16_portal_unreachable():
    fake = FakeSnnuLink()
    result, _ = _run(fake, reachable=False)
    assert result.error == SnnuAuthError.PORTAL_UNREACHABLE.value
    assert result.credentials_sent is False


# --------------------------------------------------------------------------- #
# U-4-20..23 P3 执行器协议在 SNNU 侧的实现
# --------------------------------------------------------------------------- #
def _executor(portal, wifi=True) -> SnnuBackupExecutor:
    return SnnuBackupExecutor(portal_action=lambda: portal,
                              wifi_action=lambda: wifi)


def test_u4_20_executor_portal_post():
    assert _executor(BackupLinkResult(ok=True)).execute(
        OpRequest(OpKind.PORTAL_POST, "r")) is True
    out = _executor(BackupLinkResult(ok=False, error="E_X")).execute(
        OpRequest(OpKind.PORTAL_POST, "r"))
    assert isinstance(out, str) and "E_X" in out


def test_u4_21_executor_wifi_reconnect():
    assert _executor(BackupLinkResult(ok=True), wifi=True).execute(
        OpRequest(OpKind.WIFI_RECONNECT, "r")) is True
    out = _executor(BackupLinkResult(ok=True), wifi=False).execute(
        OpRequest(OpKind.WIFI_RECONNECT, "r"))
    assert isinstance(out, str) and "失败" in out


def test_u4_22_executor_rejects_metric_and_clash():
    executor = _executor(BackupLinkResult(ok=True))
    for kind in (OpKind.METRIC_WRITE, OpKind.CLASH_TOUCH):
        out = executor.execute(OpRequest(kind, "r"))
        assert isinstance(out, str) and "P5" in out


def test_u4_23_executor_swallows_action_exceptions():
    def _boom():
        raise RuntimeError("boom")

    executor = SnnuBackupExecutor(portal_action=_boom, wifi_action=_boom)
    assert isinstance(executor.execute(OpRequest(OpKind.PORTAL_POST, "r")), str)
    assert isinstance(executor.execute(OpRequest(OpKind.WIFI_RECONNECT, "r")),
                      str)


# --------------------------------------------------------------------------- #
# U-4-24..25 契约与输出
# --------------------------------------------------------------------------- #
def test_u4_24_result_json_contract():
    fake = FakeSnnuLink()
    result, _ = _run(fake, status=SimpleNamespace(status=PortalStatus.ONLINE))
    payload = result.to_dict()
    assert payload["contract_version"] == "v03-p4-snnu-1"
    assert payload["stage"] == "DONE"
    assert payload["allow_auth"] is False
    assert payload["credentials_sent"] is False
    assert payload["link"]["state"] == "ASSOCIATED"
    assert payload["egress"]["proof"] == "CONFIRMED"
    assert payload["binding"]["if_index"] == WIFI_IF
    assert payload["portal"]["port"] == PORT
    assert payload["status"] == "ONLINE"
    assert payload["repair"] is None
    assert set(payload) >= {"contract_version", "stage", "ok", "error", "detail",
                            "link", "binding", "egress", "portal", "status",
                            "repair", "credentials_sent", "evidence"}
    json.dumps(payload, ensure_ascii=False)     # 必须可序列化


def test_u4_25_format_backup_result_is_human_readable():
    fake = FakeSnnuLink()
    result, _ = _run(fake, status=SimpleNamespace(status=PortalStatus.AUTH_REQUIRED))
    text = format_backup_result(result)
    assert "V0.3 P4 SNNU 备用链路与接口绑定认证" in text
    assert "只读识别" in text
    assert "是否提交凭据: 否" in text
    assert "出口证明" in text and "CONFIRMED" in text
    assert "AUTH_REQUIRED" in text
