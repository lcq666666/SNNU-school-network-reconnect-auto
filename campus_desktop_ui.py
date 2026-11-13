"""Shared desktop presentation. No host inspection or recovery actions on render."""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

from campus_ui_prefs import save_theme

try:
    import customtkinter as ctk
except ImportError:
    ctk = None

FONT = "Microsoft YaHei UI"
FONT_DISPLAY = "SimSun"  # P3: serif display face for editorial headings
STEP_NAMES = {"detect": "检测网络", "wifi_ready": "连接无线", "portal": "校园认证",
              "internet": "验证上网", "route": "切换出口", "uu": "恢复 UU"}

# P1: two palettes behind one key set. LIGHT is the v0.7.0 baseline (pixel-identical),
# DARK is the low-saturation signal palette defined in .impeccable.md.
_LIGHT = {
    "BG": "#F6F6F8",
    "SIDE": "#EEEEF2",
    "SURFACE": "#FFFFFF",
    "BORDER": "#DFDFE7",
    "TEXT": "#25252D",
    "MUTED": "#666673",
    "ACCENT": "#6554C0",
    "ACCENT_HOVER": "#5545AA",
    "ACCENT_TEXT": "#FFFFFF",
    "BUTTON_HOVER": "#EEEEF2",
    "NAV_HOVER": "#E3E1EC",
    "TONES": {"ok": "#237A50", "bad": "#B43838", "warn": "#956414",
              "busy": "#6554C0", "unknown": "#666673", "idle": "#666673"},
    # P4: scrollbar track / thumb / thumb-hover, low-saturation and theme-bound.
    "SCROLL_TRACK": "#E6E6EC",
    "SCROLL_THUMB": "#C6C9D4",
    "SCROLL_HOVER": "#A9ADBA",
}
_DARK = {
    "BG": "#0F1519",
    "SIDE": "#141B20",
    "SURFACE": "#1A2329",
    "BORDER": "#2B363D",
    "TEXT": "#EDE8DE",
    "MUTED": "#96A2A8",
    "ACCENT": "#4E9E84",
    "ACCENT_HOVER": "#3C816C",
    "ACCENT_TEXT": "#0B1216",
    "BUTTON_HOVER": "#223038",
    "NAV_HOVER": "#223038",
    "TONES": {"ok": "#79CFAE", "bad": "#E58474", "warn": "#E0BE6B",
              "busy": "#8FB8C8", "unknown": "#96A2A8", "idle": "#96A2A8"},
    "SCROLL_TRACK": "#1B252B",
    "SCROLL_THUMB": "#3A4A52",
    "SCROLL_HOVER": "#4C5E67",
}
THEMES = {"light": _LIGHT, "dark": _DARK}
_current_theme = "light"

# Kept as module constants: campus_proxy_ui imports them directly and offline tests
# assert the v0.7.0 state colours. They always describe the light baseline.
BG = _LIGHT["BG"]
SIDE = _LIGHT["SIDE"]
SURFACE = _LIGHT["SURFACE"]
BORDER = _LIGHT["BORDER"]
TEXT = _LIGHT["TEXT"]
MUTED = _LIGHT["MUTED"]
ACCENT = _LIGHT["ACCENT"]
TONES = dict(_LIGHT["TONES"])

# P2: the overview is a fixed dark signal console in both themes (A+B direction).
# It shares the palette key set so every factory and _paint branch can consume it.
CONSOLE = {
    "BG": "#0B1216",
    "SIDE": "#0F191E",
    "SURFACE": "#122027",
    "BORDER": "#263A42",
    "TEXT": "#E7ECE8",
    "MUTED": "#90A4A7",
    "ACCENT": "#79CFAE",
    "ACCENT_HOVER": "#5DB493",
    "ACCENT_TEXT": "#0B1216",
    "BUTTON_HOVER": "#1D3037",
    "NAV_HOVER": "#1D3037",
    "TONES": {"ok": "#79CFAE", "bad": "#E58474", "warn": "#E0BE6B",
              "busy": "#8FB8C8", "unknown": "#90A4A7", "idle": "#90A4A7"},
    "SCROLL_TRACK": "#101E24",
    "SCROLL_THUMB": "#3A4F57",
    "SCROLL_HOVER": "#4C626C",
}
# Verbatim state identifiers from campus_ui_state, never invented status words.
SIGNAL_WORDS = {"HEALTHY": "ONLINE", "DEGRADED": "DEGRADED",
                "RECOVERING": "RECOVERING", "OFFLINE": "OFFLINE",
                "PAUSED": "PAUSED", "UNKNOWN": "UNKNOWN"}


def theme_mode():
    """Active palette name. Presentation-only; never read by the network state machine."""
    return _current_theme


def tokens(mode=None):
    """Return the active palette (or an explicit one) without mutating app state."""
    return THEMES[mode or _current_theme]


def set_theme(mode):
    """Switch active palette. Returns the palette; does not touch config, timers or state."""
    global _current_theme
    if mode not in THEMES:
        raise ValueError(f"unknown theme: {mode}")
    _current_theme = mode
    if ctk is not None:
        ctk.set_appearance_mode(mode)
    return tokens()


def bind_theme(widget, role, **info):
    """Tag a widget so apply_theme() can repaint it after a palette switch."""
    widget._campus_theme = (role, info)
    if ctk is not None and isinstance(widget, ctk.CTkScrollableFrame):
        # Its scrollbar is a sibling of the inner frame, so the normal tree
        # walk cannot infer which page palette it belongs to.
        widget._scrollbar._campus_theme = (
            "scrollbar", {"console": bool(info.get("console"))})
    return widget


