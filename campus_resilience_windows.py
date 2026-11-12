"""Windows actions used by the explicitly enabled three-stage controller."""
from __future__ import annotations

import ipaddress
import json
import subprocess

from campus_windows import PROXY_REG_PATH, ProxyController, default_refresh


def set_awake(required):
    import ctypes
    flags = 0x80000001 if required else 0x80000000
    if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
        raise OSError("无法设置运行期间的自动睡眠保护")


def recover_pending_proxy():
    from campus_install import atomic_json, data_directory
    path = data_directory() / "recovery.json"
    if not path.is_file():
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("proxy_before") is not None:
        proxy_restore(state["proxy_before"])
        state["proxy_before"] = None
        atomic_json(path, state)


def proxy_snapshot():
    import winreg
    values = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_REG_PATH) as key:
        for name in ProxyController.VALUE_NAMES:
            try:
                value, kind = winreg.QueryValueEx(key, name)
                values[name] = {"value": value, "type": kind}
            except FileNotFoundError:
                pass
    return values


def proxy_disable():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_REG_PATH, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, "")
    if not default_refresh():
        raise OSError("系统代理刷新失败")


def proxy_restore(values):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_REG_PATH, 0,
                        winreg.KEY_SET_VALUE) as key:
        for name in ProxyController.VALUE_NAMES:
            if name in values:
                item = values[name]
                winreg.SetValueEx(key, name, 0, item["type"], item["value"])
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    if not default_refresh():
        raise OSError("原系统代理配置已写回，但刷新失败")


def cycle_system_proxy(journal_path, *, snapshot=None, disable=None, restore=None, pause=None):
    """One reversible cycle; injected callbacks keep tests independent of threads."""
    import time
    from pathlib import Path
    from campus_install import atomic_json
    snapshot = snapshot or proxy_snapshot
    disable = disable or proxy_disable
    restore = restore or proxy_restore
    pause = pause or time.sleep
    before = snapshot()
    if not before.get("ProxyEnable", {}).get("value"):
        return False
    journal_path = Path(journal_path)
    state = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.is_file() else {}
    if state.get("proxy_before") is not None:
        return False
    state["proxy_before"] = before
    atomic_json(journal_path, state)
    try:
        disable()
        pause(1.0)
    finally:
        restore(before)
        state["proxy_before"] = None
        atomic_json(journal_path, state)
    return True


def uu_connected():
    """External established TCP is evidence of connectivity, not a remote session."""
    from tools.netdiag import PowerShellRunner
    script = ("Get-Process -Name GameViewer,GameViewerServer -ErrorAction SilentlyContinue "
              "| ForEach-Object { Get-NetTCPConnection -OwningProcess $_.Id "
              "-State Established -ErrorAction SilentlyContinue } "
              "| Select-Object RemoteAddress | ConvertTo-Json -Compress")
    rc, output, _ = PowerShellRunner(timeout=5).run("uu_connections", script)
    if rc != 0:
        return None
    rows = json.loads(output) if output.strip() else []
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        address = ipaddress.ip_address(row.get("RemoteAddress", "127.0.0.1"))
        if not address.is_loopback and not address.is_unspecified and not address.is_link_local:
            return True
    return False


def restart_uu(log):
    from campus_remote_app import (build_windows_remote_app_ops, discover,
                                   launch_command, restart_user_app)
    from tools.netdiag import PowerShellRunner
    ops = build_windows_remote_app_ops(PowerShellRunner(timeout=5))
    status = discover(ops)
    if status.error:
        raise OSError("UU 进程或启动信息读取失败")
    if status.user_processes:
        result = restart_user_app(ops, status=status)
        if not result.ok:
            raise OSError("UU 重启未完成")
    else:
        command = launch_command(status)
        if not command or not ops.launch(command):
            raise OSError("未找到可用的 UU 启动命令，请先安装并运行 UU")
    log("UU 启动操作已完成，等待外部连接验证")


def schedule_reboot():
    subprocess.run(["shutdown.exe", "/r", "/f", "/t", "60", "/c",
                    "校园网与UU持续故障，自动恢复将在60秒后重启。请保存文件。"],
                   check=True, capture_output=True, timeout=10,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def cancel_reboot():
    done = subprocess.run(["shutdown.exe", "/a"], capture_output=True, timeout=10,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # 1116 means there is no shutdown in progress, as after the next Windows boot.
    if done.returncode not in (0, 1116):
        raise OSError("无法取消重启倒计时")
