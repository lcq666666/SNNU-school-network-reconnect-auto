# -*- coding: utf-8 -*-
"""V0.5-3 UU 远程自动恢复（``restart_after_verified_recovery``）测试。

需求（方案 §11–§16、§33–§37）：网络恢复且**接口受限验证通过**之后，清退并重启
UU 远程（网易 GameViewer）**用户态**后台；这是恢复流水线的**最后一步**。

关键门禁（每条对应一个用例）：

- 默认 ``enabled=False`` ⇒ 零副作用；
- 网络未验证 / ``PROBE_UNRELIABLE`` ⇒ 不清退；
- 同一 ``network_epoch`` 只重启一次（台账去重）；
- 冷却期内不重启（防 kill/start 抖动）；
- UU 原本未运行 ⇒ 不启动（``noop``）；原本在运行 ⇒ 清退并确认进程回来；
- **清退前轻量再确认**未通过 / 抛异常 / 未提供 ⇒ 一个进程都不清退（fail-closed）；
- kill 失败 / launch 失败 / 进程没回来 ⇒ 如实报告 ``failed`` / ``unconfirmed``；
- 备用链路（standby）刚连接 ⇒ 不进入恢复流水线（由协调器把关）。

约束：零真实进程操作（假 ops）、零真实 sleep、零真实网络。
"""
from __future__ import annotations

import campus_remote_app as cra
import campus_recovery as rec
from campus_reconnect import PATH_OK, PATH_PROBE_UNRELIABLE, PathVerificationResult
from tests.fakes import FakeRemoteAppOps

RUNNING = [{"pid": 111, "process_name": "GameViewer.exe",
            "path": r"C:\_study\GameViewer\bin\GameViewer.exe"}]
RUN_ENTRIES = [{"command": r"C:\_study\GameViewer\GameViewer.exe --auto-run"}]
LAUNCH_COMMAND = RUN_ENTRIES[0]["command"]


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _ops(**kwargs) -> FakeRemoteAppOps:
    kwargs.setdefault("processes", RUNNING)
    kwargs.setdefault("run_entries", RUN_ENTRIES)
    return FakeRemoteAppOps(**kwargs)


def _verified(role: str = "wifi", **kwargs) -> PathVerificationResult:
    kwargs.setdefault("source_ip", "172.16.0.9")
    return PathVerificationResult(verified=True, role=role,
                                  reason="接口受限验证通过", reliability=PATH_OK,
                                  **kwargs)


def _unverified(role: str = "wifi", **kwargs) -> PathVerificationResult:
    kwargs.setdefault("reason", "闸门未通过（www.baidu.com(timeout)）")
    kwargs.setdefault("reliability", PATH_OK)
    return PathVerificationResult(verified=False, role=role, **kwargs)


def _recheck_ok():
    return True, "活动路径仍可用 + 百度闸门仍通过"


def _call(ops, **kwargs) -> cra.UuRecoveryResult:
    kwargs.setdefault("active_path", "wifi")
    kwargs.setdefault("network_epoch", 2)
    kwargs.setdefault("verification", _verified())
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("recheck", _recheck_ok)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return cra.restart_after_verified_recovery(ops, **kwargs)


# --------------------------------------------------------------------------- #
# 门禁 1：默认关闭 / 网络未验证
# --------------------------------------------------------------------------- #
def test_disabled_is_zero_side_effect():
    """默认 ``enabled=False`` ⇒ 不探测、不清退、不启动（零副作用）。"""
    ops = _ops()
    ledger = cra.RecoveryRestartLedger()

    result = _call(ops, enabled=False, ledger=ledger)

    assert result.action == "disabled"
    assert result.attempted is False
    assert ops.killed == [] and ops.launched == []
    assert ops.list_calls == 0, "关闭状态下不应做任何探测"
    assert ledger.restarts == 0


def test_not_verified_never_restarts_uu():
    """网络未通过接口受限验证 ⇒ 不清退 UU。"""
    ops = _ops()

    result = _call(ops, verification=_unverified())

    assert result.action == "not-verified"
    assert result.ok is True
    assert ops.killed == [] and ops.launched == []
    assert "闸门未通过" in result.verification_detail


