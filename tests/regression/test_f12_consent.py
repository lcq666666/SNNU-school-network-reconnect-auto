# -*- coding: utf-8 -*-
"""F12 回归用例：过期授权覆盖新拒绝 / 无关设置保存隐式改偏好（对应 T23 / T24）。

依据《审查方案》F12：
- 多次 consent 共享 ``_ask_event/_ask_result``，旧弹窗结果可写回新配置（T23）；
- ``bool(None)`` 使「尚未询问」变成「拒绝」，且任意设置保存都会调用
  ``set_autostart()``（T24）。

**本文件在修复前必须失败**（阶段 2 修复后转绿）；不写真实注册表。
"""
from __future__ import annotations

import logging

import pytest

import campus_reconnect_tray as tray

pytestmark = pytest.mark.regression

LOG = logging.getLogger("campus")


def _app(monkeypatch):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.icon = None
    return app


# --------------------------------------------------------------------------- #
# T23：过期 consent 结果不得覆盖新的拒绝
# --------------------------------------------------------------------------- #
def test_t23_stale_consent_must_not_override_new_rejection(monkeypatch):
    """T23：授权弹窗迟到返回“同意”，不得覆盖用户随后做出的“拒绝”。"""
    monkeypatch.setattr(tray, "set_system_proxy", lambda enabled: True)
    monkeypatch.setattr(tray, "save_config", lambda cfg: True)

    app = _app(monkeypatch)
    app.config["clash_auto_proxy"] = None  # 尚未询问

    def stale_ask():
        # 模拟：弹窗尚未返回时，用户已通过菜单显式“拒绝”
        app.config["clash_auto_proxy"] = False
        return True  # 旧弹窗迟到地返回“同意”

    monkeypatch.setattr(app, "_ask_proxy_consent", stale_ask)
    app._after_repair(True, "detail", 1.0)

    assert app.config["clash_auto_proxy"] is False, \
        "过期 consent 覆盖了新的拒绝，授权=%r" % app.config["clash_auto_proxy"]


# --------------------------------------------------------------------------- #
# T24：保存无关设置不得改变授权与自启
# --------------------------------------------------------------------------- #
def test_t24_saving_unrelated_settings_preserves_consent_and_autostart(monkeypatch):
    """T24：仅改 interval 后保存，不得把未知授权 bool 化，也不得重写自启项。"""
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk

    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError:
        pytest.skip("无可用显示环境（Tk 不可用）")

    try:
        app = _app(monkeypatch)
        app.root = root
        app.config["clash_auto_proxy"] = None  # 未知（尚未询问）
        app.config["interval"] = 15

        monkeypatch.setattr(tray, "get_autostart", lambda: False)
        autostart_calls = []
        monkeypatch.setattr(tray, "set_autostart",
                            lambda enabled: autostart_calls.append(enabled) or True)
        monkeypatch.setattr(tray, "save_config", lambda cfg: True)
        monkeypatch.setattr(app, "notify", lambda *a, **k: None)

        app._open_settings()
        win = app.settings_win
        assert win is not None
        win.withdraw()

        def find_button(widget, text):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button) and child.cget("text") == text:
                    return child
                found = find_button(child, text)
                if found is not None:
                    return found
            return None

        button = find_button(win, "保存")
        assert button is not None, "未找到保存按钮"
        button.invoke()

        assert app.config["clash_auto_proxy"] is None, \
            "保存无关设置把未知授权改成了 %r" % app.config["clash_auto_proxy"]
        assert autostart_calls == [], \
            "保存无关设置重写了自启项，调用参数=%r" % autostart_calls
    finally:
        root.destroy()
