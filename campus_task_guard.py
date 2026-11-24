"""Independent per-user Windows task guard for CampusReconnect.

The normal tray process owns network recovery.  This module owns only the
out-of-process liveness contract: a scheduled ``--guard`` process watches a
heartbeat and starts the tray again when it crashes or stops servicing its
worker loop.  All state is deliberately stored below ``%LOCALAPPDATA%`` and
contains no credentials.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from xml.etree import ElementTree as ET

from campus_install import atomic_json, data_directory

TASK_NAME = "CampusReconnect.Guard"
HEARTBEAT_FILE = "guard-heartbeat.json"
GUARD_POLL_SECONDS = 15.0
GUARD_STALE_SECONDS = 45.0
GUARD_RESTART_LIMIT = 3
GUARD_RESTART_WINDOW = 600.0


@dataclass(frozen=True)
class GuardStatus:
    ok: bool
    action: str
    detail: str
    restart_count: int = 0


def heartbeat_path(path: Path | None = None) -> Path:
    return Path(path) if path is not None else data_directory() / HEARTBEAT_FILE


def _system_uptime_seconds() -> float:
    if os.name == "nt":
        try:
            import ctypes
            return float(ctypes.windll.kernel32.GetTickCount64()) / 1000.0
        except (AttributeError, OSError, TypeError):
            pass
    return max(0.0, float(time.monotonic()))


def current_boot_id() -> int:
    """Return a process-independent marker that changes after Windows reboot."""
    # Process start times differ by milliseconds, so use a minute bucket of
    # the calculated boot epoch rather than the current monotonic tick.
    return int((time.time() - _system_uptime_seconds()) // 60)


def write_heartbeat(path: Path | None = None, *, active: bool = True,
                    pid: int | None = None, version: str = "",
                    now: float | None = None,
                    boot_id: int | None = None) -> None:
    """Atomically write a redacted liveness record."""
    target = heartbeat_path(path)
    record = {
        "active": bool(active),
        "pid": int(pid if pid is not None else os.getpid()),
        "version": str(version or ""),
        "updated_at": float(time.time() if now is None else now),
        "boot_id": int(current_boot_id() if boot_id is None else boot_id),
    }
    if os.name == "nt" and pid is None and getattr(sys, "frozen", False):
        kernel = _process_kernel()
        record["process_created"] = _process_created(kernel, kernel.GetCurrentProcess())
    atomic_json(target, record)


def read_heartbeat(path: Path | None = None) -> dict:
    target = heartbeat_path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def heartbeat_age(record: Mapping[str, object], *, now: float | None = None) -> float | None:
    stamp = record.get("updated_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    current = time.time() if now is None else float(now)
    return max(0.0, current - float(stamp))


def build_main_command(executable: Path | None = None, *, frozen: bool | None = None,
                       script: Path | None = None) -> list[str]:
    """Build a source or frozen tray command without shell interpretation."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if frozen:
        return [str(Path(executable or sys.executable).resolve()), "--background"]
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else interpreter),
            str(Path(script or __file__).with_name("campus_reconnect_tray.py")),
            "--background"]


def build_guard_command(executable: Path | None = None, *, frozen: bool | None = None,
                        script: Path | None = None) -> list[str]:
    command = build_main_command(executable, frozen=frozen, script=script)
    # The guard is the same executable/script in a separate process.  Replace
    # the tray's background flag rather than appending duplicate switches.
    if "--background" in command:
        command.remove("--background")
    command.append("--guard")
    return command


def _task_run(args: Sequence[str], **kwargs):
    return subprocess.run(args, capture_output=True, text=True, errors="replace", check=False,
                          timeout=15,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                          **kwargs)