def _paint(widget, palette):
    mark = getattr(widget, "_campus_theme", None)
    if not mark:
        return
    role, info = mark
    try:
        if not widget.winfo_exists():
            return
    except (tk.TclError, AttributeError):
        return
    colors = CONSOLE if info.get("console") else palette
    try:
        if role == "label":
            widget.configure(text_color=colors["MUTED"] if info.get("muted") else colors["TEXT"])
        elif role == "tone":
            widget.configure(text_color=colors["TONES"].get(info.get("tone"), colors["MUTED"]))
        elif role == "button":
            primary = info.get("primary", False)
            widget.configure(
                fg_color=colors["ACCENT"] if primary else colors["SURFACE"],
                hover_color=colors["ACCENT_HOVER"] if primary else colors["BUTTON_HOVER"],
                text_color=colors["ACCENT_TEXT"] if primary else colors["TEXT"],
                border_color=colors["BORDER"])
        elif role == "nav":
            active = info.get("selected", False)
            widget.configure(
                fg_color=colors["SURFACE"] if active else "transparent",
                text_color=colors["ACCENT"] if active else colors["TEXT"],
                hover_color=colors["NAV_HOVER"])
        elif role == "mode":
            active = info.get("mode") == theme_mode()
            widget.configure(
                fg_color=colors["ACCENT"] if active else colors["SURFACE"],
                hover_color=colors["ACCENT_HOVER"] if active else colors["BUTTON_HOVER"],
                text_color=colors["ACCENT_TEXT"] if active else colors["TEXT"],
                border_color=colors["BORDER"])
        elif role == "panel":
            widget.configure(fg_color=colors["SURFACE"], border_color=colors["BORDER"])
        elif role == "side":
            widget.configure(fg_color=colors["SIDE"])
        elif role in ("surface", "window"):
            widget.configure(fg_color=colors["BG"])
        elif role == "tk_window":
            widget.configure(background=colors["BG"])
        elif role == "progress":
            widget.configure(progress_color=colors["ACCENT"], fg_color=colors["BORDER"])
        elif role == "textbox":
            widget.configure(fg_color=colors["SURFACE"], text_color=colors["TEXT"])
        elif role == "option":
            widget.configure(fg_color=colors["SIDE"], button_color=colors["ACCENT"],
                             text_color=colors["TEXT"])
        elif role == "strip":
            widget.configure(fg_color=colors["TONES"].get(info.get("tone"), colors["MUTED"]))
        elif role == "rule":
            widget.configure(fg_color=colors["BORDER"])
        elif role == "topology":
            widget.configure(bg=colors["SURFACE"])
            draw_topology(widget, colors, getattr(widget, "_campus_topology", {}))
        elif role == "scrollbar":
            if ctk is not None and isinstance(widget, ctk.CTkScrollbar):
                widget.configure(
                    fg_color=colors["SCROLL_TRACK"],
                    button_color=colors["SCROLL_THUMB"],
                    button_hover_color=colors["SCROLL_HOVER"])
    except (tk.TclError, AttributeError, TypeError, ValueError):
        return


def _walk(root):
    try:
        children = root.winfo_children()
    except (tk.TclError, AttributeError):
        return
    for child in children:
        yield child
        yield from _walk(child)


def apply_theme(*roots):
    """Repaint every tagged widget in the given window trees (plus the default root)."""
    palette = tokens()
    seen = set()
    candidates = list(roots)
    if tk._default_root is not None:
        candidates.append(tk._default_root)
    for root in candidates:
        if root is None:
            continue
        for widget in (root, *_walk(root)):
            key = id(widget)
            if key in seen:
                continue
            seen.add(key)
            # CTkScrollableFrame creates its CTkScrollbar internally without a theme
            # mark; colour it directly so scrollbars follow the active palette.
            if (ctk is not None and isinstance(widget, ctk.CTkScrollbar)
                    and not getattr(widget, "_campus_theme", None)):
                try:
                    if widget.winfo_exists():
                        widget.configure(
                            fg_color=palette["SCROLL_TRACK"],
                            button_color=palette["SCROLL_THUMB"],
                            button_hover_color=palette["SCROLL_HOVER"])
                except (tk.TclError, AttributeError, TypeError):
                    pass
                continue
            _paint(widget, palette)


def switch_theme(mode):
    """Switch palette and repaint live windows without retaining any window reference.

    Keeping this at module scope avoids a Tk widget reference cycle: a button command
    defined inside build_dashboard would capture the Toplevel, and GC would then destroy
    Tcl objects from a non-main thread.
    """
    set_theme(mode)
    save_theme(mode)
    root = tk._default_root
    if root is not None:
        configure_theme(root)
        apply_theme(root)


def theme_switcher(parent, palette):
    """Build the light/dark switch; retains no Toplevel reference."""
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.grid(row=7, column=0, sticky="ew", padx=12, pady=(20, 0))
    frame.grid_columnconfigure((0, 1), weight=1)
    label(frame, "外观", size=11, muted=True).grid(
        row=0, column=0, columnspan=2, sticky="w", padx=8)
    for column, (mode, caption) in enumerate((("light", "浅色"), ("dark", "深色"))):
        active = mode == theme_mode()
        widget = ctk.CTkButton(
            frame, text=caption, command=lambda m=mode: switch_theme(m),
            font=font(12), height=28, corner_radius=5,
            fg_color=palette["ACCENT"] if active else palette["SURFACE"],
            hover_color=(palette["ACCENT_HOVER"] if active else palette["BUTTON_HOVER"]),
            text_color=palette["ACCENT_TEXT"] if active else palette["TEXT"],
            border_width=0 if active else 1, border_color=palette["BORDER"])
        bind_theme(widget, "mode", mode=mode)
        widget.grid(row=1, column=column, sticky="ew",
                    padx=(0 if column == 0 else 3, 3 if column == 0 else 0), pady=4)


def _tone(widget, tone, *, console=False):
    """Assign a state colour and remember its semantic key for later repaints."""
    colors = CONSOLE if console else tokens()
    widget.configure(text_color=colors["TONES"].get(tone, colors["MUTED"]))
    bind_theme(widget, "tone", tone=tone, console=console)


def _set_packed_visibility(widget, visible, **pack_options):
    """Show or hide an optional packed control without reordering it on refresh."""
    try:
        manager = widget.winfo_manager()
    except (tk.TclError, AttributeError):
        manager = ""
    if visible and manager != "pack":
        widget.pack(**pack_options)
    elif not visible and manager == "pack":
        widget.pack_forget()


def sleep_action_required(report):
    """Whether readiness currently exposes the power-settings action."""
    return bool(report and any(
        item.key == "sleep" and item.status == "ACTION"
        for item in report.items))


