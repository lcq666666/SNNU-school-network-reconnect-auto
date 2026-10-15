# -*- coding: utf-8 -*-
"""V0.3 P6 托盘三模式与手动锁定（单元 / 离线 Mock）。

覆盖依据：执行计划 §0.2（P6 行：智能自动 / 仅监测 / 手动锁定，性质「否：离线
Mock + 界面走查」，前置 P3）、方案 §11（托盘三模式与展示字段）、方案 §9
（网络切换期间保持 Clash 原样，默认开启）、AGENTS §3.10（默认模式未经受控验收
不得启用）、AGENTS §3.11（权限分离：托盘不默认提权）。

约束：零真实网络 / 零真实路由写入 / 零真实注册表写入 / 零真实凭据读取 /
零真实浏览器启动 / 零真实 sleep。
"""
from __future__ import annotations

import logging

import pytest

import campus_reconnect_tray as tray
import campus_tray_failover as tf
from campus_failover import FailoverMode, FailoverState, OpKind
from campus_network import HealthAxis, HealthState, InterfaceHealth
from tests.fakes import FakeOpExecutor

LOG = logging.getLogger("campus")

ETH_IF = 10
WIFI_IF = 5


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _health(axis, state, if_index, name):
    return InterfaceHealth(axis, name, if_index, state)


def snap(eth_state=HealthState.HEALTHY, wifi_state=HealthState.HEALTHY,
         egress_if=ETH_IF, online=True):
    """构造轻量快照（零 I/O）。"""
    return tf.light_snapshot(
        system_online=online,
        ethernet=_health(HealthAxis.ETHERNET_HEALTH, eth_state, ETH_IF, "以太网"),
        wifi=_health(HealthAxis.WIFI_HEALTH, wifi_state, WIFI_IF, "WLAN"),
        egress_if=egress_if)


def _make_app(monkeypatch, **overrides):
    """构造托盘应用：替换配置来源与保存，零真实副作用。"""
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "save_config", lambda cfg: True)
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app.session = object()
    # 执行器工厂替换为假件：即使测试切到 AUTO 也不构造真实执行器。
    app.failover.executor_factory = lambda: FakeOpExecutor()
    return app


# --------------------------------------------------------------------------- #
# U-6-01 契约与三模式
# --------------------------------------------------------------------------- #
def test_u6_01_contract_and_three_modes():
    assert tf.CONTRACT_VERSION == "v03-p6-tray-1"
    assert tf.MODE_ORDER == (FailoverMode.MONITOR, FailoverMode.AUTO,
                             FailoverMode.LOCKED)
    assert tf.DEFAULT_MODE is FailoverMode.MONITOR
    assert tf.DEFAULT_CLASH_KEEP is True
    assert [tf.tray_mode_label(m) for m in tf.MODE_ORDER] == \
        ["仅监测", "智能自动", "手动锁定"]
    # 解析：大小写不敏感；非法一律回退安全值
    assert tf.parse_mode("auto") is FailoverMode.AUTO
    assert tf.parse_mode("locked") is FailoverMode.LOCKED
    assert tf.parse_mode("bogus") is FailoverMode.MONITOR
    assert tf.parse_mode(None) is FailoverMode.MONITOR
    assert tf.parse_lock("WIFI") == "wifi"
    assert tf.parse_lock("bogus") is None
    assert tf.parse_lock(None) is None


# --------------------------------------------------------------------------- #
# U-6-02..03 仅监测：默认零动作
# --------------------------------------------------------------------------- #
def test_u6_02_default_monitor_is_readonly():
    bridge = tf.TrayFailoverBridge()
    assert bridge.mode is FailoverMode.MONITOR
    assert bridge.executor_attached is False
    result = bridge.tick(0.0, snap())
    assert result.readonly is True
    assert result.executed == []
    assert bridge.manager.state is FailoverState.ETHERNET_PRIMARY
    status = bridge.status()
    assert status.mode_label == "仅监测"
    assert status.readonly is True


def test_u6_03_monitor_plans_but_never_executes():
    """仅监测：即使满足切换条件，动作也只登记为计划、绝不执行。"""
    bridge = tf.TrayFailoverBridge(mode="MONITOR")
    result = None
    for index in range(5):
        result = bridge.tick(index * 15.0,
                             snap(eth_state=HealthState.INTERNET_UNREACHABLE,
                                  egress_if=WIFI_IF))
    assert result.executed == []
    assert any(p.kind is OpKind.METRIC_WRITE for p in result.plan)
    assert result.readonly is True
    # 未执行 → 不登记切换冷却
    assert bridge.manager.timers.switch_ready_at == 0.0


