# -*- coding: utf-8 -*-
"""代理控制器单元测试（阶段 2，任务 2.1，对应 T08 / T09 / T10）。

覆盖 F04（注册表权限、部分写入、事务回滚）与 F05（端点未监听/非预期、不猜测端点）：

- 查询被拒绝 ⇒ 不产生任何写入；
- 打开句柄必须同时申请 ``KEY_QUERY_VALUE`` 与 ``KEY_SET_VALUE``；
- 写入中途失败 ⇒ 按事务回滚，回滚成功/失败**分别**报告；
- 系统刷新失败 ⇒ 结果明确为“未完全成功”，不谎报；
- 端点未监听 / 非本机地址 / 缺失 ⇒ 拒绝启用，且 ``ProxyEnable`` 不变。

约束：全部使用 :class:`tests.fakes.FakeRegistry` 与假 probe/refresh，
零真实注册表写入、零真实网络连接、零真实 Windows 刷新调用。
"""
from __future__ import annotations

import pytest

from campus_core import E_PROXY_ENDPOINT_INVALID, E_PROXY_WRITE, MISSING
from campus_windows import ProxyController, parse_endpoint, validate_proxy_endpoint
from tests.fakes import FakeRegistry


def _controller(registry, listening=True, refresh=True):
    return ProxyController(registry,
                           probe=lambda host, port: listening,
                           refresh=lambda: refresh)


# --------------------------------------------------------------------------- #
# T08：权限与「先读快照，后写入」
# --------------------------------------------------------------------------- #
def test_t08_query_denied_makes_no_write():
    """查询被拒绝时不得发生任何写入（原实现会先改 ProxyEnable）。"""
    registry = FakeRegistry(values={"ProxyEnable": 0}, deny_query=True)
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.changed is False
    assert registry.get("ProxyEnable") == 0
    assert registry.writes == []


def test_t08_handle_requests_query_and_set_rights():
    """读写句柄必须同时具备查询与写入权限。"""
    registry = FakeRegistry(values={"ProxyEnable": 0})
    _controller(registry).apply(False)
    assert registry.open_access
    for access in registry.open_access:
        assert access & registry.KEY_QUERY_VALUE
        assert access & registry.KEY_SET_VALUE


def test_t08_open_denied_is_reported_without_write():
    """连句柄都打不开时给出明确错误，且不写任何值。"""
    registry = FakeRegistry(values={"ProxyEnable": 0}, deny_open=True)
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.error_code == E_PROXY_WRITE
    assert registry.writes == []


# --------------------------------------------------------------------------- #
# T09：部分写入 / 刷新 / 回滚
# --------------------------------------------------------------------------- #
def test_t09_write_denied_keeps_original_state():
    registry = FakeRegistry(values={"ProxyEnable": 0}, deny_set=True)
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.error_code == E_PROXY_WRITE
    assert registry.get("ProxyEnable") == 0


def test_t09_mid_write_failure_rolls_back():
    """第一项写入成功、第二项失败：回滚应恢复原值。"""
    registry = FakeRegistry(values={"ProxyEnable": 0},
                            set_failures=[False, True, False])
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.rollback_ok is True
    assert registry.get("ProxyEnable") == 0
    assert "ProxyServer" not in registry.values


def test_t09_rollback_failure_is_reported_separately():
    """回滚也失败时必须单独明示，不得宣称保持原状。"""
    registry = FakeRegistry(values={"ProxyEnable": 0},
                            set_failures=[False, True, True])
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.rollback_ok is False
    assert "回滚失败" in result.detail
    assert registry.get("ProxyEnable") == 1  # 中间状态被明确暴露


def test_t09_refresh_failure_is_reported_without_lying():
    """注册表写成功但刷新失败：结果不得为“完全成功”。"""
    registry = FakeRegistry(values={"ProxyEnable": 0})
    result = _controller(registry, refresh=False).apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.refresh_ok is False
    assert result.changed is True
    assert result.error_code == E_PROXY_WRITE
    assert registry.get("ProxyEnable") == 1


# --------------------------------------------------------------------------- #
# T10：端点验证（不猜测、不误启用）
# --------------------------------------------------------------------------- #
def test_t10_endpoint_not_listening_skips_proxy():
    registry = FakeRegistry(values={"ProxyEnable": 0})
    result = _controller(registry, listening=False).apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.changed is False
    assert result.error_code == E_PROXY_ENDPOINT_INVALID
    assert registry.get("ProxyEnable") == 0


def test_t10_non_local_endpoint_is_rejected():
    registry = FakeRegistry(values={"ProxyEnable": 0})
    result = _controller(registry).apply(True, "8.8.8.8:1080")
    assert result.ok is False
    assert result.error_code == E_PROXY_ENDPOINT_INVALID
    assert registry.get("ProxyEnable") == 0


def test_t10_missing_endpoint_is_not_guessed():
    """未确认端点时不得硬编码猜测 127.0.0.1:7897。"""
    registry = FakeRegistry(values={"ProxyEnable": 0})
    result = _controller(registry).apply(True, None)
    assert result.ok is False
    assert result.error_code == E_PROXY_ENDPOINT_INVALID
    assert registry.get("ProxyEnable") == 0


