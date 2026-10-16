# -*- coding: utf-8 -*-
"""V0.5-2 恢复事件协调器（``campus_recovery``）测试。

需求（方案 §28–§32）：

- 恢复事件发生时，决定「什么时候验证」与「什么时候重启 UU」；
- 只有「活动路径 + 直连 Internet 验证通过 + 恢复事件」三条件同时成立才重启 UU；
- 备用链路刚连接 ⇒ 只验证，**不**切路由、**不**重启 UU；
- 同一 ``network_epoch`` 只产生**一次**验证与**一次** UU 重启（防 tick 级重复）；
- UU 重启冷却（默认 300s）抑制网络抖动造成的 kill/start 抖动。

约束：纯逻辑，零 I/O（不建 socket、不切路由、不认证、不杀进程）；时间用显式 ``now``。
"""
from __future__ import annotations

import campus_recovery as rec
from campus_reconnect import PATH_OK, PATH_PROBE_UNRELIABLE, PathVerificationResult


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _verified(role: str, *, source_ip: str = "") -> PathVerificationResult:
    """接口受限验证**通过**的结果（真实 ``PathVerificationResult``）。"""
    return PathVerificationResult(verified=True, role=role, source_ip=source_ip,
                                  reason="接口受限验证通过", reliability=PATH_OK)


def _failed(role: str, *, reliability: str = PATH_OK,
            reason: str = "闸门未通过（www.baidu.com(timeout)）"
            ) -> PathVerificationResult:
    """验证**未通过**的结果（可指定是否「探测不可信」）。"""
    return PathVerificationResult(verified=False, role=role, reason=reason,
                                  reliability=reliability)


def _coordinator(**kwargs) -> rec.RecoveryCoordinator:
    return rec.RecoveryCoordinator(**kwargs)


# --------------------------------------------------------------------------- #
# 备用链路刚连接：只验证
# --------------------------------------------------------------------------- #
def test_standby_connected_verifies_but_never_restarts_uu():
    """Wi-Fi standby 刚连上：安排验证，但**不**产出 UU 重启（方案 §9.1/§30）。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=1, active_path=rec.PATH_ETHERNET,
                               standby_path=rec.PATH_WIFI)

    plan = coordinator.on_event(rec.RecoveryEvent.STANDBY_CONNECTED, now=0.0,
                                path=rec.PATH_WIFI)

    assert plan.needs_verification is True
    assert plan.verify_path == rec.PATH_WIFI
    assert plan.steps == (rec.RecoveryStep.INTERNET_VERIFY,)
    assert plan.uu_restart is None
    assert coordinator.active_path == rec.PATH_ETHERNET, "备用链路连接改变了活动路径"

    follow = coordinator.on_verification(_verified(rec.PATH_WIFI), now=1.0)

    assert follow.schedules_uu_restart is False
    assert follow.verified is True
    assert coordinator.uu_restart_epoch == 0
    assert coordinator.last_uu_restart_at is None


# --------------------------------------------------------------------------- #
# 接管 / 切回：验证通过后重启 UU
# --------------------------------------------------------------------------- #
def test_failover_completed_schedules_one_uu_restart():
    """Ethernet 故障 → Wi-Fi 接管：验证通过后产出**一次** UU 重启请求。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=1, active_path=rec.PATH_ETHERNET,
                               standby_path=rec.PATH_WIFI)
    coordinator.update_network(network_epoch=2, active_path=rec.PATH_WIFI,
                               standby_path=rec.PATH_ETHERNET)

    plan = coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=100.0,
                                path=rec.PATH_WIFI)

    assert plan.verify_path == rec.PATH_WIFI
    assert plan.steps == (rec.RecoveryStep.INTERNET_VERIFY, rec.RecoveryStep.UU_RESTART)
    assert coordinator.active_path == rec.PATH_WIFI
    assert coordinator.previous_active_path == rec.PATH_ETHERNET

    follow = coordinator.on_verification(_verified(rec.PATH_WIFI, source_ip="172.16.0.9"),
                                         now=101.0)

    assert follow.schedules_uu_restart is True
    assert follow.uu_restart.network_epoch == 2
    assert follow.uu_restart.active_path == rec.PATH_WIFI
    assert follow.uu_restart.source_ip == "172.16.0.9"
    assert follow.steps == (rec.RecoveryStep.UU_RESTART,)
    assert coordinator.uu_restart_epoch == 2
    assert coordinator.last_uu_restart_at == 101.0


