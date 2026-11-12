"""Explicit per-computer setup; all host inspection runs off the Tk thread."""
from __future__ import annotations

from datetime import datetime
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from campus_desktop_ui import (ACCENT, BG, TEXT, button, configure_theme,
                               ctk, fit_window, font, label, panel)
import uuid

from campus_proxy_compat import (discover_roots, export_rules, runtime_summary,
                                 windows_compatibility)


def mutation_allowed(app):
    clash_busy = getattr(app, "_clash_check_busy", None)
    return (bool(app.get_config().get("paused")) and not app._repair_busy.is_set()
            and not app._lifeline_busy.is_set() and not (clash_busy and clash_busy.is_set()))


def assessment(report, *, online=False):
    """Explain existing inspection output without claiming a connectivity test."""
    if "上次代理事务未完成" in report:
        return "需要处理：上次修复尚未完成", "先暂停助手，再恢复最近备份。恢复完成后重新检查。"
    missing = "系统代理：已开启" in report and "系统绕过：缺少校园地址" in report
    uncertain = any(s in report for s in ("PAC：已配置", "校园规则不完整", "不可确认", "无法确认",
                                          "TUN：配置为开启", "活动的代理虚拟网卡", "代理内核进程"))
    if online:
        if missing or uncertain:
            return "网络正常，当前无需修复", "助手最近的网络验证正常。代理仍有兼容项待核对，遇到认证页打不开时再按报告处理；可直接关闭此窗口。"
        return "网络正常，无需操作", "系统代理未发现需要处理的项目，可以直接关闭此窗口。"
    if missing:
        return "建议补充校园网绕过", "系统代理已开启，但绕过列表缺少校园地址。认证页打不开时，可先暂停助手，再点击“修复系统绕过”。"
    if uncertain:
        return "有兼容项需要核对", "请展开检查报告，核对 PAC、TUN 或 Clash 规则。此检查未测试当前能否上网；网络正常时无需立即修复。"
    if "系统绕过：校园地址已配置" in report or "系统代理：未开启" in report:
        return "未发现需要处理的代理配置", "可直接关闭此窗口。此结果仅说明代理配置检查，是否能上网请回概览点击“立即检测”。"
    return "检查结果暂未确认", "请查看报告或点击“重新检查”。暂不建议修改代理配置。"


