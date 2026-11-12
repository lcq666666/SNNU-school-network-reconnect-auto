"""Render synthetic desktop states for visual review, without host/network access."""
from __future__ import annotations

import argparse
import gc
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageGrab

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import campus_reconnect_tray as tray  # noqa: E402
import campus_proxy_ui as proxy  # noqa: E402
from campus_desktop_ui import ctk  # noqa: E402
from campus_ui_state import PathCard, StepView, UiState, UuView  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", type=float, default=1)
    parser.add_argument("--width", type=int, default=980)
    parser.add_argument("--height", type=int, default=700)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    gc.disable()
    import tkinter as tk
    ctk.ScalingTracker.activate_high_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    dpi = ctk.ScalingTracker.get_window_dpi_scaling(root)
    ctk.set_widget_scaling(args.scale / dpi)
    ctk.set_window_scaling(args.scale / dpi)
    app = tray.TrayApp(logging.getLogger("preview"), config=dict(tray.DEFAULT_CONFIG, lifeline_enabled=True))
    app._autostart_enabled = True
    app.root = root
    app._open_dashboard()
    app.dashboard_win.after_cancel(app._dash_refresh_id)
    app._dash_refresh_id = None
    fields = (("源地址", "192.0.2.10"), ("最近验证", "8 秒前"), ("状态", "已验证"),
              ("角色", "PRIMARY"), ("Internet", "已验证"))
    state = UiState(overall="HEALTHY", headline="网络连接正常，正在持续守护。",
                    ethernet=PathCard(role="ethernet", title="以太网", assignment="PRIMARY",
                                      tone="ok", internet_tone="ok", state_label="已验证", verified=True, fields=fields),
                    wifi=PathCard(role="wifi", title="SNNU Wi-Fi", assignment="STANDBY",
                                  tone="warn", internet_tone="warn", state_label="需要认证", verified=False,
                                  fields=(("源地址", "192.0.2.20"), ("Portal", "需要认证"))),
                    uu=UuView(enabled=True, label="已建立连接", tone="ok", last_ok=True),
                    monitor_label="正在监测", mode_label="在线守护",
                    pipeline=(StepView("detect", "检测", "done"), StepView("portal", "认证", "pending")))
    app.ui_state = lambda: state
    app._render_dashboard_ctk(state)
    win = app.dashboard_win
    win.geometry(f"{args.width}x{args.height}+40+40")
    win.deiconify()
    win.attributes("-topmost", True)

    def capture(window, name):
        window.lift()
        window.update()
        if sys.platform == "win32":
            # Render our own client area, including when the desktop is locked.
            # Screen grabs can otherwise silently capture the lock screen.
            import ctypes
            from ctypes import wintypes
            user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
            user32.GetDC.argtypes = [wintypes.HWND]
            user32.GetDC.restype = wintypes.HDC
            gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
            gdi32.CreateCompatibleDC.restype = wintypes.HDC
            gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
            gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
            gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
            gdi32.SelectObject.restype = wintypes.HANDLE
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
            gdi32.GetBitmapBits.argtypes = [wintypes.HBITMAP, wintypes.LONG, ctypes.c_void_p]
            gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
            gdi32.DeleteDC.argtypes = [wintypes.HDC]
            user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            hwnd = user32.GetAncestor(window.winfo_id(), 2)
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            width, height = rect.right, rect.bottom
            dc = user32.GetDC(hwnd)
            memory = gdi32.CreateCompatibleDC(dc)
            bitmap = gdi32.CreateCompatibleBitmap(dc, width, height)
            previous = gdi32.SelectObject(memory, bitmap)
            try:
                if not user32.PrintWindow(hwnd, memory, 3):
                    raise RuntimeError("Synthetic window could not be rendered")
                raw = ctypes.create_string_buffer(width * height * 4)
                gdi32.GetBitmapBits(bitmap, len(raw), raw)
                result = Image.frombuffer("RGB", (width, height), raw.raw, "raw", "BGRX", 0, 1)
            finally:
                gdi32.SelectObject(memory, previous)
                gdi32.DeleteObject(bitmap)
                gdi32.DeleteDC(memory)
                user32.ReleaseDC(hwnd, dc)
        else:
            bbox = (window.winfo_rootx(), window.winfo_rooty(),
                    window.winfo_rootx() + window.winfo_width(), window.winfo_rooty() + window.winfo_height())
            result = ImageGrab.grab(bbox=bbox)
        result.save(args.output / f"{name}-{args.scale:g}x.png")

    pages = iter(("overview", "network", "recovery", "activity", "settings"))
    def next_page():
        try:
            key = next(pages)
        except StopIteration:
            show_proxy()
            return
        app._dash_ctk_widgets["navigate"](key)
        root.after(150, lambda: (capture(win, key), next_page()))

    def show_proxy():
        report = "系统代理：已开启，保留本机端口配置\n系统绕过：校园地址已配置\n当前生成配置：校园规则不完整/尚未重载\nTUN：配置为关闭"
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target
            def start(self):
                self.target()
        with patch.object(proxy, "windows_compatibility", return_value=SimpleNamespace(inspect=lambda roots: report)), \
                patch.object(proxy, "discover_roots", return_value=[]), \
                patch.object(proxy, "runtime_summary", return_value="未发现活动代理虚拟网卡"), \
                patch.object(proxy, "threading", SimpleNamespace(Thread=InlineThread)):
            app._ui_post = lambda f: f()
            proxy.open_dialog(app)
        dialog = app.proxy_compat_win
        dialog.geometry("820x480+60+60")
        dialog.deiconify()
        dialog.attributes("-topmost", True)
        root.after(250, lambda: (capture(dialog, "proxy"), root.destroy()))
    root.after(300, next_page)
    root.mainloop()
    print("Synthetic previews saved to", args.output)


if __name__ == "__main__":
    main()
