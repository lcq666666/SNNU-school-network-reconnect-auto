# -*- coding: utf-8 -*-
"""V0.6 Block 3：UU 远程后台重启两档（``campus_uu_recovery``）测试。

覆盖（每条对应 ``campus_uu_recovery`` 的一条规则）：

- ``decide``：通过分支 + 六条门禁各自的否决分支（无断网 / 恢复前未运行 /
  未验证 / 开关关闭 / 冷却期 / 同 epoch 已重启）+ 多条否决同时成立时的顺序；
- 两档进程清单：Tier1 恰为用户态两进程、Tier2 为 Tier1 + 随服务两进程、
  ``GameViewerService`` 永远不在清退范围；
- ``should_escalate``：Tier1 失败立即升级、Tier1 成功不升级、59s/60s/61s
  边界、已确认回来不升级；
- ``confirm_back``：进程全回来且直连通过才真，其余组合一律假；
- ``diagnose_snapshot``：按进程分组、空输入、无连接进程、未知 pid 的连接、
  不输出凭据类字段；
- ``clamp_uu_cooldown``：60..3600 限幅、默认值 300、非法输入回默认。

约束：零真实进程 / 网络 / 注册表 / 凭据 / 浏览器 / 真实 sleep，全是纯逻辑。
"""
from __future__ import annotations

import campus_uu_recovery as uur
from campus_uu_recovery import UuTier

# 通过分支的基准入参：六条门禁全成立。
PASS = dict(had_outage=True, was_running_before_outage=True,
            verified_recovery=True, remote_restart_enabled=True,
            cooldown_ok=True, epoch_already_restarted=False)


def _decide(**overrides):
    """``decide`` 的基准调用，``overrides`` 只改其中一条门禁。"""
    kwargs = dict(PASS)
    kwargs.update(overrides)
    return uur.decide(**kwargs)


# --------------------------------------------------------------------------- #
# decide：通过分支
# --------------------------------------------------------------------------- #
def test_decide_pass_returns_tier1():
    """六条门禁全成立 ⇒ 重启，档位 Tier1。"""
    decision = _decide()

    assert decision.should_restart is True
    assert decision.tier is UuTier.TIER1
    assert "Tier1" in decision.reason


# --------------------------------------------------------------------------- #
# decide：六条否决分支（每条单独否决）
# --------------------------------------------------------------------------- #
def test_decide_no_outage_never_restarts():
    """无断网事件 ⇒ 不重启（UU 自身卡住不归网络恢复流水线管）。"""
    decision = _decide(had_outage=False)

    assert decision.should_restart is False
    assert decision.tier is None
    assert "无断网" in decision.reason


def test_decide_not_running_before_outage_never_starts():
    """断网前 UU 没在跑 ⇒ 不启动（不替用户把已关闭的 UU 拉起来）。"""
    decision = _decide(was_running_before_outage=False)

    assert decision.should_restart is False
    assert decision.tier is None
    assert "未在运行" in decision.reason


def test_decide_unverified_never_restarts():
    """恢复未通过接口受限验证 ⇒ 不重启（进程在 ≠ 在线）。"""
    decision = _decide(verified_recovery=False)

    assert decision.should_restart is False
    assert decision.tier is None
    assert "未通过" in decision.reason


def test_decide_disabled_never_restarts():
    """开关默认关闭 ⇒ 不重启（V0.5 安全档）。"""
    decision = _decide(remote_restart_enabled=False)

    assert decision.should_restart is False
    assert decision.tier is None
    assert "未开启" in decision.reason


def test_decide_cooldown_never_restarts():
    """冷却期内 ⇒ 不重启（防 kill/start 抖动）。"""
    decision = _decide(cooldown_ok=False)

    assert decision.should_restart is False
    assert decision.tier is None
    assert "冷却" in decision.reason