def open_dialog(app):
    if ctk is None:
        messagebox.showinfo("图形组件不可用", "请安装 CustomTkinter 或下载完整的 Windows EXE，再打开代理兼容检查。",
                            parent=app.root)
        return
    existing = getattr(app, "proxy_compat_win", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    configure_theme(app.root)
    win = ctk.CTkToplevel(app.root)
    app.proxy_compat_win = win
    win.title("校园网代理兼容检查")
    win.configure(fg_color=BG)
    fit_window(win, 820, 480, minimum=(700, 470))
    frame = ctk.CTkScrollableFrame(win, fg_color=BG, corner_radius=0)
    frame.pack(fill="both", expand=True)
    label(frame, "代理兼容检查", size=24, bold=True).pack(fill="x", padx=24, pady=(24, 4))
    label(frame, "只检查这台电脑。需要修复时，先备份，再由你确认。", muted=True,
          wraplength=520).pack(fill="x", padx=24, pady=(0, 20))
    summary = panel(frame)
    summary.pack(fill="x", padx=24, pady=(0, 16))
    heading = label(summary, "正在检查本机配置…", size=19, bold=True)
    heading.pack(fill="x", padx=18, pady=(16, 6))
    guidance = label(summary, "请稍候，检查不会修改代理或断开网络。", muted=True,
                     wraplength=520, justify="left")
    guidance.pack(fill="x", padx=18, pady=(0, 16))
    roots = discover_roots()
    selected = tk.StringVar(value=str(roots[0]) if len(roots) == 1 else "仅系统代理")
    details = panel(frame)
    detail_open = {"value": False}
    repair_open = {"value": False}
    output = ctk.CTkTextbox(details, height=230, wrap="word", font=font(13),
                            fg_color="#FFFFFF", text_color=TEXT, corner_radius=5,
                            border_width=0, state="disabled")
    output.pack(fill="both", expand=True, padx=8, pady=8)
    def toggle_details():
        detail_open["value"] = not detail_open["value"]
        if detail_open["value"]:
            details.pack(fill="x", padx=24, pady=(0, 16),
                         before=advanced if repair_open["value"] else footer)
        else:
            details.pack_forget()
        detail_button.configure(text="收起检查报告" if detail_open["value"] else "查看检查报告")
    toolbar = ctk.CTkFrame(frame, fg_color="transparent")
    toolbar.pack(fill="x", padx=24, pady=(0, 16))
    detail_button = button(toolbar, "查看检查报告", toggle_details, width=150)
    detail_button.pack(side="right")
    advanced = panel(frame)
    def toggle_repairs():
        repair_open["value"] = not repair_open["value"]
        if repair_open["value"]:
            advanced.pack(fill="x", padx=24, pady=(0, 16), before=footer)
        else:
            advanced.pack_forget()
        repair_button.configure(text="收起修复选项" if repair_open["value"] else "展开修复选项")
    repair_button = button(toolbar, "展开修复选项", toggle_repairs, width=150)
    repair_button.pack(side="right", padx=8)
    label(advanced, "按需修复", size=17, bold=True).pack(fill="x", padx=18, pady=(16, 4))
    label(advanced, "网络正常时可直接关闭。修改前先暂停助手；持久修复还需退出 Clash Verge。",
          muted=True, wraplength=520, justify="left").pack(fill="x", padx=18, pady=(0, 12))
    combo = ctk.CTkOptionMenu(advanced, variable=selected,
                              values=["仅系统代理"] + [str(root) for root in roots],
                              font=font(13), dropdown_font=font(13), height=36,
                              fg_color="#EEEEF2", button_color=ACCENT, text_color=TEXT,
                              dynamic_resizing=False)
    combo.pack(fill="x", padx=18, pady=(0, 12))
    actions = ctk.CTkFrame(advanced, fg_color="transparent")
    actions.pack(fill="x", padx=18, pady=(0, 16))
    actions.grid_columnconfigure((0, 1), weight=1)
    footer = label(frame, "系统代理、PAC 和 TUN 的作用范围不同；报告不会把未确认的规则当作已生效。",
                   muted=True, size=12, wraplength=520, justify="left")
    footer.pack(fill="x", padx=24, pady=(0, 20))
    button(frame, "关闭窗口", win.destroy, width=130).pack(anchor="e", padx=24, pady=(0, 20))
    app._proxy_ui_refs = {"heading": heading, "guidance": guidance, "output": output,
                          "details": details, "toggle_details": toggle_details}
    busy = {"value": False}
    compatibility = windows_compatibility()
    def show(text, *, checked=False):
        if not win.winfo_exists():
            return
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.insert("1.0", text)
        output.configure(state="disabled")
        if checked:
            online = False
            try:
                online = app.ui_state().overall == "HEALTHY"
            except AttributeError:
                pass
            title, explanation = assessment(text, online=online)
            heading.configure(text=title)
            guidance.configure(text=explanation)
            if title.startswith(("需要处理", "建议补充")) and not repair_open["value"]:
                toggle_repairs()
        elif text == "正在处理，请稍候…":
            heading.configure(text="正在处理…")
            guidance.configure(text="请等待本次操作完成。")
        else:
            heading.configure(text="操作结果")
            guidance.configure(text=text, wraplength=520)
    def dispatch(work, *, mutation=False, checked=False):
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
            app._ui_post(lambda: show(result, checked=checked))
        threading.Thread(target=worker, name="campus-proxy-compat", daemon=True).start()
    def scan():
        targets = roots[:]
        dispatch(lambda: compatibility.inspect(targets) + "\n\n" + runtime_summary(), checked=True)
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
    button(toolbar, "重新检查", scan, primary=True, width=130).pack(side="left")
    for index, (title, command) in enumerate((
            ("修复系统绕过", lambda: repair(False)),
            ("修复所选 Clash Verge", lambda: repair(True)),
            ("选择便携版目录…", pick_root),
            ("导出其他客户端规则…", export),
            ("恢复最近备份", restore),
            ("关闭窗口", win.destroy))):
        button(actions, title, command).grid(row=index // 2, column=index % 2,
                                             sticky="ew", padx=(0, 8), pady=4)
    scan()


def offer_onboarding(app, *, background):
    from campus_install import atomic_json, data_directory
    marker = data_directory() / "proxy-compat-onboarding-v1.json"
    if marker.exists():
        return
    def offer(summary):
        online = False
        try:
            online = app.ui_state().overall == "HEALTHY"
        except AttributeError:
            pass
        title, explanation = assessment(summary, online=online)
        if title == "未发现需要处理的代理配置" or title.startswith("网络正常"):
            try:
                atomic_json(marker, {"version": 1})
            except OSError:
                app.log.warning("代理兼容提示状态未保存")
            return
        summary = title + "\n" + explanation
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
            summary = windows_compatibility().inspect(discover_roots()) + "\n" + runtime_summary()
        except Exception:
            summary = "本机代理配置暂时无法确认，可在兼容检查中重试。"
        app._ui_post(lambda: offer(summary))
    threading.Thread(target=inspect, name="campus-proxy-onboarding", daemon=True).start()
