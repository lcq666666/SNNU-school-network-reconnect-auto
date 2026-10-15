# -*- coding: utf-8 -*-
"""UU 远程（网易 GameViewer）用户态后台**识别 + 只读发现**（V0.4-2）。

需求原文（2026-10-16 用户）：UU 远程 app 在「断网 → 重连」后会卡在「正在重新连接」
而连不上本机，导致无法远控；希望确认网络**真的**连上后把 UU 远程后台清退重启。

真机取证（2026-10-16，只读）：
- 用户态：``GameViewer.exe``、``GameViewerLauncher.exe``（＋辅助 ``uuyc-cli.exe`` /
  ``uuyc-mux.exe``）
- 随服务生命期：``GameViewerServer.exe``、``GameViewerHealthd.exe``
- Windows 服务：``GameViewerService``（Auto/Running）
- 自启项：``HKCU\\...\\Run`` → ``GameViewer --auto-run``

安全边界（方案 §3.3 + AGENTS §6，**全阶段有效**）：
1. 本模块只提供能力，**默认关闭**：``remote_restart_enabled=False``，不调用零副作用；
2. 清退范围仅限**用户态**两进程；``GameViewerServer/Healthd`` 与
   ``GameViewerService`` **不动**（重启服务需 UAC，属显式授权项）；
3. 两次真实清退间隔 ≥ ``remote_restart_cooldown``（默认 300s）；
4. 失败**如实报告**（退出码/启动器缺失），不静默重试；
5. 全部副作用经注入的 :class:`RemoteAppOps`，离线测试零真实进程操作。

本文件（V0.4-2）只实现只读部分：:class:`AppProcess` / :class:`RemoteAppStatus` /
:func:`discover` / :func:`launch_command` / :func:`format_status`。
清退/重启动作在 V0.4-3 追加。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "GAMEVIEWER_USER_PROCESSES",
    "GAMEVIEWER_SERVICE_PROCESSES",
    "GAMEVIEWER_SERVICE",
    "GAMEVIEWER_PATTERNS",
    "DEFAULT_RESTART_COOLDOWN",
    "READ_ONLY_QUERIES",
    "AppProcess",
    "RemoteAppStatus",
    "RemoteAppOps",
    "RestartResult",
    "RestartGuard",
    "discover",
    "launch_command",
    "format_status",
    "restart_user_app",
    "build_windows_remote_app_ops",
]

#: 清退范围内（用户态）的进程名 —— 方案 §3.2。
GAMEVIEWER_USER_PROCESSES: Tuple[str, ...] = (
    "GameViewer.exe",
    "GameViewerLauncher.exe",
)
#: 随服务生命期的进程：**不清退**（方案 §3.2）。
GAMEVIEWER_SERVICE_PROCESSES: Tuple[str, ...] = (
    "GameViewerServer.exe",
    "GameViewerHealthd.exe",
)
#: Windows 服务名：**不重启**（需 UAC，属危险操作黑名单外的显式授权项）。
GAMEVIEWER_SERVICE = "GameViewerService"
#: 识别关键字（进程名子串，大小写不敏感）——与 ``campus_remote`` 规则保持一致。
GAMEVIEWER_PATTERNS: Tuple[str, ...] = ("gameviewer", "uuyc")
#: 两次真实清退的最小间隔（秒）——方案 §3.3。
DEFAULT_RESTART_COOLDOWN = 300.0

#: 重启后**确认进程真的回来**的轮询次数与间隔（stub 会立刻退出、主程序另起，
#: 故必须以"进程出现"为准；真机实测约 1–3s，给足余量）。
RESTART_CONFIRM_ATTEMPTS = 4
RESTART_CONFIRM_INTERVAL = 2.5

#: 可注入的休眠：默认测试集**零真实 sleep**（AGENTS §2.4），由测试替换为空操作。
_SLEEP = time.sleep

def _norm(name: Any) -> str:
    """进程名归一：小写 + **去掉尾部 ``.exe``**。

    真机取证（2026-10-16）：PowerShell ``Get-Process | Select-Object ProcessName``
    返回的名字**不带扩展名**（``GameViewer`` 而非 ``GameViewer.exe``），而
    ``GAMEVIEWER_*_PROCESSES`` 常量按用户可见的 ``.exe`` 全名书写；两侧必须归一后
    比较，否则真机上永远匹配不到（本模块 V0.4-2 真机只读取证实测到的缺口）。
    """
    text = str(name or "").strip().lower()
    return text[:-4] if text.endswith(".exe") else text


_USER_SCOPE = frozenset(_norm(name) for name in GAMEVIEWER_USER_PROCESSES)
_SERVICE_SCOPE = frozenset(_norm(name) for name in GAMEVIEWER_SERVICE_PROCESSES)


def _as_pid(value: Any) -> Optional[int]:
    """把任意 pid 表示规整为**正整数**；非法一律返回 ``None``（绝不抛错）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