def font(size=14, bold=False):
    # Share immutable font instances within this interpreter, never across Tk roots.
    root = tk._default_root
    if root is None:
        return ctk.CTkFont(family=FONT, size=size, weight="bold" if bold else "normal")
    if not hasattr(root, "_campus_fonts"):
        root._campus_fonts = {}
    key = (size, bold)
    if key not in root._campus_fonts:
        root._campus_fonts[key] = ctk.CTkFont(family=FONT, size=size,
                                            weight="bold" if bold else "normal")
    return root._campus_fonts[key]


def display_font(size=24, bold=False):
    """Serif display face for editorial headings; cached per Tk root like font()."""
    root = tk._default_root
    if root is None:
        return ctk.CTkFont(family=FONT_DISPLAY, size=size,
                           weight="bold" if bold else "normal")
    if not hasattr(root, "_campus_display_fonts"):
        root._campus_display_fonts = {}
    key = (size, bold)
    if key not in root._campus_display_fonts:
        root._campus_display_fonts[key] = ctk.CTkFont(
            family=FONT_DISPLAY, size=size, weight="bold" if bold else "normal")
    return root._campus_display_fonts[key]


def section(parent):
    """Borderless, transparent block: the editorial alternative to a white card."""
    return ctk.CTkFrame(parent, fg_color="transparent")


def divider(parent):
    colors = tokens()
    return bind_theme(ctk.CTkFrame(parent, height=1, fg_color=colors["BORDER"],
                                   corner_radius=0), "rule")


def numbered(parent, index, title):
    """Serial + serif heading + hairline, one line per section."""
    head = section(parent)
    head.pack(fill="x", padx=24, pady=(18, 6))
    label(head, f"{index:02d}", size=12, muted=True, display=True).pack(
        side="left", padx=(0, 12))
    label(head, title, size=20, bold=True, display=True).pack(side="left")
    divider(head).pack(side="left", fill="x", expand=True, padx=12)
    return head


def configure_theme(root):
    palette = tokens()
    if ctk is not None:
        ctk.set_appearance_mode(theme_mode())
    # Legacy settings use the same family, palette and readable native point sizes.
    scale = (ctk.ScalingTracker.get_window_dpi_scaling(root) if ctk is not None
             else root.winfo_fpixels("1i") / 96)
    native_font = (FONT, -round(14 * scale))
    root.option_add("*Font", native_font)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=native_font, background=palette["BG"], foreground=palette["TEXT"])
    style.configure("TButton", padding=(round(12 * scale), round(7 * scale)),
                    background=palette["SURFACE"])
    style.map("TButton", background=[("active", palette["SIDE"])])
    style.configure("TNotebook", background=palette["BG"], borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 9), background=palette["SIDE"])
    style.map("TNotebook.Tab", background=[("selected", palette["SURFACE"])],
              foreground=[("selected", palette["ACCENT"])])
    style.configure("TEntry", fieldbackground=palette["SURFACE"])
    style.configure("TCombobox", fieldbackground=palette["SURFACE"])
    # P4: native ttk scrollbar fallback (CTkScrollableFrame uses CTkScrollbar,
    # painted by apply_theme; this covers any ttk scrollbars in the shell).
    style.configure("TScrollbar", background=palette["SCROLL_TRACK"],
                    troughcolor=palette["SCROLL_TRACK"],
                    bordercolor=palette["BORDER"])
    style.map("TScrollbar",
              background=[("active", palette["SCROLL_HOVER"])],
              troughcolor=[("active", palette["SCROLL_TRACK"])])


def fit_window(win, width, height, *, minimum=(760, 460)):
    """Use the display work area instead of a fixed logical size.

    CustomTkinter sizes are logical pixels.  Computing the target from the
    scaled work area keeps the dashboard usable at 100%, 125%, 150% and 200%
    DPI while retaining a sensible minimum on small laptop screens.
    """
    scale = win._get_window_scaling()
    screen_w = max(320, int(win.winfo_screenwidth() / scale))
    screen_h = max(300, int(win.winfo_screenheight() / scale))
    max_w = max(320, int(screen_w * 0.92))
    max_h = max(300, int(screen_h * 0.90))
    target_w = min(max(width, int(screen_w * 0.88)), max_w)
    target_h = min(max(height, int(screen_h * 0.88)), max_h)
    win.geometry(f"{target_w}x{target_h}")
    win.minsize(min(minimum[0], max_w), min(minimum[1], max_h))


def keep_in_task_switcher(win):
    """Make an overrideredirect window visible to Alt-Tab on Windows."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = win.winfo_id()
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        style = get_long(hwnd, -20)
        style = (style | 0x00040000) & ~0x00000080  # APPWINDOW, no TOOLWINDOW
        set_long(hwnd, -20, style)
    except (AttributeError, OSError, tk.TclError):
        return


class _OverviewCanvasProxy:
    """Compatibility view for tests that inspect a scroll frame's canvas."""

    def __init__(self, frame, *, scrollbar=False, console=False):
        self._frame = frame
        self._scrollbar = scrollbar
        self._console = console

    def cget(self, option):
        if self._scrollbar and option == "fg_color":
            return (CONSOLE if self._console else tokens())["SCROLL_TRACK"]
        if option == "background":
            return self._frame.cget("fg_color")
        raise tk.TclError("unsupported overview canvas option: %s" % option)


def label(parent, text, *, size=14, bold=False, muted=False, console=False,
          display=False, **kwargs):
    # Fixed logical line lengths avoid a Tk geometry feedback loop at fractional DPI.
    palette = CONSOLE if console else tokens()
    text_font = display_font(size, bold) if display else font(size, bold)
    return bind_theme(
        ctk.CTkLabel(parent, text=text, font=text_font,
                     text_color=palette["MUTED"] if muted else palette["TEXT"],
                     anchor="w", **kwargs),
        "label", muted=muted, console=console)