def test_decide_same_epoch_already_restarted_never_repeats():
    """同一网络世代已重启过 ⇒ 不重复重启（世代去重）。"""
    decision = _decide(epoch_already_restarted=True)

    assert decision.should_restart is False
    assert decision.tier is None
    assert "已重启过" in decision.reason


def test_decide_veto_order_is_deterministic():
    """多条门禁同时不满足时，按固定顺序报第一条（日志可定位）。"""
    decision = _decide(had_outage=False, verified_recovery=False,
                       remote_restart_enabled=False)

    assert decision.should_restart is False
    assert "无断网" in decision.reason


def test_decide_each_veto_has_distinct_reason():
    """六条否决分支的 reason 各不相同（排查时能一眼区分是哪条门禁）。"""
    reasons = {
        _decide(had_outage=False).reason,
        _decide(was_running_before_outage=False).reason,
        _decide(verified_recovery=False).reason,
        _decide(remote_restart_enabled=False).reason,
        _decide(cooldown_ok=False).reason,
        _decide(epoch_already_restarted=True).reason,
    }

    assert len(reasons) == 6


# --------------------------------------------------------------------------- #
# 两档进程清单
# --------------------------------------------------------------------------- #
def test_tier1_processes_are_user_processes_only():
    """Tier1 恰为用户态两进程，不多不少。"""
    assert uur.tier1_processes() == ("GameViewer.exe", "GameViewerLauncher.exe")


def test_tier2_processes_add_service_children():
    """Tier2 = Tier1 + 随服务生命期的两进程。"""
    assert uur.tier2_processes() == (
        "GameViewer.exe", "GameViewerLauncher.exe",
        "GameViewerServer.exe", "GameViewerHealthd.exe")
    assert uur.tier2_processes()[:2] == uur.tier1_processes()


def test_gameviewer_service_never_in_eviction_scope():
    """GameViewerService 永远不在清退范围（重启服务需 UAC，V0.6 也不动）。"""
    assert uur.GAMEVIEWER_SERVICE == "GameViewerService"
    assert uur.GAMEVIEWER_SERVICE not in uur.tier1_processes()
    assert uur.GAMEVIEWER_SERVICE not in uur.tier2_processes()
    assert "GameViewerService.exe" not in uur.tier1_processes()
    assert "GameViewerService.exe" not in uur.tier2_processes()


def test_process_lists_are_tuples():
    """两档清单返回元组（调用方改不动模块内的常量）。"""
    assert isinstance(uur.tier1_processes(), tuple)
    assert isinstance(uur.tier2_processes(), tuple)


# --------------------------------------------------------------------------- #
# should_escalate：升级判定与 60 秒边界
# --------------------------------------------------------------------------- #
def test_escalate_pending_within_grace_period_waits():
    """Tier1 尚未成功、刚执行完（0 秒）仍未确认回来 ⇒ 继续等，不升级。

    ``tier1_result_ok`` 为假只表示「尚未成功」，升级只看 60 秒宽限期是否
    耗尽（硬失败由执行层如实报告，不经本函数立即升级）。
    """
    result = uur.should_escalate(False, 0.0, False)

    assert result.tier1_ok is False
    assert result.escalate is False


def test_escalate_tier1_success_never_escalates():
    """Tier1 成功 ⇒ 不升级（即使 elapsed 很大也不升级）。"""
    result = uur.should_escalate(True, 3600.0, True)

    assert result.tier1_ok is True
    assert result.escalate is False


def test_escalate_before_timeout_waits():
    """Tier1 未失败、59 秒仍未确认回来 ⇒ 继续等，不升级。"""
    result = uur.should_escalate(False, 59.0, False)

    assert result.escalate is False
    assert "宽限期" in result.reason


def test_escalate_at_timeout_escalates():
    """Tier1 未失败、满 60 秒仍未确认回来 ⇒ 升级（边界含 60 秒整）。"""
    result = uur.should_escalate(False, 60.0, False)

    assert result.escalate is True