# --------------------------------------------------------------------------- #
# U-6-04..07 手动锁定
# --------------------------------------------------------------------------- #
def test_u6_04_locked_pins_preferred_and_never_executes():
    bridge = tf.TrayFailoverBridge(mode="LOCKED", lock_target="ethernet")
    result = None
    for index in range(6):
        result = bridge.tick(index * 15.0,
                             snap(eth_state=HealthState.INTERNET_UNREACHABLE,
                                  wifi_state=HealthState.HEALTHY,
                                  egress_if=WIFI_IF))
    assert result.preferred == "ethernet"        # 锁定优先固定
    assert result.executed == []
    assert result.plan == []
    assert bridge.manager.state is FailoverState.ETHERNET_PRIMARY  # 不切换
    assert bridge.manager.timers.switch_ready_at == 0.0
    status = bridge.status()
    assert status.locked is True and status.lock_target == "ethernet"
    assert status.primary == "ethernet"


def test_u6_05_locked_reports_health_warning_without_actions():
    """手动锁定：仍显示健康告警；**不**自动禁用另一张网卡（零动作）。"""
    bridge = tf.TrayFailoverBridge(mode="LOCKED", lock_target="ethernet")
    bridge.tick(0.0, snap(eth_state=HealthState.INTERNET_UNREACHABLE,
                          wifi_state=HealthState.HEALTHY))
    status = bridge.status()
    assert any("健康告警" in w for w in status.warnings)
    assert bridge.last_result.plan == []
    assert bridge.last_result.executed == []
    assert bridge.manager.scheduler.executor is None


def test_u6_06_locked_wifi_preferred():
    bridge = tf.TrayFailoverBridge(mode="LOCKED", lock_target="wifi")
    result = bridge.tick(0.0, snap())
    assert result.preferred == "wifi"
    assert bridge.status().primary == "wifi"
    assert bridge.status().backup == "ethernet"


def test_u6_07_locked_without_target_behaves_like_monitor():
    """未设置锁定目标时，LOCKED 与 MONITOR 同样不执行任何动作（P3 行为）。"""
    bridge = tf.TrayFailoverBridge(mode="LOCKED")   # lock_target=None
    result = None
    for index in range(5):
        result = bridge.tick(index * 15.0,
                             snap(eth_state=HealthState.INTERNET_UNREACHABLE))
    assert result.executed == []
    assert bridge.status().locked is False


# --------------------------------------------------------------------------- #
# U-6-08..10 智能自动的显式授权门禁
# --------------------------------------------------------------------------- #
def test_u6_08_auto_unauthorized_does_not_attach_executor():
    executor = FakeOpExecutor()
    bridge = tf.TrayFailoverBridge(mode="AUTO",
                                   executor_factory=lambda: executor)
    assert bridge.executor_attached is False
    result = None
    for index in range(6):
        result = bridge.tick(index * 15.0,
                             snap(eth_state=HealthState.INTERNET_UNREACHABLE,
                                  egress_if=WIFI_IF))
    assert executor.requests == []
    assert result.executed == []
    assert any("未授权" in w for w in bridge.status().warnings)


def test_u6_09_auto_authorized_attaches_executor_and_switches():
    executor = FakeOpExecutor()
    bridge = tf.TrayFailoverBridge(mode="AUTO", authorized=True,
                                   executor_factory=lambda: executor)
    assert bridge.executor_attached is True
    executed = []
    for index in range(6):
        result = bridge.tick(index * 15.0,
                             snap(eth_state=HealthState.INTERNET_UNREACHABLE,
                                  wifi_state=HealthState.HEALTHY,
                                  egress_if=WIFI_IF))
        executed.extend(o.kind for o in result.executed)
    assert executor.calls == [OpKind.METRIC_WRITE]
    assert executed == [OpKind.METRIC_WRITE]
    assert bridge.manager.state is FailoverState.WIFI_BACKUP_ACTIVE


def test_u6_10_revoke_authorization_detaches_executor():
    executor = FakeOpExecutor()
    bridge = tf.TrayFailoverBridge(mode="AUTO", authorized=True,
                                   executor_factory=lambda: executor)
    assert bridge.executor_attached is True
    bridge.set_authorized(False)
    assert bridge.executor_attached is False
    assert bridge.manager.scheduler.executor is None
    # 重新授权 → 重新接入
    bridge.set_authorized(True)
    assert bridge.executor_attached is True


# --------------------------------------------------------------------------- #
# U-6-11 Clash 保持原样（方案 §9）
# --------------------------------------------------------------------------- #
def test_u6_11_clash_keep_default_on_and_toggle():
    bridge = tf.TrayFailoverBridge()
    assert bridge.clash_keep is True
    assert bridge.status().clash_keep is True
    assert bridge.set_clash_keep(False) is False
    assert bridge.status().clash_keep is False