def register_task(executable: Path | None = None, *, run: Callable = _task_run,
                  task_name: str = TASK_NAME, frozen: bool | None = None,
                  script: Path | None = None) -> GuardStatus:
    """Register an ONLOGON per-user task; no administrator elevation is needed."""
    if os.name != "nt":
        return GuardStatus(False, "unsupported", "非 Windows 环境，跳过任务计划守护")
    identity = run(["whoami.exe", "/user", "/fo", "csv", "/nh"])
    sid = re.search(r"\bS-1-(?:\d+-)*\d+\b", identity.stdout or "")
    if identity.returncode != 0 or sid is None:
        return GuardStatus(False, "identity_failed", "未能确认守护任务的 Windows 用户")
    command = build_guard_command(executable, frozen=frozen, script=script)
    task = ET.Element("Task", xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task",
                      version="1.2")
    trigger = ET.SubElement(ET.SubElement(task, "Triggers"), "LogonTrigger")
    principal = ET.SubElement(ET.SubElement(task, "Principals"), "Principal", id="CurrentUser")
    ET.SubElement(trigger, "UserId").text = sid.group()
    ET.SubElement(principal, "UserId").text = sid.group()
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"),
                       ("DisallowStartIfOnBatteries", "false"),
                       ("StopIfGoingOnBatteries", "false"), ("ExecutionTimeLimit", "PT0S")):
        ET.SubElement(settings, key).text = value
    action = ET.SubElement(ET.SubElement(task, "Actions", Context="CurrentUser"), "Exec")
    ET.SubElement(action, "Command").text = command[0]
    ET.SubElement(action, "Arguments").text = subprocess.list2cmdline(command[1:])
    with tempfile.TemporaryDirectory() as directory:
        xml_path = Path(directory) / "guard.xml"
        ET.ElementTree(task).write(xml_path, encoding="utf-16", xml_declaration=True)
        result = run(["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"])
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:240]
        return GuardStatus(False, "register_failed", "任务计划注册失败（退出码 %s） %s"
                           % (result.returncode, detail))
    check = run(["schtasks.exe", "/Query", "/TN", task_name])
    if check.returncode != 0:
        return GuardStatus(False, "verify_failed", "任务计划已提交，但回查失败（退出码 %s）"
                           % check.returncode)
    return GuardStatus(True, "registered", "独立任务计划守护已启用")


def unregister_task(*, run: Callable = _task_run, task_name: str = TASK_NAME) -> GuardStatus:
    if os.name != "nt":
        return GuardStatus(True, "skipped", "非 Windows 环境，跳过任务计划清理")
    result = run(["schtasks.exe", "/Delete", "/TN", task_name, "/F"])
    if result.returncode not in (0, 1):
        return GuardStatus(False, "unregister_failed", "任务计划清理失败（退出码 %s）" % result.returncode)
    return GuardStatus(True, "unregistered", "独立任务计划守护已清理")


class GuardSupervisor:
    """Bounded stale-heartbeat recovery, injectable for deterministic tests."""

    def __init__(self, launch: Callable[[], object], *, heartbeat: Path | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 stale_after: float = GUARD_STALE_SECONDS,
                 max_restarts: int = GUARD_RESTART_LIMIT,
                 restart_window: float = GUARD_RESTART_WINDOW,
                 boot_id: int | None = None,
                 retire: Callable[[Mapping[str, object]], bool] | None = None,
                 log: Callable[[str], None] | None = None):
        self.launch = launch
        self.heartbeat = heartbeat_path(heartbeat)
        self.clock = clock
        self.sleep = sleep
        self.stale_after = float(stale_after)
        self.max_restarts = int(max_restarts)
        self.restart_window = float(restart_window)
        self.boot_id = int(current_boot_id() if boot_id is None else boot_id)
        self.retire = retire
        self.log = log or (lambda message: None)
        self.restarts: list[float] = []

    def check_once(self) -> GuardStatus:
        now = self.clock()
        record = read_heartbeat(self.heartbeat)
        age = heartbeat_age(record, now=now)
        stopped_boot = record.get("boot_id")
        same_boot = stopped_boot is None or stopped_boot == self.boot_id
        if record.get("active") is False and age is not None and same_boot:
            return GuardStatus(True, "stopped", "主程序由用户正常退出")
        if same_boot and record.get("active") is True and age is not None and age <= self.stale_after:
            return GuardStatus(True, "healthy", "主程序心跳正常")
        self.restarts = [stamp for stamp in self.restarts
                         if now - stamp < self.restart_window]
        if len(self.restarts) >= self.max_restarts:
            return GuardStatus(False, "restart_limited", "主程序持续失联，已达到守护拉起上限",
                               len(self.restarts))
        try:
            if (self.retire is not None and record.get("active") is True and same_boot
                    and not self.retire(record)):
                return GuardStatus(False, "previous_instance_unconfirmed",
                                   "旧助手进程尚未确认退出", len(self.restarts))
            self.launch()
        except Exception as exc:  # guard must keep running after a failed launch
            self.log("守护拉起主程序失败（%s）" % type(exc).__name__)
            return GuardStatus(False, "launch_failed", "主程序拉起失败（%s）" % type(exc).__name__,
                               len(self.restarts))
        self.restarts.append(now)
        self.log("守护已拉起主程序（第 %d 次）" % len(self.restarts))
        return GuardStatus(True, "launched", "已拉起主程序，等待心跳恢复", len(self.restarts))

    def run(self, *, stop: Callable[[], bool] | None = None,
            interval: float = GUARD_POLL_SECONDS) -> None:
        stop = stop or (lambda: False)
        while not stop():
            self.check_once()
            self.sleep(float(interval))


