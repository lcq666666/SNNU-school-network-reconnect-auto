# -*- coding: utf-8 -*-
"""校园网自动重连助手 · 托盘版 (Windows)

在引擎 campus_reconnect.py 之上提供:
  - 任务栏托盘图标与右键菜单(四态: 在线 / 重连中 / 已断开 / 已暂停)
  - 气泡通知: 启动状态 / 网络断开开始重连 / 重连成功(耗时) / 重连失败(原因)
  - 设置面板(tkinter): 实时状态、参数、开关
  - "重连成功后自动开启 Clash 系统代理" 的同意询问(记住选择, 可随时改)
  - 开机自启开关: HKCU Run 注册表项 CampusReconnectTray
  - Clash 系统代理开关: 注册表 ProxyEnable(即时生效; Verge 界面状态可能滞后到下次手动切换)

依赖: py -m pip install pystray pillow
运行: py campus_reconnect_tray.py   (run_tray.bat 可双击启动)
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import keyring
import pystray
import winreg
from PIL import Image, ImageDraw

from campus_reconnect import (edge_fallback, ensure_single_instance,
                              get_credentials, internet_ok, portal_login,
                              portal_reachable, resolve_port, setup_logging,
                              wifi_state)

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent   # PyInstaller 单文件: 用 exe 所在目录
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "tray_config.json"
LOG_DIR = SCRIPT_DIR / "logs"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "CampusReconnectTray"
PYTHONW = str(Path(sys.executable).with_name("pythonw.exe"))
CLASH_SERVER_DEFAULT = "127.0.0.1:7897"
APP_NAME = "校园网自动重连助手"

DEFAULT_CONFIG = {
    "ssid": "SNNU",
    "yys": "mobile",
    "interval": 15,
    "threshold": 3,
    "cooldown": 180,
    "browser_fallback": True,
    "clash_auto_proxy": None,   # None=首次成功时询问; True/False=用户选择
    "paused": False,
}

MODE_COLOR = {"online": (63, 185, 80), "reconnecting": (210, 153, 34),
              "offline": (248, 81, 73), "paused": (139, 148, 158),
              "boot": (88, 166, 255)}
MODE_TEXT = {"online": "在线", "reconnecting": "重连中", "offline": "已断开",
             "paused": "已暂停", "boot": "启动中"}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass


def get_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
    except OSError:
        return False


def launcher_command() -> str:
    """开机自启优先用打包好的 exe；没有 exe 时退回 pythonw + 脚本。"""
    exe = SCRIPT_DIR / "CampusReconnect.exe"
    if exe.is_file():
        return '"%s"' % exe
    return '"%s" "%s"' % (PYTHONW, SCRIPT_DIR / "campus_reconnect_tray.py")


def set_autostart(enabled: bool) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = launcher_command()
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def set_system_proxy(enabled: bool) -> bool:
    """开关 Windows 系统代理(即 Clash Verge 的系统代理开关), 即时生效。"""
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD,
                              1 if enabled else 0)
            if enabled:
                try:
                    server = winreg.QueryValueEx(key, "ProxyServer")[0]
                except FileNotFoundError:
                    server = None
                if not server:
                    winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ,
                                      CLASH_SERVER_DEFAULT)
        return True
    except OSError:
        return False


class TrayApp:
    def __init__(self, log):
        self.log = log
        self.config = load_config()
        self.session = None
        self.state = {"mode": "boot", "detail": "", "port": None,
                      "last_ok": None, "failures": 0}
        self.stop_event = threading.Event()
        self.icon = None
        self.root = None
        self.settings_win = None
        self._ask_event = threading.Event()
        self._ask_result = {"value": None}

    # ---------- 图标与通知 ----------
    def make_image(self):
        color = MODE_COLOR.get(self.state["mode"], (139, 148, 158))
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([6, 6, 58, 58], fill=color + (255,))
        d.ellipse([22, 22, 42, 42], fill=(255, 255, 255, 235))
        return img

    def tooltip(self):
        return "%s · %s" % (APP_NAME, MODE_TEXT.get(self.state["mode"], "?"))

    def set_state(self, mode, detail="", notify=None):
        prev = self.state["mode"]
        self.state["mode"] = mode
        self.state["detail"] = detail
        if self.icon is not None:
            try:
                self.icon.icon = self.make_image()
                self.icon.title = self.tooltip()
            except Exception:
                pass
        self.log.info("状态 -> %s（%s）", mode, detail)
        if notify and prev != mode:
            self.notify(*notify)

    def notify(self, title, body):
        """发送气泡通知。注意 pystray 签名是 notify(message, title)。"""
        if self.icon is None:
            return
        message = (body or "")[:255]
        head = (title or "")[:63]
        try:
            self.icon.notify(message, head)
        except Exception:
            self.log.warning("通知发送失败:\n%s", traceback.format_exc())

    # ---------- 重连核心 ----------
    def repair_once(self):
        """执行一次完整重连, 返回 (成功?, 详情, 耗时秒)。"""
        from campus_reconnect import direct_session
        if self.session is None:
            self.session = direct_session()
        start = time.monotonic()
        cfg = self.config
        ssid, _ = wifi_state()
        target = (cfg.get("ssid") or "").strip()
        if target and (not ssid or ssid.casefold() != target.casefold()):
            return False, "当前 Wi-Fi 为 %s，未连接目标 %s" % (ssid or "未连接", target), 0.0
        port, source = resolve_port(self.session, None)
        self.state["port"] = port
        if not portal_reachable(self.session, port):
            return False, "Portal 登录页不可达（端口 %d）" % port, time.monotonic() - start
        creds = get_credentials()
        if not creds:
            return False, "尚未保存校园网凭据（请先运行 configure）", time.monotonic() - start
        account, password = creds
        ok, detail = portal_login(self.session, account, password,
                                  cfg.get("yys") or "mobile", port, self.log)
        if not ok:
            return False, detail, time.monotonic() - start
        for delay in (3, 5, 8):
            time.sleep(delay)
            online, _ = internet_ok(self.session)
            if online:
                return True, "Portal 确认在线，公网已恢复（端口 %d · %s）" % (port, source), \
                    time.monotonic() - start
        return False, "Portal 已在线但公网探测仍未通过", time.monotonic() - start

    def _after_repair(self, ok, detail, elapsed):
        cfg = self.config
        if ok:
            self.state["last_ok"] = datetime.now().strftime("%H:%M:%S")
            self.set_state("online", detail)
            allow = cfg.get("clash_auto_proxy")
            if allow is None:
                allow = self._ask_proxy_consent()
                if allow is not None:
                    cfg["clash_auto_proxy"] = allow
                    save_config(cfg)
            proxy_note = ""
            if allow:
                if set_system_proxy(True):
                    proxy_note = "\n已自动开启 Clash 系统代理"
                else:
                    proxy_note = "\nClash 系统代理开启失败（可手动开启）"
            self.notify("重连成功", "用时约 %.0f 秒 · %s%s" % (elapsed, detail, proxy_note))
        else:
            self.set_state("offline", detail)
            extra = ""
            if cfg.get("browser_fallback"):
                edge_fallback(self.log)
                extra = "\n已打开 Edge 供手动登录"
            self.notify("重连失败", "%s%s" % (detail, extra))

    def _ask_proxy_consent(self):
        """首次重连成功时询问是否同意联动开启 Clash 系统代理。"""
        if self.root is None:
            return None
        self._ask_event.clear()
        self._ask_result["value"] = None
        try:
            self.root.after(0, self._ask_dialog)
        except Exception:
            return None
        self._ask_event.wait(60)
        return self._ask_result["value"]

    def _ask_dialog(self):
        try:
            ans = messagebox.askyesno(
                "Clash 系统代理联动",
                "校园网重连成功。\n\n是否同意：以后每次重连成功后，"
                "自动开启 Clash 的系统代理？\n（可随时在托盘菜单或设置面板更改）")
            self._ask_result["value"] = bool(ans)
        except Exception:
            pass
        finally:
            self._ask_event.set()

    # ---------- 监测循环 ----------
    def monitor_loop(self):
        from campus_reconnect import direct_session
        self.session = direct_session()
        cfg = self.config
        if cfg.get("paused"):
            self.set_state("paused", "监测已暂停")
        else:
            self.set_state("boot", "正在检查网络状态")
        while not self.stop_event.is_set():
            cfg = self.config
            if cfg.get("paused"):
                if self.state["mode"] != "paused":
                    self.set_state("paused", "监测已暂停")
                self.stop_event.wait(max(1, int(cfg.get("interval", 15))))
                continue
            if self.state["mode"] == "paused":
                self.set_state("boot", "监测恢复，正在检查网络")
            try:
                online, via = internet_ok(self.session)
            except Exception as exc:
                self.log.warning("探测异常: %s", type(exc).__name__)
                online, via = False, ""
            if online:
                if self.state["mode"] != "online":
                    self.set_state("online", "网络正常")
                self.state["failures"] = 0
                self.stop_event.wait(max(1, int(cfg.get("interval", 15))))
                continue
            self.state["failures"] += 1
            threshold = max(1, int(cfg.get("threshold", 3)))
            if self.state["failures"] < threshold:
                if self.state["mode"] != "offline":
                    self.set_state("offline", "公网探测失败（%d/%d）"
                                   % (self.state["failures"], threshold))
                self.stop_event.wait(max(1, int(cfg.get("interval", 15))))
                continue
            if self.state["mode"] != "reconnecting":
                self.set_state("reconnecting", "正在尝试重新连接",
                               notify=("校园网已断开", "正在尝试重新连接，请稍候…"))
            ok, detail, elapsed = self.repair_once()
            self._after_repair(ok, detail, elapsed)
            self.state["failures"] = 0
            self.stop_event.wait(max(60, int(cfg.get("cooldown", 180))))
        self.log.info("监测循环已退出")

    # ---------- 菜单动作 ----------
    def _status_text(self, item=None):
        extra = ""
        if self.state.get("port"):
            extra += " · 端口 %s" % self.state["port"]
        if self.state.get("last_ok"):
            extra += " · 上次重连 %s" % self.state["last_ok"]
        return "状态：%s%s" % (MODE_TEXT.get(self.state["mode"], "?"), extra)

    def _pause_text(self, item=None):
        return "恢复监测" if self.config.get("paused") else "暂停监测"

    def on_reconnect(self, icon, item=None):
        threading.Thread(target=self._reconnect_click, daemon=True).start()

    def _reconnect_click(self):
        online, _ = internet_ok(self.session)
        if online:
            self.notify("无需重连", "当前网络正常，未发送认证请求")
            return
        self.set_state("reconnecting", "手动触发重连")
        ok, detail, elapsed = self.repair_once()
        self._after_repair(ok, detail, elapsed)

    def on_toggle_pause(self, icon, item=None):
        self.config["paused"] = not self.config.get("paused")
        save_config(self.config)
        self.notify("监测已暂停" if self.config["paused"] else "监测已恢复",
                    "可在菜单或设置面板随时切换")

    def on_toggle_proxy(self, icon, item=None):
        self.config["clash_auto_proxy"] = not bool(self.config.get("clash_auto_proxy"))
        save_config(self.config)
        on = self.config["clash_auto_proxy"]
        self.notify("Clash 联动已%s" % ("开启" if on else "关闭"),
                    "重连成功后%s自动开启 Clash 系统代理" % ("会" if on else "不会"))

    def on_toggle_fallback(self, icon, item=None):
        self.config["browser_fallback"] = not bool(self.config.get("browser_fallback"))
        save_config(self.config)
        self.notify("Edge 兜底已%s" % ("开启" if self.config["browser_fallback"] else "关闭"), "")

    def on_toggle_autostart(self, icon, item=None):
        target = not get_autostart()
        if set_autostart(target):
            self.notify("开机自启已%s" % ("开启" if target else "关闭"),
                        "登录 Windows 后自动启动托盘助手" if target else "不再自动启动")
        else:
            self.notify("设置失败", "无法写入开机自启注册表项")
        if self.icon is not None:
            self.icon.update_menu()

    def on_open_log(self, icon, item=None):
        log_file = LOG_DIR / "campus_reconnect.log"
        if log_file.exists():
            try:
                os.startfile(str(log_file))
            except OSError:
                pass

    def on_settings(self, icon, item=None):
        if self.root is not None:
            self.root.after(0, self._open_settings)

    def on_quit(self, icon, item=None):
        self.log.info("用户退出托盘助手")
        self.stop_event.set()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        if self.root is not None:
            self.root.after(0, self.root.destroy)

    def build_menu(self):
        return pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.MenuItem("立即重连", self.on_reconnect),
            pystray.MenuItem(self._pause_text, self.on_toggle_pause),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", self.on_toggle_autostart,
                             checked=lambda item: get_autostart()),
            pystray.MenuItem("重连后自动开启 Clash 系统代理", self.on_toggle_proxy,
                             checked=lambda item: bool(self.config.get("clash_auto_proxy"))),
            pystray.MenuItem("失败时 Edge 兜底", self.on_toggle_fallback,
                             checked=lambda item: bool(self.config.get("browser_fallback"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置面板", self.on_settings),
            pystray.MenuItem("打开日志", self.on_open_log),
            pystray.MenuItem("退出", self.on_quit),
        )

    # ---------- 设置面板 ----------
    def _open_settings(self):
        if self.settings_win is not None:
            try:
                if self.settings_win.winfo_exists():
                    self.settings_win.lift()
                    self.settings_win.focus_force()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("校园网自动重连助手 · 设置面板")
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=12)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text="运行状态", font=("Microsoft YaHei", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text=self._status_text()).grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="上次重连成功：%s" % (self.state.get("last_ok") or "—")).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(0, 10))

        v_auto = tk.BooleanVar(value=get_autostart())
        v_proxy = tk.BooleanVar(value=bool(self.config.get("clash_auto_proxy")))
        v_fallback = tk.BooleanVar(value=bool(self.config.get("browser_fallback")))
        ttk.Checkbutton(frame, text="开机自启（登录 Windows 后自动启动）",
                        variable=v_auto).grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(frame, text="重连成功后自动开启 Clash 系统代理",
                        variable=v_proxy).grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(frame, text="重连失败时自动打开 Edge 兜底",
                        variable=v_fallback).grid(row=5, column=0, columnspan=3, sticky="w")

        pad = {"padx": 4, "pady": 3}
        ttk.Label(frame, text="Wi-Fi 名称").grid(row=6, column=0, sticky="e", **pad)
        v_ssid = tk.StringVar(value=self.config.get("ssid") or "SNNU")
        ttk.Entry(frame, textvariable=v_ssid, width=18).grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(frame, text="运营商").grid(row=7, column=0, sticky="e", **pad)
        v_yys = tk.StringVar(value=self.config.get("yys") or "mobile")
        ttk.Combobox(frame, textvariable=v_yys, width=15, state="readonly",
                     values=["mobile", "unicom", "telecom", ""]).grid(
                         row=7, column=1, sticky="w", **pad)
        ttk.Label(frame, text="检测间隔（秒）").grid(row=8, column=0, sticky="e", **pad)
        v_interval = tk.IntVar(value=int(self.config.get("interval", 15)))
        ttk.Spinbox(frame, from_=5, to=120, textvariable=v_interval, width=16).grid(
            row=8, column=1, sticky="w", **pad)
        ttk.Label(frame, text="连续失败阈值").grid(row=9, column=0, sticky="e", **pad)
        v_thr = tk.IntVar(value=int(self.config.get("threshold", 3)))
        ttk.Spinbox(frame, from_=1, to=10, textvariable=v_thr, width=16).grid(
            row=9, column=1, sticky="w", **pad)
        ttk.Label(frame, text="重连冷却（秒）").grid(row=10, column=0, sticky="e", **pad)
        v_cd = tk.IntVar(value=int(self.config.get("cooldown", 180)))
        ttk.Spinbox(frame, from_=60, to=3600, textvariable=v_cd, width=16).grid(
            row=10, column=1, sticky="w", **pad)

        def on_save():
            try:
                interval = max(5, int(v_interval.get()))
                threshold = max(1, int(v_thr.get()))
                cooldown = max(60, int(v_cd.get()))
            except (tk.TclError, ValueError):
                messagebox.showwarning("输入无效", "间隔/阈值/冷却必须是数字")
                return
            self.config.update({
                "ssid": v_ssid.get().strip() or "SNNU",
                "yys": v_yys.get(),
                "interval": interval,
                "threshold": threshold,
                "cooldown": cooldown,
                "clash_auto_proxy": bool(v_proxy.get()),
                "browser_fallback": bool(v_fallback.get()),
            })
            save_config(self.config)
            set_autostart(bool(v_auto.get()))
            self.notify("设置已保存", "新参数即时生效")
            win.destroy()

        def on_reconnect_click():
            win.destroy()
            self.on_reconnect(None)

        def on_pause_click():
            self.on_toggle_pause(None)
            win.destroy()
            self._open_settings()

        ttk.Button(frame, text="保存", command=on_save).grid(row=11, column=0, pady=(12, 0))
        ttk.Button(frame, text="立即重连", command=on_reconnect_click).grid(
            row=11, column=1, sticky="w", padx=4)
        ttk.Button(frame, text="暂停/恢复", command=on_pause_click).grid(
            row=11, column=2, sticky="w")
        win.protocol("WM_DELETE_WINDOW", win.destroy)

    # ---------- 启动 ----------
    def start(self):
        self.icon = pystray.Icon(APP_NAME, self.make_image(), self.tooltip(),
                                 self.build_menu())
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self.monitor_loop, daemon=True).start()
        threading.Timer(2.0, self._startup_notify).start()

    def _startup_notify(self):
        ssid, _ = wifi_state()
        online, _ = internet_ok(self.session)
        creds = get_credentials()
        body = "Wi-Fi：%s · %s" % (ssid or "未连接", "网络正常" if online else "网络不可用")
        if not creds:
            body += "\n尚未保存凭据：请运行 py campus_reconnect.py configure"
        self.notify("校园网助手已启动", body)


def prompt_credentials_dialog(root):
    """首次运行且无凭据时弹出图形界面输入账号密码（exe 版自包含所需）。"""
    result = {"value": None}
    win = tk.Toplevel(root)
    win.title("校园网账号设置")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    f = ttk.Frame(win, padding=14)
    f.grid(sticky="nsew")
    ttk.Label(f, text="首次使用：请输入校园网账号与密码",
              font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=2)
    ttk.Label(f, text="仅保存在本机 Windows 凭据管理器中").grid(
        row=1, column=0, columnspan=2, pady=(0, 10))
    ttk.Label(f, text="账号").grid(row=2, column=0, sticky="e", padx=4)
    v_user = tk.StringVar()
    ttk.Entry(f, textvariable=v_user, width=22).grid(row=2, column=1, sticky="w", padx=4)
    ttk.Label(f, text="密码").grid(row=3, column=0, sticky="e", padx=4, pady=6)
    v_pwd = tk.StringVar()
    ttk.Entry(f, textvariable=v_pwd, width=22, show="*").grid(
        row=3, column=1, sticky="w", padx=4, pady=6)

    def on_ok():
        u, p = v_user.get().strip(), v_pwd.get()
        if u and p:
            result["value"] = (u, p)
            win.destroy()
        else:
            messagebox.showwarning("信息不完整", "账号和密码都不能为空")

    ttk.Button(f, text="保存", command=on_ok).grid(row=4, column=0, columnspan=2, pady=(10, 0))
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.grab_set()
    root.wait_window(win)
    return result["value"]


def main():
    log = setup_logging(False)
    if not ensure_single_instance(log):
        root0 = tk.Tk()
        root0.withdraw()
        messagebox.showerror(APP_NAME,
                             "已有一个监控实例在运行。\n如需重启，请先在托盘右键退出旧实例。")
        root0.destroy()
        return
    root = tk.Tk()
    root.withdraw()
    if not get_credentials():
        log.warning("尚未保存凭据，弹出账号设置窗口")
        creds = prompt_credentials_dialog(root)
        if creds:
            keyring.set_password(SERVICE, "username", creds[0])
            keyring.set_password(SERVICE, "password", creds[1])
            log.info("凭据已通过界面保存")
        else:
            log.warning("未设置账号，自动重连暂不可用（可在凭据管理器中补存后重启）")
    app = TrayApp(log)
    app.root = root
    log.info("托盘助手启动（autostart=%s, clash_auto_proxy=%s）",
             get_autostart(), app.config.get("clash_auto_proxy"))
    app.start()
    root.mainloop()
    log.info("托盘助手已退出")


if __name__ == "__main__":
    main()