def test_probe_unreliable_never_restarts_uu():
    """接口约束不可信（PROBE_UNRELIABLE）⇒ 不清退 UU（方案 §26 T12 的下游守门）。"""
    ops = _ops()
    verification = _unverified(reason="PROBE_UNRELIABLE：接口约束未证实",
                              reliability=PATH_PROBE_UNRELIABLE)

    result = _call(ops, verification=verification)

    assert result.action == "not-verified"
    assert ops.killed == [] and ops.launched == []


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_active_wifi_recovery_restarts_and_confirms_process():
    """活动路径（Wi-Fi）恢复 ⇒ 清退用户态进程、按自启项重启、并确认进程回来。"""
    ops = _ops()
    ledger = cra.RecoveryRestartLedger()

    result = _call(ops, active_path="wifi", network_epoch=2, ledger=ledger)

    assert result.action == "restarted", result.detail
    assert result.ok is True and result.restarted is True and result.alive is True
    assert result.was_running is True
    assert ops.killed == [111]
    assert ops.launched == [LAUNCH_COMMAND]
    assert result.killed == [111] and result.launched == LAUNCH_COMMAND
    assert ledger.last_epoch == 2 and ledger.restarts == 1


def test_active_ethernet_failback_restarts_uu():
    """活动路径（Ethernet，切回后）恢复 ⇒ 同样清退重启 UU。"""
    ops = _ops()

    result = _call(ops, active_path="ethernet", network_epoch=3,
                   verification=_verified("ethernet", source_ip="10.0.0.5"))

    assert result.action == "restarted"
    assert result.active_path == "ethernet"
    assert ops.killed == [111]


def test_uu_not_running_is_noop_and_never_launches():
    """UU 原本未运行 ⇒ 不启动（不替用户把已关闭的 UU 拉起来，方案 §16）。"""
    ops = _ops(processes=[])
    ledger = cra.RecoveryRestartLedger()

    result = _call(ops, ledger=ledger)

    assert result.action == "noop"
    assert result.was_running is False
    assert ops.killed == [] and ops.launched == []
    assert "不启动新实例" in result.detail
    assert ledger.last_epoch == 2, "名额应被消耗，避免同世代反复尝试"


# --------------------------------------------------------------------------- #
# 去重与冷却
# --------------------------------------------------------------------------- #
def test_same_epoch_restart_is_deduplicated():
    """同一 ``network_epoch`` 只重启一次（防 tick 级重复）。"""
    ledger = cra.RecoveryRestartLedger()
    first_ops = _ops()
    second_ops = _ops()

    first = _call(first_ops, network_epoch=7, ledger=ledger)
    second = _call(second_ops, network_epoch=7, ledger=ledger)

    assert first.action == "restarted"
    assert second.action == "deduped"
    assert second.attempted is False
    assert second_ops.killed == [] and second_ops.launched == []
    assert ledger.restarts == 1


def test_cooldown_blocks_restart_across_epochs():
    """冷却期内即使世代变了也不重启（防网络抖动 kill/start 抖动，方案 §14）。"""
    guard = cra.RestartGuard(cooldown=300.0)
    first_ops = _ops()
    second_ops = _ops()
    ledger = cra.RecoveryRestartLedger()

    first = _call(first_ops, network_epoch=2, guard=guard, ledger=ledger, now=100.0)
    second = _call(second_ops, network_epoch=3, guard=guard, ledger=ledger, now=150.0)

    assert first.action == "restarted"
    assert second.action == "skipped"
    assert "冷却" in second.detail
    assert second_ops.killed == []
    assert ledger.restarts == 1


def test_cooldown_expired_allows_new_epoch_restart():
    """冷却过后，新的网络世代可以再次重启。"""
    guard = cra.RestartGuard(cooldown=300.0)
    ledger = cra.RecoveryRestartLedger()

    _call(_ops(), network_epoch=2, guard=guard, ledger=ledger, now=100.0)
    later_ops = _ops()
    later = _call(later_ops, network_epoch=3, guard=guard, ledger=ledger, now=500.0)

    assert later.action == "restarted"
    assert later_ops.killed == [111]


# --------------------------------------------------------------------------- #
# 清退前轻量再确认（fail-closed）
# --------------------------------------------------------------------------- #
def test_recheck_failure_never_kills_any_process():
    """清退前再确认未通过（网络刚断）⇒ **不清退**任何进程（方案 §13）。"""
    ops = _ops()
    ledger = cra.RecoveryRestartLedger()

    result = _call(ops, recheck=lambda: (False, "百度闸门 FAIL（刚断网）"),
                   ledger=ledger)

    assert result.action == "recheck-failed"
    assert ops.killed == [] and ops.launched == []
    assert "百度闸门" in result.recheck_detail
    assert ledger.restarts == 0


def test_recheck_exception_never_kills_any_process():
    """再确认本身抛异常 ⇒ 同样不清退（探测不可信时不做破坏性动作）。"""
    def _boom():
        raise OSError("socket layer unavailable")

    ops = _ops()

    result = _call(ops, recheck=_boom)

    assert result.action == "recheck-failed"
    assert "OSError" in result.detail
    assert ops.killed == [] and ops.launched == []


