"""Quiet per-machine setup, coordinated with the tray's recovery lock."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import time

from campus_proxy_compat import merge_bypass


@dataclass(frozen=True)
class SetupResult:
    ready: bool
    message: str
    changed: bool = False


class AutomaticSetup:
    """Inject every host operation so the default suite never touches a live client."""

    def __init__(self, compatibility, roots, lifecycle, *, cancelled=lambda: False):
        self.compat = compatibility
        self.roots = roots
        self.lifecycle = lifecycle
        self.cancelled = cancelled

    def run(self, *, restart_client=False):
        recover = getattr(self.lifecycle, "recover", None)
        if recover is not None:
            recover()
        if self.cancelled():
            return SetupResult(False, "在线守护已暂停")
        changed = False
        pending = self.compat.pending_backup()
        if pending is not None:
            # Existing conflict checks prevent overwriting later user changes.
            journal = json.loads(Path(pending).read_text(encoding="utf-8"))
            token = self.lifecycle.pause() if journal.get("root") else []
            try:
                self.compat.restore(pending)
            finally:
                self.lifecycle.resume(token)
        values = self.compat.read_registry()
        override = values.get("ProxyOverride", {}).get("value", "")
        if merge_bypass(override) != override:
            self.compat.apply()
            changed = True
        roots = self.roots()
        if len(roots) > 1:
            return SetupResult(False, "基础联网保护已就绪；检测到多份网络配置，已保留现有选择", changed)
        if not roots:
            pac = bool(values.get("AutoConfigURL", {}).get("value"))
            if pac:
                return SetupResult(False, "基础保护已就绪；当前联网软件需要进一步确认，可在需要帮助时导出诊断", changed)
            return SetupResult(True, "基础联网保护已就绪", changed)
        root = roots[0]
        ready = self.compat.persistent_ready(root)
        if ready and not restart_client:
            return SetupResult(True, "联网保护已就绪", changed)
        if self.cancelled():
            return SetupResult(False, "在线守护已暂停", changed)
        token = self.lifecycle.pause()
        try:
            if self.cancelled():
                return SetupResult(False, "在线守护已暂停", changed)
            if restart_client and not token and ready:
                return SetupResult(False, "联网软件尚未运行，当前仅保持基础联网保护", changed)
            # apply re-reads and backs up files AFTER the client stopped, checks
            # for concurrent changes, and rolls back on any write/refresh failure.
            if not ready:
                self.compat.apply(root)
                changed = True
        finally:
            self.lifecycle.resume(token)
        return SetupResult(True, "联网保护已自动配置，已恢复联网软件" if token else "联网保护已自动配置", changed)


class WindowsClientLifecycle:
    """Restart only the discovered Clash Verge GUI; leave services and UU alone."""

    def __init__(self, *, pause=time.sleep, run=subprocess.run, launch=subprocess.Popen,
                 clients=None, journal=None):
        self.wait = pause
        self.run = run
        self.launch = launch
        self.clients = clients or self._clients
        self.journal = Path(journal) if journal is not None else None

    def recover(self):
        if self.journal is None or not self.journal.exists():
            return
        rows = json.loads(self.journal.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise OSError("上次联网软件的恢复记录暂时无法确认")
        token = []
        for row in rows:
            path = row.get("executable")
            if (not isinstance(path, str) or not Path(path).is_absolute()
                    or Path(path).name.lower() not in ("clash-verge.exe", "clash-verge-rev.exe")
                    or not Path(path).is_file()):
                raise OSError("上次联网软件的启动位置已变化")
            token.append((0, path))
        self.resume(token)

    @staticmethod
    def _clients():
        from tools.netdiag import PowerShellRunner
        code, output, _ = PowerShellRunner(timeout=5).run(
            "automatic_setup_clients", "Get-CimInstance Win32_Process | Where-Object { "
            "$_.Name -in @('clash-verge.exe','clash-verge-rev.exe') } "
            "| Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress")
        if code:
            raise OSError("联网软件状态暂时无法读取")
        rows = json.loads(output) if output.strip() else []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise ValueError("联网软件状态暂时无法确认")
        result = []
        for row in rows:
            pid, name = row.get("ProcessId"), row.get("ExecutablePath")
            if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                    or not isinstance(name, str) or not Path(name).is_absolute()
                    or Path(name).name.lower() not in ("clash-verge.exe", "clash-verge-rev.exe")
                    or not Path(name).is_file()):
                raise OSError("联网软件启动位置暂时无法确认")
            result.append((pid, name))
        return result

    def pause(self):
        token = self.clients()
        if len(token) > 1:
            raise OSError("检测到多个联网软件实例，已保留现状")
        if self.journal is not None and token:
            from campus_install import atomic_json
            atomic_json(self.journal, [{"executable": path} for _, path in token])
        try:
            for pid, path in token:
                if (pid, path) not in self.clients():
                    raise OSError("联网软件状态已变化，稍后重试")
                self.run(["taskkill.exe", "/PID", str(pid), "/F"], check=True,
                         capture_output=True, timeout=8,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for _ in range(20):
                if not self.clients():
                    return token
                self.wait(0.25)
            raise OSError("联网软件尚未退出，将在后台重试")
        except Exception:
            self.resume(token)
            raise

    def resume(self, token):
        if not token:
            return
        clients = self.clients()
        if clients:
            expected = {str(Path(path)).casefold() for _, path in token}
            if any(str(Path(path)).casefold() not in expected for _, path in clients):
                raise OSError("联网软件启动位置已变化，已保留恢复记录")
            if self.journal is not None:
                self.journal.unlink(missing_ok=True)
            return
        for _, executable in token:
            self.launch([executable], cwd=str(Path(executable).parent),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if self.journal is not None:
            for _ in range(10):
                clients = self.clients()
                if clients and all(str(Path(path)).casefold() == str(Path(token[0][1])).casefold()
                                   for _, path in clients):
                    self.journal.unlink(missing_ok=True)
                    return
                self.wait(0.5)
            raise OSError("联网软件正在重新启动，将在后台继续检查")


def windows_setup(*, cancelled=lambda: False):
    from campus_install import data_directory
    from campus_proxy_compat import discover_roots, windows_compatibility
    return AutomaticSetup(windows_compatibility(), discover_roots,
                          WindowsClientLifecycle(journal=data_directory() / "client-restart.json"), cancelled=cancelled)
