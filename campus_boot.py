"""Local setup through Microsoft's signed Autologon utility."""
from __future__ import annotations

import ctypes
import io
import json
import os
import subprocess
import zipfile

from campus_install import data_directory

WINLOGON = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
AUTOLOGON_URL = "https://download.sysinternals.com/files/AutoLogon.zip"


def autologon_ready():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WINLOGON) as key:
            enabled = winreg.QueryValueEx(key, "AutoAdminLogon")[0]
            username = winreg.QueryValueEx(key, "DefaultUserName")[0]
        return enabled == "1" and username.lower() == os.environ.get("USERNAME", "").lower()
    except OSError:
        return False


def lock_after_boot():
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetTickCount64.restype = ctypes.c_ulonglong
    if kernel.GetTickCount64() < 180_000:
        ctypes.WinDLL("user32").LockWorkStation()


def prepare_autologon(session):
    response = session.get(AUTOLOGON_URL, timeout=(5, 30))
    try:
        if response.status_code != 200 or len(response.content) > 3 * 1024 * 1024:
            raise OSError("微软自动登录工具下载失败")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            entry = archive.getinfo("Autologon64.exe")
            if entry.file_size > 3 * 1024 * 1024:
                raise ValueError("微软自动登录工具文件大小异常")
            content = archive.read(entry)
    finally:
        response.close()
    directory = data_directory() / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "Autologon64.exe"
    target.write_bytes(content)
    literal = str(target).replace("'", "''")
    # Some Windows PowerShell 5 installations preload this type data twice.
    # Remove only that child process's formatting metadata before module autoload.
    script = ("Remove-TypeData -TypeName System.Security.AccessControl.ObjectSecurity "
              "-ErrorAction SilentlyContinue; "
              "$signature = Get-AuthenticodeSignature -LiteralPath '" + literal + "'; "
              "[pscustomobject]@{Status=$signature.Status.ToString(); "
              "Signer=$signature.SignerCertificate.Subject} | ConvertTo-Json -Compress")
    environment = os.environ.copy()
    environment.pop("PSModulePath", None)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=20, check=True, env=environment,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    signature = json.loads(result.stdout.decode("utf-8-sig"))
    if signature.get("Status") != "Valid" or "O=Microsoft Corporation" not in signature.get("Signer", ""):
        target.unlink(missing_ok=True)
        raise ValueError("微软自动登录工具数字签名校验失败")
    return target


def setup_dialog():
    import requests
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    try:
        if not messagebox.askyesno(
                "重启恢复设置", "将下载并打开微软官方 Autologon 工具。\n"
                "请在该工具中输入 Windows 账户密码并启用自动登录。\n"
                "密码保存在本机 LSA 机密中，管理员仍可能读取。\n"
                "三层恢复启用后，助手在开机自启时锁定屏幕。是否继续？", parent=root):
            return
        with requests.Session() as session:
            try:
                target = prepare_autologon(session)
            except (OSError, requests.RequestException):
                session.trust_env = False
                target = prepare_autologon(session)
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(target), None, None, 1)
        if result <= 32:
            raise OSError("微软自动登录工具未启动")
    except Exception as exc:
        messagebox.showerror("设置未完成", str(exc), parent=root)
    finally:
        root.destroy()