# --------------------------------------------------------------------------- #
# U-6-12..15 展示模型（方案 §11）
# --------------------------------------------------------------------------- #
def test_u6_12_status_exposes_all_section11_fields():
    bridge = tf.TrayFailoverBridge()
    bridge.tick(0.0, snap(eth_state=HealthState.HEALTHY,
                          wifi_state=HealthState.CAPTIVE_PORTAL,
                          egress_if=ETH_IF))
    status = bridge.status()
    data = status.to_dict()
    for key in ("mode", "mode_label", "state", "primary", "backup",
                "ethernet_status", "wifi_status", "egress", "remote_guard",
                "lock_target", "locked", "readonly", "clash_keep", "authorized",
                "executor_attached", "last_switch_at", "last_switch_reason",
                "last_switch_result", "warnings"):
        assert key in data, key
    text = "\n".join(status.lines())
    for frag in ("运行模式", "当前主网络", "当前备用网络", "有线状态",
                 "Wi-Fi 状态", "当前系统出口", "远程保障", "最近切换"):
        assert frag in text, frag
    assert status.ethernet_status == "正常"
    assert status.wifi_status == "需要认证"
    assert status.egress == "以太网（ifIndex=10）"


@pytest.mark.parametrize("state,expected", [
    (HealthState.HEALTHY, "正常"),
    (HealthState.INTERNET_UNREACHABLE, "异常（INTERNET_UNREACHABLE）"),
    (HealthState.CAPTIVE_PORTAL, "需要认证"),
    (HealthState.UNKNOWN, "不可判定（UNKNOWN）"),
])
def test_u6_15a_ethernet_status_text(state, expected):
    bridge = tf.TrayFailoverBridge()
    bridge.tick(0.0, snap(eth_state=state, wifi_state=HealthState.HEALTHY))
    assert bridge.status().ethernet_status == expected


@pytest.mark.parametrize("state,expected", [
    (HealthState.HEALTHY, "已认证"),
    (HealthState.CAPTIVE_PORTAL, "需要认证"),
    (HealthState.LINK_DOWN, "不可用（未连接）"),
    (HealthState.NO_IP, "已连接（无有效地址）"),
])
def test_u6_15b_wifi_status_text(state, expected):
    bridge = tf.TrayFailoverBridge()
    bridge.tick(0.0, snap(eth_state=HealthState.HEALTHY, wifi_state=state))
    assert bridge.status().wifi_status == expected


def test_u6_13_remote_guard_states():
    executor = FakeOpExecutor()
    bridge = tf.TrayFailoverBridge(mode="AUTO", authorized=True,
                                   executor_factory=lambda: executor)
    assert bridge.status().remote_guard == tf.REMOTE_ENABLED
    bridge.pause(0.0)
    assert bridge.status().remote_guard == tf.REMOTE_PAUSED
    bridge.resume(0.0)
    bridge.manager.require_manual(0.0, "凭据错误")
    assert bridge.status().remote_guard == tf.REMOTE_MANUAL


def test_u6_14_records_last_switch():
    executor = FakeOpExecutor()
    bridge = tf.TrayFailoverBridge(mode="AUTO", authorized=True,
                                   executor_factory=lambda: executor)
    for index in range(6):
        bridge.tick(index * 15.0,
                    snap(eth_state=HealthState.INTERNET_UNREACHABLE,
                         wifi_state=HealthState.HEALTHY, egress_if=WIFI_IF))
    status = bridge.status()
    assert status.last_switch_at == 60.0
    assert status.last_switch_reason and status.last_switch_result


# --------------------------------------------------------------------------- #
# U-6-16..17 托盘配置（读取 / 非法回退 / 往返持久化）
# --------------------------------------------------------------------------- #
def test_u6_16_config_keys_and_reader():
    for key in ("failover_mode", "failover_lock", "clash_keep",
                "auto_switch_authorized"):
        assert key in tray.DEFAULT_CONFIG, key
    assert tray.DEFAULT_CONFIG["failover_mode"] == "MONITOR"
    assert tray.DEFAULT_CONFIG["clash_keep"] is True
    assert tray.DEFAULT_CONFIG["auto_switch_authorized"] is False
    # 非法值一律回退安全默认（AUTO 授权 fail-closed）
    bad = tray._read_failover_keys(
        '{"failover_mode":"bogus","failover_lock":"x","clash_keep":"yes",'
        '"auto_switch_authorized":"yes"}')
    assert bad == {"failover_mode": "MONITOR", "failover_lock": None,
                   "clash_keep": True, "auto_switch_authorized": False}
    good = tray._read_failover_keys(
        '{"failover_mode":"AUTO","failover_lock":"wifi","clash_keep":false,'
        '"auto_switch_authorized":true}')
    assert good == {"failover_mode": "AUTO", "failover_lock": "wifi",
                    "clash_keep": False, "auto_switch_authorized": True}


