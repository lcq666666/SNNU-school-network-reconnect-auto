"""V0.6.2 自动更新提醒测试：版本比较 / GitHub 查询 / 去重 / 横幅。

零真实网络、零真实注册表、零真实凭据、零真实浏览器、零真实 sleep。
GitHub 查询一律注入假 session；横幅渲染复用 V0.6.1 的 xvfb 真窗口模式。
"""

import logging

import pytest

import campus_reconnect_tray as tray
import campus_update as upd
from campus_core import __version__ as CURRENT_VERSION
from campus_update import UpdateInfo

LOG = logging.getLogger("campus")


def _app(**cfg):
    app = tray.TrayApp(LOG, config=dict(tray.DEFAULT_CONFIG))
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(cfg)
    return app


class _Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._payload


class _Session:
    """假 requests session：记录调用，可抛异常、可返回指定响应。"""

    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.resp


def _current_tag():
    """当前版本对应的标签（例如 v0.6.1），直接取自产品版本号。"""
    return "v" + CURRENT_VERSION


def _newer_tag():
    """比当前版本高一个补丁号的假最新版本标签（当前 0.6.1 → v0.6.2）。

    测试不得把版本号写死：版本号升级后假最新版本仍比当前新，
    同类失败不再重现。
    """
    parts = upd.parse_version(CURRENT_VERSION)
    return "v%d.%d.%d" % (parts[0], parts[1], parts[2] + 1)


def _release(tag=None):
    """假 GitHub 最新 Release；默认标签比当前版本高一补丁号。"""
    if tag is None:
        tag = _newer_tag()
    return _Resp(200, {
        "tag_name": tag,
        "name": tag + " 融合仪表盘",
        "html_url": "https://github.com/lcq666666/"
                    "SNNU-school-network-reconnect-auto/releases/tag/" + tag,
        "published_at": "2026-11-11T06:00:00Z",
    })


def _info(tag=None):
    """假 UpdateInfo；默认标签比当前版本高一补丁号。"""
    if tag is None:
        tag = _newer_tag()
    return UpdateInfo(tag=tag, name=tag,
                      html_url="https://example.invalid/releases/tag/" + tag)


def test_newer_tag_follows_current_version():
    """回归：假最新版本必须始终比当前版本新，否则本文件一多半测试失效。"""
    assert upd.is_newer(_newer_tag(), CURRENT_VERSION) is True
    assert upd.is_newer(_current_tag(), CURRENT_VERSION) is False


# ---------- 1. 版本解析与比较（纯函数） ----------

def test_parse_version_variants():
    assert upd.parse_version("v0.6.0") == (0, 6, 0)
    assert upd.parse_version("0.6.0") == (0, 6, 0)
    assert upd.parse_version("V1.2.3") == (1, 2, 3)
    assert upd.parse_version("0.6") == (0, 6, 0)
    assert upd.parse_version("  v0.6.1  ") == (0, 6, 1)
    assert upd.parse_version("10.0.0") == (10, 0, 0)


def test_parse_version_illegal_is_none():
    for bad in ("", "v", "1.2.3.4", "1.x", "0.6.0-beta", "0.6.0+build",
                ".6.0", "0..0", None, 123, "latest"):
        assert upd.parse_version(bad) is None, bad


def test_is_newer_matrix():
    assert upd.is_newer("v0.6.1", "0.6.0") is True
    assert upd.is_newer("v0.7.0", "0.6.9") is True
    assert upd.is_newer("v1.0.0", "0.9.9") is True
    assert upd.is_newer("v0.6.0", "0.6.0") is False
    assert upd.is_newer("v0.6.0", "0.6.1") is False
    assert upd.is_newer("v0.5.9", "0.6.0") is False
    assert upd.is_newer("bogus", "0.6.0") is False
    assert upd.is_newer("v0.6.1", "bogus") is False


def test_should_notify_only_once_per_tag():
    assert upd.should_notify(_info("v0.6.1"), "0.6.0", None) is True
    assert upd.should_notify(_info("v0.6.1"), "0.6.0", "v0.6.1") is False
    assert upd.should_notify(_info("v0.6.1"), "0.6.0", "v0.6.0") is True
    assert upd.should_notify(_info("v0.6.0"), "0.6.0", None) is False
    assert upd.should_notify(_info("v0.5.0"), "0.6.0", None) is False
    assert upd.should_notify(None, "0.6.0", None) is False


# ---------- 2. check_latest：成功 / 404 / 超时 / 缺 tag ----------

