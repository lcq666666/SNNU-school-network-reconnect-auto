# -*- coding: utf-8 -*-
"""V0.6 Block 3：UU 远程后台重启两档（纯逻辑层）。

本模块只做**判定**，不做任何真实动作：不读进程、不杀进程、不启动进程、
不发网络请求、不读注册表、不碰凭据。执行仍由 ``campus_remote_app`` 负责
（``restart_after_verified_recovery`` / ``restart_user_app``），本模块的
``decide`` 结果供上层在调用执行层之前再把一道关。

设计前提（写给后来人，勿改语义）：
**进程在 ≠ 在线**。UU 远程假死的典型形态正是进程还在（``GameViewer.exe``
在任务管理器里能看到），但远控会话卡在「正在重新连接」，网络并不通。
所以「进程在」只能作为「恢复前 UU 在跑、值得重启」的前提，绝不能反过来
当成「UU 在线、无需重启」的证据；重启与否只看断网事件 + 验证通过这两点。

两档（V0.6 修订触发条件）：
- Tier1（用户态清退）：只清退 ``GameViewer.exe`` /
  ``GameViewerLauncher.exe``，与 V0.4/V0.5 的清退范围完全一致；
- Tier2（升级清退）：Tier1 执行后满 60 秒仍未确认 UU 回来时，在 Tier1
  基础上再清退随服务生命期的 ``GameViewerServer.exe`` /
  ``GameViewerHealthd.exe``。60 秒是宽限期，未满 60 秒继续等 Tier1；
  Tier2 是**同一恢复事件的升级**，不另计一次重启、不消耗新的世代名额、
  不重置冷却。
- ``GameViewerService``（Windows 服务）**永远不在清退范围**：重启服务需
  UAC，属显式授权项，V0.6 也不动它。

与既有模块的关系：
- 进程名常量复用 ``campus_remote_app``（``GAMEVIEWER_USER_PROCESSES`` 等），
  不在本模块另造一份，避免两处常量漂移；
- 冷却默认值 300s 与 ``campus_remote_app.DEFAULT_RESTART_COOLDOWN`` 同源；
- 世代去重语义与 ``campus_recovery.RecoveryCoordinator`` /
  ``campus_remote_app.RecoveryRestartLedger`` 一致：同一 ``network_epoch``
  只重启一次。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from campus_remote_app import (
    DEFAULT_RESTART_COOLDOWN,
    GAMEVIEWER_SERVICE,
    GAMEVIEWER_SERVICE_PROCESSES,
    GAMEVIEWER_USER_PROCESSES,
)

__all__ = [
    "GAMEVIEWER_SERVICE",
    "UU_COOLDOWN_DEFAULT",
    "UU_COOLDOWN_MAX",
    "UU_COOLDOWN_MIN",
    "TIER1_ESCALATION_TIMEOUT",
    "UuTier",
    "UuRestartDecision",
    "UuEscalation",
    "decide",
    "tier1_processes",
    "tier2_processes",
    "should_escalate",
    "confirm_back",
    "diagnose_snapshot",
    "clamp_uu_cooldown",
]

#: Tier1 失败后、确认 UU 回来之前的宽限期（秒）。未满 60 秒继续等 Tier1，
#: 满 60 秒仍未确认回来才升级到 Tier2（见 :func:`should_escalate`）。
TIER1_ESCALATION_TIMEOUT = 60.0

#: UU 重启冷却的下限 / 上限 / 默认值（秒）。默认值与
#: ``campus_remote_app.DEFAULT_RESTART_COOLDOWN`` 同源（300s）。
UU_COOLDOWN_MIN = 60.0
UU_COOLDOWN_MAX = 3600.0
UU_COOLDOWN_DEFAULT = float(DEFAULT_RESTART_COOLDOWN)


class UuTier(Enum):
    """UU 重启的档位。"""

    #: 只清退用户态两进程（V0.4/V0.5 的既有范围）。
    TIER1 = "tier1"
    #: Tier1 基础上再清退随服务生命期的两进程（不碰服务本身）。
    TIER2 = "tier2"


@dataclass(frozen=True)
class UuRestartDecision:
    """一次「是否重启 UU」的判定结果（纯数据，上层据此执行或跳过）。"""

    #: 是否应该重启。``False`` 时 ``tier`` 为 ``None``（没有可执行的档位）。
    should_restart: bool
    tier: Optional[UuTier]
    #: 通过时说明触发依据；否决时说明**哪一条**门禁拦住了（逐条可查）。
    reason: str


@dataclass(frozen=True)
class UuEscalation:
    """一次「是否从 Tier1 升级到 Tier2」的判定结果。"""

    #: Tier1 本身是否成功（``True`` = 已成功，无需升级）。
    tier1_ok: bool
    #: 是否升级到 Tier2。升级属于同一恢复事件，不另计一次重启。
    escalate: bool
    #: 判定依据（成功 / 失败 / 超时未确认回来 / 仍在宽限期内等）。
    reason: str = ""


def tier1_processes() -> Tuple[str, ...]:
    """Tier1 清退清单：用户态两进程（与 V0.4/V0.5 完全一致）。"""
    return tuple(GAMEVIEWER_USER_PROCESSES)


def tier2_processes() -> Tuple[str, ...]:
    """Tier2 清退清单：Tier1 两进程 + 随服务生命期的两进程。

    ``GameViewerService`` 服务本身不在其中（也不在 Tier1 中），见模块 docstring。
    """
    return tuple(GAMEVIEWER_USER_PROCESSES) + tuple(GAMEVIEWER_SERVICE_PROCESSES)


def decide(had_outage: bool, was_running_before_outage: bool,
           verified_recovery: bool, remote_restart_enabled: bool,
           cooldown_ok: bool, epoch_already_restarted: bool) -> UuRestartDecision:
    """判定本次恢复事件是否重启 UU（Tier1），以及否决原因。

    六条门禁**全部**成立才重启，按下列顺序逐条检查，任一不满足即否决，
    ``reason`` 指明是哪一条（调用方原样记日志即可定位）：

    1. ``had_outage``：本次确有断网事件。无断网事件不重启（UU 可能只是
       自身卡住，不归网络恢复流水线管）；
    2. ``was_running_before_outage``：断网**前** UU 在跑。恢复前没在跑不启动
       —— 不替用户把已关闭的 UU 拉起来（V0.4/V0.5 既有规则）；
    3. ``verified_recovery``：恢复已通过接口受限验证（直连验证通过）。
       未验证不重启（进程在 ≠ 在线，见模块 docstring）；
    4. ``remote_restart_enabled``：开关已开。默认关闭不重启（V0.5 安全档）；
    5. ``cooldown_ok``：不在冷却期内。冷却期内不重启（防 kill/start 抖动）；
    6. ``epoch_already_restarted``：本网络世代尚未重启过。同 epoch 已重启过
       不重复（世代去重，与 ``RecoveryRestartLedger`` 语义一致）。

    通过时 ``tier`` 为 :attr:`UuTier.TIER1`；否决时 ``tier`` 为 ``None``。
    """
    if not had_outage:
        return UuRestartDecision(False, None, "无断网事件：不重启 UU")
    if not was_running_before_outage:
        return UuRestartDecision(False, None, "断网前 UU 未在运行：不启动新实例")
    if not verified_recovery:
        return UuRestartDecision(False, None, "恢复未通过接口受限验证：不重启 UU")
    if not remote_restart_enabled:
        return UuRestartDecision(False, None, "UU 自动恢复未开启：不重启 UU")
    if not cooldown_ok:
        return UuRestartDecision(False, None, "冷却期内：不重启 UU")
    if epoch_already_restarted:
        return UuRestartDecision(False, None, "同一网络世代已重启过 UU：不重复重启")
    return UuRestartDecision(True, UuTier.TIER1, "断网后恢复已验证：重启 UU（Tier1）")


def should_escalate(tier1_result_ok: bool, elapsed_since_tier1: float,
                    tier1_confirmed_back: bool) -> UuEscalation:
    """判定 Tier1 之后是否升级到 Tier2（同一恢复事件的升级，不另计一次）。

    规则（``tier1_result_ok`` 为假只表示「Tier1 尚未成功」，不单独区分
    「已失败」与「还在等」；硬失败由执行层如实报告，本函数只看超时）：

    - Tier1 成功（``tier1_result_ok`` 为真）⇒ 不升级；
    - Tier1 未成功、但满 60 秒仍未确认 UU 回来 ⇒ 升级。60 秒是宽限期：
      未满 60 秒（例如 59 秒）继续等 Tier1，不升级；满 60 秒（60 秒与 61 秒）
      仍未确认回来则升级；
    - 未满 60 秒且已确认回来 ⇒ 不升级（Tier1 只是慢了一点，起来了就好）。

    ``elapsed_since_tier1`` 为负数按 0 计（时钟回拨也不应立刻升级）。
    """
    if tier1_result_ok:
        return UuEscalation(tier1_ok=True, escalate=False, reason="Tier1 已成功：不升级")
    elapsed = max(0.0, float(elapsed_since_tier1))
    if tier1_confirmed_back:
        return UuEscalation(tier1_ok=False, escalate=False,
                            reason="Tier1 进程已确认回来：不升级")
    if elapsed >= TIER1_ESCALATION_TIMEOUT:
        return UuEscalation(
            tier1_ok=False, escalate=True,
            reason="Tier1 满 %.0fs 仍未确认 UU 回来：升级到 Tier2" % TIER1_ESCALATION_TIMEOUT)
    return UuEscalation(tier1_ok=False, escalate=False,
                        reason="Tier1 宽限期内（%.0fs/%.0fs）：继续等待，不升级"
                        % (elapsed, TIER1_ESCALATION_TIMEOUT))


def _norm_process_name(name: Any) -> str:
    """进程名归一：去空白 + 小写 + 去掉尾部 ``.exe``。

    与 ``campus_remote_app._norm`` 同规则：PowerShell 取到的进程名常不带
    ``.exe``，而常量按 ``.exe`` 全名书写，两侧须归一后比较。
    """
    text = str(name or "").strip().lower()
    return text[:-4] if text.endswith(".exe") else text


def confirm_back(processes_present: Set[str], expected: Set[str],
                 direct_verified: bool) -> bool:
    """确认 UU 是不是真的回来了（进程回来 **且** 直连验证通过才算）。

    - ``processes_present``：当前在跑的进程名集合；``expected``：期望回来的
      进程名集合（一般取 :func:`tier1_processes` 或 :func:`tier2_processes`）；
    - 进程名按 :func:`_norm_process_name` 归一后比较（大小写 / 有无 ``.exe``
      后缀不影响结果）；
    - ``expected`` 为空 ⇒ 无可确认的对象，按 fail-closed 返回 ``False``；
    - 只满足其中一边（进程全回来但直连未通过，或直连通过但进程缺一个）
      都返回 ``False``：进程在 ≠ 在线，缺一不可。
    """
    if not expected:
        return False
    if not direct_verified:
        return False
    present = {_norm_process_name(name) for name in processes_present}
    wanted = {_norm_process_name(name) for name in expected}
    return wanted <= present


def clamp_uu_cooldown(value: Any) -> float:
    """把 UU 重启冷却限幅到 60..3600 秒，默认 300 秒。

    - 低于 60 ⇒ 60；高于 3600 ⇒ 3600；区间内原值返回；
    - ``None`` / 非数字 / NaN / 无穷大 ⇒ 默认 300（不抛异常，调用方照常跑）。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return UU_COOLDOWN_DEFAULT
    if not math.isfinite(number):
        return UU_COOLDOWN_DEFAULT
    return min(UU_COOLDOWN_MAX, max(UU_COOLDOWN_MIN, number))