def test_u6_17_config_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "tray_config.json"
    monkeypatch.setattr(tray, "CONFIG_FILE", path)
    cfg = dict(tray.DEFAULT_CONFIG)
    cfg.update({"failover_mode": "LOCKED", "failover_lock": "wifi",
                "clash_keep": False, "auto_switch_authorized": True})
    assert tray.save_config(cfg) is True
    loaded = tray.load_config()
    assert loaded["failover_mode"] == "LOCKED"
    assert loaded["failover_lock"] == "wifi"
    assert loaded["clash_keep"] is False
    assert loaded["auto_switch_authorized"] is True


# --------------------------------------------------------------------------- #
# U-6-18..21 托盘菜单与应用动作
# --------------------------------------------------------------------------- #
def test_u6_18_menu_has_three_modes_lock_and_clash(monkeypatch):
    app = _make_app(monkeypatch)
    menu = app.build_menu()
    texts = [getattr(item, "text", None) for item in menu]
    assert "运行模式" in texts
    assert "手动锁定目标" in texts
    assert "网络切换期间保持 Clash 原样" in texts
    mode_item = next(i for i in menu if getattr(i, "text", None) == "运行模式")
    assert [getattr(s, "text", None) for s in mode_item.submenu] == \
        ["仅监测", "智能自动", "手动锁定"]
    lock_item = next(i for i in menu if getattr(i, "text", None) == "手动锁定目标")
    assert [getattr(s, "text", None) for s in lock_item.submenu] == \
        ["以太网优先", "Wi-Fi 优先", "不锁定"]


def test_u6_19_tray_apply_mode_lock_clash(monkeypatch):
    app = _make_app(monkeypatch)
    app.apply_lock("wifi")
    assert app.config["failover_lock"] == "wifi"
    assert app.failover.lock_target == "wifi"
    app.apply_mode("LOCKED")
    assert app.config["failover_mode"] == "LOCKED"
    assert app.failover.mode is FailoverMode.LOCKED
    assert app._current_mode() is FailoverMode.LOCKED
    app.apply_clash_keep(False)
    assert app.config["clash_keep"] is False
    assert app.failover.clash_keep is False


def test_u6_20_auto_requires_explicit_authorization(monkeypatch):
    """未授权时选择「智能自动」不得生效（root=None → 询问无应答 → 保持仅监测）。"""
    app = _make_app(monkeypatch)
    app.on_set_mode(FailoverMode.AUTO)
    assert app.config["failover_mode"] == "MONITOR"
    assert app.config["auto_switch_authorized"] is False
    assert app._current_mode() is FailoverMode.MONITOR


def test_u6_21_failover_owns_automatic_actions_in_all_modes(monkeypatch):
    """U-6-21（V0.3 P7.5-1 修订）：**自动**副作用在**所有模式**下都由调度器独占。

    P7.5-1 前：``_failover_owns_actions()`` 仅 AUTO 为真，于是 MONITOR/LOCKED
    下 V0.2 自动重连照常触发认证（F75-02）。修复后该方法在任何模式都返回
    ``True`` —— 含义是"后台自动副作用**只可能**来自 P3 单一副作用门"，
    而 V0.2 自动重连路径已被整体移除（仅保留显式手动重连）。
    """
    app = _make_app(monkeypatch)
    assert app._failover_owns_actions() is True
    app.failover.set_mode("LOCKED")
    assert app._failover_owns_actions() is True
    app.failover.set_mode("AUTO")
    assert app._failover_owns_actions() is True
    # 桥接缺失（构造异常）时回退为 False，不臆断
    app.failover = None
    assert app._failover_owns_actions() is False


# --------------------------------------------------------------------------- #
# U-6-22 单一副作用门：每轮至多一类动作（执行计划 §0.4）
# --------------------------------------------------------------------------- #
def test_u6_22_single_side_effect_gate_per_round():
    executor = FakeOpExecutor()
    bridge = tf.TrayFailoverBridge(mode="AUTO", authorized=True,
                                   executor_factory=lambda: executor)
    for index in range(8):
        result = bridge.tick(index * 15.0,
                             snap(eth_state=HealthState.INTERNET_UNREACHABLE,
                                  wifi_state=HealthState.HEALTHY,
                                  egress_if=WIFI_IF))
        assert len(result.plan) <= 1
        assert len(result.executed) <= 1