def button(parent, text, command, *, primary=False, console=False, **kwargs):
    palette = CONSOLE if console else tokens()
    height = kwargs.pop("height", 38)
    return bind_theme(
        ctk.CTkButton(parent, text=text, command=command, font=font(), height=height,
                      corner_radius=5, fg_color=palette["ACCENT"] if primary else palette["SURFACE"],
                      hover_color=(palette["ACCENT_HOVER"] if primary
                                   else palette["BUTTON_HOVER"]),
                      text_color=palette["ACCENT_TEXT"] if primary else palette["TEXT"],
                      border_width=0 if primary else 1, border_color=palette["BORDER"], **kwargs),
        "button", primary=primary, console=console)


def panel(parent, *, console=False, flat=False):
    palette = CONSOLE if console else tokens()
    return bind_theme(
        ctk.CTkFrame(parent, fg_color=palette["SURFACE"],
                     corner_radius=0 if flat else 7,
                     border_width=0 if flat else 1, border_color=palette["BORDER"]),
        "panel", console=console)


def translated_event(line):
    names = {"RECOVERY_COMPLETE": "网络恢复", "NETWORK_DOWN": "网络中断",
             "NETWORK_UP": "网络连通", "NETWORK_DEGRADED": "网络降级",
             "UU_RESTART_STARTED": "开始恢复 UU", "GAMEVIEWER_RESTARTED": "UU 已重启",
             "UU_RESTART_FAILED": "UU 重启失败", "UU_SKIPPED": "跳过 UU 重启",
             "RECONNECT_OK": "重连成功", "RECONNECT_FAILED": "重连失败",
             "MONITOR_PAUSED": "监测已暂停", "MONITOR_RESUMED": "监测已恢复",
             "AXIS_STATE": "链路状态", "FAILOVER_STATE": "恢复状态",
             "ACTIVE_PATH": "当前出口", "RECOVERY_EVENT": "恢复事件",
             "RECOVERY_STAGE": "恢复阶段", "VERIFY_GATE": "验证入口",
             "VERIFY_CROSS": "交叉验证", "INTERNET_VERIFIED": "上网验证通过",
             "INTERNET_UNVERIFIED": "上网未验证", "VERIFY_UNRELIABLE": "验证不可信",
             "STANDBY_VERIFIED": "备用链路验证通过", "ROUTE_SWITCH_DONE": "出口切换完成",
             "ROUTE_SWITCH": "准备切换出口", "SNAPSHOT_UNTRUSTED": "网卡信息不可信",
             "ethernet": "以太网", "wifi": "无线网络"}
    for key in sorted(names, key=len, reverse=True):
        line = line.replace(key, names[key])
    return line


def path_summary(card):
    if card is None:
        return "待确认", "unknown"
    if card.verified is True:
        return "已验证", "ok"
    if card.tone in ("bad", "warn"):
        return card.state_label, card.tone
    result = card.field("Internet")
    return (result if result not in ("", "—", "已验证") else "未验证"), card.internet_tone


def draw_topology(canvas, colors, data):
    """Redraw the link graph from real UiState values. No timers, no invented values.

    One text line per node keeps labels clear; detailed states live in the data strips
    below the graph, so the graph itself never repeats or crowds them.
    """
    canvas.delete("all")
    width = max(360, canvas.winfo_width())
    scale = width / 460.0
    try:
        ui_scale = ctk.ScalingTracker.get_widget_scaling(canvas)
    except (AttributeError, tk.TclError):
        ui_scale = 1.0
    small = (FONT, max(8, round(9 * min(1.5, max(1.0, ui_scale)))))
    muted, text = colors["MUTED"], colors["TEXT"]
    line = colors["BORDER"]

    def tone(role):
        return colors["TONES"].get(data.get(role, {}).get("tone", "unknown"), muted)

    def suffix(role):
        prefix = data.get(role, {}).get("role") or ""
        return f"（{prefix}）" if prefix else ""

    def node(x, y, radius, color, caption):
        canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                           fill=colors["SURFACE"], outline=color, width=2)
        canvas.create_text(x, y - radius - 15, text=caption, fill=text, font=small)

    left_x, mid_x, right_x = 46 * scale, 200 * scale, 400 * scale
    top_y, mid_y, bottom_y = 38, 66, 94
    node(left_x, mid_y, 12, colors["ACCENT"], "本机")
    node(mid_x, top_y, 8, tone("ethernet"), "以太网" + suffix("ethernet"))
    node(mid_x, bottom_y, 8, tone("wifi"), "SNNU Wi-Fi" + suffix("wifi"))
    node(right_x, mid_y, 10, tone("uu"), "UU 远程")

    canvas.create_line(left_x + 12, mid_y, 110 * scale, mid_y, 110 * scale, top_y,
                       mid_x - 8, top_y, fill=tone("ethernet"), width=2)
    canvas.create_line(left_x + 12, mid_y, 110 * scale, mid_y, 110 * scale, bottom_y,
                       mid_x - 8, bottom_y, fill=tone("wifi"), width=2, dash=(4, 3))
    canvas.create_line(mid_x + 8, top_y, 300 * scale, top_y, 300 * scale, mid_y,
                       right_x - 10, mid_y, fill=line, width=1, dash=(2, 4))
    canvas.create_line(mid_x + 8, bottom_y, 300 * scale, bottom_y, 300 * scale, mid_y,
                       right_x - 10, mid_y, fill=line, width=1, dash=(2, 4))
    canvas.create_text(left_x, 120, text="直连探测", fill=muted, font=small)
    canvas.create_text(right_x, 120, text="恢复链路", fill=muted, font=small)


def _on_topology_resize(event):
    canvas = event.widget
    draw_topology(canvas, CONSOLE, getattr(canvas, "_campus_topology", {}))


