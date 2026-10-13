# -*- coding: utf-8 -*-
"""campus_core 共享契约与配置验证器的单元测试（阶段 1，任务 1.2 / 1.3）。

这些用例针对**新增**的契约与验证器，必须全部通过；它们同时为阶段 2/3 的
修复提供断言锚点。覆盖：
- 数据模型字段语义（Config 不含密码、秘密容器 repr、结果对象）；
- 统一错误码表；
- 配置验证器的全部非法输入路径（JSON null/list/截断、字符串开关、负数、
  小数、超大值、未知 schema_version、未知键隔离）。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from campus_core import (ACCOUNT_FORMATS, COOLDOWN_RANGE, ERROR_CODES,
                         ERROR_MESSAGES, INTERVAL_RANGE, OPERATORS,
                         SCHEMA_VERSION, THRESHOLD_RANGE, BrowserLaunchResult,
                         Config, ConfigIssue, PortalResolution, PortalStatus,
                         ProbeResult, RepairResult, Scheduler, Secret,
                         default_config, load_config_file, load_config_text,
                         validate_config)


# --------------------------------------------------------------------------- #
# 契约模型
# --------------------------------------------------------------------------- #
def test_config_has_no_password_field():
    """Config 不得包含任何密码/凭据字段。"""
    names = {f.name for f in dataclasses.fields(Config)}
    assert names == {
        "schema_version", "ssid", "profile", "interface", "operator",
        "account_format", "interval", "threshold", "cooldown",
        "browser_fallback", "proxy_consent", "proxy_endpoint",
    }
    for forbidden in ("password", "passwd", "pwd", "credential", "secret", "cookie"):
        assert forbidden not in names


def test_secret_repr_and_str_hide_plaintext():
    """秘密容器 repr/str 不含明文，reveal 才取出。"""
    secret = Secret("FAKE_PASSWORD")
    assert "FAKE_PASSWORD" not in repr(secret)
    assert "FAKE_PASSWORD" not in str(secret)
    assert repr(secret) == "Secret(***)"
    assert secret.reveal() == "FAKE_PASSWORD"
    assert bool(secret) is True
    assert Secret() == Secret("")
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
    assert Secret("a") != "a"  # 与普通字符串比较返回 NotImplemented -> 不相等
    assert hash(Secret("a")) == hash("a")
    assert Secret("a") in {Secret("a")}


def test_config_repr_has_no_secret():
    """Config 的 repr 不含任何敏感值（结构上就无秘密字段）。"""
    assert "password" not in repr(default_config()).lower()


def test_default_config_is_safe():
    """安全默认配置：代理联动关闭、运营商为 mobile、版本正确。"""
    cfg = default_config()
    assert cfg.proxy_enabled is False
    assert cfg.proxy_consent is None
    assert cfg.schema_version == SCHEMA_VERSION
    assert cfg.operator == "mobile"
    assert cfg.ssid == "SNNU"


def test_error_code_table_complete():
    """错误码表：码值唯一、均有中文说明、与常量集合一致。"""
    assert len(ERROR_CODES) == len(ERROR_MESSAGES)
    assert set(ERROR_CODES) == set(ERROR_MESSAGES)
    assert all(code.startswith("E_") for code in ERROR_CODES)
    assert all(msg for msg in ERROR_MESSAGES.values())


def test_portal_status_enum_values():
    """状态枚举覆盖《审查方案》7.3 节建议的全部状态。"""
    assert {s.value for s in PortalStatus} == {
        "STARTING", "ONLINE", "SUSPECT", "WIFI_DISCONNECTED", "NON_TARGET",
        "AUTH_REQUIRED", "REPAIRING", "AUTHENTICATED_DEGRADED", "NEEDS_USER",
        "ERROR", "PAUSED", "STOPPING",
    }


def test_result_models_semantics():
    """结果对象的判定语义：三项结果独立、失败不谎报成功。"""
    assert RepairResult(network_ok=True, auth_ok=True).ok is True
    assert RepairResult(network_ok=True, auth_ok=False).ok is False
    assert RepairResult(network_ok=True, auth_ok=True,
                        error_code="E_AUTH_REJECTED").ok is False
    assert PortalResolution(port=8601).ok is True
    assert PortalResolution(port=None, error="E_PORTAL_UNREACHABLE").ok is False
    assert ProbeResult(ok=True, source="x").ok is True
    assert BrowserLaunchResult().launched is False


# --------------------------------------------------------------------------- #
# 配置验证器：非法输入一律安全回退
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [None, [], [1, 2], "text", 42, True])
def test_non_object_toplevel_falls_back(raw):
    """顶层非 JSON object：整体回退安全默认值，且不启用代理。"""
    result = validate_config(raw)
    assert result.config == default_config()
    assert result.source_ok is False
    assert result.issues
    assert result.config.proxy_enabled is False


@pytest.mark.parametrize("text", ["", "{", '{"interval": 15', "not json", "{"])
def test_truncated_or_broken_json_is_io_error(text):
    """JSON 截断/损坏：回退默认值并给出可见错误。"""
    result = load_config_text(text)
    assert result.config == default_config()
    assert result.source_ok is False
    assert any(i.code == "E_CONFIG_IO" for i in result.issues)


@pytest.mark.parametrize("raw", [None, "null", "[]", "{}"])
def test_json_null_list_object_from_text(raw):
    """文本形式的 null/list 也被安全处理。"""
    if raw is None:
        result = load_config_text("null")
        assert result.config == default_config()
        assert result.source_ok is False
    else:
        result = load_config_text(raw)
        if raw == "{}":
            # 合法对象但缺少 schema_version：按当前版本解析
            assert result.config == default_config()
            assert result.source_ok is True
            assert any(i.key == "schema_version" for i in result.issues)
        else:
            assert result.source_ok is False


def test_string_switch_is_rejected():
    """字符串开关 "false" 不得被当作真值；代理授权回退未知。"""
    result = validate_config({"clash_auto_proxy": "false", "browser_fallback": "false"})
    assert result.config.proxy_consent is None
    assert result.config.proxy_enabled is False
    assert result.config.browser_fallback is True  # 回退默认
    keys = {i.key for i in result.issues}
    assert "proxy_consent" in keys and "browser_fallback" in keys


def test_bool_as_int_is_rejected():
    """bool 充当 int 必须被拒绝（bool 是 int 子类）。"""
    result = validate_config({"interval": True, "threshold": False, "cooldown": True})
    assert result.config.interval == 15
    assert result.config.threshold == 3
    assert result.config.cooldown == 180
    assert {i.key for i in result.issues} >= {"interval", "threshold", "cooldown"}


@pytest.mark.parametrize("key,value", [
    ("interval", "bad"), ("interval", 4), ("interval", 3601),
    ("interval", -5), ("interval", 7.5),
    ("threshold", 0), ("threshold", 21), ("threshold", 2.5),
    ("cooldown", 59), ("cooldown", 86401), ("cooldown", 3.14),
])
def test_numeric_out_of_range_or_wrong_type(key, value):
    """数值越界 / 小数 / 字符串一律回退默认值并记录问题。"""
    result = validate_config({key: value})
    default_value = getattr(default_config(), key)
    assert getattr(result.config, key) == default_value
    assert any(i.key == key for i in result.issues)


def test_numeric_extremely_large_is_rejected():
    """超大值（如 10**9）超出范围被拒绝。"""
    result = validate_config({"interval": 10 ** 9, "cooldown": 10 ** 9})
    assert result.config.interval == 15
    assert result.config.cooldown == 180


def test_unknown_schema_version_falls_back():
    """未知 schema_version：整体不可信，回退默认值且不启用代理。"""
    result = validate_config({"schema_version": 99, "clash_auto_proxy": True})
    assert result.config == default_config()
    assert result.config.proxy_enabled is False
    assert result.source_ok is False
    assert any(i.code == "E_CONFIG_INVALID" and i.key == "schema_version"
               for i in result.issues)


def test_non_integer_schema_version_rejected():
    """schema_version 非整数：整体回退。"""
    result = validate_config({"schema_version": "1"})
    assert result.source_ok is False
    assert result.config == default_config()


def test_valid_config_roundtrip():
    """合法配置被完整接受，空运营商被保留。"""
    raw = {
        "schema_version": SCHEMA_VERSION,
        "ssid": "SNNU",
        "profile": "SNNU-Profile",
        "interface": "WLAN",
        "yys": "",
        "account_format": "isp",
        "interval": 30,
        "threshold": 5,
        "cooldown": 300,
        "browser_fallback": False,
        "clash_auto_proxy": True,
        "proxy_endpoint": "127.0.0.1:7897",
    }
    result = validate_config(raw)
    assert result.ok is True
    assert result.unknown_keys == ()
    assert result.config.operator == ""
    assert result.config.account_format == "isp"
    assert result.config.interval == 30
    assert result.config.threshold == 5
    assert result.config.cooldown == 300
    assert result.config.browser_fallback is False
    assert result.config.proxy_consent is True
    assert result.config.proxy_enabled is True
    assert result.config.proxy_endpoint == "127.0.0.1:7897"


@pytest.mark.parametrize("key", ["yys", "operator"])
def test_operator_alias_and_invalid(key):
    """运营商支持 yys/operator 两个键名；非法值回退 mobile。"""
    ok = validate_config({"schema_version": SCHEMA_VERSION, key: "unicom"})
    assert ok.config.operator == "unicom"
    bad = validate_config({"schema_version": SCHEMA_VERSION, key: "campus"})
    assert bad.config.operator == "mobile"
    assert any(i.key == key for i in bad.issues)


def test_account_format_invalid_falls_back():
    """account_format 非法值回退 plain。"""
    result = validate_config({"schema_version": SCHEMA_VERSION,
                              "account_format": "weird"})
    assert result.config.account_format == "plain"
    assert any(i.key == "account_format" for i in result.issues)


def test_ssid_empty_and_non_string():
    """ssid 非字符串或为空：回退默认并记录问题。"""
    for value in ("", "   ", 123):
        result = validate_config({"schema_version": SCHEMA_VERSION, "ssid": value})
        assert result.config.ssid == "SNNU"
        assert any(i.key == "ssid" for i in result.issues)


def test_optional_string_fields():
    """profile/interface/proxy_endpoint：None 保留、非字符串丢弃为 None。"""
    result = validate_config({"schema_version": SCHEMA_VERSION,
                              "profile": 123, "interface": None,
                              "proxy_endpoint": 456})
    assert result.config.profile is None
    assert result.config.interface is None
    assert result.config.proxy_endpoint is None


@pytest.mark.parametrize("value,expected", [
    (None, None), (True, True), (False, False), ("false", None), (1, None), ([], None),
])
def test_consent_three_state(value, expected):
    """代理授权仅接受 None/True/False；其余（含字符串）回退 None 且不启用。"""
    result = validate_config({"schema_version": SCHEMA_VERSION,
                              "clash_auto_proxy": value})
    assert result.config.proxy_consent is expected
    assert result.config.proxy_enabled is (expected is True)


def test_consent_alias_proxy_consent():
    """proxy_consent 与 clash_auto_proxy 两个键名均被识别。"""
    result = validate_config({"schema_version": SCHEMA_VERSION, "proxy_consent": True})
    assert result.config.proxy_consent is True


def test_unknown_keys_isolated_without_values():
    """未知键被隔离并记录**脱敏**告警（不含原始值）。"""
    result = validate_config({"schema_version": SCHEMA_VERSION,
                              "mystery": "SECRET-VALUE", "another": 1})
    assert result.unknown_keys == ("another", "mystery")
    assert result.config == default_config()
    joined = " ".join(i.detail for i in result.issues)
    assert "SECRET-VALUE" not in joined


def test_missing_schema_version_is_warning_not_fatal():
    """缺少 schema_version：按当前版本解析（source_ok=True）并给出提示。"""
    result = validate_config({"interval": 20})
    assert result.source_ok is True
    assert result.config.interval == 20
    assert any(i.key == "schema_version" for i in result.issues)


def test_load_config_file_roundtrip(tmp_path):
    """从文件加载合法配置成功；文件不存在时安全回退。"""
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "interval": 45}),
                    encoding="utf-8")
    result = load_config_file(path)
    assert result.config.interval == 45

    missing = load_config_file(tmp_path / "nope.json")
    assert missing.source_ok is False
    assert missing.config == default_config()


def test_constants_are_consistent():
    """常量取值范围自洽，且包含空运营商。"""
    assert "" in OPERATORS
    assert set(OPERATORS) == {"mobile", "unicom", "telecom", ""}
    assert ACCOUNT_FORMATS == ("plain", "isp")
    assert INTERVAL_RANGE == (5, 3600)
    assert THRESHOLD_RANGE == (1, 20)
    assert COOLDOWN_RANGE == (60, 86400)


def test_config_issue_is_frozen_value():
    """ConfigIssue 为不可变值对象。"""
    issue = ConfigIssue("interval", "E_CONFIG_INVALID", "x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.key = "y"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 调度接口草案
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_scheduler_cooldown_blocks_new_attempt():
    """冷却期内不允许新认证；register/finish 正确置位忙标志。"""
    clock = _Clock()
    sched = Scheduler(clock=clock, cooldown=180.0)
    assert sched.can_attempt() is True
    sched.register_attempt()
    assert sched.busy is True
    assert sched.can_attempt() is False
    sched.finish_attempt()
    assert sched.busy is False
    sched.note_failure(retryable=True)
    assert sched.can_attempt() is False
    clock.now = 179.0
    assert sched.can_attempt() is False
    clock.now = 180.0
    assert sched.can_attempt() is True


def test_scheduler_backoff_has_upper_bound():
    """网络类失败指数退避，且有上限。"""
    clock = _Clock()
    sched = Scheduler(clock=clock, cooldown=60.0, max_backoff=120.0)
    sched.note_failure()
    assert sched.backoff == 60.0
    sched.note_failure()
    assert sched.backoff == 120.0
    sched.note_failure()
    assert sched.backoff == 120.0  # 不超过上限


def test_scheduler_success_keeps_min_interval():
    """成功不抹掉最短间隔（next_attempt_at 保持）。"""
    clock = _Clock()
    sched = Scheduler(clock=clock, cooldown=180.0)
    sched.register_attempt()
    sched.note_failure()
    sched.finish_attempt()
    next_before = sched.next_attempt_at
    sched.note_success()
    assert sched.failure_streak == 0
    assert sched.next_attempt_at == next_before


def test_scheduler_non_retryable_needs_user():
    """不可重试失败进入需用户处理，不再自动尝试。"""
    clock = _Clock()
    sched = Scheduler(clock=clock, cooldown=60.0)
    sched.note_failure(retryable=False)
    assert sched.needs_user is True
    assert sched.can_attempt() is False
    sched.note_success()
    assert sched.needs_user is False


def test_scheduler_bump_config_version():
    """配置版本变化重置失败计数。"""
    clock = _Clock()
    sched = Scheduler(clock=clock)
    sched.note_failure()
    sched.bump_config_version(3)
    assert sched.config_version == 3
    assert sched.failure_streak == 0
