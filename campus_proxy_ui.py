"""Explicit per-computer setup; all host inspection runs off the Tk thread."""
from __future__ import annotations

from datetime import datetime
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from campus_proxy_compat import (discover_roots, export_rules, runtime_summary,
                                 windows_compatibility)


def mutation_allowed(app):
    clash_busy = getattr(app, "_clash_check_busy", None)
    return (bool(app.get_config().get("paused")) and not app._repair_busy.is_set()
            and not app._lifeline_busy.is_set() and not (clash_busy and clash_busy.is_set()))


def open_dialog(app):
    existing = getattr(app, "proxy_compat_win", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    win = tk.Toplevel(app.root)
    app.proxy_compat_win = win
    win.title("校园网代理兼容检查")
    frame = ttk.Frame(win, padding=14)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text="每台电脑独立检查和备份，保留原有代理端口、订阅和模式。",
              wraplength=650).grid(row=0, column=0, columnspan=3, sticky="w")
    roots = discover_roots()
    selected = tk.StringVar(value=str(roots[0]) if len(roots) == 1 else "仅系统代理")
    combo = ttk.Combobox(frame, textvariable=selected, state="readonly", width=67,
                         values=["仅系统代理"] + [str(root) for root in roots])
    combo.grid(row=1, column=0, columnspan=2, pady=8, sticky="ew")
    output = tk.Text(frame, width=87, height=17, wrap="word", state="disabled")
    output.grid(row=2, column=0, columnspan=3, sticky="nsew")
    busy = {"value": False}
    compatibility = windows_compatibility()
    def show(text):
        if not win.winfo_exists():
            return
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.insert("1.0", text)
        output.configure(state="disabled")
    def dispatch(work, *, mutation=False):
        if busy["value"]:
            return
        if mutation and not mutation_allowed(app):
            messagebox.showinfo("请先暂停助手", "请先在托盘菜单暂停自动恢复，并等待正在进行的重连结束，再修改代理配置。", parent=win)
            return
        busy["value"] = True
        if mutation:
            app._repair_busy.set()
        show("正在处理，请稍候…")
        def worker():
            try:
                result = work()
            except Exception as error:
                # Known adapter errors contain no subscription URLs or controller keys.
                result = str(error) if isinstance(error, (ValueError, OSError)) else "操作未完成，请检查本机配置和备份。"
            finally:
                if mutation:
                    app._repair_busy.clear()
                busy["value"] = False
            app._ui_post(lambda: show(result))
        threading.Thread(target=worker, name="campus-proxy-compat", daemon=True).start()
    def scan():
        targets = roots[:]
        dispatch(lambda: compatibility.inspect(targets) + "\n\n" + runtime_summary())
    def pick_root():
        if busy["value"]:
            return
        folder = filedialog.askdirectory(title="选择含 verge.yaml 与 profiles.yaml 的 Clash Verge 配置目录", parent=win)
        if folder:
            from pathlib import Path
            path = Path(folder).resolve()
            if path not in roots:
                roots.append(path)
            combo.configure(values=["仅系统代理"] + [str(root) for root in roots])
            selected.set(str(path))
            scan()
    def repair(persistent):
        target = selected.get() if persistent else None
        if persistent and target == "仅系统代理":
            messagebox.showinfo("请选择配置目录", "请选择识别到的 Clash Verge 配置目录，便携版可手动选择。其他客户端请导出规则。", parent=win)
            return
        if not mutation_allowed(app):
            messagebox.showinfo("请先暂停助手", "请先暂停自动恢复并等待重连结束。持久修复还需先从托盘退出 Clash Verge。", parent=win)
            return
        description = ("备份并修改所选 Clash Verge 的持久绕过与全局脚本，以及当前 Windows 绕过列表。\n请先退出 Clash Verge，完成后重新启动它。" if persistent else
                       "备份当前 Windows 绕过列表，并追加校园认证 IP 与域名。\n代理开关、端口和 PAC URL 保持原值。PAC/TUN 可能仍需另行配置。")
        if not messagebox.askyesno("修复校园网绕过", description + "\n是否继续？", parent=win):
            return
        def work():
            backup = compatibility.apply(target)
            return f"已写入并备份：{backup}\n\n" + ("请重新启动 Clash Verge，再点击重新检查。\n" if persistent else "") + "请访问校园认证页验证；完成后在托盘恢复助手监测。PAC 与其他 VPN/TUN 请按检查结果处理。"
        dispatch(work, mutation=True)
    def restore():
        if not mutation_allowed(app):
            messagebox.showinfo("请先暂停助手", "请先暂停自动恢复并等待重连结束，再恢复备份。", parent=win)
            return
        if not messagebox.askyesno("恢复最近一次代理备份", "只撤销助手最近一次尚未撤销的修改。若用户此后改过配置，将停止覆盖。\nClash Verge 的备份需先退出客户端。是否继续？", parent=win):
            return
        def work():
            backup = compatibility.latest_backup()
            if backup is None:
                return "没有可恢复的代理备份。"
            compatibility.restore(backup)
            return "已写回备份内容。请重新检查系统代理，必要时重新启动 Clash Verge；然后恢复助手监测。"
        dispatch(work, mutation=True)
    def export():
        if busy["value"]:
            return
        parent = filedialog.askdirectory(title="选择规则导出的保存位置", parent=win)
        if parent:
            from pathlib import Path
            destination = Path(parent) / ("CampusDirect-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
            def work():
                export_rules(destination)
                return f"规则已导出到：{destination}\n\n请先阅读 README.txt。YAML 是需合并的片段，不能替换完整配置或直接作为订阅导入。"
            dispatch(work)
    ttk.Button(frame, text="选择便携版目录…", command=pick_root).grid(row=1, column=2, padx=6)
    buttons = ttk.Frame(frame)
    buttons.grid(row=3, column=0, columnspan=3, pady=10, sticky="w")
    for title, command in (("重新检查", scan), ("修复系统绕过", lambda: repair(False)),
                            ("修复所选 Clash Verge", lambda: repair(True)),
                            ("恢复最近备份", restore), ("导出其他客户端规则…", export)):
        ttk.Button(buttons, text=title, command=command).pack(side="left", padx=3)
    ttk.Label(frame, text="持久修复后需要重启代理客户端。检查不发送校园账号密码，也不主动断网。",
              wraplength=680).grid(row=4, column=0, columnspan=3, sticky="w")
    scan()


def offer_onboarding(app, *, background):
    from campus_install import atomic_json, data_directory
    marker = data_directory() / "proxy-compat-onboarding-v1.json"
    if marker.exists():
        return
    def offer(summary):
        if background:
            app.notify("首次使用：检查校园网代理兼容", "请在设置 → 网络中打开代理兼容检查。每台电脑需要独立检查 PAC/TUN 与持久绕过。")
            # Keep the foreground guide pending until the user has seen it.
            return
        if messagebox.askyesno("首次使用：校园网代理兼容", summary + "\n\n检查只读，修改前另行确认。现在打开完整检查？", parent=app.root):
            open_dialog(app)
        try:
            atomic_json(marker, {"version": 1})
        except OSError:
            app.log.warning("代理兼容提示状态未保存，下次启动将再次提示")
    def inspect():
        try:
            lines = windows_compatibility().inspect(discover_roots()).splitlines()
            summary = "\n".join(lines[:3])
        except Exception:
            summary = "本机代理配置暂时无法确认，可在兼容检查中重试。"
        app._ui_post(lambda: offer(summary))
    threading.Thread(target=inspect, name="campus-proxy-onboarding", daemon=True).start()