def build_console(app, body, refs):
    """Build the overview signal console. Module scope keeps build_dashboard small."""
    banner = panel(body, console=True)
    refs.update(update_banner=banner, update_banner_visible=False)
    refs["update_banner_label"] = label(banner, "", console=True, wraplength=330)
    refs["update_banner_label"].pack(side="left", padx=16, pady=12)
    button(banner, "下载更新", app._on_update_download, primary=True, console=True,
           width=100).pack(side="right", padx=12)
    button(banner, "以后再说", app._on_update_dismiss, console=True, width=90).pack(
        side="right")

    hero = panel(body, console=True, flat=True)
    hero.pack(fill="x", padx=24, pady=(0, 16))
    refs["gauges_frame"] = hero  # Anchor for the existing in-app update banner.
    label(hero, "链路状态  /  SIGNAL", size=11, muted=True, console=True).pack(
        fill="x", padx=20, pady=(18, 2))
    status_line = ctk.CTkFrame(hero, fg_color="transparent")
    status_line.pack(fill="x", padx=20, pady=(0, 4))
    refs["light"] = label(status_line, "●", size=26, console=True)
    refs["light"].pack(side="left", padx=(0, 10))
    refs["overall"] = label(status_line, "正在读取状态", size=34, bold=True, console=True)
    refs["overall"].pack(side="left")
    refs["signal_word"] = label(status_line, "UNKNOWN", size=12, bold=True, console=True)
    refs["signal_word"].pack(side="left", padx=(14, 0))
    refs["headline"] = label(hero, "", muted=True, wraplength=520, console=True)
    refs["headline"].pack(fill="x", padx=20, pady=(0, 4))
    refs["monitor"] = label(hero, "", size=12, muted=True, wraplength=520, console=True)
    refs["monitor"].pack(fill="x", padx=20, pady=(0, 10))

    topo = tk.Canvas(hero, height=136, highlightthickness=0, bd=0, bg=CONSOLE["SURFACE"])
    topo.pack(fill="x", padx=20, pady=(0, 18))
    topo._campus_topology = {}
    bind_theme(topo, "topology", console=True)
    topo.bind("<Configure>", _on_topology_resize)
    refs["topology"] = topo
    draw_topology(topo, CONSOLE, {})

    cards = ctk.CTkFrame(body, fg_color="transparent")
    cards.pack(fill="x", padx=24)
    for column in range(3):
        cards.grid_columnconfigure(column, weight=1, uniform="status")
    refs["status_cards"] = {}
    for column, (role, title) in enumerate((("ethernet", "以太网"), ("wifi", "SNNU Wi-Fi"),
                                            ("uu", "UU 远程恢复"))):
        card = panel(cards, console=True, flat=True)
        card.grid(row=0, column=column, sticky="nsew",
                  padx=(0 if column == 0 else 5, 0 if column == 2 else 5))
        strip = ctk.CTkFrame(card, height=3, fg_color=CONSOLE["MUTED"], corner_radius=0)
        strip.pack(fill="x")
        bind_theme(strip, "strip", tone="unknown", console=True)
        label(card, title, bold=True, console=True).pack(fill="x", padx=14, pady=(12, 8))
        value = label(card, "待确认", size=18, bold=True, wraplength=128,
                      justify="left", console=True)
        value.pack(fill="x", padx=14)
        fact = label(card, "", muted=True, size=12, wraplength=128, justify="left",
                     console=True)
        fact.pack(fill="x", padx=14, pady=(6, 14))
        refs["status_cards"][role] = {"value": value, "fact": fact, "strip": strip}

    actions = ctk.CTkFrame(body, fg_color="transparent")
    actions.pack(fill="x", padx=24, pady=20)
    for title, cmd, primary in (("立即检查", app.on_check_connection, True),
                                 ("尝试恢复", app.on_reconnect, False),
                                 ("开启在线守护", app.on_toggle_online_guard, False)):
        b = button(actions, title, cmd, primary=primary, console=True, width=130)
        b.pack(side="left", padx=(0, 10))
        refs["buttons"].append(b)
    note = panel(body, console=True, flat=True)
    note.pack(fill="x", padx=24, pady=(0, 24))
    label(note, "关于 UU 在线状态", bold=True, console=True).pack(
        fill="x", padx=18, pady=(14, 4))
    label(note, "这里显示助手的恢复状态；是否能从另一台设备远程连接，仍需在 UU 客户端确认。",
          muted=True, wraplength=520, justify="left", console=True).pack(
        fill="x", padx=18, pady=(0, 14))


def render_console(refs, state, app):
    """Map the read-only UiState onto console widgets. Never invents a value."""
    from campus_ui_state import OVERALL_TONE
    tone = OVERALL_TONE.get(state.overall, "unknown")
    _tone(refs["light"], tone, console=True)
    _tone(refs["signal_word"], tone, console=True)
    refs["signal_word"].configure(text=SIGNAL_WORDS.get(state.overall, "UNKNOWN"))
    refs["overall"].configure(text=state.overall_label)
    refs["headline"].configure(text=state.headline or "等待首次检测结果")
    monitor = "  ·  ".join(v for v in (state.monitor_label, state.mode_label) if v)
    refs["monitor"].configure(text=f"●  {monitor or '等待监测'}")
    app._render_update_banner(refs)
    data = {}
    for role in ("ethernet", "wifi"):
        card = state.card(role)
        slot = refs["status_cards"][role]
        caption, card_tone = path_summary(card)
        _tone(slot["value"], card_tone, console=True)
        slot["value"].configure(text=caption)
        slot["fact"].configure(text=(f"地址 {card.field('源地址')}\n最近验证 {card.field('最近验证')}"
                                     if card else "尚无检测数据"))
        bind_theme(slot["strip"], "strip", tone=card_tone, console=True)
        slot["strip"].configure(fg_color=CONSOLE["TONES"].get(card_tone, CONSOLE["MUTED"]))
        prefix = ""
        if state.active_path == role:
            prefix = "主用"
        elif state.standby_path == role:
            prefix = "备用"
        elif card and card.assignment == "PRIMARY":
            prefix = "主用"
        elif card and card.assignment == "STANDBY":
            prefix = "备用"
        data[role] = {"tone": card_tone, "label": caption, "role": prefix}
    uu = state.uu
    slot = refs["status_cards"]["uu"]
    uu_tone = uu.tone if uu.enabled else "unknown"
    _tone(slot["value"], uu_tone, console=True)
    slot["value"].configure(text=uu.label if uu.enabled else "未启用")
    slot["fact"].configure(text=uu.detail or ("恢复结果待确认" if uu.enabled
                                              else "可在设置中开启自动恢复"))
    bind_theme(slot["strip"], "strip", tone=uu_tone, console=True)
    slot["strip"].configure(fg_color=CONSOLE["TONES"].get(uu_tone, CONSOLE["MUTED"]))
    data["uu"] = {"tone": uu_tone, "label": uu.label if uu.enabled else "未启用"}
    topo = refs["topology"]
    topo._campus_topology = data
    draw_topology(topo, CONSOLE, data)


