# -*- coding: utf-8 -*-
"""V0.5-5 前端 UI 重构（状态映射 + 仪表盘接线 + 手动操作）。

方案 §4（V0.5-5 出口条件）：**UI 状态映射全绿；网络操作仍在后台线程**；
§7 测试计划：``test_v05_ui_state.py`` 覆盖
「NetworkHealth / FailoverState / UU state → UI state 映射；状态变化不阻塞 UI」。

本文件分四组：

- ``UI0x``：纯映射（常量对齐 / 健康态 / 网卡卡片 / 恢复流水线 / 总体状态灯）；
- ``UI5x``：**纯函数 / 不阻塞**：``build_ui_state`` 零 I/O、``ui_state()`` 不探测；
- ``UI6x``：托盘接线（``ui_state()`` 反映快照、菜单项、后台线程手动操作）；
- ``UI7x``：``立即检测`` 命令（立刻跑一轮探测）。

约束：零真实网络 / 零真实进程 / 零真实注册表写入 / 零真实凭据读取 / 零真实 sleep。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pytest

import campus_reconnect as cr
import campus_reconnect_tray as tray
import campus_remote_app as cra
import campus_ui_state as ui
from campus_failover import FailoverMode, FailoverState, TickResult
from campus_network import (EgressInfo, HealthAxis, HealthState, InterfaceHealth,
                            NetworkHealthSnapshot, Recommendation, SystemHealth)
from campus_tray_failover import FailoverStatus
from campus_tray_observer import ObservationResult
from tests.fakes import FakeRemoteAppOps
from tests.unit.test_tray_worker import wait_until

LOG = logging.getLogger("campus")

H = HealthState.HEALTHY
D = HealthState.LINK_DOWN
C = HealthState.CAPTIVE_PORTAL
IU = HealthState.INTERNET_UNREACHABLE
PU = HealthState.PROBE_UNRELIABLE

ETH_IF, WIFI_IF = 12, 14
ETH_IP, WIFI_IP = "192.168.1.20", "10.20.30.40"
ETH_DNS, WIFI_DNS = ("192.168.1.1",), ("10.20.30.1",)

UU_PROCESSES = [
    {"pid": 100, "process_name": "GameViewer",
     "path": r"C:\_study\GameViewer\GameViewer.exe"},
    {"pid": 200, "process_name": "GameViewerLauncher",
     "path": r"C:\_study\GameViewer\GameViewerLauncher.exe"},
    {"pid": 300, "process_name": "GameViewerServer",
     "path": r"C:\_study\GameViewer\GameViewerServer.exe"},
]
UU_RUN_ENTRIES = [{"name": "GameViewer",
                   "command": r'"C:\_study\GameViewer\GameViewer.exe" --auto-run'}]


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _health(axis: HealthAxis, state: Optional[HealthState], if_index: int,
            source_ip: str, dns, interface: str) -> InterfaceHealth:
    return InterfaceHealth(axis=axis, interface=interface, if_index=if_index,
                           state=state, source_ip=source_ip,
                           dns_servers=list(dns))


def snapshot(eth: Optional[HealthState] = H, wifi: Optional[HealthState] = D,
             egress_if: Optional[int] = ETH_IF, *,
             eth_ip: str = ETH_IP, wifi_ip: str = WIFI_IP,
             eth_dns=ETH_DNS, wifi_dns=WIFI_DNS) -> NetworkHealthSnapshot:
    """构造完整三轴快照（零 I/O）。"""
    ethernet = None if eth is None else _health(
        HealthAxis.ETHERNET_HEALTH, eth, ETH_IF, eth_ip, eth_dns, "以太网")
    wifi_axis = None if wifi is None else _health(
        HealthAxis.WIFI_HEALTH, wifi, WIFI_IF, wifi_ip, wifi_dns, "WLAN")
    egress = None if egress_if is None else EgressInfo(
        target="1.1.1.1", source_ip=eth_ip, if_index=egress_if)
    return NetworkHealthSnapshot(
        generated_at="2026-10-16T00:00:00+08:00", host="test-host",
        ethernet=ethernet, wifi=wifi_axis,
        system=SystemHealth(online=egress_if is not None, source="fake", egress=egress),
        recommendation=Recommendation(preferred=None, reason="test",
                                      would_switch=False))


def verified(at: float, role: str = "ethernet", *, ok: bool = True,
             reliability: str = cr.PATH_OK, captive: bool = False,
             source_ip: str = ETH_IP, sites: bool = True) -> Tuple[float, Any]:
    """构造一条 ``(at, result)`` 验证记录（与 ``DualPathStatus.ethernet`` 同构）。"""
    result = cr.PathVerificationResult(
        verified=bool(ok), role=role, source_ip=source_ip,
        reason="离线放行证据" if ok else "闸门未通过",
        reliability=reliability, captive_portal=captive,
        successful_sites=("www.baidu.com", "www.qq.com", "www.163.com") if (ok and sites)
        else (),
        failed_sites=() if ok else ("www.baidu.com",))
    return (at, result)


class ScriptedVerifier:
    """最小「接口受限验证」假件：记录请求，按 ``ok`` 返回结论（零真实网络）。"""

    def __init__(self, *, ok: bool = True, gate_ok: bool = True) -> None:
        self.ok = bool(ok)
        self.gate_ok = bool(gate_ok)
        self.calls: List[Any] = []
        self.gate_calls: List[Any] = []

    def verify(self, request: Any) -> cr.PathVerificationResult:
        self.calls.append(request)
        return cr.PathVerificationResult(
            verified=self.ok, role=request.role,
            interface_index=request.interface_index, source_ip=request.source_ip,
            reason="接口受限验证通过" if self.ok else "闸门未通过",
            reliability=cr.PATH_OK,
            successful_sites=("www.baidu.com",) if self.ok else (),
            failed_sites=() if self.ok else ("www.baidu.com",))

    def verify_gate(self, request: Any) -> cr.PathVerificationResult:
        self.gate_calls.append(request)
        return cr.PathVerificationResult(
            verified=self.gate_ok, role=request.role,
            interface_index=request.interface_index, source_ip=request.source_ip,
            reason="活动路径仍可用" if self.gate_ok else "闸门未通过",
            reliability=cr.PATH_OK)


def _make_app(monkeypatch, **overrides) -> tray.TrayApp:
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    monkeypatch.setattr(app, "notify", lambda *a, **k: None)
    return app


# --------------------------------------------------------------------------- #
# UI0x：常量对齐与健康态映射
# --------------------------------------------------------------------------- #
def test_ui00_probe_unreliable_constant_matches_product_code():
    """UI 的 ``PROBE_UNRELIABLE`` 定性必须与产品常量**同源**（防口径漂移）。"""
    assert ui.PATH_PROBE_UNRELIABLE == cr.PATH_PROBE_UNRELIABLE


def test_ui01_overall_and_step_labels_are_complete():
    assert set(ui.OVERALL_LABEL) == {ui.OVERALL_HEALTHY, ui.OVERALL_DEGRADED,
                                    ui.OVERALL_RECOVERING, ui.OVERALL_OFFLINE,
                                    ui.OVERALL_PAUSED, ui.OVERALL_UNKNOWN}
    assert set(ui.OVERALL_TONE) == set(ui.OVERALL_LABEL)
    assert set(ui.OVERALL_TONE.values()) <= set(ui.TONE_COLOR)
    # 每个阶段状态都必须有中文标签与色调（仪表盘直接渲染，不得出现英文原值）。
    for status in (ui.STEP_PENDING, ui.STEP_ACTIVE, ui.STEP_DONE, ui.STEP_FAILED,
                   ui.STEP_SKIPPED, ui.STEP_DISABLED):
        assert ui.STEP_STATUS_LABEL[status] and status in ui.STEP_TONE
    assert [key for key, _ in ui.PIPELINE_LABELS] == [
        "detect", "wifi_ready", "portal", "internet", "route", "uu"]
    assert ui.CARD_ROWS[0] == "角色" and "Internet" in ui.CARD_ROWS


@pytest.mark.parametrize("state", list(HealthState))
def test_ui10_health_view_covers_every_health_state(state):
    """每个 ``HealthState`` 都必须有确定性映射（不猜、不回退成"正常"）。"""
    tone, label = ui.health_view(state)
    assert tone in ui.TONE_COLOR and label


def test_ui11_health_view_is_unknown_for_probe_unreliable_and_none():
    assert ui.health_view(PU) == (ui.TONE_UNKNOWN, "不可判定（探测不可信）")
    assert ui.health_view(None) == (ui.TONE_UNKNOWN, "未知")
    assert ui.health_view("HEALTHY")[0] == ui.TONE_UNKNOWN, "非枚举值不得被当作正常"


# --------------------------------------------------------------------------- #
# UI2x：网卡卡片
# --------------------------------------------------------------------------- #
def test_ui20_path_card_marks_assignment_primary_and_standby():
    active = ui.build_path_card(role="ethernet", health=_health(
        HealthAxis.ETHERNET_HEALTH, H, ETH_IF, ETH_IP, ETH_DNS, "以太网"),
        assignment=ui.ROLE_PRIMARY, now=100.0)
    standby = ui.build_path_card(role="wifi", health=_health(
        HealthAxis.WIFI_HEALTH, D, WIFI_IF, WIFI_IP, WIFI_DNS, "WLAN"),
        assignment=ui.ROLE_STANDBY, now=100.0)
    assert active.assignment == "PRIMARY" and active.title == "以太网"
    assert standby.assignment == "STANDBY" and standby.title == "SNNU Wi-Fi"
    assert standby.field("Wi-Fi") == "SNNU", "Wi-Fi 卡片必须显示 SSID"


def test_ui21_path_card_internet_is_unverified_without_a_record():
    card = ui.build_path_card(role="ethernet", health=_health(
        HealthAxis.ETHERNET_HEALTH, H, ETH_IF, ETH_IP, ETH_DNS, "以太网"),
        assignment="PRIMARY", verification=None, now=100.0)
    assert card.verified is None
    assert card.field("Internet") == "未验证"
    assert card.internet_tone == ui.TONE_IDLE
    assert card.field("最近验证") == "—"


def test_ui22_path_card_shows_fresh_verification():
    card = ui.build_path_card(role="ethernet", health=_health(
        HealthAxis.ETHERNET_HEALTH, H, ETH_IF, ETH_IP, ETH_DNS, "以太网"),
        assignment="PRIMARY", verification=verified(70.0), now=100.0)
    assert card.verified is True and card.internet_tone == ui.TONE_OK
    assert "已验证" in card.field("Internet")
    assert card.field("最近验证") == "30s 前"


def test_ui23_path_card_marks_expired_verification_as_warn():
    card = ui.build_path_card(role="ethernet", health=_health(
        HealthAxis.ETHERNET_HEALTH, H, ETH_IF, ETH_IP, ETH_DNS, "以太网"),
        assignment="PRIMARY", verification=verified(0.0), now=100.0, max_age=60.0)
    assert card.verified is True, "记录仍是『通过』，但已过期"
    assert card.internet_tone == ui.TONE_WARN
    assert "已过期" in card.field("Internet")


def test_ui24_path_card_never_calls_probe_unreliable_verified():
    """接口约束失败（``PROBE_UNRELIABLE``）**不得**显示成"已验证"。"""
    card = ui.build_path_card(
        role="wifi", health=_health(HealthAxis.WIFI_HEALTH, H, WIFI_IF, WIFI_IP,
                                    WIFI_DNS, "WLAN"),
        assignment="PRIMARY",
        verification=verified(99.0, role="wifi", ok=False,
                              reliability=cr.PATH_PROBE_UNRELIABLE),
        now=100.0)
    assert card.verified is False and card.internet_tone == ui.TONE_UNKNOWN
    assert "不可信" in card.field("Internet")


def test_ui25_path_card_shows_captive_portal_not_internet():
    card = ui.build_path_card(
        role="wifi", health=_health(HealthAxis.WIFI_HEALTH, C, WIFI_IF, WIFI_IP,
                                    WIFI_DNS, "WLAN"),
        assignment="PRIMARY",
        verification=verified(99.0, role="wifi", ok=False, captive=True), now=100.0)
    assert card.field("Portal") == "需要认证"
    assert card.verified is False and "门户劫持" in card.field("Internet")


def test_ui26_path_card_exposes_interface_facts_and_manual_sites():
    card = ui.build_path_card(
        role="wifi", health=_health(HealthAxis.WIFI_HEALTH, H, WIFI_IF, WIFI_IP,
                                    WIFI_DNS, "WLAN"),
        assignment="PRIMARY", verification=None, now=100.0,
        manual=ui.verification_lines(
            cr.PathVerificationResult(
                verified=True, role="wifi", source_ip=WIFI_IP,
                reason="闸门 PASS 且交叉 ≥2",
                successful_sites=("www.baidu.com", "www.qq.com", "www.163.com"),
                failed_sites=("www.sohu.com",)), role="wifi"))
    assert card.field("ifIndex") == str(WIFI_IF)
    assert card.field("DNS") == WIFI_DNS[0]
    assert card.field("源地址") == WIFI_IP
    joined = " ".join(card.manual)
    assert "逐站" in joined and "通过 3" in joined and "失败 1" in joined


# --------------------------------------------------------------------------- #
# UI3x：恢复流水线
# --------------------------------------------------------------------------- #
def _eth(state=H):
    return _health(HealthAxis.ETHERNET_HEALTH, state, ETH_IF, ETH_IP, ETH_DNS, "以太网")


def _wifi(state=H):
    return _health(HealthAxis.WIFI_HEALTH, state, WIFI_IF, WIFI_IP, WIFI_DNS, "WLAN")


def _pipe(**kwargs) -> Dict[str, Any]:
    base = dict(failover_state=FailoverState.ETHERNET_PRIMARY, ethernet=_eth(),
                wifi=_wifi(), verification={}, active_path="ethernet", now=100.0)
    base.update(kwargs)
    steps = ui.build_pipeline(**base)
    return {step.key: step for step in steps}


def test_ui30_pipeline_is_pending_without_any_observation():
    steps = _pipe(failover_state=None, ethernet=None, wifi=None, active_path=None)
    assert steps["detect"].status == ui.STEP_PENDING
    assert steps["wifi_ready"].status == ui.STEP_PENDING
    assert steps["uu"].status == ui.STEP_DISABLED


def test_ui31_wifi_ready_done_when_link_and_ip_up():
    steps = _pipe()
    assert steps["wifi_ready"].status == ui.STEP_DONE
    assert steps["route"].status == ui.STEP_DONE, "ETHERNET_PRIMARY ⇒ 路由段已完成"


def test_ui32_portal_active_when_captive():
    steps = _pipe(wifi=_wifi(C))
    assert steps["portal"].status == ui.STEP_ACTIVE


def test_ui33_internet_active_while_verification_in_flight():
    steps = _pipe(verify_roles=("wifi",))
    assert steps["internet"].status == ui.STEP_ACTIVE
    assert "Wi-Fi" in steps["internet"].detail


def test_ui34_internet_done_on_fresh_pass():
    steps = _pipe(verification={"ethernet": verified(95.0)}, active_path="ethernet")
    assert steps["internet"].status == ui.STEP_DONE


def test_ui35_internet_failed_when_verification_did_not_pass():
    steps = _pipe(verification={"ethernet": verified(95.0, ok=False)},
                  active_path="ethernet")
    assert steps["internet"].status == ui.STEP_FAILED


def test_ui36_route_tracks_state_machine():
    assert _pipe(failover_state=FailoverState.WIFI_SWITCHING)["route"].status \
        == ui.STEP_ACTIVE
    assert _pipe(failover_state=FailoverState.WIFI_BACKUP_ACTIVE)["route"].status \
        == ui.STEP_DONE
    assert _pipe(failover_state=FailoverState.PAUSED)["route"].status \
        == ui.STEP_SKIPPED
    assert _pipe(failover_state=FailoverState.BOTH_OFFLINE)["route"].status \
        == ui.STEP_PENDING


def test_ui37_uu_step_reflects_enable_and_outcome():
    assert _pipe()["uu"].status == ui.STEP_DISABLED
    assert _pipe(uu_enabled=True, uu_pending=True)["uu"].status == ui.STEP_ACTIVE
    assert _pipe(uu_enabled=True, uu_last_ok=True)["uu"].status == ui.STEP_DONE
    assert _pipe(uu_enabled=True, uu_last_ok=False)["uu"].status == ui.STEP_FAILED
    assert _pipe(uu_enabled=True)["uu"].status == ui.STEP_PENDING
    assert _pipe(uu_enabled=True)["uu"].color == ui.TONE_COLOR[ui.TONE_IDLE]


def test_ui38_step_display_has_two_lines_and_tone():
    step = _pipe(verification={"ethernet": verified(95.0)}, active_path="ethernet"
                 )["internet"]
    assert step.display.splitlines() == ["Internet Verify", "完成"]
    assert step.color == ui.TONE_COLOR[ui.TONE_OK]


# --------------------------------------------------------------------------- #
# UI4x：总体状态灯
# --------------------------------------------------------------------------- #
def _overall(**kwargs) -> str:
    base = dict(failover_state=FailoverState.ETHERNET_PRIMARY, ethernet=_eth(),
                wifi=_wifi(), verification={"ethernet": verified(95.0)},
                active_path="ethernet", now=100.0)
    base.update(kwargs)
    return ui.overall_status(**base)


def test_ui40_overall_unknown_without_any_state():
    assert _overall(failover_state=None, ethernet=None, wifi=None,
                    active_path=None, verification={}) == ui.OVERALL_UNKNOWN


def test_ui41_overall_healthy_when_active_path_verified():
    assert _overall() == ui.OVERALL_HEALTHY


def test_ui42_overall_degraded_when_link_up_but_internet_unproven():
    """``Connected ≠ Internet``：连着网但没验证 ⇒ 降级（不是"正常"）。"""
    assert _overall(verification={}) == ui.OVERALL_DEGRADED
    assert _overall(verification={"ethernet": verified(95.0, ok=False)}) \
        == ui.OVERALL_DEGRADED
    assert _overall(verification={"ethernet": verified(
        95.0, ok=False, reliability=cr.PATH_PROBE_UNRELIABLE)}) == ui.OVERALL_DEGRADED


def test_ui43_overall_recovering_during_transitions_and_verifications():
    for state in (FailoverState.WIFI_PREPARING, FailoverState.WIFI_AUTHENTICATING,
                  FailoverState.WIFI_SWITCHING, FailoverState.ETHERNET_FAILBACK):
        assert _overall(failover_state=state) == ui.OVERALL_RECOVERING
    assert _overall(verify_roles=("wifi",)) == ui.OVERALL_RECOVERING
    assert _overall(recovery_event="FAILOVER_COMPLETED") == ui.OVERALL_RECOVERING


def test_ui44_overall_offline_when_both_paths_down():
    assert _overall(failover_state=FailoverState.BOTH_OFFLINE) == ui.OVERALL_OFFLINE
    assert _overall(ethernet=_eth(D), wifi=_wifi(D)) == ui.OVERALL_OFFLINE


def test_ui45_overall_paused_wins_over_everything():
    assert _overall(paused=True) == ui.OVERALL_PAUSED
    assert _overall(failover_state=FailoverState.PAUSED) == ui.OVERALL_PAUSED


def test_ui46_overall_degraded_when_monitor_stopped_or_warnings():
    assert _overall(monitor_alive=False) == ui.OVERALL_DEGRADED
    assert _overall(warnings=("无人值守缺口：未配置自启",)) == ui.OVERALL_DEGRADED


# --------------------------------------------------------------------------- #
# UI5x：纯函数 / 不阻塞
# --------------------------------------------------------------------------- #
def test_ui50_build_ui_state_is_a_pure_function():
    kwargs = dict(now=100.0, ethernet=_eth(), wifi=_wifi(),
                  verification={"ethernet": verified(95.0)}, active_path="ethernet",
                  standby_path="wifi", failover_state=FailoverState.ETHERNET_PRIMARY,
                  mode_label="智能自动", monitor_label="监测运行中",
                  uu_enabled=False)
    first = ui.build_ui_state(**kwargs)
    second = ui.build_ui_state(**kwargs)
    assert first.to_dict() == second.to_dict()
    assert first.overall == ui.OVERALL_HEALTHY
    assert first.headline == "以太网 PRIMARY · Wi-Fi STANDBY · Internet 已验证"
    assert first.card("ethernet").assignment == "PRIMARY"
    assert [step.key for step in first.pipeline] == [k for k, _ in ui.PIPELINE_LABELS]


def test_ui51_tray_ui_state_never_triggers_a_probe(monkeypatch):
    """``ui_state()`` 只消费状态：绝不调用验证/探测能力（网络操作只在后台线程）。"""
    app = _make_app(monkeypatch)
    app.last_observation = ObservationResult(snapshot=snapshot(H, H), trusted=True)
    app.failover_status = FailoverStatus(state=FailoverState.ETHERNET_PRIMARY,
                                         primary="ethernet", backup="wifi")
    app._path_verifier = ScriptedVerifier()
    state = app.ui_state(now=100.0)
    assert state.ethernet.assignment == "PRIMARY"
    assert state.wifi.assignment == "STANDBY"
    assert app._path_verifier.calls == [], "UI 状态映射发起了一次真实探测"
    busy = getattr(app, "_verify_path_busy", None)
    assert busy is None or not busy.is_set(), "UI 状态映射触发了后台验证线程"


def test_ui52_tray_ui_state_works_without_a_tk_root(monkeypatch):
    """无 Tk 根窗口也必须能取到状态映射（UI 与网络核心解耦）。"""
    app = _make_app(monkeypatch)
    app.root = None
    assert app.ui_state(now=1.0).overall == ui.OVERALL_UNKNOWN
    assert isinstance(app.ui_state_lines(), list)


def test_ui53_status_changes_do_not_block_or_raise(monkeypatch):
    """状态在两次取样之间变化：映射如实反映，且不抛错、不阻塞。"""
    app = _make_app(monkeypatch)
    app.last_observation = ObservationResult(snapshot=snapshot(H, D), trusted=True)
    app.failover_status = FailoverStatus(state=FailoverState.ETHERNET_PRIMARY,
                                         primary="ethernet", backup="wifi")
    before = app.ui_state(now=10.0)
    app.last_observation = ObservationResult(snapshot=snapshot(IU, H, WIFI_IF),
                                             trusted=True)
    app.failover_status = FailoverStatus(state=FailoverState.WIFI_BACKUP_ACTIVE,
                                         primary="wifi", backup="ethernet")
    after = app.ui_state(now=20.0)
    assert before.overall != after.overall or before.ethernet.state_label != \
        after.ethernet.state_label
    assert after.wifi.state_label == ui.health_view(H)[1]


# --------------------------------------------------------------------------- #
# UI6x：托盘接线（菜单 / 手动操作 / 后台线程）
# --------------------------------------------------------------------------- #
def test_ui60_tray_ui_state_reflects_failover_and_epoch(monkeypatch):
    app = _make_app(monkeypatch)
    app.last_observation = ObservationResult(snapshot=snapshot(H, C), trusted=True)
    app.failover_status = FailoverStatus(state=FailoverState.ETHERNET_SUSPECT,
                                         primary="ethernet", backup="wifi")
    state = app.ui_state(now=50.0)
    assert state.active_path == "ethernet" and state.standby_path == "wifi"
    assert state.wifi.field("Portal") == "需要认证"
    assert state.network_epoch == app.dual_path.network_epoch
    assert state.uu.enabled is False, "默认安全档：UU 自动恢复关闭"


def test_ui61_on_dashboard_posts_to_the_tk_queue(monkeypatch):
    """打开主界面只投递动作到 Tk 队列（网络线程不直接碰 Tk）。"""
    app = _make_app(monkeypatch)
    app.root = object()                       # 只需非 None（_ui_post 不触碰 Tk）
    app.on_dashboard()
    assert app._ui_actions.get_nowait() == app._open_dashboard
    app.root = None
    app.on_dashboard()
    assert app._ui_actions.empty(), "无 Tk 根窗口时不得排队打开窗口"


def test_ui62_menu_exposes_dashboard_verification_and_manual_restart(monkeypatch):
    app = _make_app(monkeypatch)
    menu = app.build_menu()
    texts = [getattr(item, "text", None) for item in menu]
    for expected in ("打开主界面", "立即检测", "立即验证以太网",
                     "立即验证 SNNU Wi-Fi", "重启 UU Remote（手动）"):
        assert expected in texts, "托盘菜单缺少 %s" % expected
    assert "运行模式" in texts, "既有菜单项必须保留"
    assert isinstance(app._link_text(), str)
    assert app._uu_text().startswith("UU 远程：")


def test_ui63_manual_verification_runs_in_background_and_shows_sites(monkeypatch):
    app = _make_app(monkeypatch)
    verifier = ScriptedVerifier(ok=True)
    app._path_verifier = verifier
    # 先喂一份快照给整合层（真实入口是 _dual_path_step；这里用 public tick）。
    result = TickResult(at=10.0, mode=FailoverMode.AUTO,
                        state=FailoverState.ETHERNET_PRIMARY,
                        previous_state=FailoverState.ETHERNET_PRIMARY)
    app.dual_path.tick(result, snapshot(H, H), trusted=True, now=10.0)

    app.on_verify_now("wifi")
    assert wait_until(lambda: "wifi" in app.manual_verification), "手动验证未完成"
    outcome = app.manual_verification["wifi"]
    assert outcome.allows_recovery is True
    assert verifier.calls and verifier.calls[0].role == "wifi"
    card = app.ui_state(now=11.0).card("wifi")
    assert any("逐站" in line for line in card.manual), "未展示逐站结果"


def test_ui64_manual_verification_without_interface_facts_is_fail_closed(monkeypatch):
    app = _make_app(monkeypatch)
    app._path_verifier = ScriptedVerifier()
    notes = []
    monkeypatch.setattr(app, "notify", lambda title, body="": notes.append(title))
    app.on_verify_now("wifi")                 # 从未观测 ⇒ 缺 ifIndex/源地址/DNS
    assert notes and "无法验证" in notes[0]
    assert app.manual_verification == {}, "缺接口事实却发起了探测"
    assert app._path_verifier.calls == []


def test_ui65_manual_uu_restart_requires_explicit_confirmation(monkeypatch):
    app = _make_app(monkeypatch)
    app._remote_app_ops = FakeRemoteAppOps(processes=UU_PROCESSES,
                                           run_entries=UU_RUN_ENTRIES)
    monkeypatch.setattr(app, "_ui_ask", lambda *a, **k: None)
    app.on_restart_uu_remote()
    assert app._remote_app_ops.killed == [], "未确认却清退了 UU 进程"


def test_ui66_manual_uu_restart_aborts_when_verification_fails(monkeypatch):
    """手动清退仍必须**先**通过接口受限验证（fail-closed）。"""
    monkeypatch.setattr(cra, "_SLEEP", lambda seconds: None)
    app = _make_app(monkeypatch)
    app._path_verifier = ScriptedVerifier(ok=False)
    app._remote_app_ops = FakeRemoteAppOps(processes=UU_PROCESSES,
                                           run_entries=UU_RUN_ENTRIES)
    result = TickResult(at=10.0, mode=FailoverMode.AUTO,
                        state=FailoverState.ETHERNET_PRIMARY,
                        previous_state=FailoverState.ETHERNET_PRIMARY)
    app.dual_path.tick(result, snapshot(H, H), trusted=True, now=10.0)
    monkeypatch.setattr(app, "_ui_ask", lambda *a, **k: True)
    app._manual_uu_restart_run()
    assert app._remote_app_ops.killed == [], "验证未通过却清退了 UU 进程"


def test_ui67_manual_uu_restart_kills_only_after_verification_passes(monkeypatch):
    monkeypatch.setattr(cra, "_SLEEP", lambda seconds: None)
    app = _make_app(monkeypatch)
    app._path_verifier = ScriptedVerifier(ok=True, gate_ok=True)
    ops = FakeRemoteAppOps(processes=UU_PROCESSES, run_entries=UU_RUN_ENTRIES)
    app._remote_app_ops = ops
    result = TickResult(at=10.0, mode=FailoverMode.AUTO,
                        state=FailoverState.ETHERNET_PRIMARY,
                        previous_state=FailoverState.ETHERNET_PRIMARY)
    app.dual_path.tick(result, snapshot(H, H), trusted=True, now=10.0)
    monkeypatch.setattr(app, "_ui_ask", lambda *a, **k: True)
    app._manual_uu_restart_run()
    assert sorted(ops.killed) == [100, 200], "只清退用户态两进程"
    assert 300 not in ops.killed, "GameViewerServer 不属于用户态清退范围"
    assert ops.launched, "清退后必须按自启项重新启动"


def test_ui68_manual_uu_restart_is_noop_when_not_running(monkeypatch):
    monkeypatch.setattr(cra, "_SLEEP", lambda seconds: None)
    app = _make_app(monkeypatch)
    app._path_verifier = ScriptedVerifier(ok=True)
    ops = FakeRemoteAppOps(processes=[], run_entries=UU_RUN_ENTRIES)
    app._remote_app_ops = ops
    result = TickResult(at=10.0, mode=FailoverMode.AUTO,
                        state=FailoverState.ETHERNET_PRIMARY,
                        previous_state=FailoverState.ETHERNET_PRIMARY)
    app.dual_path.tick(result, snapshot(H, H), trusted=True, now=10.0)
    monkeypatch.setattr(app, "_ui_ask", lambda *a, **k: True)
    app._manual_uu_restart_run()
    assert ops.killed == [] and ops.launched == []
    assert not ops.launched, "没在跑就不该替用户启动"


# --------------------------------------------------------------------------- #
# UI7x：立即检测命令
# --------------------------------------------------------------------------- #
def test_ui70_dispatch_detect_forces_an_immediate_probe(monkeypatch):
    app = _make_app(monkeypatch)
    seen = []
    monkeypatch.setattr(app, "_probe_tick",
                        lambda next_probe: seen.append(next_probe) or 42.0)
    assert app._dispatch(("detect", None), 999.0) == 42.0
    assert seen == [0.0], "立即检测必须绕过周期节流（next_probe=0.0）"


def test_ui71_on_detect_now_enqueues_and_respects_pause(monkeypatch):
    app = _make_app(monkeypatch)
    monkeypatch.setattr(app, "_ensure_worker", lambda: True)
    app.on_detect_now()
    assert app._commands.get_nowait()[0] == "detect"
    app.config["paused"] = True
    monkeypatch.setattr(app, "_ensure_worker", lambda: True)
    app.on_detect_now()
    assert app._commands.empty(), "暂停时不得排队自动检测"


# --------------------------------------------------------------------------- #
# UI8x：真实 Tk 冒烟（无 Tk 环境自动跳过；**不依赖截图**）
# --------------------------------------------------------------------------- #
def test_ui80_dashboard_and_settings_windows_build_and_render(monkeypatch):
    """主界面 / 设置面板能真实建起来并渲染一次（覆盖纯映射之外的 Tk 装配代码）。

    只做**构造 + 一次渲染 + 关闭**：不点击网络按钮、不读写注册表、不发起探测。
    """
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk

    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "get_autostart", lambda: False)
    try:
        root = tk.Tk()
    except tk.TclError as exc:                     # 无显示环境：如实跳过
        pytest.skip("本环境无法创建 Tk 根窗口：%s" % exc)
    root.withdraw()
    try:
        app = tray.TrayApp(LOG)
        app.root = root
        app.icon = None
        app.config = dict(tray.DEFAULT_CONFIG)
        app.last_observation = ObservationResult(snapshot=snapshot(H, C), trusted=True)
        app.failover_status = FailoverStatus(state=FailoverState.ETHERNET_PRIMARY,
                                             primary="ethernet", backup="wifi")

        app._open_dashboard()
        win = app.dashboard_win
        assert win is not None and win.winfo_exists()
        app._render_dashboard(app.ui_state(now=5.0))
        assert app._dash_light.cget("bg") == app.ui_state(now=5.0).overall_color
        assert app._dash_widgets["ethernet"]["rows"]["角色"].get() == "PRIMARY"
        assert app._dash_widgets["wifi"]["rows"]["Portal"].get() == "需要认证"
        assert "Internet Verify" in app._dash_widgets["steps"]["internet"].cget("text")
        app._close_dashboard()
        assert app.dashboard_win is None

        app._open_settings()
        settings = app.settings_win
        assert settings is not None and settings.winfo_exists()
        notebook = [child for child in settings.winfo_children()
                    if isinstance(child, ttk.Frame)][0].winfo_children()[0]
        tabs = [notebook.tab(index, "text") for index in range(
            len(notebook.tabs()))]
        assert tabs == ["基本", "网络", "恢复", "UU Remote", "诊断"], tabs
        settings.destroy()
        app.settings_win = None
    finally:
        root.destroy()