def _as_pid(value: Any) -> Optional[int]:
    """把任意 pid 表示规整为正整数；非法返回 ``None``（绝不抛错）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _pid_label(value: Any) -> str:
    """诊断行里的 PID 写法：合法 pid 用数字，非法用「未知」。"""
    pid = _as_pid(value)
    return str(pid) if pid is not None else "未知"


def _tcp_label(row: Dict[str, Any]) -> str:
    """一条 TCP 连接的诊断片段：只取白名单字段，绝不输出凭据类字段。"""
    state = str(row.get("state") or "").strip() or "未知状态"
    local = str(row.get("local") or row.get("local_address") or "").strip()
    remote = str(row.get("remote") or row.get("remote_address") or "").strip()
    parts = ["TCP %s" % state]
    if local:
        parts.append("本地 %s" % local)
    if remote:
        parts.append("远端 %s" % remote)
    return " ".join(parts)


def diagnose_snapshot(processes: Sequence[Dict[str, Any]],
                      tcp_connections: Sequence[Dict[str, Any]]) -> List[str]:
    """把 UU 进程快照 + TCP 连接快照整理成按进程分组的诊断行（供假死时记日志）。

    输入只取白名单字段：进程行取 ``pid`` / ``name``（或 ``process_name``），
    TCP 行取 ``pid`` / ``local`` / ``remote`` / ``state``；其余字段
    （含调用方误传的密码 / token 类字段）一律忽略，**绝不**出现在输出里。

    输出规则：

    - 进程与 TCP 按 pid 分组，进程按 pid 升序、每组内 TCP 按远端地址排序，
      顺序确定，便于两次快照对照；
    - 每个进程至少一行：有 TCP 连接时每条连接一行，无连接时一行
      「无 TCP 连接」；
    - TCP 行的 pid 不在进程列表中时，单独成组，标注「未在进程列表中」；
    - 进程与 TCP 都为空 ⇒ ``["UU 进程未发现"]``。
    """
    proc_rows = [row for row in processes if isinstance(row, dict)]
    tcp_rows = [row for row in tcp_connections if isinstance(row, dict)]
    if not proc_rows and not tcp_rows:
        return ["UU 进程未发现"]

    lines: List[str] = []
    if not proc_rows:
        lines.append("UU 进程未发现")

    # 按 pid 分组 TCP 连接（pid 非法的一律归入「未知」组，排在最后）。
    by_pid: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for row in tcp_rows:
        by_pid.setdefault(_as_pid(row.get("pid")), []).append(row)

    def _sorted_pids(pids: Iterable[Optional[int]]) -> List[Optional[int]]:
        known = sorted(pid for pid in pids if pid is not None)
        return known + ([None] if None in set(pids) else [])

    # 进程组：按 pid 升序；pid 非法的进程排在最后。
    def _proc_sort_key(row: Dict[str, Any]) -> Tuple[int, int]:
        pid = _as_pid(row.get("pid"))
        return (0, pid) if pid is not None else (1, 0)

    seen_pids: Set[Optional[int]] = set()
    for row in sorted(proc_rows, key=_proc_sort_key):
        pid = _as_pid(row.get("pid"))
        seen_pids.add(pid)
        name = str(row.get("name") or row.get("process_name") or "").strip() or "未知进程"
        header = "PID %s %s" % (_pid_label(row.get("pid")), name)
        conns = by_pid.get(pid, [])
        if not conns:
            lines.append("%s：无 TCP 连接" % header)
            continue
        for conn in sorted(conns, key=lambda c: str(c.get("remote") or "")):
            lines.append("%s：%s" % (header, _tcp_label(conn)))

    # 不在进程列表中的 TCP 连接：单独成组（假死排查时别漏了幽灵连接）。
    for pid in _sorted_pids(set(by_pid) - seen_pids):
        for conn in sorted(by_pid[pid], key=lambda c: str(c.get("remote") or "")):
            lines.append("PID %s（未在进程列表中）：%s"
                         % (_pid_label(pid), _tcp_label(conn)))
    return lines
