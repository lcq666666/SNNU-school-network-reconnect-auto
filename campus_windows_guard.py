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
_POWER_SCHEME_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.I)


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


def _restore(run: Callable, scheme: str, values: Mapping[str, int], *,
             activate: bool = True) -> bool:
    ok = True
    for key, (subgroup, setting, _) in POWER_GROUPS.items():
        value = int(values[key])
        # /set*valueindex expects seconds for these timeout settings.
        for scope, switch in (("ac", "/setacvalueindex"), ("dc", "/setdcvalueindex")):
            if (key.endswith("_" + scope)):
                result = run(["powercfg.exe", switch, scheme, subgroup, setting,
                              str(value)])
                ok = ok and result.returncode == 0
    if ok and activate:
        result = run(["powercfg.exe", "/setactive", scheme])
        ok = result.returncode == 0
    return ok


def _normalize_backup(data: object) -> dict:
    """Read both the original single-plan journal and the current multi-plan format."""
    if not isinstance(data, dict):
        raise ValueError("电源恢复记录格式无效")
    if data.get("version") == 2:
        plans = data.get("plans")
        active_scheme = data.get("active_scheme")
    else:
        plans = {data.get("scheme"): data.get("values")}
        active_scheme = data.get("scheme")
    if (not isinstance(plans, dict) or not plans
            or not isinstance(active_scheme, str)
            or not _POWER_SCHEME_RE.fullmatch(active_scheme)):
        raise ValueError("电源恢复记录缺少有效电源计划")
    checked = {}
    for scheme, values in plans.items():
        if not isinstance(scheme, str) or not _POWER_SCHEME_RE.fullmatch(scheme):
            raise ValueError("电源恢复记录包含无效电源计划")
        if not isinstance(values, dict) or set(values) != set(POWER_GROUPS):
            raise ValueError("电源恢复记录包含不完整设置")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in values.values()):
            raise ValueError("电源恢复记录包含无效超时值")
        checked[scheme] = dict(values)
    if active_scheme not in checked:
        raise ValueError("电源恢复记录缺少原始活动计划")
    return {"version": 2, "active_scheme": active_scheme, "plans": checked}


def _load_backup(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    return _normalize_backup(json.loads(path.read_text(encoding="utf-8")))


def configure_power(*, backup_path: Path, run: Callable = _default_run) -> GuardResult:
    """Keep the active plan protected while preserving pre-guard values per plan."""
    if os.name != "nt":
        return GuardResult(True, True, False, "非 Windows 环境，跳过电源配置")
    backup_path = Path(backup_path)
    try:
        previous_backup = _load_backup(backup_path)
    except (OSError, ValueError, TypeError) as exc:
        return GuardResult(True, False, False,
                           "电源恢复记录无法读取：%s" % type(exc).__name__)
    scheme = _active_scheme(run)
    if not scheme:
        return GuardResult(True, False, False, "无法读取当前电源计划")
    values = _read_power_values(run, scheme)
    if values is None:
        return GuardResult(True, False, False, "当前电源计划格式无法自动确认")
    values = {key: int(value) for key, value in values.items()}
    if all(value == 0 for value in values.values()):
        if previous_backup is not None and scheme not in previous_backup["plans"]:
            previous_backup["plans"][scheme] = values
            try:
                atomic_json(backup_path, previous_backup)
            except OSError as exc:
                return GuardResult(True, False, False,
                                   "电源恢复记录保存失败：%s" % type(exc).__name__)
        return GuardResult(True, True, False, "当前电源计划已关闭睡眠/休眠超时")

    updated_backup = previous_backup or {
        "version": 2, "active_scheme": scheme, "plans": {}}
    if scheme not in updated_backup["plans"]:
        updated_backup["plans"][scheme] = values
    try:
        atomic_json(backup_path, updated_backup)
    except OSError as exc:
        return GuardResult(True, False, False,
                           "电源恢复记录保存失败：%s" % type(exc).__name__)

    changed = False
    try:
        for key, (_, _, option) in POWER_GROUPS.items():
            if values[key] == 0:
                continue
            if not _change(run, scheme, option):
                raise OSError("powercfg 修改失败：%s" % option)
            changed = True
        if changed:
            result = run(["powercfg.exe", "/setactive", scheme])
            if result.returncode != 0:
                raise OSError("powercfg 应用电源计划失败")
    except Exception:
        try:
            rolled_back = _restore(run, scheme, values)
        except Exception:
            rolled_back = False
        if rolled_back:
            if previous_backup is None:
                backup_path.unlink(missing_ok=True)
            else:
                try:
                    atomic_json(backup_path, previous_backup)
                except OSError:
                    rolled_back = False
        detail = "设置失败，已回滚本轮修改" if rolled_back else \
            "设置失败且回滚未完成，恢复记录已保留"
        return GuardResult(True, False, changed and not rolled_back,
                           "电源防睡眠设置未完成；" + detail)
    return GuardResult(True, True, changed, "已关闭睡眠/休眠超时，原值已保存")


def restore_power(*, backup_path: Path, run: Callable = _default_run) -> bool:
    """Restore the last saved power plan values; never raises for malformed data."""
    path = Path(backup_path)
    if not path.is_file() or os.name != "nt":
        return False
    try:
        data = _normalize_backup(json.loads(path.read_text(encoding="utf-8")))
        active_scheme = _active_scheme(run) or data["active_scheme"]
        if active_scheme not in data["plans"]:
            return False
        for scheme, values in data["plans"].items():
            if not _restore(run, scheme, values, activate=False):
                return False
        result = run(["powercfg.exe", "/setactive", active_scheme])
        if result.returncode != 0:
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