# --------------------------------------------------------------------------- #
# 正常路径与既有配置保留
# --------------------------------------------------------------------------- #
def test_enable_writes_endpoint_and_preserves_other_settings():
    registry = FakeRegistry(values={"ProxyEnable": 0, "ProxyOverride": "<local>",
                                    "AutoConfigURL": "http://pac.local/proxy.pac"})
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is True
    assert result.changed is True
    assert result.endpoint == "127.0.0.1:7897"
    assert registry.get("ProxyEnable") == 1
    assert registry.get("ProxyServer") == "127.0.0.1:7897"
    assert registry.get("ProxyOverride") == "<local>"           # 既有绕过列表保留
    assert registry.get("AutoConfigURL") == "http://pac.local/proxy.pac"  # 既有 PAC 保留


def test_disable_only_touches_enable_flag():
    registry = FakeRegistry(values={"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"})
    result = _controller(registry).apply(False)
    assert result.ok is True
    assert result.changed is True
    assert registry.get("ProxyEnable") == 0
    assert registry.get("ProxyServer") == "127.0.0.1:7897"


def test_apply_is_idempotent_without_change():
    registry = FakeRegistry(values={"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"})
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is True
    assert result.changed is False
    assert registry.writes == []


def test_changes_record_old_and_new_values():
    registry = FakeRegistry(values={"ProxyEnable": 0})
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    mapping = {name: (old, new) for name, old, new in result.changes}
    assert mapping["ProxyEnable"] == (0, 1)
    assert mapping["ProxyServer"][0] is MISSING
    assert mapping["ProxyServer"][1] == "127.0.0.1:7897"


# --------------------------------------------------------------------------- #
# 端点解析与验证（纯函数）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("127.0.0.1:7897", ("127.0.0.1", 7897)),
    ("http://127.0.0.1:7897", ("127.0.0.1", 7897)),
    ("localhost:1080/", ("localhost", 1080)),
    ("[::1]:7897", ("::1", 7897)),
    ("127.0.0.1", (None, None)),
    ("127.0.0.1:0", (None, None)),
    ("127.0.0.1:70000", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
    (12345, (None, None)),
])
def test_parse_endpoint(text, expected):
    assert parse_endpoint(text) == expected


def test_validate_endpoint_rejects_and_accepts():
    ok, normalized, code, _detail = validate_proxy_endpoint("nope",
                                                            probe=lambda h, p: True)
    assert ok is False and normalized is None and code == E_PROXY_ENDPOINT_INVALID

    ok, normalized, code, _detail = validate_proxy_endpoint(
        "127.0.0.1:7897", probe=lambda h, p: True)
    assert ok is True and normalized == "127.0.0.1:7897" and code is None

    def boom(host, port):
        raise OSError("connection refused")

    ok, _normalized, code, _detail = validate_proxy_endpoint("127.0.0.1:7897", probe=boom)
    assert ok is False and code == E_PROXY_ENDPOINT_INVALID


def test_validate_endpoint_normalizes_ipv6():
    ok, normalized, code, _detail = validate_proxy_endpoint("[::1]:7897",
                                                            probe=lambda h, p: True)
    assert ok is True and normalized == "[::1]:7897" and code is None


@pytest.mark.parametrize("text", ["host:abc", ":", "[::1", "[::1]7897"])
def test_parse_endpoint_rejects_malformed(text):
    assert parse_endpoint(text) == (None, None)


def test_rollback_deletes_value_that_did_not_exist_before():
    """回滚时若原值不存在，应删除本次写入而非写成 0/空串。"""
    registry = FakeRegistry(values={}, set_failures=[False, True, False])
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.rollback_ok is True
    assert "ProxyEnable" not in registry.values
    assert registry.deletes == ["ProxyEnable"]


def test_refresh_exception_is_treated_as_failure():
    def broken_refresh():
        raise OSError("wininet unavailable")

    registry = FakeRegistry(values={"ProxyEnable": 0})
    controller = ProxyController(registry, probe=lambda h, p: True,
                                 refresh=broken_refresh)
    result = controller.apply(True, "127.0.0.1:7897")
    assert result.ok is False
    assert result.refresh_ok is False


def test_snapshot_read_failure_after_write_is_tolerated():
    """写入成功后再读快照失败时，结果对象仍应可用（after 为空即可）。"""
    class FlakyRegistry(FakeRegistry):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.query_calls = 0

        def QueryValueEx(self, key, name):
            self.query_calls += 1
            if self.query_calls > 4:      # 前 4 次是写入前的完整快照
                raise PermissionError("query denied")
            return super().QueryValueEx(key, name)

    registry = FlakyRegistry(values={"ProxyEnable": 0})
    result = _controller(registry).apply(True, "127.0.0.1:7897")
    assert result.ok is True
    assert result.changed is True
    assert result.after == {}


def test_missing_sentinel_is_falsy_and_readable():
    """``MISSING`` 哨兵用于区分“值不存在”与“值为 None”。"""
    assert repr(MISSING) == "<missing>"
    assert bool(MISSING) is False