def test_failback_completed_schedules_uu_restart_after_cooldown():
    """Wi-Fi PRIMARY + 有线恢复稳定 → 切回：验证通过后（冷却已过）重启 UU。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=2, active_path=rec.PATH_WIFI,
                               standby_path=rec.PATH_ETHERNET)
    coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=100.0,
                         path=rec.PATH_WIFI)
    coordinator.on_verification(_verified(rec.PATH_WIFI), now=101.0)

    # 有线连续稳定 → 世代推进 → 切回以太网
    coordinator.update_network(network_epoch=3, active_path=rec.PATH_ETHERNET,
                               standby_path=rec.PATH_WIFI)
    plan = coordinator.on_event(rec.RecoveryEvent.FAILBACK_COMPLETED, now=500.0,
                                path=rec.PATH_ETHERNET)

    assert plan.verify_path == rec.PATH_ETHERNET
    follow = coordinator.on_verification(_verified(rec.PATH_ETHERNET), now=501.0)

    assert follow.schedules_uu_restart is True, follow.notes
    assert follow.uu_restart.network_epoch == 3
    assert follow.uu_restart.active_path == rec.PATH_ETHERNET


def test_uu_restart_cooldown_blocks_failback_too_soon():
    """切回间隔太短（< 冷却）⇒ 跳过 UU 重启，避免 kill/start 抖动（方案 §14）。"""
    coordinator = _coordinator(uu_cooldown=300.0)
    coordinator.update_network(network_epoch=2, active_path=rec.PATH_WIFI,
                               standby_path=rec.PATH_ETHERNET)
    coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=100.0,
                         path=rec.PATH_WIFI)
    assert coordinator.on_verification(_verified(rec.PATH_WIFI), now=101.0
                                       ).schedules_uu_restart is True

    coordinator.update_network(network_epoch=3, active_path=rec.PATH_ETHERNET,
                               standby_path=rec.PATH_WIFI)
    coordinator.on_event(rec.RecoveryEvent.FAILBACK_COMPLETED, now=150.0,
                         path=rec.PATH_ETHERNET)
    follow = coordinator.on_verification(_verified(rec.PATH_ETHERNET), now=151.0)

    assert follow.schedules_uu_restart is False
    assert any("冷却" in note for note in follow.notes), follow.notes
    assert coordinator.uu_restart_epoch == 3, "冷却跳过应消耗本世代名额"


# --------------------------------------------------------------------------- #
# 去重：同一世代最多一次验证 / 一次 UU 重启
# --------------------------------------------------------------------------- #
def test_repeated_events_and_verifications_yield_at_most_one_uu_restart():
    """同一事件/验证重复出现（tick 级重复）⇒ 只产出一次 UU 重启。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=2, active_path=rec.PATH_WIFI,
                               standby_path=rec.PATH_ETHERNET)

    first = coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=10.0,
                                 path=rec.PATH_WIFI)
    second = coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=11.0,
                                  path=rec.PATH_WIFI)
    third = coordinator.on_event(rec.RecoveryEvent.ACTIVE_PATH_RECOVERED, now=12.0,
                                 path=rec.PATH_WIFI)

    assert first.needs_verification is True
    assert second.deduped is True and second.needs_verification is False
    assert third.deduped is True
    assert coordinator.verification_epoch == 1

    accepted = coordinator.on_verification(_verified(rec.PATH_WIFI), now=13.0)
    again = coordinator.on_verification(_verified(rec.PATH_WIFI), now=14.0)

    assert accepted.schedules_uu_restart is True
    assert again.schedules_uu_restart is False and again.deduped is True
    assert coordinator.uu_restart_epoch == 2


def test_new_network_epoch_reopens_verification_and_uu_slot():
    """世代推进后，允许重新验证、重新重启一次 UU。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=1, active_path=rec.PATH_WIFI,
                               standby_path=rec.PATH_ETHERNET)
    coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=1.0,
                         path=rec.PATH_WIFI)
    coordinator.on_verification(_verified(rec.PATH_WIFI), now=2.0)

    changed = coordinator.update_network(network_epoch=2, active_path=rec.PATH_WIFI,
                                         standby_path=rec.PATH_ETHERNET)
    plan = coordinator.on_event(rec.RecoveryEvent.ACTIVE_PATH_RECOVERED, now=400.0,
                                path=rec.PATH_WIFI)
    follow = coordinator.on_verification(_verified(rec.PATH_WIFI), now=401.0)

    assert changed is True
    assert plan.needs_verification is True and plan.deduped is False
    assert follow.schedules_uu_restart is True
    assert coordinator.verification_epoch == 2


def test_route_switch_must_bump_epoch_to_reverify_same_path():
    """同一世代内同路径只验证一次 —— 故**切换路由必须由上层推进 epoch**。

    这条是刻意的契约：同世代重复事件一律去重（防 tick 级重复），
    上层（V0.5-4）在活动路径变化时必须 ``update_network(network_epoch=+1)``。
    """
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=5, active_path=rec.PATH_ETHERNET,
                               standby_path=rec.PATH_WIFI)

    standby = coordinator.on_event(rec.RecoveryEvent.STANDBY_CONNECTED, now=0.0,
                                   path=rec.PATH_WIFI)
    # 未推进世代就报告「Wi-Fi 接管」⇒ 视为同一世代重复事件，被去重
    takeover = coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=1.0,
                                    path=rec.PATH_WIFI)

    assert standby.needs_verification is True
    assert takeover.deduped is True
    assert coordinator.verification_epoch == 1


# --------------------------------------------------------------------------- #
# fail-closed：验证未通过 / 探测不可信
# --------------------------------------------------------------------------- #
def test_not_verified_never_restarts_uu():
    """验证未通过 ⇒ 不重启 UU（即使处于恢复事件中）。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=2, active_path=rec.PATH_WIFI,
                               standby_path=rec.PATH_ETHERNET)
    coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=1.0,
                         path=rec.PATH_WIFI)

    follow = coordinator.on_verification(_failed(rec.PATH_WIFI), now=2.0)

    assert follow.schedules_uu_restart is False
    assert follow.verified is False
    assert any("未通过" in note for note in follow.notes), follow.notes
    assert coordinator.uu_restart_epoch == 0