@dataclass(frozen=True)
class AppProcess:
    """UU 远程进程族中的一个进程（只读快照）。"""

    pid: int
    name: str
    path: str = ""

    @classmethod
    def from_mapping(cls, row: Any) -> Optional["AppProcess"]:
        """从探测行构造；缺 pid / 名字的脏数据按 ``None`` 丢弃。"""
        if not isinstance(row, dict):
            return None
        pid = _as_pid(row.get("pid"))
        name = str(row.get("process_name") or row.get("name") or "").strip()
        if pid is None or not name:
            return None
        return cls(pid=pid, name=name, path=str(row.get("path") or ""))

    @property
    def in_restart_scope(self) -> bool:
        """是否属于「清退后重新启动」的用户态范围。"""
        return _norm(self.name) in _USER_SCOPE

    @property
    def is_service_child(self) -> bool:
        """是否随 ``GameViewerService`` 生命期（**不清退**）。"""
        return _norm(self.name) in _SERVICE_SCOPE


@dataclass
class RemoteAppStatus:
    """一次**只读**发现的完整结果（进程族 / 服务 / 自启项）。"""

    processes: List[AppProcess] = field(default_factory=list)
    service_state: Dict[str, Any] = field(default_factory=dict)
    run_entries: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def user_processes(self) -> List[AppProcess]:
        return [p for p in self.processes if p.in_restart_scope]

    @property
    def service_processes(self) -> List[AppProcess]:
        return [p for p in self.processes if p.is_service_child]

    @property
    def other_processes(self) -> List[AppProcess]:
        """同族里既不在清退范围、也非服务子进程的进程（辅助进程，**一律不动**）。

        如实列出，避免"没显示就当没在跑"的盲区（真机实测存在 ``uuyc-*``）。
        """
        return [p for p in self.processes
                if not p.in_restart_scope and not p.is_service_child]

    @property
    def running(self) -> bool:
        """UU 远程进程族是否有任何进程在跑。"""
        return bool(self.processes)

    @property
    def service_running(self) -> bool:
        return str(self.service_state.get("status") or "").lower() == "running"

    def lines(self) -> List[str]:
        """人类可读要点（CLI ``remote-app --status``）。"""
        out: List[str] = []
        out.append("UU 远程（网易 GameViewer）只读发现 —— 不切换路由、不清退任何进程")
        out.append("服务          : %s（%s / %s）" % (
            GAMEVIEWER_SERVICE,
            self.service_state.get("status") or "未知",
            self.service_state.get("start_type") or "未知"))
        out.append("清退范围进程  : %s" % (_join([p.name for p in self.user_processes])
                                          or "（无）"))
        out.append("随服务进程    : %s" % (_join([p.name for p in self.service_processes])
                                          or "（无）"))
        out.append("同族其他进程  : %s（不动）"
                   % (_join([p.name for p in self.other_processes]) or "（无）"))
        command = launch_command(self)
        out.append("重启命令      : %s" % (command or "（未找到启动器/自启项）"))
        if self.error:
            out.append("发现失败      : %s" % self.error)
        return out


def _join(names: Any) -> str:
    return "、".join(str(name) for name in names)