def test_escalate_after_timeout_escalates():
    """Tier1 未失败、61 秒仍未确认回来 ⇒ 升级。"""
    result = uur.should_escalate(False, 61.0, False)

    assert result.escalate is True


def test_escalate_confirmed_back_never_escalates():
    """Tier1 只是慢了一点、61 秒时已确认回来 ⇒ 不升级。"""
    result = uur.should_escalate(False, 61.0, True)

    assert result.escalate is False


def test_escalate_negative_elapsed_counts_as_zero():
    """elapsed 为负（时钟回拨）按 0 计：仍在宽限期内，不升级。"""
    result = uur.should_escalate(False, -5.0, False)

    assert result.escalate is False


# --------------------------------------------------------------------------- #
# confirm_back：进程回来 + 直连通过，缺一不可
# --------------------------------------------------------------------------- #
def test_confirm_back_all_present_and_verified():
    """进程全回来且直连验证通过 ⇒ 真回来了。"""
    assert uur.confirm_back(
        {"GameViewer.exe", "GameViewerLauncher.exe"},
        {"GameViewer.exe", "GameViewerLauncher.exe"},
        True) is True


def test_confirm_back_missing_process_is_false():
    """缺一个进程 ⇒ 假（即使直连通过）。"""
    assert uur.confirm_back(
        {"GameViewer.exe"}, {"GameViewer.exe", "GameViewerLauncher.exe"},
        True) is False


def test_confirm_back_unverified_is_false():
    """进程全回来但直连未通过 ⇒ 假（进程在 ≠ 在线）。"""
    assert uur.confirm_back(
        {"GameViewer.exe", "GameViewerLauncher.exe"},
        {"GameViewer.exe", "GameViewerLauncher.exe"},
        False) is False


def test_confirm_back_both_missing_is_false():
    """进程缺了、直连也没通过 ⇒ 假。"""
    assert uur.confirm_back(set(), {"GameViewer.exe"}, False) is False


def test_confirm_back_empty_expected_is_false():
    """期望集合为空 ⇒ 无可确认的对象，按 fail-closed 返回假。"""
    assert uur.confirm_back({"GameViewer.exe"}, set(), True) is False


def test_confirm_back_name_normalization():
    """进程名大小写 / 有无 .exe 后缀不影响比较结果。"""
    assert uur.confirm_back(
        {"gameviewer", "GAMEVIEWERLAUNCHER"},
        {"GameViewer.exe", "GameViewerLauncher.exe"}, True) is True


# --------------------------------------------------------------------------- #
# diagnose_snapshot：按进程分组的诊断行
# --------------------------------------------------------------------------- #
PROCESSES = [
    {"pid": 111, "name": "GameViewer.exe"},
    {"pid": 222, "name": "GameViewerLauncher.exe"},
]
CONNECTIONS = [
    {"pid": 111, "local": "192.168.1.5:50001", "remote": "10.0.0.1:443",
     "state": "ESTABLISHED"},
    {"pid": 111, "local": "192.168.1.5:50002", "remote": "10.0.0.2:443",
     "state": "TIME_WAIT"},
    {"pid": 222, "local": "192.168.1.5:50003", "remote": "10.0.0.1:443",
     "state": "ESTABLISHED"},
]


def test_diagnose_snapshot_groups_by_process():
    """诊断行按进程分组：每条含 PID 与 TCP 状态，进程 111 的两条连在一起。"""
    lines = uur.diagnose_snapshot(PROCESSES, CONNECTIONS)

    assert len(lines) == 3
    assert lines[0].startswith("PID 111 GameViewer.exe")
    assert lines[1].startswith("PID 111 GameViewer.exe")
    assert lines[2].startswith("PID 222 GameViewerLauncher.exe")
    assert "ESTABLISHED" in lines[0] and "TIME_WAIT" in lines[1]
    assert "ESTABLISHED" in lines[2]
    assert "10.0.0.1:443" in lines[0]