def build_network_page(app, body, refs):
    """Editorial network page: numbered blocks separated by hairlines."""
    numbered(body, 1, "联网保护")
    block = section(body)
    block.pack(fill="x", padx=24, pady=(0, 10))
    refs["setup_status"] = label(block, "正在检查", muted=True,
                                 wraplength=520, justify="left")
    refs["setup_status"].pack(fill="x", pady=(0, 8))
    button(block, "重新检查", app.on_check_connection, primary=True).pack(anchor="w")
    divider(body).pack(fill="x", padx=24, pady=(8, 0))
    for index, (role, title) in enumerate((("ethernet", "以太网"),
                                           ("wifi", "SNNU Wi-Fi")), start=2):
        numbered(body, index, title)
        block = section(body)
        block.pack(fill="x", padx=24, pady=(0, 10))
        rows = {}
        for name in ("状态", "Internet", "最近验证"):
            row = section(block)
            row.pack(fill="x", pady=1)
            caption = {"Portal": "校园认证", "Internet": "上网验证",
                       "ifIndex": "接口编号"}.get(name, name)
            label(row, caption, muted=True, width=86).pack(side="left")
            rows[name] = label(row, "—", wraplength=380, justify="left")
            rows[name].pack(side="left", fill="x", expand=True)
        button(block, "验证这条链路", lambda r=role: app.on_verify_now(r)).pack(
            anchor="w", pady=(8, 0))
        refs[role] = rows
        divider(body).pack(fill="x", padx=24, pady=(8, 0))
    refs["manual"] = label(body, "尚未手动验证", muted=True, wraplength=520, justify="left")
    refs["manual"].pack(fill="x", padx=24, pady=(14, 24))


def build_recovery_page(app, body, refs):
    numbered(body, 1, "守护状态")
    block = section(body)
    block.pack(fill="x", padx=24, pady=(0, 10))
    refs["uu"] = label(block, "", wraplength=520, justify="left")
    refs["uu"].pack(fill="x", pady=(0, 4))
    refs["protection"] = label(block, "", muted=True, wraplength=520, justify="left")
    refs["protection"].pack(fill="x")
    refs["readiness"] = label(block, "正在自动检查系统与 UU…", muted=True,
                              wraplength=520, justify="left")
    refs["readiness"].pack(fill="x", pady=(8, 0))
    refs["sleep_action"] = button(
        block, "打开电源设置", app.on_open_power_settings)
    refs["sleep_action"].pack_forget()
    divider(body).pack(fill="x", padx=24, pady=(10, 0))
    refs["steps"] = {}
    for index, (key, title) in enumerate((("detect", "检查连接"),
                                          ("internet", "保持网络在线"),
                                          ("uu", "保持 UU 在线")), start=2):
        row = section(body)
        row.pack(fill="x", padx=24, pady=(10, 4))
        label(row, f"{index:02d}", size=12, muted=True, display=True).pack(
            side="left", padx=(0, 12))
        label(row, title, size=16, bold=True).pack(side="left")
        step = label(row, "待执行", muted=True)
        step.pack(side="right")
        refs["steps"][key] = step
        divider(body).pack(fill="x", padx=24)
    refs["progress"] = bind_theme(
        ctk.CTkProgressBar(body, height=3, progress_color=tokens()["ACCENT"],
                           fg_color=tokens()["BORDER"]), "progress")
    refs["progress"].pack(fill="x", padx=24, pady=(14, 16))
    actions = section(body)
    actions.pack(fill="x", padx=24, pady=(0, 24))
    button(actions, "重启 UU 远程", app.on_restart_uu_remote).pack(side="left", padx=(0, 10))
    refs["recovery_guard_button"] = button(actions, "开启在线守护",
                                           app.on_toggle_online_guard)
    refs["recovery_guard_button"].pack(side="left")


def build_activity_page(app, body, refs):
    numbered(body, 1, "最近事件")
    block = section(body)
    block.pack(fill="x", padx=24, pady=(0, 8))
    refs["timeline"] = label(block, "尚无网络故障事件记录", wraplength=520,
                             justify="left")
    refs["timeline"].pack(fill="x", pady=(0, 8))
    label(block, "断网、认证恢复、出口切换与 UU 处理会按时间记录在这里，最新的在前。",
          muted=True, size=12, wraplength=520, justify="left").pack(fill="x")
    divider(body).pack(fill="x", padx=24, pady=(14, 0))
    button(body, "查看完整日志", app.on_open_log).pack(anchor="w", padx=24, pady=(18, 24))


def build_settings_page(app, body, refs):
    entries = (
        ("保持在线", "在线守护的唯一开关；开启后自动联网、恢复 UU，联网软件异常时自动处理并保留原设置。",
         "开启在线守护", app.on_toggle_online_guard),
        ("开机运行", "电脑登录后自动开始守护。", "设置开机运行", app.on_toggle_autostart),
        ("校园网账号", "填写校园网账号和密码，供断网后自动登录使用。", "设置账号",
         app.on_update_credentials),
        ("软件更新", "检查新版本，在程序内下载并更新。", "检查更新",
         app.on_check_update_now),
    )
    for index, (title, description, action, cmd) in enumerate(entries, start=1):
        numbered(body, index, title)
        block = section(body)
        block.pack(fill="x", padx=24, pady=(0, 12))
        label(block, description, muted=True, wraplength=520).pack(fill="x", pady=(0, 10))
        control = button(block, action, cmd)
        control.pack(anchor="w")
        if title == "保持在线":
            refs["guard_button"] = control
        if title == "开机运行":
            refs["autostart_button"] = control
        divider(body).pack(fill="x", padx=24, pady=(6, 0))
    advanced = section(body)

    def toggle_advanced():
        panel_widget = refs["advanced_section"]
        toggle = refs["advanced_toggle"]
        if panel_widget.winfo_manager():
            panel_widget.pack_forget()
            toggle.configure(text="显示高级选项")
        else:
            panel_widget.pack(fill="x", padx=24, pady=(0, 20))
            toggle.configure(text="收起高级选项")

    refs["advanced_section"] = advanced
    advanced_toggle = button(body, "显示高级选项", toggle_advanced)
    refs["advanced_toggle"] = advanced_toggle
    advanced_toggle.pack(anchor="w", padx=24, pady=(16, 12))
    label(advanced, "供进一步调整或技术排查使用，日常使用无需修改。", muted=True,
          wraplength=520).pack(fill="x", pady=(0, 10))
    button(advanced, "详细设置", app.on_settings).pack(anchor="w", pady=(0, 10))
    button(advanced, "高级网络诊断", app._open_proxy_compat).pack(anchor="w", pady=(0, 14))