def test_check_latest_success():
    tag = _newer_tag()
    session = _Session(_release(tag))
    info, reason = upd.check_latest(session)
    assert reason == ""
    assert info.tag == tag
    assert info.name == tag + " 融合仪表盘"
    assert info.html_url.endswith("/releases/tag/" + tag)
    assert info.published_at == "2026-11-11T06:00:00Z"
    url, kwargs = session.calls[0]
    assert "api.github.com" in url
    assert "releases/latest" in url
    assert kwargs["timeout"] == 10


def test_check_latest_404_is_none_with_reason():
    info, reason = upd.check_latest(_Session(_Resp(404, {"message": "Not Found"})))
    assert info is None
    assert "404" in reason


def test_check_latest_timeout_is_none_with_reason():
    import requests

    info, reason = upd.check_latest(
        _Session(exc=requests.exceptions.ConnectTimeout()))
    assert info is None
    assert reason  # 只断言给了原因，不依赖异常类名拼写


def test_check_latest_missing_tag_is_none():
    info, reason = upd.check_latest(_Session(_Resp(200, {"name": "x"})))
    assert info is None
    assert "tag_name" in reason


def test_check_latest_unparsable_tag_is_none():
    info, reason = upd.check_latest(_Session(_release("nightly")))
    assert info is None
    assert "nightly" in reason


def test_check_latest_bad_json_is_none():
    info, reason = upd.check_latest(_Session(_Resp(200, bad_json=True)))
    assert info is None
    assert reason


# ---------- 3. 配置键：非法回退 / 合法保留 ----------

def test_read_update_keys_invalid_falls_back():
    keys = tray._read_update_keys(
        '{"update_check_enabled": "yes", "update_notified_tag": 123, '
        '"update_dismissed_tag": ""}')
    assert keys == {"update_check_enabled": True,
                    "update_notified_tag": None,
                    "update_dismissed_tag": None}


def test_read_update_keys_valid_kept():
    keys = tray._read_update_keys(
        '{"update_check_enabled": false, "update_notified_tag": "v0.6.1", '
        '"update_dismissed_tag": "v0.6.0"}')
    assert keys == {"update_check_enabled": False,
                    "update_notified_tag": "v0.6.1",
                    "update_dismissed_tag": "v0.6.0"}


def test_default_config_update_keys():
    assert tray.DEFAULT_CONFIG["update_check_enabled"] is True
    assert tray.DEFAULT_CONFIG["update_notified_tag"] is None
    assert tray.DEFAULT_CONFIG["update_dismissed_tag"] is None


# ---------- 4. 去重与静默失败（托盘级，save_config 打桩） ----------

def _patched_app(monkeypatch, **cfg):
    app = _app(**cfg)
    saved = []
    monkeypatch.setattr(tray, "save_config",
                        lambda cfg_dict: saved.append(dict(cfg_dict)) or True)
    notified = []
    app.notify = lambda title, body: notified.append((title, body))
    return app, saved, notified


def test_on_update_checked_notifies_once_per_tag(monkeypatch):
    newer = _newer_tag()
    app, saved, notified = _patched_app(monkeypatch)
    app._on_update_checked(_info(newer), "")
    assert len(notified) == 1
    assert newer in notified[0][0]
    assert app.config["update_notified_tag"] == newer
    assert saved and saved[-1]["update_notified_tag"] == newer
    # 同一版本第二次检查：只记 _update_info，不再弹气泡。
    app._on_update_checked(_info(newer), "")
    assert len(notified) == 1


def test_on_update_checked_same_version_is_silent(monkeypatch):
    app, saved, notified = _patched_app(monkeypatch)
    app._on_update_checked(_info(_current_tag()), "")
    assert notified == []
    assert app.config["update_notified_tag"] is None
    assert app._update_info.tag == _current_tag()


def test_on_update_checked_failure_is_silent(monkeypatch):
    app, saved, notified = _patched_app(monkeypatch)
    app._on_update_checked(None, "请求失败：ConnectTimeout")
    assert notified == []
    assert app._update_info is None
    assert saved == []


def test_update_status_text_variants():
    app = _app()
    assert _current_tag() in app._update_status_text()
    assert "尚未检查到" in app._update_status_text()
    app._update_info = _info()
    assert "有更新可用" in app._update_status_text()
    app._update_info = _info(_current_tag())
    assert "已是最新" in app._update_status_text()


def test_maybe_check_update_schedule_gating(monkeypatch):
    app = _app()
    called = []
    app._check_update_async = lambda: called.append(True)
    app._update_next_check_at = 1000.0
    app._maybe_check_update(now=999.0)  # 未到点：不检查
    assert called == []
    app._maybe_check_update(now=1000.0)  # 到点：检查一次，并推后 6 小时
    assert len(called) == 1
    assert app._update_next_check_at == pytest.approx(1000.0 + 6 * 3600)
    app._maybe_check_update(now=1001.0)  # 刚查过：不重复
    assert len(called) == 1


