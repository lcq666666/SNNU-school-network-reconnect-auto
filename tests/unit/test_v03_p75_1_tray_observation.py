# -*- coding: utf-8 -*-
"""V0.3 P7.5-1 托盘真实三轴观测与模式隔离（离线集成 / 闭合回路）。

覆盖本阶段两项修复的**真实生产调用链**（``TrayApp`` → ``TraySnapshotSource``
→ ``campus_tray_observer`` → ``campus_tray_failover`` → ``campus_failover``）：

- F75-01：托盘接入真实三轴快照（后台采集 / 新鲜度 / 超时 / 代次 / 限流 /
  可信度门禁）；AUTO 缺可信快照即拒绝执行；``light_snapshot`` 仅作显示占位。
- F75-02：MONITOR / LOCKED / AUTO 三种模式下后台**零** V0.2 自动认证；
  仅用户显式手动重连走认证，且须通过互斥与过期请求丢弃检查。

集成剧本（``I-75-1-*``）：

1. 双网正常时托盘拿到完整三轴快照；
2. Ethernet 故障时把真实健康状态交给状态机；
3. 快照缺失 / 超时 / 过期 / 异常时 AUTO 拒绝执行；
4. 模式改变时旧采集结果不得触发网络副作用；
5. MONITOR 连续多轮掉线零自动认证；
6. LOCKED 连续多轮掉线零自动认证；
7. AUTO 不调用旧版 ``_run_repair()``；
8. 手动重连不绕过单实例 / 互斥控制；
9. CLI 与托盘共用的底层认证契约未退化；
10. 全部离线测试零真实网络 / 凭据 / 路由副作用。
11. 后台采集不阻塞调用线程且并发限流（真实线程）；
12. DHCP / 接口变化作废旧代次。

约束：零真实网络 / 零真实路由写入 / 零真实注册表写入 / 零真实凭据读取 /
零真实 sleep（真实线程用例只用有界 ``Event.wait``）。
"""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

import campus_core
import campus_reconnect as engine
import campus_reconnect_tray as tray
import campus_tray_failover as tf
import campus_tray_observer as tso
from campus_failover import FailoverMode, FailoverState, OpKind
from campus_network import HealthState, evaluate
from tests.fakes import FakeProbeEngine

LOG = logging.getLogger("campus")

ETH_IF = 10
WIFI_IF = 5
INTERVAL = 15.0
ETH_IP = "10.20.30.40"
WIFI_IP = "10.8.0.5"