def test_diagnose_snapshot_empty_input():
    """进程与 TCP 都为空 ⇒ 一行明确的「UU 进程未发现」。"""
    assert uur.diagnose_snapshot([], []) == ["UU 进程未发现"]


def test_diagnose_snapshot_process_without_tcp():
    """有进程、无 TCP 连接 ⇒ 该进程一行「无 TCP 连接」（假死时这行就是证据）。"""
    lines = uur.diagnose_snapshot(PROCESSES, [])

    assert lines == ["PID 111 GameViewer.exe：无 TCP 连接",
                     "PID 222 GameViewerLauncher.exe：无 TCP 连接"]


def test_diagnose_snapshot_unknown_pid_connection():
    """TCP 行的 pid 不在进程列表中 ⇒ 单独成组，标注「未在进程列表中」。"""
    lines = uur.diagnose_snapshot(
        PROCESSES,
        [{"pid": 999, "local": "192.168.1.5:50009", "remote": "10.0.0.9:443",
          "state": "CLOSE_WAIT"}])

    assert lines[-1].startswith("PID 999（未在进程列表中）")
    assert "CLOSE_WAIT" in lines[-1]
    assert "无 TCP 连接" in lines[0] and "无 TCP 连接" in lines[1]


def test_diagnose_snapshot_never_leaks_credentials():
    """输入里误带的密码 / token 类字段绝不出现在输出里。"""
    lines = uur.diagnose_snapshot(
        [{"pid": 111, "name": "GameViewer.exe", "password": "secret-pw",
          "token": "secret-token"}],
        [{"pid": 111, "local": "192.168.1.5:50001", "remote": "10.0.0.1:443",
          "state": "ESTABLISHED", "password": "secret-pw"}])

    assert len(lines) == 1
    assert "secret-pw" not in lines[0] and "secret-token" not in lines[0]
    assert "PID 111" in lines[0] and "ESTABLISHED" in lines[0]


def test_diagnose_snapshot_tolerates_dirty_rows():
    """缺 pid / 缺名字 / 非字典行都不抛异常，照常出诊断行。"""
    lines = uur.diagnose_snapshot(
        [{"name": "GameViewer.exe"}, {"pid": "bad", "name": ""}, "not-a-dict"],
        [{"pid": "bad", "state": ""}, None])

    assert lines  # 至少有行，不抛错
    assert any("GameViewer.exe" in line for line in lines)
    assert any("未知" in line for line in lines)


# --------------------------------------------------------------------------- #
# clamp_uu_cooldown：冷却限幅 60..3600，默认 300
# --------------------------------------------------------------------------- #
def test_clamp_cooldown_default_and_bounds():
    """默认值 300；上下界原值返回。"""
    assert uur.UU_COOLDOWN_DEFAULT == 300.0
    assert uur.clamp_uu_cooldown(300) == 300.0
    assert uur.clamp_uu_cooldown(60) == 60.0
    assert uur.clamp_uu_cooldown(3600) == 3600.0


def test_clamp_cooldown_clamps_out_of_range():
    """低于 60 抬到 60，高于 3600 压到 3600；区间内原值返回。"""
    assert uur.clamp_uu_cooldown(0) == 60.0
    assert uur.clamp_uu_cooldown(30) == 60.0
    assert uur.clamp_uu_cooldown(7200) == 3600.0
    assert uur.clamp_uu_cooldown(120) == 120.0


def test_clamp_cooldown_invalid_input_returns_default():
    """None / 非数字 / NaN / 无穷大 ⇒ 默认 300，不抛异常。"""
    assert uur.clamp_uu_cooldown(None) == 300.0
    assert uur.clamp_uu_cooldown("not-a-number") == 300.0
    assert uur.clamp_uu_cooldown(float("nan")) == 300.0
    assert uur.clamp_uu_cooldown(float("inf")) == 300.0
    assert uur.clamp_uu_cooldown("120") == 120.0