def _mapping_rows(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [dict(row) for row in raw if isinstance(row, dict)]


def discover(ops: Any) -> RemoteAppStatus:
    """**只读**发现 UU 远程进程族 / 服务 / 自启项（零副作用，绝不抛异常）。

    ``ops`` 为 :class:`RemoteAppOps` 同构对象（真实实现在
    :func:`build_windows_remote_app_ops`；离线测试用
    :class:`tests.fakes.FakeRemoteAppOps`）。任一只读源失败只记 ``error``，
    其余结果照常返回。
    """
    status = RemoteAppStatus()
    errors: List[str] = []

    try:
        rows = ops.list_processes()
    except Exception as exc:                                 # noqa: BLE001 - 探测源不可信
        errors.append("读取进程失败：%s" % type(exc).__name__)
    else:
        for row in _mapping_rows(rows):
            proc = AppProcess.from_mapping(row)
            if proc is not None:
                status.processes.append(proc)

    try:
        status.service_state = dict(ops.service_state() or {})
        if not isinstance(status.service_state, dict):
            status.service_state = {}
    except Exception as exc:                                 # noqa: BLE001
        errors.append("读取服务失败：%s" % type(exc).__name__)

    try:
        status.run_entries = _mapping_rows(ops.run_entries())
    except Exception as exc:                                 # noqa: BLE001
        errors.append("读取自启项失败：%s" % type(exc).__name__)

    status.error = "；".join(errors)
    return status


def _path_of(status: RemoteAppStatus, names: Tuple[str, ...]) -> str:
    wanted = {_norm(name) for name in names}
    for proc in status.processes:
        if _norm(proc.name) in wanted and proc.path:
            return proc.path
    return ""


def launch_command(status: RemoteAppStatus) -> str:
    """决定「重启 UU 远程」用的命令（**按用户原本的启动方式**，只读推导）。

    优先级：
    1. 自启项里指向 GameViewer 的命令（真机实测为 ``GameViewer --auto-run``）——
       最贴近用户日常启动路径；
    2. 已在运行的 ``GameViewerLauncher.exe`` / ``GameViewer.exe`` 的可执行路径
       （带引号，避免路径含空格被拆）。
    找不到则返回 ``""`` ⇒ 调用方须 **fail-closed**（不做任何改动）。
    """
    for row in status.run_entries:
        command = str(row.get("command") or "").strip()
        if not command:
            continue
        lowered = command.lower()
        if any(pattern in lowered for pattern in GAMEVIEWER_PATTERNS):
            return command

    path = _path_of(status, ("GameViewerLauncher.exe",)) \
        or _path_of(status, ("GameViewer.exe",))
    if not path:
        return ""
    return '"%s"' % path


def format_status(status: RemoteAppStatus) -> str:
    """渲染 ``remote-app --status`` 报告。"""
    lines: List[str] = []
    lines.append("=" * 72)
    lines.extend(status.lines())
    lines.append("=" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 只读能力集合（真实实现经 P0 netdiag 只读管道，写命令 fail-closed 拒绝）
# --------------------------------------------------------------------------- #
#: 本模块自带的只读查询（首段以 ``Get-`` 开头，逐条经
#: :func:`tools.netdiag.assert_read_only` 自检后才执行）。
READ_ONLY_QUERIES: Dict[str, str] = {
    "app_processes": (
        "Get-Process | Where-Object { $_.ProcessName -match 'gameviewer|uuyc' }"
        " | Select-Object Id,ProcessName,Path | ConvertTo-Json -Depth 2 -Compress"),
    "app_service": (
        "Get-Service -Name GameViewerService -ErrorAction SilentlyContinue"
        " | Select-Object Name,Status,StartType | ConvertTo-Json -Depth 2 "
        "-Compress"),
}

#: ``ServiceControllerStatus`` 枚举 → 名称（PowerShell ``ConvertTo-Json`` 会渲染成整数）。
_SERVICE_STATUS_NAMES = {1: "Stopped", 2: "StartPending", 3: "StopPending",
                         4: "Running", 5: "ContinuePending",
                         6: "PausePending", 7: "Paused"}
#: ``ServiceStartMode`` 枚举 → 名称。
_START_TYPE_NAMES = {0: "Boot", 1: "System", 2: "Automatic", 3: "Manual",
                     4: "Disabled"}


def _enum_name(value: Any, names: Dict[int, str]) -> str:
    """把 PowerShell 枚举值（可能是整数/数字串/已命名字符串）规整为名称。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        return names.get(int(value), str(value))
    return str(value)


@dataclass(frozen=True)
class RemoteAppOps:
    """UU 远程能力集合。

    前三项**只读**；后两项**有副作用**，且只允许由
    :func:`restart_user_app`（显式调用）使用：

    - ``kill(pid) -> bool``：清退单个用户态进程（``campus_windows.kill_process``）；
    - ``launch(command) -> bool``：分离式重启（``campus_windows.launch_detached``）。
    """

    list_processes: Callable[[], List[Dict[str, Any]]]
    service_state: Callable[[], Dict[str, Any]]
    run_entries: Callable[[], List[Dict[str, Any]]]
    kill: Callable[[int], bool]
    launch: Callable[[str], bool]


def _read_only(runner: Any, name: str, script: str) -> str:
    """执行一条**只读**查询并返回 stdout（失败返回空串，不抛异常）。"""
    try:
        _rc, out, _err = runner.run(name, script)
    except Exception:                                        # noqa: BLE001
        return ""
    return out or ""


def build_windows_remote_app_ops(runner: Any = None) -> RemoteAppOps:
    """组装真实**只读**能力集合。

    ``runner`` 为 :class:`tools.netdiag.PowerShellRunner`（逐命令
    ``assert_read_only`` fail-closed）或同构假件。本函数**不引入任何写命令**。
    """
    from tools import netdiag

    if runner is None:
        runner = netdiag.PowerShellRunner()

    def _list_processes() -> List[Dict[str, Any]]:
        return netdiag.parse_remote_processes(
            _read_only(runner, "app_processes", READ_ONLY_QUERIES["app_processes"]))

    def _service_state() -> Dict[str, Any]:
        text = _read_only(runner, "app_service", READ_ONLY_QUERIES["app_service"])
        try:
            data: Any = json.loads(text.strip()) if text.strip() else None
        except ValueError:
            data = None
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return {}
        return {"name": str(data.get("Name") or GAMEVIEWER_SERVICE),
                "status": _enum_name(data.get("Status"), _SERVICE_STATUS_NAMES),
                "start_type": _enum_name(data.get("StartType"), _START_TYPE_NAMES)}

    def _run_entries() -> List[Dict[str, Any]]:
        return netdiag.parse_run_entries(
            _read_only(runner, "run_entries", netdiag.COMMANDS["run_entries"]))

    def _kill(pid: int) -> bool:
        from campus_windows import kill_process

        ok, detail = kill_process(pid)
        return bool(ok)

    def _launch(command: str) -> bool:
        from campus_windows import launch_detached

        ok, detail = launch_detached(command)
        return bool(ok)

    return RemoteAppOps(list_processes=_list_processes,
                        service_state=_service_state,
                        run_entries=_run_entries,
                        kill=_kill,
                        launch=_launch)


# --------------------------------------------------------------------------- #
# V0.4-3 清退 / 重启动作（**有副作用**，须显式调用）
# --------------------------------------------------------------------------- #
@dataclass
class RestartResult:
    """一次清退/重启的结构化结果（如实报告，绝不谎报成功）。"""

    ok: bool
    action: str          # restart / noop / skipped / failed / unconfirmed
    detail: str
    killed: List[int] = field(default_factory=list)
    launched: str = ""


class RestartGuard:
    """清退冷却（方案 §3.3）：两次**真实**清退间隔 ≥ ``cooldown`` 秒。

    时钟可注入（测试零真实 sleep）；只有**真正完成**的重启才计入冷却
    （失败不计，避免「失败一次要等 5 分钟才能补救」）。

    已知取舍：``last_at`` 只在进程内保存（不落盘），故**托盘重启会重置冷却**。
    冷却用于抑制同一运行期内的连环触发，这一取舍已记录在方案 §3.3。
    """

    def __init__(self, cooldown: float = DEFAULT_RESTART_COOLDOWN,
                 clock: Callable[[], float] = time.time) -> None:
        self.cooldown = float(cooldown)
        self._clock = clock
        self.last_at: Optional[float] = None

    def allow(self, now: Optional[float] = None) -> Tuple[bool, str]:
        """是否允许再次真实清退（返回 ``(允许, 拒绝原因)``）。"""
        if self.last_at is None:
            return True, ""
        current = self._clock() if now is None else float(now)
        elapsed = current - float(self.last_at)
        if elapsed >= self.cooldown:
            return True, ""
        return False, "冷却中：距上次清退 %.0fs（需 ≥%.0fs）" % (elapsed, self.cooldown)

    def mark(self, now: Optional[float] = None) -> None:
        """记录一次**真实**清退时刻。"""
        self.last_at = self._clock() if now is None else float(now)


def restart_user_app(ops: Any, *, status: Optional[RemoteAppStatus] = None,
                     guard: Optional[RestartGuard] = None,
                     now: Optional[float] = None,
                     sleep: Optional[Callable[[float], None]] = None) -> RestartResult:
    """清退并重启 UU 远程**用户态**进程（方案 §3.2/§3.3）。

    行为约束（每条都对应一个单测）：

    1. **只动用户态两进程**：``GameViewer`` / ``GameViewerLauncher``；
       ``GameViewerServer`` / ``GameViewerHealthd`` / ``GameViewerService`` 一律不动；
    2. **没在跑就不动**：不替用户把已关闭的 UU 启动起来（``action="noop"``）；
    3. **找不到重启命令 ⇒ fail-closed**：一个进程都不清退（避免清退了却起不来）；
    4. **清退失败立即中止**且不启动新实例（避免两个实例抢会话）；
    5. **启动失败如实报告**（``action="failed"``，带明细）；
    6. **启动成功 ≠ 起来了**：必须确认用户态进程真的出现；确认不到则补一次启动，
       仍确认不到 ⇒ ``action="unconfirmed"`` 如实报失败（真机实测过"命令成功但
       应用没回来"，见 `docs/V0.4…` §4.4）；
    7. 冷却期内拒绝（``action="skipped"``），零副作用。
    """
    current = status if status is not None else discover(ops)
    pending = current.user_processes
    if not pending:
        return RestartResult(ok=True, action="noop",
                             detail="UU 远程用户态进程未在运行（不启动新实例）")

    if guard is not None:
        allowed, reason = guard.allow(now)
        if not allowed:
            return RestartResult(ok=False, action="skipped", detail=reason)

    command = launch_command(current)
    if not command:
        return RestartResult(
            ok=False, action="failed",
            detail="未找到重启命令（自启项与启动器路径均缺失）：未做任何改动")

    killed: List[int] = []
    for proc in pending:
        try:
            success = bool(ops.kill(proc.pid))
        except Exception:                                 # noqa: BLE001 - 能力不可信
            success = False
        if not success:
            return RestartResult(
                ok=False, action="failed", killed=killed,
                detail="清退 %s（PID %d）失败：已中止，未启动新实例"
                       % (proc.name, proc.pid))
        killed.append(proc.pid)

    sleeper = sleep if sleep is not None else _SLEEP
    outcome = _launch_and_confirm(ops, command, sleeper)
    if outcome == "launch-failed":
        return RestartResult(
            ok=False, action="failed", killed=killed,
            detail="已清退 %d 个进程，但重启命令执行失败：%s" % (len(killed), command))
    if outcome != "confirmed" and _launch_and_confirm(ops, command, sleeper) != "confirmed":
        return RestartResult(
            ok=False, action="unconfirmed", killed=killed, launched=command,
            detail="已清退 %d 个用户态进程并执行了重启命令，但未确认到 UU 远程"
                   "进程回来：请手动启动一次 UU 远程（下次登录自启也会拉起）"
                   % len(killed))

    if guard is not None:
        guard.mark(now)
    return RestartResult(ok=True, action="restart", killed=killed,
                         launched=command,
                         detail="已清退 %d 个用户态进程并按自启项重新启动"
                                % len(killed))


def _launch_and_confirm(ops: Any, command: str,
                        sleep: Callable[[float], None]) -> str:
    """执行重启命令并**确认进程真的出现**（命令返回成功不算数）。

    返回 ``"confirmed"``（真的起来了）/ ``"launch-failed"``（命令本身失败）/
    ``"unconfirmed"``（命令成功但进程始终没出现）。
    """
    try:
        if not ops.launch(command):
            return "launch-failed"
    except Exception:                                     # noqa: BLE001
        return "launch-failed"
    for _ in range(RESTART_CONFIRM_ATTEMPTS):
        sleep(RESTART_CONFIRM_INTERVAL)
        try:
            if discover(ops).user_processes:
                return "confirmed"
        except Exception:                                 # noqa: BLE001 - 探测不可信
            continue
    return "unconfirmed"