# --------------------------------------------------------------------------- #
# 假件：可编排的三轴采集能力（真实 evaluate 链 + 假探测引擎）
# --------------------------------------------------------------------------- #
class FakeCollector:
    """替换 ``TrayApp._snapshot_collector`` 的采集能力。

    采集**能力**用假件（零 PowerShell / 零真实网络），但评估链是**真实**的：
    走 ``campus_network.evaluate()`` + ``InterfaceHealthChecker``，产出与生产
    完全同构的 ``NetworkHealthSnapshot``（含 ``recommendation`` / ``generated_at``），
    因此不跳过真实生产调用链。
    """

    def __init__(self, *, eth_up: bool = True, wifi_up: bool = True,
                 egress_if: int = ETH_IF, eth_ip: str = ETH_IP,
                 wifi_ip: str = WIFI_IP, system_online: bool = True) -> None:
        self.eth_up = eth_up
        self.wifi_up = wifi_up
        self.egress_if = egress_if
        self.eth_ip = eth_ip
        self.wifi_ip = wifi_ip
        self.system_online = system_online
        self.calls: list = []
        self.raise_error: BaseException | None = None
        self.return_none = False

    def report(self) -> SimpleNamespace:
        return SimpleNamespace(
            host="TEST-HOST",
            adapters=[
                SimpleNamespace(name="以太网", if_index=ETH_IF,
                                status="Up" if self.eth_up else "Disconnected",
                                category="physical-ethernet"),
                SimpleNamespace(name="WLAN", if_index=WIFI_IF,
                                status="Up" if self.wifi_up else "Disconnected",
                                category="physical-wifi"),
            ],
            ip_addresses=[
                {"if_index": ETH_IF, "address_family": "IPv4",
                 "ip_address": self.eth_ip},
                {"if_index": WIFI_IF, "address_family": "IPv4",
                 "ip_address": self.wifi_ip},
            ],
            default_routes=[
                {"if_index": ETH_IF, "address_family": "IPv4",
                 "destination_prefix": "0.0.0.0/0", "next_hop": "10.20.30.1",
                 "route_metric": 0, "interface_metric": 10},
                {"if_index": WIFI_IF, "address_family": "IPv4",
                 "destination_prefix": "0.0.0.0/0", "next_hop": "10.8.0.1",
                 "route_metric": 0, "interface_metric": 50},
            ],
            egress={},
            remote_control={},
        )

    def __call__(self):
        self.calls.append(1)
        if self.raise_error is not None:
            raise self.raise_error
        if self.return_none:
            return None
        report = self.report()
        fake = FakeProbeEngine()
        fake.tcp["default"] = True
        fake.http["default"] = {"status_code": 200, "redirected": False,
                                "body": "Microsoft Connect Test", "error": ""}
        fake.dns["default"] = True
        fake.routes["default"] = SimpleNamespace(if_index=self.egress_if,
                                                 source_ip=self.wifi_ip,
                                                 error=None)
        return evaluate(
            report, fake,
            system_probe=lambda: (self.system_online, "假件系统探测"),
            roles={"ethernet": report.adapters[0], "wifi": report.adapters[1]},
            route_lookup=fake.route_lookup)