def test_missing_recheck_is_fail_closed():
    """未提供再确认能力 ⇒ fail-closed，不清退（不假定"刚刚验证通过"仍成立）。"""
    ops = _ops()

    result = _call(ops, recheck=None)

    assert result.action == "recheck-unavailable"
    assert ops.killed == [] and ops.launched == []
    assert ops.list_calls >= 1, "应先做只读发现（记录 UU 是否在跑）"


def test_recheck_accepts_bool_and_tuple():
    """再确认返回值兼容 ``bool`` 与 ``(ok, detail)`` 两种形态。"""
    ok_ops = _ops()
    assert _call(ok_ops, recheck=lambda: True).action == "restarted"

    bad_ops = _ops()
    assert _call(bad_ops, recheck=lambda: False).action == "recheck-failed"
    assert bad_ops.killed == []


# --------------------------------------------------------------------------- #
# 失败如实报告
# --------------------------------------------------------------------------- #
def test_kill_failure_is_reported_and_never_launches():
    """清退失败 ⇒ 中止且**不启动新实例**（避免两个实例抢会话）。"""
    ops = _ops(kill_results=False)
    ledger = cra.RecoveryRestartLedger()

    result = _call(ops, ledger=ledger)

    assert result.action == "failed"
    assert result.ok is False
    assert ops.launched == [], "清退失败却启动了新实例"
    assert ledger.last_epoch == 2, "失败同样消耗名额（方案 §36）"


def test_launch_failure_is_reported():
    """启动命令失败 ⇒ 如实报告 ``failed``（不谎报成功）。"""
    ops = _ops(launch_results=False)

    result = _call(ops)

    assert result.action == "failed"
    assert result.ok is False
    assert ops.killed == [111]
    assert "重启命令执行失败" in result.detail


def test_process_never_returns_is_unconfirmed():
    """命令成功但进程始终没回来 ⇒ ``unconfirmed``（真机实测过「命令成功但没起来」）。"""
    ops = _ops(launch_spawns=False)

    result = _call(ops)

    assert result.action == "unconfirmed"
    assert result.ok is False
    assert result.restarted is False and result.alive is False
    assert ops.launched, "未执行重启命令"
    assert set(ops.launched) == {LAUNCH_COMMAND}
    assert len(ops.launched) <= 2, "确认不到时最多补一次启动（V0.4 既有行为）"


# --------------------------------------------------------------------------- #
# 与恢复协调器的边界（standby 不进入流水线）
# --------------------------------------------------------------------------- #
def test_standby_connected_never_enters_uu_recovery():
    """备用链路刚连接 ⇒ 协调器不产出重启请求，故不会进入 UU 恢复流水线。"""
    coordinator = rec.RecoveryCoordinator()
    coordinator.update_network(network_epoch=1, active_path=rec.PATH_ETHERNET,
                               standby_path=rec.PATH_WIFI)

    plan = coordinator.on_event(rec.RecoveryEvent.STANDBY_CONNECTED, now=0.0,
                               path=rec.PATH_WIFI)
    follow = coordinator.on_verification(_verified("wifi"), now=1.0)

    assert plan.uu_restart is None
    assert follow.uu_restart is None
    assert follow.schedules_uu_restart is False


def test_coordinator_request_drives_recovery_end_to_end():
    """协调器产出重启请求 → 恢复流水线据其证据完成清退重启（离线闭环）。"""
    coordinator = rec.RecoveryCoordinator()
    coordinator.update_network(network_epoch=2, active_path=rec.PATH_WIFI,
                               standby_path=rec.PATH_ETHERNET)
    coordinator.on_event(rec.RecoveryEvent.FAILOVER_COMPLETED, now=10.0,
                         path=rec.PATH_WIFI)
    verification = _verified("wifi")
    follow = coordinator.on_verification(verification, now=11.0)

    assert follow.schedules_uu_restart is True
    request = follow.uu_restart
    ops = _ops()

    result = _call(ops, active_path=request.active_path,
                   network_epoch=request.network_epoch, verification=verification,
                   reason=request.reason)

    assert result.action == "restarted"
    assert result.network_epoch == 2
    assert result.reason == request.reason
    assert ops.killed == [111]


# --------------------------------------------------------------------------- #
# 结果自描述
# --------------------------------------------------------------------------- #
def test_result_is_serializable_and_human_readable():
    """结果须可序列化 + 有人类可读要点（托盘 / 日志用），且不含凭据。"""
    result = _call(_ops())

    payload = result.to_dict()

    assert payload["action"] == "restarted"
    assert payload["attempted"] is True
    assert payload["alive"] is True
    assert payload["killed"] == [111]
    assert set(payload) == {"ok", "action", "detail", "reason", "network_epoch",
                            "active_path", "was_running", "restarted", "alive",
                            "attempted", "killed", "launched", "recheck_detail",
                            "verification_detail"}
    assert len(result.lines()) >= 5