def _process_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "GetCurrentProcess": (wintypes.HANDLE, []),
        "OpenProcess": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        "GetProcessTimes": (wintypes.BOOL, [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4),
        "QueryFullProcessImageNameW": (
            wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                           ctypes.POINTER(wintypes.DWORD)]),
        "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        "TerminateProcess": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
    }
    for name, (result, arguments) in signatures.items():
        fn = getattr(kernel, name)
        fn.restype, fn.argtypes = result, arguments
    return kernel


def _process_created(kernel, handle):
    created, exited, cpu_kernel, cpu_user = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
        return None
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def _retire_previous(record, executable, heartbeat):
    pid, created = record.get("pid"), record.get("process_created")
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        return False
    kernel = _process_kernel()
    handle = kernel.OpenProcess(0x101001, False, pid)  # synchronize, query, terminate
    if not handle:
        return ctypes.get_last_error() == 87  # PID no longer exists
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return True
        if type(created) is not int:
            return False
        actual_created = _process_created(kernel, handle)
        if actual_created is None:
            return False
        if actual_created != created:
            return True  # heartbeat is for a PID that has since been reused
        name = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return False
        if os.path.normcase(name.value) != os.path.normcase(executable):
            return False
        if read_heartbeat(heartbeat) != record:
            return False
        kernel.TerminateProcess(handle, 1)
        return kernel.WaitForSingleObject(handle, 8000) == 0
    finally:
        kernel.CloseHandle(handle)


def run_guard(*, executable: Path | None = None, heartbeat: Path | None = None,
              log: Callable[[str], None] | None = None) -> int:
    """Run the long-lived guard entrypoint used by Task Scheduler."""
    command = build_main_command(executable, frozen=getattr(sys, "frozen", False),
                                 script=Path(__file__))
    target = heartbeat_path(heartbeat)

    def launch():
        subprocess.Popen(command, close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    retire = None
    if os.name == "nt" and getattr(sys, "frozen", False):
        def retire(record):
            return _retire_previous(record, command[0], target)

    GuardSupervisor(launch, heartbeat=target, retire=retire, log=log).run()
    return 0


__all__ = [
    "TASK_NAME", "HEARTBEAT_FILE", "GUARD_POLL_SECONDS", "GUARD_STALE_SECONDS",
    "GUARD_RESTART_LIMIT", "GUARD_RESTART_WINDOW", "GuardStatus", "GuardSupervisor",
    "heartbeat_path", "current_boot_id", "write_heartbeat", "read_heartbeat", "heartbeat_age",
    "build_main_command", "build_guard_command", "register_task", "unregister_task",
    "run_guard",
]