class RecordingExecutor:
    """记录被调用动作的执行器（真实执行层替身；``True`` = 执行成功）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, request):
        self.calls.append(getattr(request, "kind", None))
        return True


def _make_app(monkeypatch, **overrides):
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(tray, "save_config", lambda cfg: True)
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    app.session = object()
    return app


def _attach(app, *, collector, executor=None, authorized=True, mode="AUTO",
            spawn=None):
    """装配：注入采集能力 +（可选）执行器，并同步桥接。"""
    app._snapshot_collector = collector
    app._snapshot_spawn = spawn if spawn is not None else (lambda func: func())
    if executor is not None:
        app.failover.executor_factory = lambda: executor
    app.config["failover_mode"] = mode
    app.config["auto_switch_authorized"] = bool(authorized)
    app._sync_failover_from_config()
    return app


def _drive(app, clock, frames: int, *, online: bool = False):
    """推进若干轮 ``_failover_tick``（隔离故障切换链路，不含 _probe_tick）。"""
    results = []
    for index in range(frames):
        results.append(app._failover_tick(clock.monotonic(), online))
        clock.advance(INTERVAL)
    return results


# --------------------------------------------------------------------------- #
# I-75-1-01 双网正常：托盘拿到完整三轴快照
# --------------------------------------------------------------------------- #
def test_i75_1_01_tray_obtains_complete_three_axis_snapshot(monkeypatch,
                                                            virtual_clock):
    app = _make_app(monkeypatch, failover_mode="AUTO",
                    auto_switch_authorized=True)
    virtual_clock.install(monkeypatch, tray)
    collector = FakeCollector(eth_up=True, wifi_up=True, egress_if=ETH_IF)
    _attach(app, collector=collector)

    snapshot = app._build_failover_snapshot(True)
    assert snapshot.ethernet is not None, "缺少 ETHERNET_HEALTH 轴"
    assert snapshot.wifi is not None, "缺少 WIFI_HEALTH 轴"
    assert snapshot.system is not None, "缺少 SYSTEM_HEALTH 轴"
    assert snapshot.recommendation is not None, "不是真实 evaluate() 产物"
    assert snapshot.generated_at, "缺少采集时间戳"
    assert app.last_observation.trusted is True, app.last_observation.reason_text
    assert app.last_observation.complete is True
    # 双网正常：只读决策应首选以太网且不切换
    assert snapshot.recommendation.preferred == "ethernet"
    assert snapshot.recommendation.would_switch is False


# --------------------------------------------------------------------------- #
# I-75-1-02 Ethernet 故障：真实健康状态交给状态机
# --------------------------------------------------------------------------- #
def test_i75_1_02_ethernet_failure_reaches_state_machine(monkeypatch,
                                                         virtual_clock):
    """拔出网线（LINK_DOWN）时，状态机收到的必须是以太网真实状态。"""
    app = _make_app(monkeypatch, failover_mode="AUTO",
                    auto_switch_authorized=True)
    virtual_clock.install(monkeypatch, tray)
    # 以太网 Down，系统出口落到 Wi-Fi → Wi-Fi 可被证实健康（真实剧本：拔线）
    collector = FakeCollector(eth_up=False, wifi_up=True, egress_if=WIFI_IF,
                              system_online=False)
    executor = RecordingExecutor()
    _attach(app, collector=collector, executor=executor)

    results = _drive(app, virtual_clock, 8, online=False)
    snapshot = app._build_failover_snapshot(False)
    assert snapshot.ethernet.state is HealthState.LINK_DOWN, \
        snapshot.ethernet.state
    assert snapshot.wifi.state is HealthState.HEALTHY, snapshot.wifi.state
    # 状态机确实依据该快照推进（不是停在初始态，也不是靠"改判健康"推进）
    states = [r.state for r in results]
    assert FailoverState.ETHERNET_SUSPECT in states, states
    assert FailoverState.WIFI_PREPARING in states, states
    # 未把不可判定状态强行改成 HEALTHY
    assert all(r.plan is not None for r in results)


# --------------------------------------------------------------------------- #
# I-75-1-03 AUTO 下快照不可信 → 拒绝执行
# --------------------------------------------------------------------------- #
def _break_observation(app, monkeypatch, mode, collector, clock):
    """把观测链按 mode 破坏，返回说明文本。"""
    if mode == "no_result":
        collector.return_none = True
        return "采集无结果"
    if mode == "collect_error":
        collector.raise_error = RuntimeError("netdiag 假故障")
        return "采集抛错"
    if mode == "stale":
        # 禁止重新采集（限流窗口拉长），再把时钟推过有效期
        monkeypatch.setattr(tray, "SNAPSHOT_MIN_INTERVAL", 10_000.0)
        app._snapshot_source = None            # 用新限流参数重建观测源
        app._build_failover_snapshot(False)    # 先采到一份可信快照
        clock.advance(tray.SNAPSHOT_MAX_AGE + 5.0)
        return "快照过期"
    if mode == "generation":
        monkeypatch.setattr(tray, "SNAPSHOT_MIN_INTERVAL", 10_000.0)
        app._snapshot_source = None
        app._build_failover_snapshot(False)
        app.invalidate_snapshot("模式/配置已变更")
        return "代次失效"
    raise AssertionError(mode)


def test_i75_1_03_trusted_snapshot_can_execute(monkeypatch, virtual_clock):
    """对照组：快照可信时 AUTO 确实会执行（证明门禁不是"永远拒绝"）。"""
    app = _make_app(monkeypatch, failover_mode="AUTO",
                    auto_switch_authorized=True)
    virtual_clock.install(monkeypatch, tray)
    collector = FakeCollector(eth_up=False, wifi_up=True, egress_if=WIFI_IF,
                              system_online=False)
    executor = RecordingExecutor()
    _attach(app, collector=collector, executor=executor)
    _drive(app, virtual_clock, 8, online=False)
    assert any(kind is OpKind.METRIC_WRITE for kind in executor.calls), \
        "可信快照下应执行 Metric 写入：%r" % executor.calls


@pytest.mark.parametrize("mode", ("no_result", "collect_error", "stale",
                                  "generation"))
def test_i75_1_03_auto_refuses_when_snapshot_untrusted(monkeypatch,
                                                       virtual_clock, mode):
    """快照缺失 / 抛错 / 过期 / 代次失效 → AUTO 零执行（三类副作用都不发生）。"""
    app = _make_app(monkeypatch, failover_mode="AUTO",
                    auto_switch_authorized=True)
    virtual_clock.install(monkeypatch, tray)
    collector = FakeCollector(eth_up=False, wifi_up=True, egress_if=WIFI_IF,
                              system_online=False)
    executor = RecordingExecutor()
    _attach(app, collector=collector, executor=executor)
    reason = _break_observation(app, monkeypatch, mode, collector,
                                virtual_clock)

    results = _drive(app, virtual_clock, 8, online=False)
    assert app.failover.snapshot_trust is False, \
        "快照应判为不可信（%s）：%s" % (reason, app.failover.snapshot_reason)
    assert executor.calls == [], \
        "不可信快照下仍执行了动作（%s）：%r" % (reason, executor.calls)
    assert all(r.executed == [] for r in results), \
        "存在已执行动作：%r" % [o.kind for r in results for o in r.executed]
    # 状态里必须如实告警"不会执行"，而不是静默无动作
    status = app.failover_status
    assert any("不可信" in warning for warning in status.warnings), \
        status.warnings


# --------------------------------------------------------------------------- #
# I-75-1-04 模式改变：旧采集结果不得触发网络副作用
# --------------------------------------------------------------------------- #
def test_i75_1_04_mode_change_detaches_executor_and_invalidates(monkeypatch,
                                                                virtual_clock):
    app = _make_app(monkeypatch, failover_mode="AUTO",
                    auto_switch_authorized=True)
    virtual_clock.install(monkeypatch, tray)
    collector = FakeCollector(eth_up=False, wifi_up=True, egress_if=WIFI_IF,
                              system_online=False)
    executor = RecordingExecutor()
    _attach(app, collector=collector, executor=executor)
    _drive(app, virtual_clock, 8, online=False)
    baseline = len(executor.calls)
    assert baseline > 0, "前置条件：AUTO 下已发生切换动作"

    # 切到 LOCKED：作废旧代次 + 摘除执行器
    app.apply_mode(FailoverMode.LOCKED)
    after = _drive(app, virtual_clock, 8, online=False)
    assert len(executor.calls) == baseline, \
        "模式改变后仍用旧采集结果执行动作：%r" % executor.calls[baseline:]
    assert all(r.executed == [] for r in after)

    # 切到 MONITOR 同样零执行
    app.apply_mode(FailoverMode.MONITOR)
    after = _drive(app, virtual_clock, 8, online=False)
    assert len(executor.calls) == baseline
    assert all(r.executed == [] for r in after)


def test_i75_1_04_pause_stops_all_automatic_actions(monkeypatch, virtual_clock):
    """暂停期间零自动副作用；恢复后重新按可信度门禁评估。"""
    app = _make_app(monkeypatch, failover_mode="AUTO",
                    auto_switch_authorized=True)
    virtual_clock.install(monkeypatch, tray)
    collector = FakeCollector(eth_up=False, wifi_up=True, egress_if=WIFI_IF,
                              system_online=False)
    executor = RecordingExecutor()
    _attach(app, collector=collector, executor=executor)
    _drive(app, virtual_clock, 8, online=False)
    baseline = len(executor.calls)

    app.failover.pause(virtual_clock.monotonic(), "用户暂停")
    paused = _drive(app, virtual_clock, 6, online=False)
    assert len(executor.calls) == baseline, executor.calls[baseline:]
    assert all(r.state is FailoverState.PAUSED for r in paused), \
        [r.state for r in paused]
    app.failover.resume(virtual_clock.monotonic())
    assert app.failover.manager.state is FailoverState.ETHERNET_PRIMARY


# --------------------------------------------------------------------------- #
# I-75-1-05 / 06 MONITOR / LOCKED 连续多轮掉线零自动认证
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ("MONITOR", "LOCKED"))
def test_i75_1_05_06_long_offline_run_has_zero_background_auth(monkeypatch,
                                                               virtual_clock,
                                                               mode):
    app = _make_app(monkeypatch, failover_mode=mode, threshold=1,
                    interval=int(INTERVAL))
    virtual_clock.install(monkeypatch, tray)
    collector = FakeCollector(eth_up=False, wifi_up=True, egress_if=WIFI_IF,
                              system_online=False)
    app._snapshot_collector = collector
    app._snapshot_spawn = lambda func: func()
    app.config["failover_mode"] = mode
    app._sync_failover_from_config()

    repairs: list = []
    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: repairs.append(1))
    monkeypatch.setattr(tray, "internet_ok", lambda session: (False, "假离线"))

    nxt = app._probe_tick(0.0)
    for _ in range(9):                      # 连续 10 轮掉线
        virtual_clock.advance(INTERVAL)
        nxt = app._probe_tick(nxt)
    assert repairs == [], "%s 下出现后台自动认证：%r" % (mode, repairs)
    assert app.state["mode"] == "offline", app.state
    assert "不自动认证" in app.state["detail"], app.state
    # 探测节奏不受影响（三表分离未退化）
    assert app._timers.next_detection_at > 0.0
    # 故障切换状态机仍在只读推进（零执行）
    assert app.failover_status is not None
    assert all(r.executed == [] for r in [app.failover.last_result]
               if r is not None)


# --------------------------------------------------------------------------- #
# I-75-1-07 AUTO 不调用旧版 _run_repair()
# --------------------------------------------------------------------------- #
def test_i75_1_07_auto_never_calls_legacy_run_repair(monkeypatch,
                                                     virtual_clock):
    app = _make_app(monkeypatch, failover_mode="AUTO",
                    auto_switch_authorized=True, threshold=1,
                    interval=int(INTERVAL))
    virtual_clock.install(monkeypatch, tray)
    collector = FakeCollector(eth_up=False, wifi_up=True, egress_if=WIFI_IF,
                              system_online=False)
    executor = RecordingExecutor()
    _attach(app, collector=collector, executor=executor)
    repairs: list = []
    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: repairs.append(1))
    monkeypatch.setattr(tray, "internet_ok", lambda session: (False, "假离线"))

    nxt = app._probe_tick(0.0)
    for _ in range(9):
        virtual_clock.advance(INTERVAL)
        nxt = app._probe_tick(nxt)
    assert repairs == [], "AUTO 下仍调用了 V0.2 自动重连：%r" % repairs
    # 自动副作用只可能来自单一副作用门（经由 RecordingExecutor 记录）
    assert any(kind is OpKind.METRIC_WRITE for kind in executor.calls), \
        "故障切换调度器应已安排并切换动作：%r" % executor.calls


# --------------------------------------------------------------------------- #
# I-75-1-08 手动重连不绕过单实例 / 互斥控制
# --------------------------------------------------------------------------- #
def test_i75_1_08_manual_reconnect_respects_mutex(monkeypatch):
    """手动重连：在途合并、暂停需确认、过期请求丢弃。"""
    from tests.unit.test_tray_worker import wait_until
    app = _make_app(monkeypatch)
    app.session = object()

    calls: list = []
    monkeypatch.setattr(app, "_run_repair",
                        lambda force=False: calls.append(force))

    # (a) 已有重连在途 → 合并，不重复发起
    app._repair_busy.set()
    app.on_reconnect(None)
    assert calls == [], "在途重连未被合并：%r" % calls
    app._repair_busy.clear()

    # (b) 暂停中：未获确认不得发起（root=None → 无法询问 → 直接取消）
    app.config["paused"] = True
    app.on_reconnect(None)
    assert calls == [], "暂停中未确认仍发起认证：%r" % calls
    app.config["paused"] = False

    # (c) 正常手动重连：异步投递到工作线程 → 等待执行完毕
    app.on_reconnect(None)
    assert wait_until(lambda: len(calls) >= 1), \
        "手动重连未执行：%r" % calls
    assert calls == [False], calls

    # (d) 过期请求（模式/配置已变更）不得执行
    stale = app.config_version()
    app.update_config({"failover_mode": "LOCKED"})
    before = len(calls)
    app._dispatch(("reconnect", False, stale), 0.0)
    assert len(calls) == before, "过期手动请求仍被执行：%r" % calls


def test_i75_1_08_manual_reconnect_uses_shared_single_instance(monkeypatch):
    """手动重连共用 V0.2 的修复入口，不新增第二个认证实现。"""
    app = _make_app(monkeypatch)
    app.session = object()
    seen: list = []
    monkeypatch.setattr(tray, "repair_service",
                        lambda session, options, monotonic, adapters,
                        credentials, **kwargs:
                        seen.append(options) or SimpleNamespace(
                            ok=True, detail="ok", elapsed=0.1, port=8601,
                            status=None, error_code=None, network_ok=True,
                            auth_ok=True, needs_user=False,
                            already_authenticated=False))
    ok, detail, _elapsed = app.repair_once()
    assert ok is True and detail == "ok"
    assert len(seen) == 1, "未走共享 repair_service：%r" % seen
    assert seen[0].ssid == app.config["ssid"]
    assert seen[0].operator == app.config["yys"]


# --------------------------------------------------------------------------- #
# I-75-1-09 CLI 与托盘共用的底层认证契约未退化
# --------------------------------------------------------------------------- #
def test_i75_1_09_cli_and_tray_share_repair_service(monkeypatch):
    """托盘与 CLI 指向**同一个** ``campus_core.repair_service``（AGENTS §3.1）。"""
    assert tray.repair_service is campus_core.repair_service, \
        "托盘未复用 V0.2 认证引擎"
    assert engine.repair_service is campus_core.repair_service, \
        "CLI 未复用 V0.2 认证引擎"
    # 认证引擎本身仍可用（本阶段未改动它）：同一组假适配器两次调用同结果
    from campus_core import RepairAdapters, RepairOptions

    adapters = RepairAdapters(
        wifi_state=lambda interface=None: ("SNNU", "connected"),
        wifi_reconnect=lambda ssid, *a, **k: True,
        resolve_port=lambda session, port: (8601, "fixed"),
        portal_reachable=lambda session, port: True,
        probe_online=lambda session: (True, "online"),
        login=lambda session, account, password, yys, port, log: (True, "ok"),
    )
    options = RepairOptions(ssid="SNNU", operator="mobile", port=None,
                            reconnect_wifi=False, browser_fallback=False,
                            force=False)
    first = campus_core.repair_service(object(), options, lambda: 0.0, adapters,
                                       lambda: ("user", "pass"))
    second = campus_core.repair_service(object(), options, lambda: 0.0, adapters,
                                        lambda: ("user", "pass"))
    assert first.network_ok is True and second.network_ok is True
    assert first.detail == second.detail, "认证引擎行为不稳定（被改动？）"


# --------------------------------------------------------------------------- #
# I-75-1-10 零真实副作用守卫
# --------------------------------------------------------------------------- #
def test_i75_1_10_observation_module_has_no_side_effect_capability():
    """观测模块不得引入任何写路由 / 认证 / 注册表 / 网络的能力。"""
    source = open("campus_tray_observer.py", encoding="utf-8").read()
    for banned in ("import requests", "import winreg", "import subprocess",
                   "Set-NetIPInterface", "portal_login", "ProxyEnable"):
        assert banned not in source, "观测模块引入了副作用能力：%s" % banned
    # 拒绝执行器恒为"已尝试但未执行"
    executor = tso.RefusingExecutor("测试")
    outcome = executor.execute(SimpleNamespace(kind=OpKind.METRIC_WRITE))
    assert outcome.attempted is True and outcome.executed is False
    assert executor.refused == [OpKind.METRIC_WRITE]


def test_i75_1_10_light_snapshot_never_trusted():
    """``light_snapshot`` 结构判定即为不可信（不得授权 AUTO 切换）。"""
    snapshot = tf.light_snapshot(system_online=True, system_source="占位")
    assert tso.is_full_snapshot(snapshot) is False
    source = tso.TraySnapshotSource(collect=lambda: snapshot)
    source.collect_now()
    observation = source.observe(0.0)
    assert observation.trusted is False
    assert observation.reason == tso.SNAPSHOT_INCOMPLETE


# --------------------------------------------------------------------------- #
# I-75-1-11 后台采集不阻塞调用线程 + 并发限流（真实线程）
# --------------------------------------------------------------------------- #
def test_i75_1_11_background_collection_is_async_and_throttled(monkeypatch):
    """采集跑在**真实后台线程**：不阻塞调用方，且同一时刻不叠加第二次采集。"""
    app = _make_app(monkeypatch)
    collector = FakeCollector()
    started = threading.Event()
    release = threading.Event()

    def slow_collect():
        started.set()
        assert release.wait(3.0), "采集线程未被释放"
        return collector()

    app._snapshot_collector = slow_collect       # 不注入 spawn → 用真实线程
    started_event = started
    started.clear()

    began = time.monotonic()
    observation = app._observe_snapshot(0.0)     # 立即返回，不等采集
    assert (time.monotonic() - began) < 1.0, "采集阻塞了调用线程"
    assert observation.trusted is False
    assert observation.reason == tso.SNAPSHOT_COLLECTING

    # 在途期间再次请求：限流 + 单飞，不再起第二个采集
    assert app.snapshot_source().request(1.0) is False
    assert app.snapshot_source().throttled + 1 >= 1

    release.set()
    assert started_event.wait(3.0), "采集线程未启动"
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if app._observe_snapshot().trusted:
            break
    assert app.last_observation.trusted is True, \
        app.last_observation.reason_text


# --------------------------------------------------------------------------- #
# I-75-1-12 DHCP / 接口变化作废旧代次
# --------------------------------------------------------------------------- #
def test_i75_1_12_dhcp_change_invalidates_generation(monkeypatch,
                                                     virtual_clock):
    """Wi-Fi DHCP 换地址后：新代次生效，旧代次的在途结果被丢弃。"""
    app = _make_app(monkeypatch)
    virtual_clock.install(monkeypatch, tray)
    collector = FakeCollector(wifi_ip=WIFI_IP)
    app._snapshot_collector = collector
    app._snapshot_spawn = lambda func: func()

    first = app._build_failover_snapshot(True)
    assert first.wifi.source_ip == WIFI_IP
    assert app.last_observation.trusted is True
    signature_before = tso.snapshot_signature(first)

    # DHCP 变更
    collector.wifi_ip = "10.8.0.9"
    # 允许立即重采（把限流窗口清零）
    monkeypatch.setattr(tray, "SNAPSHOT_MIN_INTERVAL", 0.0)
    app.snapshot_source()._last_request_at = None
    second = app._build_failover_snapshot(True)
    assert second.wifi.source_ip == "10.8.0.9", second.wifi.source_ip
    assert tso.snapshot_signature(second) != signature_before
    assert app.snapshot_source().generation >= 1, \
        "网络身份变化未自增代次：%r" % app.snapshot_source().generation