def test_maybe_check_update_disabled_skips_request(monkeypatch):
    app = _app(update_check_enabled=False)
    called = []
    app._check_update_async = lambda: called.append(True)
    app._update_next_check_at = 0.0
    app._maybe_check_update(now=1000.0)
    assert called == []
    # 开关关闭仍推迟下次检查（下次打开开关时才恢复节奏）。
    assert app._update_next_check_at == pytest.approx(1000.0 + 6 * 3600)


def test_check_update_async_end_to_end_with_fake_session(monkeypatch):
    app, saved, notified = _patched_app(monkeypatch)
    app._update_session_factory = lambda: _Session(_release())
    app._check_update_async()
    assert app._update_thread is not None
    app._update_thread.join(timeout=5)
    assert not app._update_thread.is_alive(), "检查线程应在 5 秒内完成"
    assert app._update_info is not None
    assert app._update_info.tag == _newer_tag()
    assert len(notified) == 1


def test_on_check_update_now_triggers_async(monkeypatch):
    app = _app()
    called = []
    app._check_update_async = lambda: called.append(True)
    app.on_check_update_now()
    assert len(called) == 1
    assert app._update_next_check_at > 1000.0  # 下次定时推后 6 小时


# ---------- 5. 横幅：真窗口渲染（xvfb；无显示跳过） ----------

def test_banner_shows_for_newer_and_dismiss_hides(monkeypatch):
    from tests.unit.test_v06_ui_fusion import _open_app, _state

    monkeypatch.setattr(tray, "save_config", lambda cfg_dict: True)
    app, root, refs = _open_app()
    try:
        assert not refs["update_banner"].winfo_ismapped()
        app._update_info = _info()
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert refs["update_banner"].winfo_ismapped()
        assert _newer_tag() in refs["update_banner_label"].cget("text")
        # 点“以后再说”：横幅收起，且下次渲染也不再出现。
        app._on_update_dismiss()
        root.update_idletasks()
        assert not refs["update_banner"].winfo_ismapped()
        assert app.config["update_dismissed_tag"] == _newer_tag()
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert not refs["update_banner"].winfo_ismapped()
    finally:
        app._close_dashboard()
        root.destroy()


def test_banner_download_opens_local_progress_dialog(monkeypatch):
    from tests.unit.test_v06_ui_fusion import _open_app, _state

    monkeypatch.setattr(tray, "save_config", lambda cfg_dict: True)
    opened = []
    app, root, refs = _open_app()
    started = []
    class DownloadThread:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]
        def start(self):
            started.append(self.name)
    monkeypatch.setattr(tray.sys, "frozen", True, raising=False)
    from types import SimpleNamespace
    monkeypatch.setattr(tray, "threading", SimpleNamespace(**{**vars(tray.threading), "Thread": DownloadThread}))
    from concurrent.futures import ThreadPoolExecutor
    # Download's test double must not replace the standard library's threads.
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(lambda: "isolated").result(timeout=1) == "isolated"
    try:
        app._update_opener = lambda url: opened.append(url)
        app._update_info = _info()
        app._render_dashboard_ctk(_state())
        root.update_idletasks()
        assert refs["update_banner"].winfo_ismapped()
        app._on_update_download()
        assert opened == []
        assert started == ["campus-download"]
        assert app._update_install_busy.is_set()
        assert app.config["update_dismissed_tag"] != _newer_tag()
    finally:
        app._close_dashboard()
        root.destroy()


def test_settings_shows_update_section(monkeypatch):
    import tkinter as tk

    if tray.ctk is None:
        pytest.skip("本机无 CustomTkinter")
    monkeypatch.setattr(tray, "get_autostart", lambda: False)
    try:
        from tests.fakes import desktop_test_root
        root = desktop_test_root()
    except tk.TclError:
        pytest.skip("无显示环境")
    root.withdraw()
    app = tray.TrayApp(LOG, config=dict(tray.DEFAULT_CONFIG))
    app.root = root
    app.config = dict(tray.DEFAULT_CONFIG)
    try:
        app._update_info = _info()
        app._open_settings()
        root.update_idletasks()
        texts = []

        def walk(widget):
            try:
                text = widget.cget("text")
            except Exception:
                text = ""
            if text:
                texts.append(text)
            for child in widget.winfo_children():
                walk(child)

        walk(app.settings_win)
        joined = "\n".join(texts)
        assert "软件更新" in joined
        assert "当前版本：" + _current_tag() in joined
        assert "有更新可用" in joined
        assert "立即检查更新" in joined
    finally:
        if app.settings_win is not None:
            try:
                app.settings_win.destroy()
            except Exception:
                pass
        root.destroy()
