# -*- coding: utf-8 -*-
"""V0.5-4 双链路恢复整合（状态机 × 接口受限验证 × 恢复协调器 × UU 恢复）。

方案 §4（V0.5-4 出口条件）：**20 场景全绿；无 route/auth/UU storm**。

本文件把「真实 ``NetworkFailoverManager`` + 真实 ``RecoveryCoordinator`` +
真实 ``VerifiedActionGate`` + 真实 ``restart_after_verified_recovery``」接成一条
流水线，用**假件**提供三轴快照与探测能力（零真实网络、零真实进程、零真实 sleep）：

- ``I01..I20``：方案 §4 的 20 个整合场景（单链路 / 双链路 / standby / failover /
  Portal / 假死 / 切回 / 切回失败回滚 / 双断 / 重复失败恢复 / UU 去重冷却 /
  PROBE_UNRELIABLE / 快照不可信）；
- ``G01..G06``：验证前置门（``VerifiedActionGate``）自身的契约；
- ``T01..T04``：托盘接线（默认关闭、门禁已装入单一副作用门、快照不可信零动作）。

核心不变量（每个场景都在守护它）：

    Connected ≠ Internet
    verify(Ethernet) → 只走 Ethernet socket；verify(WiFi) → 只走 Wi-Fi socket
    没有「新鲜的接口受限验证通过」⇒ 不切路由；没有恢复事件 + 验证通过 ⇒ 不重启 UU
    fail-closed：PROBE_UNRELIABLE / 快照不可信 ⇒ 不切换、不重启、不风暴
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, NamedTuple, Optional

import campus_dual_path as dp
import campus_reconnect as cr
import campus_reconnect_tray as tray
import campus_remote_app as cra
from campus_failover import (FailoverMode, FailoverPolicy, FailoverState,
                             NetworkFailoverManager, OpKind, OpRequest,
                             TickResult)
from campus_network import (EgressInfo, HealthAxis, HealthState, InterfaceHealth,
                            NetworkHealthSnapshot, Recommendation, SystemHealth)
from campus_recovery import PATH_ETHERNET, PATH_WIFI
from tests.fakes import (FakeOpExecutor, FakePathEngine, FakeRemoteAppOps,
                         FakeResponse, FakeSession)
from tests.unit.test_tray_worker import wait_until

LOG = logging.getLogger("campus")

H = HealthState.HEALTHY
D = HealthState.LINK_DOWN
C = HealthState.CAPTIVE_PORTAL
IU = HealthState.INTERNET_UNREACHABLE
PU = HealthState.PROBE_UNRELIABLE

ETH_IF, WIFI_IF = 12, 14
ETH_IP, WIFI_IP = "192.168.1.20", "10.20.30.40"
ETH_DNS, WIFI_DNS = ("192.168.1.1",), ("10.20.30.1",)
STEP = 5.0

UU_PROCESSES = [
    {"pid": 100, "process_name": "GameViewer",
     "path": r"C:\_study\GameViewer\GameViewer.exe"},
    {"pid": 200, "process_name": "GameViewerLauncher",
     "path": r"C:\_study\GameViewer\GameViewerLauncher.exe"},
    {"pid": 300, "process_name": "GameViewerServer",
     "path": r"C:\_study\GameViewer\GameViewerServer.exe"},
]
UU_RUN_ENTRIES = [{"name": "GameViewer",
                   "command": r'"C:\_study\GameViewer\GameViewer.exe" --auto-run'}]


# --------------------------------------------------------------------------- #
# 快照夹具
# --------------------------------------------------------------------------- #
def _health(axis: HealthAxis, state: HealthState, if_index: int,
            source_ip: str, dns, interface: str) -> InterfaceHealth:
    return InterfaceHealth(axis=axis, interface=interface, if_index=if_index,
                           state=state, source_ip=source_ip,
                           dns_servers=list(dns))


def snapshot(eth: Optional[HealthState] = H, wifi: Optional[HealthState] = D,
             egress_if: Optional[int] = ETH_IF, *,
             eth_ip: str = ETH_IP, wifi_ip: str = WIFI_IP,
             eth_dns=ETH_DNS, wifi_dns=WIFI_DNS) -> NetworkHealthSnapshot:
    """构造完整三轴快照（ifIndex 固定为以太网 12 / Wi-Fi 14）。"""
    ethernet = None if eth is None else _health(
        HealthAxis.ETHERNET_HEALTH, eth, ETH_IF, eth_ip, eth_dns, "以太网")
    wifi = None if wifi is None else _health(
        HealthAxis.WIFI_HEALTH, wifi, WIFI_IF, wifi_ip, wifi_dns, "WLAN")
    egress = None if egress_if is None else EgressInfo(
        target="1.1.1.1", source_ip=eth_ip, if_index=egress_if)
    return NetworkHealthSnapshot(
        generated_at="2026-10-16T00:00:00+08:00", host="test-host",
        ethernet=ethernet, wifi=wifi,
        system=SystemHealth(online=egress_if is not None, source="fake",
                            egress=egress),
        recommendation=Recommendation(preferred=None, reason="test",
                                      would_switch=False))


def _title_page(spec: Any) -> FakeResponse:
    """跟随重定向后的 200 页面（正文含该探测点的期望标题），与 V0.5-1 同构。"""
    body = ("<HTML><HEAD><TITLE>%s</TITLE></HEAD><BODY>%s</BODY></HTML>"
            % (spec.expect, spec.expect))
    return FakeResponse(200, body, url=spec.url)


# --------------------------------------------------------------------------- #
# 接口受限验证假件（零真实网络）
# --------------------------------------------------------------------------- #
class ScriptedVerifier:
    """按路径脚本化的「接口受限验证」假件：记录请求、返回可编排结论。

    支持的编排：

    - :meth:`script`：给某条路径排一个结论序列（用完之后沿用最后一个）；
    - :meth:`fail`：把某条路径设为「验证未通过」；
    - ``gate_ok``：轻量闸门确认（``verify_gate``）的结论。
    """

    def __init__(self) -> None:
        self.calls: List[Any] = []
        self.gate_calls: List[Any] = []
        self._pending: Dict[str, List[Dict[str, Any]]] = {}
        self._default = {"verified": True, "reliability": cr.PATH_OK,
                         "reason": "接口受限验证通过（百度闸门 + 交叉 2/3）"}
        self.gate_ok = True
        self.gate_reason = "活动路径仍可用（接口受限轻量确认：闸门 PASS）"

    # ---- 编排 ---- #
    def script(self, role: str, *steps: Dict[str, Any]) -> None:
        self._pending[role] = [dict(step) for step in steps]

    def fail(self, role: str, *, reliability: str = cr.PATH_OK,
             reason: str = "闸门未通过（www.baidu.com(timeout)）") -> None:
        self.script(role, {"verified": False, "reliability": reliability,
                           "reason": reason})

    @property
    def roles(self) -> List[str]:
        return [str(request.role) for request in self.calls]

    # ---- 能力 ---- #
    def _spec(self, role: str) -> Dict[str, Any]:
        spec = dict(self._default)
        steps = self._pending.get(role)
        if steps:
            spec.update(steps[0] if len(steps) == 1 else steps.pop(0))
        return spec

    def verify(self, request: Any) -> cr.PathVerificationResult:
        self.calls.append(request)
        spec = self._spec(request.role)
        ok = bool(spec["verified"]) and spec["reliability"] == cr.PATH_OK
        return cr.PathVerificationResult(
            verified=bool(spec["verified"]), role=request.role,
            interface_index=request.interface_index, source_ip=request.source_ip,
            reason=str(spec.get("reason", "")),
            reliability=str(spec.get("reliability", cr.PATH_OK)),
            successful_sites=("www.baidu.com",) if ok else (),
            failed_sites=() if ok else ("www.baidu.com",))

    def verify_gate(self, request: Any) -> cr.PathVerificationResult:
        self.gate_calls.append(request)
        return cr.PathVerificationResult(
            verified=bool(self.gate_ok), role=request.role,
            interface_index=request.interface_index, source_ip=request.source_ip,
            reason=self.gate_reason, reliability=cr.PATH_OK)


# --------------------------------------------------------------------------- #
# 离线整合台架
# --------------------------------------------------------------------------- #
class Step(NamedTuple):
    """一轮整合推进的三段结果。"""

    result: TickResult                     # 状态机
    tick: dp.DualPathTick                  # 整合判定
    follow: Optional[dp.DualPathTick]      # 验证结果回灌后的跟进判定


class Rig:
    """离线整合台架：真实状态机 + 真实协调器 + 真实门禁 + 真实 UU 恢复。

    ``step`` 复刻托盘的真实时序：**先**推进状态机（``manager.tick``），**再**让整合层
    判定（``controller.tick``），**最后**在"后台线程"里跑接口受限验证并把结果回灌
    （``controller.on_verification`` → ``restart_after_verified_recovery``）。
    """

    def __init__(self, *, mode: FailoverMode = FailoverMode.AUTO,
                 policy: Optional[FailoverPolicy] = None,
                 verifier: Optional[ScriptedVerifier] = None,
                 executor: Any = None, uu_enabled: bool = True,
                 uu_cooldown: float = 300.0, max_age: float = 60.0,
                 retry_interval: float = 30.0) -> None:
        self.now = 0.0
        self.uu_enabled = bool(uu_enabled)
        self.verifier = verifier if verifier is not None else ScriptedVerifier()
        self.executor = executor if executor is not None else FakeOpExecutor()
        self.gate = dp.VerifiedActionGate(
            mode=mode, executor=self.executor, max_age=max_age,
            clock=lambda: self.now)
        self.controller = dp.DualPathRecoveryController(
            verifier=self.verifier.verify,
            gate_verifier=self.verifier.verify_gate, gate=self.gate,
            uu_restart_enabled=lambda: self.uu_enabled, clock=lambda: self.now,
            verification_max_age=max_age,
            verification_retry_interval=retry_interval)
        self.manager = NetworkFailoverManager(
            policy=policy or FailoverPolicy(), mode=mode, scheduler=self.gate)
        self.uu_ops = FakeRemoteAppOps(processes=UU_PROCESSES,
                                       service={"name": "GameViewerService"},
                                       run_entries=UU_RUN_ENTRIES)
        self.uu_guard = cra.RestartGuard(cooldown=uu_cooldown)
        self.ledger = cra.RecoveryRestartLedger()
        self.uu_results: List[cra.UuRecoveryResult] = []
        self.steps: List[Step] = []

    # ---- 推进 ---- #
    def step(self, eth: Optional[HealthState] = H, wifi: Optional[HealthState] = D,
             egress_if: Optional[int] = ETH_IF, *, dt: float = STEP,
             trusted: bool = True, **snapshot_kwargs: Any) -> Step:
        self.now += float(dt)
        snap = snapshot(eth, wifi, egress_if, **snapshot_kwargs)
        result = self.manager.tick(self.now, snap)
        tick = self.controller.tick(result, snap, trusted=trusted, now=self.now)
        follow = None
        for request in tick.verify_requests:
            follow = self._consume(request)
        step = Step(result, tick, follow)
        self.steps.append(step)
        return step

    def _consume(self, request: Any) -> dp.DualPathTick:
        """模拟托盘的「后台验证 + 验证通过后清退 UU」两步。"""
        verification = self.verifier.verify(request)
        follow = self.controller.on_verification(verification, now=self.now)
        if follow.uu_request is not None and follow.uu_enabled:
            outcome = cra.restart_after_verified_recovery(
                self.uu_ops, active_path=follow.uu_request.active_path,
                network_epoch=follow.uu_request.network_epoch,
                verification=verification, reason=follow.uu_request.reason,
                enabled=True, guard=self.uu_guard, ledger=self.ledger,
                recheck=self.controller.build_recheck(
                    follow.uu_request.active_path),
                now=self.now, sleep=lambda seconds: None)
            self.controller.note_uu_outcome(bool(outcome.restarted),
                                           detail=outcome.detail)
            self.uu_results.append(outcome)
        return follow

    def feed(self, verification: Any) -> dp.DualPathTick:
        """把一份外部构造的验证结果直接回灌整合层（用于去重/冷却场景）。"""
        return self.controller.on_verification(verification, now=self.now)

    # ---- 断言辅助 ---- #
    @property
    def executed(self) -> List[Any]:
        """**真实交给执行器**的动作请求（被门禁拦下的不会出现在这里）。"""
        return list(self.executor.requests)

    def executed_kinds(self, kind: OpKind) -> List[Any]:
        return [r for r in self.executor.requests if r.kind is kind]

    def planned_kinds(self, kind: OpKind) -> List[Any]:
        return [r for step in self.steps for r in step.result.plan
                if r.kind is kind]


def drive_to_backup(rig: Rig, *, wifi: HealthState = H,
                    dt: float = STEP) -> Step:
    """把台架驱动到 Wi-Fi 接管完成（含切换前验证与门禁放行）。

    帧序：基线 → 有线连续可靠失败 3 次 → 疑似异常 → 准备备用链路（验证 Wi-Fi）
    → 执行切换 → 出口变为 Wi-Fi ⇒ ``WIFI_BACKUP_ACTIVE``。
    """
    rig.step(H, wifi, dt=dt)
    for _ in range(3):
        rig.step(IU, wifi, dt=dt)
    rig.step(IU, wifi, dt=dt)                     # → WIFI_PREPARING（并验证 Wi-Fi）
    rig.step(IU, wifi, dt=dt)                     # → 切换已执行 → WIFI_SWITCHING
    return rig.step(IU, wifi, WIFI_IF, dt=dt)     # → 出口已改为 Wi-Fi → 接管完成


def pump(rig: Rig, eth: Optional[HealthState], wifi: Optional[HealthState],
         egress_if: Optional[int], frames: int, *, dt: float = STEP) -> Step:
    step: Optional[Step] = None
    for _ in range(frames):
        step = rig.step(eth, wifi, egress_if, dt=dt)
    assert step is not None
    return step


# --------------------------------------------------------------------------- #
# I01–I04：单链路 / 双链路 / standby / 假死
# --------------------------------------------------------------------------- #
def test_i01_single_path_ethernet_is_verified_once_and_never_acts():
    """单链路（仅以太网）：连接成功即验证一次；不切路由、不重启 UU、不重复验证。"""
    rig = Rig()
    first = rig.step(H, D)
    assert first.result.state is FailoverState.ETHERNET_PRIMARY
    assert [r.role for r in first.tick.verify_requests] == [PATH_ETHERNET]

    again = rig.step(H, D)
    assert again.tick.verify_requests == (), "同一网络世代不应重复验证"
    assert rig.verifier.roles == [PATH_ETHERNET]
    assert rig.executor.requests == []
    assert rig.uu_results == [] and rig.uu_ops.killed == []


def test_i02_single_path_outage_goes_offline_without_any_destructive_action():
    """单链路（以太网故障、Wi-Fi 不可用）：双断；不切路由、不认证、不重启 UU。"""
    rig = Rig()
    rig.step(H, D)
    for _ in range(3):
        rig.step(IU, D)
    step = rig.step(IU, D)
    assert step.result.state is FailoverState.BOTH_OFFLINE

    pump(rig, IU, D, ETH_IF, 12, dt=15.0)
    assert rig.manager.state is FailoverState.BOTH_OFFLINE
    assert rig.executed_kinds(OpKind.METRIC_WRITE) == []
    assert rig.executed_kinds(OpKind.PORTAL_POST) == []
    assert rig.uu_results == [] and rig.uu_ops.killed == []


def test_i03_standby_connected_is_verified_but_never_switches_or_restarts_uu():
    """双链路：备用链路刚连上 ⇒ 只验证；不切路由、不重启 UU（方案 §30）。"""
    rig = Rig()
    rig.step(H, D)
    step = rig.step(H, H)
    assert step.result.state is FailoverState.ETHERNET_PRIMARY
    assert [r.role for r in step.tick.verify_requests] == [PATH_WIFI]
    assert step.follow is not None
    assert step.follow.uu_request is None, "备用链路连接不得触发 UU 重启"
    assert step.follow.event is not None
    assert rig.executor.requests == []
    assert rig.uu_ops.killed == []


def test_i04_fake_alive_wifi_connected_but_no_internet_never_switches():
    """假死连通：Wi-Fi「已连接」但上不了网 ⇒ 验证不通过 ⇒ 不切换、不重启 UU。"""
    rig = Rig()
    rig.step(H, D)
    rig.verifier.fail(PATH_WIFI, reason="闸门未通过（www.baidu.com(timeout)）")

    step = rig.step(H, IU)          # Wi-Fi 有链路有地址，但 Internet 不通
    assert [r.role for r in step.tick.verify_requests] == [PATH_WIFI]
    assert step.follow is not None
    assert step.follow.verification is not None
    assert step.follow.verification.allows_recovery is False
    assert step.follow.uu_request is None
    assert rig.executor.requests == [], "假死不得触发任何路由变更"
    assert rig.uu_ops.killed == []


# --------------------------------------------------------------------------- #
# I05–I08：failover（含 Portal）与失败如实上报
# --------------------------------------------------------------------------- #
def test_i05_failover_completed_restarts_uu_exactly_once():
    """故障切换完成（出口已改为 Wi-Fi）⇒ 验证通过 ⇒ 清退重启一次 UU。"""
    rig = Rig()
    step = drive_to_backup(rig)

    assert step.result.state is FailoverState.WIFI_BACKUP_ACTIVE
    assert step.result.previous_state is FailoverState.WIFI_SWITCHING
    assert step.tick.event is not None and step.tick.event.value == "FAILOVER_COMPLETED"
    metrics = rig.executed_kinds(OpKind.METRIC_WRITE)
    assert [r.payload["target"] for r in metrics] == ["wifi"]

    assert len(rig.uu_results) == 1
    outcome = rig.uu_results[0]
    assert outcome.restarted is True and outcome.alive is True
    assert outcome.active_path == PATH_WIFI
    # 只清退**用户态**进程，不动 GameViewerService（需要 UAC）
    assert 100 in rig.uu_ops.killed and 200 in rig.uu_ops.killed
    assert 300 not in rig.uu_ops.killed
    assert rig.ledger.restarts == 1


def test_i06_failover_blocked_when_target_path_not_verified():
    """切换到 Wi-Fi 前必须先证实 Wi-Fi 能真上网：验证未通过 ⇒ 门禁拒绝切换。"""
    rig = Rig()
    rig.step(H, D)
    rig.verifier.fail(PATH_WIFI)
    for _ in range(3):
        rig.step(IU, D)
    rig.step(IU, H)                        # 疑似异常 + 无线可用 → 准备备用链路
    step = rig.step(IU, H)                 # 想切换，但目标路径未通过验证

    assert step.result.state is FailoverState.WIFI_PREPARING
    assert rig.executor.requests == [], "目标路径未证实 ⇒ 一个路由动作都不执行"
    assert step.result.executed == []
    assert rig.gate.gate_blocked, "应留下『验证前置门拦下』的审计记录"
    assert "验证未通过" in rig.gate.gate_blocked[-1][1]
    assert rig.uu_ops.killed == []


def test_i07_portal_auth_then_verified_then_switch_and_restart():
    """Wi-Fi 需 Portal 认证：认证 → 重新验证通过 → 切换 → 重启 UU（真实时序）。"""
    rig = Rig(retry_interval=30.0)
    rig.step(H, D)
    for _ in range(3):
        rig.step(IU, D)
    # 第 1 次验证：Portal 拦截 ⇒ 未通过（此时**不**允许切路由）
    rig.verifier.script(PATH_WIFI,
                        {"verified": False, "reliability": cr.PATH_OK,
                         "reason": "CAPTIVE_PORTAL：疑似校园网门户劫持（闸门未通过）"},
                        {"verified": True, "reliability": cr.PATH_OK,
                         "reason": "接口受限验证通过（Portal 后：闸门 + 交叉 2/3）"})
    step = rig.step(IU, C)                 # 确认有线故障 + 无线需认证 → 准备备用链路
    assert step.result.state is FailoverState.WIFI_PREPARING
    auth = rig.step(IU, C)                 # Portal 认证（经 SNNU 接口）
    assert rig.executed_kinds(OpKind.PORTAL_POST), "应发起 Portal 认证"
    assert auth.result.state is FailoverState.WIFI_AUTHENTICATING
    assert rig.executed_kinds(OpKind.METRIC_WRITE) == [], "认证前不得切路由"

    # 认证成功后每轮都尝试切换；验证限速 30s，窗口一到就重新验证并放行
    for _ in range(12):
        if rig.executed_kinds(OpKind.METRIC_WRITE):
            break
        rig.step(IU, H, ETH_IF, dt=15.0)
    assert [r.payload["target"] for r in rig.executed_kinds(OpKind.METRIC_WRITE)] \
        == ["wifi"], "重新验证通过后**才**放行切换"
    assert rig.manager.state is FailoverState.WIFI_SWITCHING

    final = rig.step(IU, H, WIFI_IF, dt=STEP)      # 出口已改为 Wi-Fi → 接管完成
    assert final.result.state is FailoverState.WIFI_BACKUP_ACTIVE
    assert len(rig.uu_results) == 1
    assert rig.uu_results[-1].active_path == PATH_WIFI
    assert rig.uu_results[-1].restarted is True


def test_i08_portal_failure_is_rate_limited_and_never_storms():
    """Portal 认证失败：受认证冷却限频（不风暴），不切路由、不重启 UU。"""
    rig = Rig()
    rig.step(H, D)
    rig.executor.results[OpKind.PORTAL_POST] = False
    for _ in range(3):
        rig.step(IU, D)
    rig.step(IU, C)
    pump(rig, IU, C, ETH_IF, 15, dt=15.0)

    attempts = rig.executed_kinds(OpKind.PORTAL_POST)
    assert 1 <= len(attempts) <= 2, "认证冷却（180s）内不得反复认证：%d 次" % len(attempts)
    assert rig.executed_kinds(OpKind.METRIC_WRITE) == []
    assert rig.uu_results == []
    assert rig.manager.state in (FailoverState.WIFI_PREPARING,
                                 FailoverState.MANUAL_REQUIRED)


# --------------------------------------------------------------------------- #
# I09–I13：切回（failback）与执行失败
# --------------------------------------------------------------------------- #
def test_i09_failback_after_ethernet_stable_restarts_uu():
    """有线恢复稳定 ⇒ 切回以太网（先重新验证）⇒ 切回完成 ⇒ UU 重启一次。"""
    rig = Rig()
    drive_to_backup(rig)
    assert len(rig.uu_results) == 1

    rig.step(H, H, WIFI_IF, dt=400.0)              # 有线恢复 → 恢复观察期
    assert rig.manager.state is FailoverState.ETHERNET_RECOVERY_CHECK
    for _ in range(30):
        rig.step(H, H, WIFI_IF, dt=30.0)
        if rig.manager.state is FailoverState.ETHERNET_FAILBACK:
            break
    assert rig.manager.state is FailoverState.ETHERNET_FAILBACK

    step = rig.step(H, H, ETH_IF, dt=6.0)          # 出口已回到以太网
    assert step.result.state is FailoverState.ETHERNET_PRIMARY
    assert step.result.previous_state is FailoverState.ETHERNET_FAILBACK
    assert step.tick.event is not None and step.tick.event.value == "FAILBACK_COMPLETED"

    metrics = [r.payload["target"] for r in rig.executed_kinds(OpKind.METRIC_WRITE)]
    assert metrics == ["wifi", "ethernet"], "只允许一次切换 + 一次切回（无风暴）"
    assert len(rig.uu_results) == 2
    assert rig.uu_results[-1].restarted is True
    assert rig.uu_results[-1].active_path == PATH_ETHERNET


def test_i10_failback_blocked_when_ethernet_verification_fails():
    """有线恢复但接口受限验证失败 ⇒ **即使 link=Connected 也不切回**。"""
    rig = Rig()
    drive_to_backup(rig)
    rig.verifier.fail(PATH_ETHERNET, reason="闸门未通过（www.baidu.com(timeout)）")

    rig.step(H, H, WIFI_IF, dt=400.0)
    pump(rig, H, H, WIFI_IF, 12, dt=30.0)

    assert rig.manager.state is FailoverState.ETHERNET_RECOVERY_CHECK
    assert [r.payload["target"] for r in rig.executed_kinds(OpKind.METRIC_WRITE)] \
        == ["wifi"], "验证未通过 ⇒ 不得切回"
    assert len(rig.uu_results) == 1, "切回未发生 ⇒ 不得新增 UU 重启"
    assert rig.gate.gate_blocked


def test_i11_unstable_ethernet_recovery_resets_window_without_uu_storm():
    """有线恢复后再次失败 ⇒ 回到 Wi-Fi 接管并重置观察窗口；不得重复重启 UU。"""
    rig = Rig()
    drive_to_backup(rig)
    rig.step(H, H, WIFI_IF, dt=400.0)               # 进入恢复观察期
    rig.step(H, H, WIFI_IF, dt=30.0)                # 观察 1 次成功
    assert rig.manager.recovery_successes >= 1

    step = rig.step(IU, H, WIFI_IF, dt=30.0)        # 有线再次失败（无线仍可用）
    assert step.result.state is FailoverState.WIFI_BACKUP_ACTIVE
    assert rig.manager.recovery_successes == 0, "观察窗口必须重置"
    assert step.tick.uu_request is None, "回到接管不得授权 UU"
    assert len(rig.uu_results) == 1, "不得因抖动重复重启 UU"


def test_i12_switch_execution_failure_is_reported_and_never_lies():
    """切换动作执行失败 ⇒ 状态机不谎报已切换（停在准备态），如实记录未执行。"""
    rig = Rig(executor=FakeOpExecutor(results={OpKind.METRIC_WRITE: False}))
    rig.step(H, H)
    for _ in range(3):
        rig.step(IU, H)
    rig.step(IU, H)                                 # → WIFI_PREPARING（并验证 Wi-Fi）
    step = rig.step(IU, H)                          # 门禁放行，但执行器失败

    assert step.result.state is FailoverState.WIFI_PREPARING
    assert step.result.executed == []
    assert any("未执行" in note for note in step.result.notes)
    assert rig.uu_results == []
    assert rig.manager.state is FailoverState.WIFI_PREPARING


def test_i13_rollback_failure_is_reported_without_storm():
    """切换验证期间有线恢复：撤销切换（回退）被执行；回退失败如实记录、不风暴。"""
    rig = Rig(executor=FakeOpExecutor(
        results={OpKind.METRIC_WRITE: [True, False]}))
    rig.step(H, H)
    for _ in range(3):
        rig.step(IU, H)
    rig.step(IU, H)                                 # → WIFI_PREPARING
    rig.step(IU, H)                                 # 切换执行 → WIFI_SWITCHING
    assert rig.manager.state is FailoverState.WIFI_SWITCHING

    step = rig.step(H, H)                           # 有线恢复 → 撤销切换（执行失败）
    targets = [r.payload["target"] for r in rig.executed_kinds(OpKind.METRIC_WRITE)]
    assert targets == ["wifi", "ethernet"], "必须尝试回退以太网"
    assert step.result.executed == [], "回退失败不得谎报已执行"
    assert rig.uu_ops.killed == [] or len(rig.uu_results) <= 1


# --------------------------------------------------------------------------- #
# I14–I17：双断、重复失败/恢复、去重
# --------------------------------------------------------------------------- #
def test_i14_both_paths_down_never_storms():
    """双断：不发生无限认证、不反复改路由、不杀 UU；重连尝试按冷却限频。"""
    rig = Rig()
    rig.step(H, IU)
    for _ in range(3):
        rig.step(IU, IU)
    rig.step(IU, IU)
    assert rig.manager.state is FailoverState.BOTH_OFFLINE

    pump(rig, IU, IU, ETH_IF, 12, dt=15.0)          # 180s 窗口
    reconnects = rig.executed_kinds(OpKind.WIFI_RECONNECT)
    assert 1 <= len(reconnects) <= 4, "重连必须限频：%d 次 / 180s" % len(reconnects)
    assert rig.executed_kinds(OpKind.METRIC_WRITE) == []
    assert rig.executed_kinds(OpKind.PORTAL_POST) == []
    assert rig.uu_results == [] and rig.uu_ops.killed == []


def test_i15_repeated_failures_enter_suspect_once():
    """有线连续可靠失败多帧：只进入一次疑似异常，不反复切换。"""
    rig = Rig()
    rig.step(H, H)
    states = []
    for _ in range(6):
        states.append(rig.step(IU, H).result.state)
    assert states.count(FailoverState.ETHERNET_SUSPECT) <= 1
    assert states[-1] in (FailoverState.WIFI_PREPARING, FailoverState.WIFI_SWITCHING)
    assert len(rig.executed_kinds(OpKind.METRIC_WRITE)) <= 1


def test_i16_repeated_ticks_in_same_epoch_do_not_reverify_or_restart_again():
    """同一网络世代内反复 tick：不重复验证、不重复重启 UU（方案 §31 去重）。"""
    rig = Rig()
    drive_to_backup(rig)
    verifications = len(rig.verifier.calls)
    assert len(rig.uu_results) == 1

    pump(rig, IU, D, WIFI_IF, 10, dt=STEP)
    assert len(rig.verifier.calls) == verifications, "同世代不得重复验证"
    assert len(rig.uu_results) == 1, "同世代只允许重启一次 UU"
    assert len(rig.uu_ops.killed) <= 2


def test_i17_duplicate_verification_result_is_deduped_by_epoch():
    """同一世代重复回灌验证结果 ⇒ 协调器去重，不再产出第二次 UU 重启请求。"""
    rig = Rig()
    drive_to_backup(rig)
    assert len(rig.uu_results) == 1

    again = rig.feed(cr.PathVerificationResult(
        verified=True, role=PATH_WIFI, source_ip=WIFI_IP,
        reason="接口受限验证通过", reliability=cr.PATH_OK))
    assert again.uu_request is None
    assert any("去重" in note for note in again.notes)
    assert len(rig.uu_results) == 1


# --------------------------------------------------------------------------- #
# I18–I20：fail-closed 与冷却
# --------------------------------------------------------------------------- #
def test_i18_probe_unreliable_never_switches_or_restarts():
    """接口约束失败（``PROBE_UNRELIABLE``）⇒ 不切换、不重启 UU（方案 §26 T12）。"""
    rig = Rig()
    rig.step(H, D)
    rig.verifier.fail(PATH_WIFI, reliability=cr.PATH_PROBE_UNRELIABLE,
                      reason="接口约束失败：IP_UNICAST_IF 未回读确认")
    for _ in range(3):
        rig.step(IU, D)
    step = rig.step(IU, H)                          # 准备备用链路（验证不可信）
    assert step.follow is not None and step.follow.verification is not None
    assert step.follow.verification.reliability == cr.PATH_PROBE_UNRELIABLE
    assert step.follow.verification.allows_recovery is False
    assert step.follow.uu_request is None

    rig.step(IU, H)
    assert rig.executor.requests == []
    assert rig.uu_results == []
    assert rig.gate.verified_for(PATH_WIFI) is None or \
        not rig.gate.verified_for(PATH_WIFI).allows_recovery


def test_i19_untrusted_snapshot_performs_no_recovery_judgement():
    """三轴快照不可信 ⇒ 零恢复判定：不验证、不推进世代、不产生任何请求。"""
    rig = Rig()
    step = rig.step(H, H, ETH_IF, trusted=False)
    assert step.tick.trusted is False
    assert step.tick.verify_requests == ()
    assert step.tick.event is None
    assert rig.controller.network_epoch == 0
    assert rig.verifier.calls == [] and rig.uu_results == []

    ok = rig.step(H, H)
    assert ok.tick.trusted is True and ok.tick.verify_requests


def test_i20_uu_restart_cooldown_blocks_the_next_recovery_event():
    """UU 重启冷却期内出现新的恢复事件 ⇒ 只记录冷却、不再次清退（方案 §14）。"""
    rig = Rig()
    drive_to_backup(rig)
    assert len(rig.uu_results) == 1
    killed_before = list(rig.uu_ops.killed)

    # 活动链路（Wi-Fi）掉线后用**新地址**重新连上 ⇒ 新世代 + 新的 ACTIVE_PATH_RECOVERED
    rig.step(IU, D, WIFI_IF, dt=STEP)
    step = rig.step(IU, H, WIFI_IF, dt=STEP, wifi_ip="10.20.30.99")

    assert step.tick.event is not None \
        and step.tick.event.value == "ACTIVE_PATH_RECOVERED"
    assert step.follow is not None and step.follow.verification is not None
    assert step.follow.verification.allows_recovery is True
    assert step.follow.uu_request is None, "冷却期内不得产出第二次 UU 重启"
    assert any("冷却" in note for note in step.follow.notes)
    assert rig.uu_ops.killed == killed_before, "冷却期内一个进程都不清退"
    assert len(rig.uu_results) == 1


# --------------------------------------------------------------------------- #
# G01–G06：验证前置门契约
# --------------------------------------------------------------------------- #
def _metric(target: str) -> OpRequest:
    return OpRequest(OpKind.METRIC_WRITE, "切换", {"target": target})


def test_g01_gate_blocks_metric_write_without_any_evidence():
    """没有「接口受限验证通过」记录 ⇒ 拒绝执行路由变更（不执行、不谎报）。"""
    executor = FakeOpExecutor()
    gate = dp.VerifiedActionGate(mode=FailoverMode.AUTO, executor=executor,
                                 clock=lambda: 100.0)
    outcome = gate.submit(_metric("wifi"))
    assert outcome.executed is False and outcome.attempted is False
    assert executor.requests == [], "被拦下的动作绝不能到达真实执行器"
    assert gate.gate_blocked and "尚无「接口受限验证通过」记录" in gate.gate_blocked[0][1]


def test_g02_gate_allows_after_fresh_verification_and_blocks_when_stale():
    """新鲜验证通过 ⇒ 放行；超过新鲜度上限 ⇒ 重新拦下（不拿旧证据切换）。"""
    executor = FakeOpExecutor()
    now = {"t": 100.0}
    gate = dp.VerifiedActionGate(mode=FailoverMode.AUTO, executor=executor,
                                 max_age=60.0, clock=lambda: now["t"])
    gate.note_verification(cr.PathVerificationResult(
        verified=True, role=PATH_WIFI, reason="通过"), now=100.0)
    assert gate.submit(_metric("wifi")).executed is True

    gate.begin_round(2)
    now["t"] = 200.0
    stale = gate.submit(_metric("wifi"))
    assert stale.executed is False and "已过期" in stale.detail
    assert gate.needs_reverification(PATH_WIFI) is True


def test_g03_gate_blocks_when_verification_failed():
    """验证未通过 ⇒ 拒绝切换，理由带上验证原因（可追溯）。"""
    gate = dp.VerifiedActionGate(mode=FailoverMode.AUTO,
                                 executor=FakeOpExecutor(), clock=lambda: 10.0)
    gate.note_verification(cr.PathVerificationResult(
        verified=False, role=PATH_WIFI, reason="闸门未通过（timeout）"), now=10.0)
    outcome = gate.submit(_metric("wifi"))
    assert outcome.executed is False
    assert "验证未通过" in outcome.detail and "timeout" in outcome.detail


def test_g04_gate_passes_through_non_routing_actions():
    """Portal / 重连 / Clash 不在验证前置门范围（它们正是用来把网络修好的）。"""
    executor = FakeOpExecutor()
    gate = dp.VerifiedActionGate(mode=FailoverMode.AUTO, executor=executor,
                                 clock=lambda: 1.0)
    assert gate.submit(OpRequest(OpKind.PORTAL_POST, "认证")).executed is True
    gate.begin_round(2)                     # 互斥串行：一轮只安排一类动作
    assert gate.submit(OpRequest(OpKind.WIFI_RECONNECT, "重连")).executed is True
    gate.begin_round(3)
    assert gate.submit(OpRequest(OpKind.CLASH_TOUCH, "触碰")).executed is True
    assert len(executor.requests) == 3
    assert all(r.kind is not OpKind.METRIC_WRITE for r in executor.requests)


def test_g05_gate_blocks_metric_write_without_target():
    """路由变更未指定目标路径 ⇒ 无法证明目标接口 ⇒ fail-closed 拒绝。"""
    gate = dp.VerifiedActionGate(mode=FailoverMode.AUTO,
                                 executor=FakeOpExecutor(), clock=lambda: 1.0)
    outcome = gate.submit(OpRequest(OpKind.METRIC_WRITE, "切换", {}))
    assert outcome.executed is False and "未指定目标路径" in outcome.detail


def test_g06_gate_keeps_single_side_effect_semantics_without_executor():
    """未接入真实执行层时，放行的动作仍是「已批准但未执行」（与 P6 语义一致）。"""
    gate = dp.VerifiedActionGate(mode=FailoverMode.AUTO, executor=None,
                                 clock=lambda: 1.0)
    gate.note_verification(cr.PathVerificationResult(
        verified=True, role=PATH_WIFI, reason="通过"), now=1.0)
    outcome = gate.submit(_metric("wifi"))
    assert outcome.gate_passed is True and outcome.executed is False
    assert "未接入" in outcome.detail


# --------------------------------------------------------------------------- #
# 接口受限请求构造与轻量确认
# --------------------------------------------------------------------------- #
def test_request_from_snapshot_requires_interface_ip_and_dns():
    """缺 ifIndex / 源地址 / 该接口 DNS ⇒ 无法构造接口受限请求（fail-closed）。"""
    full = dp.path_request_from_snapshot(snapshot(H, H), PATH_WIFI)
    assert full is not None
    assert (full.interface_index, full.source_ip) == (WIFI_IF, WIFI_IP)
    assert full.dns_servers == WIFI_DNS

    assert dp.path_request_from_snapshot(
        snapshot(H, H, wifi_dns=()), PATH_WIFI) is None, "缺该接口 DNS：不回退全局解析器"
    assert dp.path_request_from_snapshot(
        snapshot(H, H, wifi_ip=""), PATH_WIFI) is None
    assert dp.path_request_from_snapshot(snapshot(H, None), PATH_WIFI) is None
    assert dp.path_request_from_snapshot(snapshot(H, H), "loopback") is None


def test_light_gate_verification_only_probes_the_gate_site():
    """轻量闸门确认（kill 前再确认）只跑闸门一站，仍走接口受限通道。"""
    session = FakeSession()
    session.route(cr.VERIFY_GATE.url, _title_page(cr.VERIFY_GATE))
    engine = FakePathEngine()
    verifier = cr.PathVerifier(engine=engine, session_factory=lambda: session)

    request = dp.path_request_from_snapshot(snapshot(H, H), PATH_WIFI)
    result = verifier.verify_gate(request)

    assert result.verified is True and result.allows_recovery is True
    assert result.cross_results == (), "轻量确认不跑交叉站点"
    assert "轻量确认" in result.reason
    assert engine.calls == [(WIFI_IF, WIFI_IP, WIFI_DNS)], "必须绑定该接口自己的 DNS"
    assert len(session.calls) == 1, "只跑闸门一站"
    assert session.calls[0]["url"] == cr.VERIFY_GATE.url
    assert session.mounts, "会话必须挂到接口受限适配器上（有接口约束）"


def test_light_gate_verification_is_fail_closed_without_interface():
    """接口约束不可信 ⇒ 轻量确认同样是 ``PROBE_UNRELIABLE``（不发请求）。"""
    session = FakeSession()
    engine = FakePathEngine(interface_bound=False)
    verifier = cr.PathVerifier(engine=engine, session_factory=lambda: session)

    request = dp.path_request_from_snapshot(snapshot(H, H), PATH_WIFI)
    result = verifier.verify_gate(request)
    assert result.reliability == cr.PATH_PROBE_UNRELIABLE
    assert result.allows_recovery is False
    assert session.calls == [], "未证明接口约束 ⇒ 一个请求都不发"


def test_recheck_fails_closed_when_gate_verification_fails():
    """清退前轻量再确认不通过 ⇒ ``restart_after_verified_recovery`` 不清退。"""
    rig = Rig()
    step = drive_to_backup(rig)
    assert step.follow is not None and step.follow.uu_request is not None

    rig.verifier.gate_ok = False
    rig.verifier.gate_reason = "闸门未通过（www.baidu.com(timeout)）"
    ops = FakeRemoteAppOps(processes=UU_PROCESSES, run_entries=UU_RUN_ENTRIES)
    outcome = cra.restart_after_verified_recovery(
        ops, active_path=PATH_WIFI, network_epoch=99,
        verification=cr.PathVerificationResult(
            verified=True, role=PATH_WIFI, reason="接口受限验证通过",
            reliability=cr.PATH_OK),
        enabled=True, guard=cra.RestartGuard(cooldown=0.0),
        ledger=cra.RecoveryRestartLedger(),
        recheck=rig.controller.build_recheck(PATH_WIFI),
        now=1000.0, sleep=lambda seconds: None)
    assert outcome.restarted is False and outcome.action == "recheck-failed"
    assert ops.killed == [], "再确认未通过 ⇒ 一个进程都不清退"


def test_recheck_uses_the_active_path_interface():
    """轻量再确认必须绑定**活动路径**接口（而不是系统最佳出口）。"""
    rig = Rig()
    drive_to_backup(rig)
    recheck = rig.controller.build_recheck(PATH_WIFI)
    ok, detail = recheck()
    assert ok is True and "活动路径仍可用" in detail
    assert rig.verifier.gate_calls, "应经接口受限通道做一次轻量确认"
    assert rig.verifier.gate_calls[-1].interface_index == WIFI_IF
    assert rig.verifier.gate_calls[-1].role == PATH_WIFI


# --------------------------------------------------------------------------- #
# T01–T04：托盘接线
# --------------------------------------------------------------------------- #
def _make_tray(monkeypatch, **overrides) -> tray.TrayApp:
    monkeypatch.setattr(tray, "load_config", lambda: dict(tray.DEFAULT_CONFIG))
    app = tray.TrayApp(LOG)
    app.config = dict(tray.DEFAULT_CONFIG)
    app.config.update(overrides)
    app.icon = None
    app.root = None
    monkeypatch.setattr(app, "notify", lambda *a, **k: None)
    return app


def test_t01_tray_installs_verification_gate_as_the_single_side_effect_gate(monkeypatch):
    """托盘接线：验证前置门就是状态机的**单一副作用门**（不新增执行点）。"""
    app = _make_tray(monkeypatch)
    assert app.dual_path is not None
    assert isinstance(app.dual_path.gate, dp.VerifiedActionGate)
    assert app.failover.manager.scheduler is app.dual_path.gate


def test_t02_tray_default_config_keeps_uu_recovery_disabled(monkeypatch):
    """默认安全档：``remote_restart_enabled=False`` ⇒ UU 自动恢复关闭（只登记）。"""
    app = _make_tray(monkeypatch)
    assert tray.DEFAULT_CONFIG["remote_restart_enabled"] is False
    assert app.dual_path.status().uu_enabled is False


def test_t03_tray_untrusted_snapshot_requests_no_verification(monkeypatch):
    """快照不可信 ⇒ 托盘不做任何恢复判定，也不起后台验证线程。"""
    app = _make_tray(monkeypatch)
    called = []
    monkeypatch.setattr(app, "_verify_paths_async",
                        lambda requests: called.append(tuple(requests)))
    snap = snapshot(H, H)
    result = TickResult(at=10.0, mode=FailoverMode.AUTO,
                        state=FailoverState.ETHERNET_PRIMARY,
                        previous_state=FailoverState.ETHERNET_SUSPECT)
    tick = app._dual_path_step(10.0, snap, trusted=False, result=result)
    assert tick is not None and tick.trusted is False
    assert called == [] and app.dual_path_tick is tick


def test_t04_tray_wants_verification_only_for_paths_with_full_interface_facts(monkeypatch):
    """托盘判定出的验证请求必须带全接口事实（ifIndex/源地址/该接口 DNS）。"""
    app = _make_tray(monkeypatch)
    seen = []
    monkeypatch.setattr(app, "_verify_paths_async",
                        lambda requests: seen.extend(requests))
    result = TickResult(
        at=10.0, mode=FailoverMode.AUTO, state=FailoverState.WIFI_PREPARING,
        previous_state=FailoverState.ETHERNET_SUSPECT,
        plan=[OpRequest(OpKind.METRIC_WRITE, "切换", {"target": "wifi"})])
    tick = app._dual_path_step(10.0, snapshot(IU, H), trusted=True, result=result)
    assert tick is not None and tick.needs_verification

    roles = [request.role for request in seen]
    assert PATH_WIFI in roles, "切换目标路径必须先被证实"
    assert len(roles) == len(set(roles)), "同一路径一轮最多一个验证请求"
    for request in seen:
        # 每条请求都必须自带完整的接口事实（绝不回退全局出口/全局解析器）
        assert request.interface_index in (ETH_IF, WIFI_IF)
        assert request.source_ip in (ETH_IP, WIFI_IP)
        assert request.dns_servers in (ETH_DNS, WIFI_DNS)
    wifi = [r for r in seen if r.role == PATH_WIFI][0]
    assert (wifi.interface_index, wifi.source_ip) == (WIFI_IF, WIFI_IP)
    assert wifi.dns_servers == WIFI_DNS, "必须用该接口自己的 DNS（不回退全局）"
    assert wifi.network_generation == app.dual_path.network_epoch


def test_t05_tray_background_verification_feeds_the_gate(monkeypatch):
    """托盘**真实**后台验证接缝：请求 → 后台线程验证 → 回灌门禁 ⇒ 目标路径可放行。

    这条用例覆盖 ``_verify_paths_async`` 的真实实现（不 monkeypatch 它），只把
    **验证能力**换成假件，因此仍是零真实网络；它同时说明了 P7.5-1 那些用例为什么
    不会触发真实探测：它们的假快照不带「该接口自己的 DNS」，请求构造即 fail-closed。
    """
    app = _make_tray(monkeypatch)
    verifier = ScriptedVerifier()
    app._path_verifier = verifier                # 注入假验证能力（零真实网络）
    result = TickResult(
        at=10.0, mode=FailoverMode.AUTO, state=FailoverState.WIFI_PREPARING,
        previous_state=FailoverState.ETHERNET_SUSPECT,
        plan=[OpRequest(OpKind.METRIC_WRITE, "切换", {"target": "wifi"})])
    snap = snapshot(IU, H)                       # 带 ifIndex / 源地址 / 接口内 DNS

    tick = app._dual_path_step(10.0, snap, trusted=True, result=result)
    assert tick.needs_verification
    assert wait_until(lambda: len(verifier.calls) == len(tick.verify_requests)), \
        "后台验证线程未完成本轮接口受限验证"

    request = [r for r in verifier.calls if r.role == PATH_WIFI][0]
    assert (request.role, request.interface_index) == (PATH_WIFI, WIFI_IF)
    assert request.source_ip == WIFI_IP and request.dns_servers == WIFI_DNS

    evidence = app.dual_path.gate.verified_for(PATH_WIFI)
    assert evidence is not None and evidence.allows_recovery is True
    assert app.dual_path.gate.needs_reverification(PATH_WIFI) is False

    # 门禁已放行：同一条 METRIC_WRITE 不再因"缺少证据"被拦
    verdict = app.dual_path.gate.submit(OpRequest(
        OpKind.METRIC_WRITE, "切换", {"target": "wifi"}))
    assert "尚未" not in (verdict.detail or "")
    assert "验证未通过" not in (verdict.detail or "")
    assert verifier.gate_calls == [], "放行判定不应额外发起轻量确认"