def test_probe_unreliable_never_restarts_uu():
    """接口约束不可信（PROBE_UNRELIABLE）⇒ 不重启 UU（方案 §26 T12）。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=2, active_path=rec.PATH_WIFI,
                               standby_path=rec.PATH_ETHERNET)
    coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=1.0,
                         path=rec.PATH_WIFI)

    follow = coordinator.on_verification(
        _failed(rec.PATH_WIFI, reliability=PATH_PROBE_UNRELIABLE,
                reason="PROBE_UNRELIABLE：接口约束未证实"), now=2.0)

    assert follow.schedules_uu_restart is False
    assert follow.verified is False
    assert "PROBE_UNRELIABLE" in follow.notes[0], follow.notes


def test_verification_without_recovery_event_never_restarts_uu():
    """没有恢复事件的「裸验证」（例如用户点「立即验证」）不重启 UU。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=2, active_path=rec.PATH_ETHERNET,
                               standby_path=rec.PATH_WIFI)

    follow = coordinator.on_verification(_verified(rec.PATH_ETHERNET), now=1.0)

    assert follow.schedules_uu_restart is False
    assert any("不属于任何可用路径恢复事件" in note for note in follow.notes), follow.notes


def test_event_without_resolved_path_only_records():
    """路径未解析（None）⇒ 只记录事件，不安排验证。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=1, active_path=None)

    plan = coordinator.on_event(rec.RecoveryEvent.ACTIVE_PATH_RECOVERED, now=0.0)

    assert plan.needs_verification is False
    assert plan.uu_restart is None
    assert coordinator.verification_epoch == 0


# --------------------------------------------------------------------------- #
# 执行回报 / 快照 / 一致性
# --------------------------------------------------------------------------- #
def test_uu_restart_outcome_is_recorded_without_changing_dedupe():
    """执行回报只影响状态展示；去重与冷却语义不变（失败同样进冷却）。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=1, active_path=rec.PATH_WIFI)
    coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=1.0,
                         path=rec.PATH_WIFI)
    assert coordinator.on_verification(_verified(rec.PATH_WIFI), now=2.0
                                       ).schedules_uu_restart is True

    coordinator.note_uu_restart_outcome(False, detail="UU_RESTART_FAILED：launch failed")

    assert coordinator.last_uu_restart_ok is False
    assert "UU_RESTART_FAILED" in coordinator.last_uu_restart_detail
    assert coordinator.uu_restart_epoch == 1
    # 同一世代仍去重
    again = coordinator.on_verification(_verified(rec.PATH_WIFI), now=3.0)
    assert again.schedules_uu_restart is False


def test_snapshot_and_plans_are_serializable():
    """快照与计划须可序列化（日志 / UI 用），且不含凭据。"""
    coordinator = _coordinator()
    coordinator.update_network(network_epoch=7, active_path=rec.PATH_ETHERNET,
                               standby_path=rec.PATH_WIFI)
    plan = coordinator.on_event(rec.RecoveryEvent.STANDBY_CONNECTED, now=0.0,
                                path=rec.PATH_WIFI)
    follow = coordinator.on_verification(_verified(rec.PATH_WIFI), now=1.0)

    payload = coordinator.snapshot()

    assert payload["network_epoch"] == 7
    assert payload["active_path"] == rec.PATH_ETHERNET
    assert payload["standby_path"] == rec.PATH_WIFI
    assert payload["uu_cooldown"] == rec.DEFAULT_UU_RESTART_COOLDOWN
    assert [step.value for step in plan.steps] == ["internet_verify"]
    assert plan.to_dict()["verify_path"] == "wifi"
    assert follow.to_dict()["uu_restart"] is None
    assert follow.verified is True


def test_default_cooldown_matches_remote_app():
    """冷却默认值与 ``campus_remote_app.DEFAULT_RESTART_COOLDOWN`` 同源（单一真值）。"""
    import campus_remote_app as remote_app

    assert rec.DEFAULT_UU_RESTART_COOLDOWN == remote_app.DEFAULT_RESTART_COOLDOWN
    assert rec.DEFAULT_UU_RESTART_COOLDOWN == 300.0


def test_invalid_cooldown_rejected():
    """负冷却参数直接拒绝（fail-fast，而不是静默禁用守卫）。"""
    try:
        _coordinator(uu_cooldown=-1.0)
    except ValueError:
        return
    raise AssertionError("负冷却未被拒绝")