def build_dashboard(app):
    configure_theme(app.root)
    palette = tokens()
    win = ctk.CTkToplevel(app.root)
    app.dashboard_win = win
    win.title("校园网自动重连助手")
    win.configure(fg_color=palette["BG"])
    bind_theme(win, "window")
    # The dashboard uses a compact, app-owned chrome so the content starts at
    # the top of the work area and never inherits the oversized Windows frame.
    win.overrideredirect(True)
    win._campus_maximized = False
    fit_window(win, 980, 700, minimum=(820, 560))
    win.update_idletasks()
    keep_in_task_switcher(win)
    win.protocol("WM_DELETE_WINDOW", app._close_dashboard)
    win.grid_rowconfigure(1, weight=1)
    win.grid_columnconfigure(1, weight=1)

    titlebar = ctk.CTkFrame(win, height=42, fg_color=palette["SIDE"], corner_radius=0)
    titlebar.grid(row=0, column=0, columnspan=2, sticky="ew")
    titlebar.grid_columnconfigure(0, weight=1)
    bind_theme(titlebar, "side")
    title_label = label(titlebar, "校园网自动重连助手", size=13, bold=True)
    title_label.grid(row=0, column=0, sticky="w", padx=16, pady=8)

    drag = {"x": 0, "y": 0}
    def begin_drag(event):
        drag["x"], drag["y"] = event.x_root, event.y_root
    def move_drag(event):
        if win._campus_maximized:
            return
        x = win.winfo_x() + event.x_root - drag["x"]
        y = win.winfo_y() + event.y_root - drag["y"]
        drag["x"], drag["y"] = event.x_root, event.y_root
        win.geometry(f"+{x}+{y}")
    def toggle_maximize(event=None):
        if win._campus_maximized:
            win._campus_maximized = False
            fit_window(win, 980, 700, minimum=(820, 560))
            win.geometry("%sx%s+%s+%s" % (win._campus_restore[0], win._campus_restore[1],
                                          win._campus_restore[2], win._campus_restore[3]))
            max_button.configure(text="□")
            return
        win._campus_restore = (win.winfo_width(), win.winfo_height(),
                               win.winfo_x(), win.winfo_y())
        win._campus_maximized = True
        win.geometry("%sx%s+0+0" % (win.winfo_screenwidth(), win.winfo_screenheight()))
        max_button.configure(text="❐")
    for widget in (titlebar, title_label):
        widget.bind("<ButtonPress-1>", begin_drag)
        widget.bind("<B1-Motion>", move_drag)
        widget.bind("<Double-Button-1>", toggle_maximize)
    button(titlebar, "—", win.iconify, width=34, height=28).grid(row=0, column=1, padx=(0, 2))
    max_button = button(titlebar, "□", toggle_maximize, width=34, height=28)
    max_button.grid(row=0, column=2, padx=2)
    button(titlebar, "×", app._close_dashboard, width=34, height=28).grid(row=0, column=3, padx=(2, 8))

    side = ctk.CTkFrame(win, width=178, fg_color=palette["SIDE"], corner_radius=0)
    side.grid(row=1, column=0, sticky="nsew")
    side.grid_propagate(False)
    side.grid_columnconfigure(0, weight=1)
    side.grid_rowconfigure(9, weight=1)
    bind_theme(side, "side")
    label(side, "校园网助手", size=19, bold=True).grid(row=0, padx=20, pady=(28, 2), sticky="w")
    from campus_core import __version__
    label(side, f"版本 {__version__}", size=12, muted=True).grid(
        row=1, padx=20, pady=(0, 28), sticky="w")
    content = ctk.CTkFrame(win, fg_color=palette["BG"], corner_radius=0)
    content.grid(row=1, column=1, sticky="nsew")
    bind_theme(content, "surface")
    refs = {"pages": {}, "navigation": {}, "buttons": []}

    def navigate(key):
        active_palette = tokens()
        for name, page in refs["pages"].items():
            if name == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
            nav = refs["navigation"][name]
            selected = name == key
            nav.configure(fg_color=active_palette["SURFACE"] if selected else "transparent",
                          text_color=active_palette["ACCENT"] if selected else active_palette["TEXT"])
            bind_theme(nav, "nav", selected=selected)
        refs["selected_page"] = key

    def page(key, title, subtitle, *, console=False, editorial=False, index=0):
        colors = CONSOLE if console else tokens()
        if key == "overview":
            # Keep the first screen's status console fully visible without a
            # scrollbar; detail pages retain scrolling when content is long.
            body = ctk.CTkFrame(content, fg_color=colors["BG"], corner_radius=0)
        else:
            body = ctk.CTkScrollableFrame(
                content, fg_color=colors["BG"], corner_radius=0,
                scrollbar_fg_color=colors["SCROLL_TRACK"],
                scrollbar_button_color=colors["SCROLL_THUMB"],
                scrollbar_button_hover_color=colors["SCROLL_HOVER"])
        if key == "overview":
            # Older render tests use CTkScrollableFrame's internal parent
            # handle to check page visibility.  Keep that small compatibility
            # surface while the overview itself remains a plain frame.
            body._parent_frame = body
            body._parent_canvas = _OverviewCanvasProxy(body)
            body._scrollbar = _OverviewCanvasProxy(body, scrollbar=True, console=console)
        bind_theme(body, "surface", console=console)
        refs["pages"][key] = body
        if editorial:
            eyebrow = section(body)
            eyebrow.pack(fill="x", padx=24, pady=(24, 2))
            label(eyebrow, f"NO. {index:02d}", size=11, muted=True, display=True).pack(
                side="left")
            divider(eyebrow).pack(side="left", fill="x", expand=True, padx=12)
            label(body, title, size=28, bold=True, display=True).pack(
                fill="x", padx=24, pady=(8, 2))
        else:
            label(body, title, size=24, bold=True, console=console).pack(
                fill="x", padx=24, pady=(24, 4))
        label(body, subtitle, muted=True, wraplength=520, justify="left",
              console=console).pack(fill="x", padx=24, pady=(0, 20))
        return body

    for index, (key, title) in enumerate((("overview", "概览"), ("network", "连接状态"),
                                         ("recovery", "在线守护"), ("activity", "活动记录"),
                                         ("settings", "设置")), start=2):
        nav = ctk.CTkButton(side, text=title, command=lambda k=key: navigate(k),
                            font=font(), anchor="w", height=42, corner_radius=5,
                            fg_color="transparent", hover_color=palette["NAV_HOVER"],
                            text_color=palette["TEXT"])
        bind_theme(nav, "nav", selected=False)
        nav.grid(row=index, column=0, sticky="ew", padx=12, pady=3)
        refs["navigation"][key] = nav

    theme_switcher(side, palette)
    refs["set_theme"] = switch_theme

    button(side, "打开日志", app.on_open_log, width=140).grid(row=10, padx=16, pady=8)
    label(side, "关闭窗口后仍在托盘运行", size=11, muted=True).grid(row=11, padx=12, pady=(0, 18))

    overview = page("overview", "概览", "查看网络连接和远程恢复状态。", console=True)
    build_console(app, overview, refs)

    network = page("network", "连接状态", "助手会在后台检查并恢复连接，无需手动配置联网软件。",
                   editorial=True, index=2)
    build_network_page(app, network, refs)
    recovery = page("recovery", "在线守护", "自动保持网络连接，恢复 UU，必要时恢复联网软件。",
                    editorial=True, index=3)
    build_recovery_page(app, recovery, refs)
    activity = page("activity", "活动记录", "最近的网络中断、恢复和 UU 处理记录，最新的在前。",
                    editorial=True, index=4)
    build_activity_page(app, activity, refs)
    settings = page("settings", "设置", "配置保存在本机，更新程序时沿用现有设置。",
                    editorial=True, index=5)
    build_settings_page(app, settings, refs)
    refs["navigate"] = navigate
    app._dash_ctk_widgets = refs
    app._dash_widgets = None
    navigate("overview")
    app._refresh_dashboard()


