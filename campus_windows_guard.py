"""Reversible Windows settings used by the unattended online guard.

The module is deliberately callback based.  Production code supplies the real
autostart callback and ``powercfg.exe`` runner; tests can exercise the complete
transaction without touching the registry, power plans, or a live machine.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from campus_install import atomic_json, data_directory

POWER_GROUPS = {
    "standby_ac": ("SUB_SLEEP", "STANDBYIDLE", "standby-timeout-ac"),
    "standby_dc": ("SUB_SLEEP", "STANDBYIDLE", "standby-timeout-dc"),
    "hibernate_ac": ("SUB_SLEEP", "HIBERNATEIDLE", "hibernate-timeout-ac"),
    "hibernate_dc": ("SUB_SLEEP", "HIBERNATEIDLE", "hibernate-timeout-dc"),
}


@dataclass(frozen=True)
class GuardResult:
    """Outcome of one startup configuration pass."""

    autostart: bool
    power: bool
    changed: bool
    detail: str


def _default_run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=False,
                          timeout=10,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                          **kwargs)


def _parse_number(text: str) -> Optional[int]:
    match = re.search(r"(?:0x)?([0-9a-f]{1,8})\s*$", text.strip(), re.I)
    if match is None:
        return None
    try:
        return int(match.group(1), 16)
    except ValueError:
        return None


def _active_scheme(run: Callable) -> Optional[str]:
    result = run(["powercfg.exe", "/getactivescheme"])
    output = (result.stdout or "") if result.returncode == 0 else ""
    match = re.search(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", output, re.I)
    return match.group(1) if match else None


def _read_power_values(run: Callable, scheme: str) -> Optional[dict]:
    result = run(["powercfg.exe", "/query", scheme])
    if result.returncode != 0:
        return None
    lines = (result.stdout or "").splitlines()
    values = {}
    current_setting = None
    for raw in lines:
        line = raw.strip().lower()
        for _, (_, setting, _) in POWER_GROUPS.items():
            if setting.lower() in line:
                current_setting = setting.lower()
                break
        if current_setting is None or "current ac power setting index" not in line and \
                "current dc power setting index" not in line:
            continue
        value = _parse_number(raw)
        if value is not None:
            scope = "ac" if "current ac" in line else "dc"
            key = next((name for name, (_, setting, _) in POWER_GROUPS.items()
                        if setting.lower() == current_setting and name.endswith("_" + scope)), None)
            if key is not None:
                values[key] = value
            # Keep the setting heading for the following DC index; powercfg
            # prints AC before DC in each setting block.
            if scope == "dc":
                current_setting = None
    # powercfg /query emits the setting name and both AC/DC indexes.  If a
    # localized output cannot be parsed, fail closed and leave the plan alone.
    return values if len(values) == len(POWER_GROUPS) else None


def _change(run: Callable, scheme: str, option: str) -> bool:
    result = run(["powercfg.exe", "/change", option, "0"])
    return result.returncode == 0


def _restore(run: Callable, scheme: str, values: Mapping[str, int]) -> bool:
    ok = True
    for key, (subgroup, setting, _) in POWER_GROUPS.items():
        value = int(values[key])
        # /set*valueindex expects seconds for these timeout settings.
        for scope, switch in (("ac", "/setacvalueindex"), ("dc", "/setdcvalueindex")):
            if (key.endswith("_" + scope)):
                result = run(["powercfg.exe", switch, scheme, subgroup, setting,
                              str(value)])
                ok = ok and result.returncode == 0
    if ok:
        result = run(["powercfg.exe", "/setactive", scheme])
        ok = result.returncode == 0
    return ok


def configure_power(*, backup_path: Path, run: Callable = _default_run) -> GuardResult:
    """Disable sleep/hibernate timeouts once, retaining a reversible snapshot."""
    if os.name != "nt":
        return GuardResult(True, True, False, "非 Windows 环境，跳过电源配置")
    backup_path = Path(backup_path)
    if backup_path.is_file():
        return GuardResult(True, True, False, "已存在电源恢复记录")
    scheme = _active_scheme(run)
    if not scheme:
        return GuardResult(True, False, False, "无法读取当前电源计划")
    values = _read_power_values(run, scheme)
    if values is None:
        return GuardResult(True, False, False, "当前电源计划格式无法自动确认")
    atomic_json(backup_path, {"scheme": scheme, "values": values})
    changed = False
    try:
        for _, (_, _, option) in POWER_GROUPS.items():
            if not _change(run, scheme, option):
                raise OSError("powercfg 修改失败：%s" % option)
            changed = True
    except Exception:
        backup_path.unlink(missing_ok=True)
        return GuardResult(True, False, changed, "电源防睡眠设置未完成")
    return GuardResult(True, True, changed, "已关闭睡眠/休眠超时，旧值已保存")


def restore_power(*, backup_path: Path, run: Callable = _default_run) -> bool:
    """Restore the last saved power plan values; never raises for malformed data."""
    path = Path(backup_path)
    if not path.is_file() or os.name != "nt":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        scheme, values = data["scheme"], data["values"]
        if not isinstance(scheme, str) or not isinstance(values, dict):
            return False
        if set(values) != set(POWER_GROUPS):
            return False
        if not _restore(run, scheme, values):
            return False
    except (OSError, ValueError, TypeError, KeyError):
        return False
    path.unlink(missing_ok=True)
    return True


def configure_startup_guard(*, get_autostart: Callable[[], bool],
                            set_autostart: Callable[[bool], bool],
                            backup_path: Optional[Path] = None,
                            run: Callable = _default_run) -> GuardResult:
    """Enable reversible startup protection and power anti-sleep settings."""
    autostart = bool(get_autostart())
    changed = False
    if not autostart:
        autostart = bool(set_autostart(True))
        changed = autostart
    power = configure_power(backup_path=backup_path or
                            (data_directory() / "power-guard-backup.json"), run=run)
    changed = changed or power.changed
    detail = power.detail if autostart else "助手登录后自启未完成；" + power.detail
    return GuardResult(autostart, power.power, changed, detail)