def render_dashboard(app, state):
    refs = app._dash_ctk_widgets
    from campus_ui_state import STEP_STATUS_LABEL, STEP_TONE
    render_console(refs, state, app)
    uu = state.uu
    for role in ("ethernet", "wifi"):
        card = state.card(role)
        for name, widget in refs[role].items():
            text = card.field(name) if card else "—"
            if name == "角色":
                text = {"PRIMARY": "主用链路", "STANDBY": "备用链路"}.get(text, text)
            widget.configure(text=text)
    for step in state.pipeline:
        if step.key in refs["steps"]:
            refs["steps"][step.key].configure(text=STEP_STATUS_LABEL.get(step.status, "待确认"))
            _tone(refs["steps"][step.key], STEP_TONE.get(step.status, "unknown"))
    displayed = [s for s in state.pipeline if s.key in refs["steps"]]
    refs["progress"].set(sum(s.status == "done" for s in displayed) / max(1, len(displayed)))
    refs["uu"].configure(text=f"UU 远程自动恢复：{uu.label}" + (f"（{uu.detail}）" if uu.detail else ""))
    cfg = app.get_config()
    guard_text = "关闭在线守护" if cfg.get("lifeline_enabled") else "开启在线守护"
    refs["guard_button"].configure(text=guard_text)
    refs["recovery_guard_button"].configure(text=guard_text)
    refs["buttons"][-1].configure(text=guard_text)
    startup = getattr(app, "_autostart_enabled", None)
    refs["autostart_button"].configure(text="关闭开机运行" if startup else "开启开机运行")
    refs["setup_status"].configure(text=getattr(app, "_automatic_setup_message", "等待后台检查"))
    # The guard switch lives on the settings page; do not repeat its state here.
    report = getattr(app, "readiness_report", None)
    reboot = "已启用" if cfg.get("lifeline_reboot_enabled") else "未启用"
    if report is not None:
        logon = next((item for item in report.items if item.key == "auto_logon"), None)
        autostart = next((item for item in report.items if item.key == "tray_autostart"), None)
        if (logon is not None and logon.status != "PASS") or \
                (autostart is not None and autostart.status != "PASS"):
            reboot = "需完成 Windows 登录后自启设置"
    refs["protection"].configure(text=f"电脑重启恢复：{reboot}")
    if report is None:
        refs["readiness"].configure(text="正在自动检查系统与 UU…")
        _set_packed_visibility(refs["sleep_action"], False)
    else:
        lines = ["自动检查：%s" % ("全部通过" if report.automatic_ok else
                                  "有项目需要处理")]
        for item in report.items:
            status = {"PASS": "已通过", "ACTION": "需要处理",
                      "CHECKING": "检查中", "MANUAL": "无法自动确认"}.get(
                          item.status, "待确认")
            lines.append("%s：%s（%s）" % (item.label, status, item.detail))
        refs["readiness"].configure(text="\n".join(lines))
        _set_packed_visibility(refs["sleep_action"], sleep_action_required(report),
                               anchor="w", pady=(8, 0))
    lines = app._dashboard_timeline_lines(limit=10)
    refs["timeline"].configure(text="\n\n".join(map(translated_event, lines)) if lines
                              else "尚无网络故障事件记录")
    manual = [line for role in ("ethernet", "wifi") if state.card(role)
              for line in state.card(role).manual]
    refs["manual"].configure(text="\n".join(manual) if manual
                             else "尚未手动验证，可点击上方按钮查看逐站结果。")
