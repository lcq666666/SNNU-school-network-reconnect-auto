# -*- coding: utf-8 -*-
"""V0.3 P7.5-1 托盘真实三轴观测与快照可信度门禁（campus_tray_observer）。

本模块修复 **F75-01**（托盘缺少真实三轴健康快照）并为 **F75-02** 提供"数据不足
即拒绝执行"的门禁基础。它回答一个问题：**这一轮喂给故障切换状态机的数据，
能不能作为执行网络副作用的依据？**

背景（P7.5-0 复现，基线 ``cebc622``）
=====================================
``campus_reconnect_tray._failover_snapshot_provider`` 恒为 ``None``，
``_build_failover_snapshot`` 因此永远回退到 ``light_snapshot`` —— 只有
``system.online`` 一个轴，以太网/Wi-Fi 两轴恒为 ``None``。AUTO 模式下状态机
据此既无法证明"以太网故障"也无法证明"Wi-Fi 可用"。

职责边界
========
1. **纯编排 + 注入式 I/O**：本模块不认识 PowerShell / requests / tkinter；
   采集能力由 ``collect`` 注入（生产侧由托盘注入真实
   ``NetworkObserver`` + ``InterfaceHealthChecker`` + ``evaluate()``）。
2. **后台采集，绝不阻塞 UI**：``request()`` 只负责在**独立线程**里发起一次采集
   （限流 + 单飞），``observe()`` 永不等待，只返回上一次**已发布**的结果。
3. **可信度是一等公民**：``ObservationResult.trusted`` 综合了
   *存在性 / 新鲜度 / 代次 / 采集错误 / 结构完整性* 五个维度；
   ``light_snapshot`` 因缺少 ``recommendation`` 与 ``generated_at``，
   **结构上即为不可信**，永远无法授权 AUTO 执行（P7.5-1 硬要求）。
4. **不篡改健康分类**：本模块不把 ``UNKNOWN`` / ``PROBE_UNRELIABLE`` 改写成
   ``HEALTHY``。接口级探测的可靠性问题由 P7.5-2 在 ``campus_network`` 内解决；
   本模块对不可靠数据一律 **fail-closed**（不可信 = 不执行）。
5. **零副作用**：本模块没有任何写路由 / 认证 / 触碰 Clash 的入口。

测试分级：``U-75-1-*`` 单元 / 离线 Mock；``I-75-1-*`` 离线集成闭合回路。
约束：零真实网络 / 零真实路由写入 / 零真实注册表 / 零真实凭据 / 零真实 sleep。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from campus_network import NetworkHealthSnapshot

__all__ = [
    "CONTRACT_VERSION", "DEFAULT_MAX_AGE", "DEFAULT_COLLECT_TIMEOUT",
    "DEFAULT_MIN_INTERVAL", "SNAPSHOT_STATES", "SNAPSHOT_OK",
    "SNAPSHOT_NO_DATA", "SNAPSHOT_COLLECTING", "SNAPSHOT_COLLECT_ERROR",
    "SNAPSHOT_COLLECT_TIMEOUT", "SNAPSHOT_STALE", "SNAPSHOT_GENERATION",
    "SNAPSHOT_INCOMPLETE", "SNAPSHOT_EXTERNAL", "ObservationResult",
    "snapshot_signature", "is_full_snapshot", "TraySnapshotSource",
    "RefusingExecutor", "format_observation", "is_auto_executable_snapshot",
]

CONTRACT_VERSION = "v03-p75-obs-1"

#: 快照有效期（秒）：超过该年龄的快照不得用于 AUTO 决策。
# 保守初始值：缩短 AUTO 可消费的缓存窗口；尚无受控真机性能结论。
DEFAULT_MAX_AGE = 8.0
#: 单次采集的超时容忍（秒）：超时后放弃本轮并**丢弃**迟到的结果。
# 有限等待预算；实际接口探测性能留待后续受控验收。
DEFAULT_COLLECT_TIMEOUT = 12.0
#: 两次采集之间的最小间隔（秒）：限流，避免 PowerShell 诊断进程堆积。
# 最小重采间隔，配合单飞与在途上限限制诊断任务堆积。
DEFAULT_MIN_INTERVAL = 3.0

#: 快照可信度结论取值。
SNAPSHOT_OK = "OK"                              # 可信
SNAPSHOT_NO_DATA = "NO_DATA"                    # 从未采集成功
SNAPSHOT_COLLECTING = "COLLECTING"              # 首次采集进行中
SNAPSHOT_COLLECT_ERROR = "COLLECT_ERROR"        # 采集抛错
SNAPSHOT_COLLECT_TIMEOUT = "COLLECT_TIMEOUT"    # 采集超时
SNAPSHOT_STALE = "STALE"                        # 已过期
SNAPSHOT_GENERATION = "GENERATION_MISMATCH"     # 代次失效（配置/SSID/DHCP 变化）
SNAPSHOT_INCOMPLETE = "INCOMPLETE"              # 结构不完整（轻量快照等）
SNAPSHOT_EXTERNAL = "EXTERNAL_PROVIDER"         # 外部注入提供者（仅结构判定）

SNAPSHOT_STATES: Tuple[str, ...] = (
    SNAPSHOT_OK, SNAPSHOT_NO_DATA, SNAPSHOT_COLLECTING,
    SNAPSHOT_COLLECT_ERROR, SNAPSHOT_COLLECT_TIMEOUT, SNAPSHOT_STALE,
    SNAPSHOT_GENERATION, SNAPSHOT_INCOMPLETE, SNAPSHOT_EXTERNAL,
)

#: 各结论的人类可读说明（托盘告警与日志用）。
STATE_TEXT: Dict[str, str] = {
    SNAPSHOT_OK: "三轴快照可信",
    SNAPSHOT_NO_DATA: "尚无可用三轴快照",
    SNAPSHOT_COLLECTING: "首次三轴采集进行中",
    SNAPSHOT_COLLECT_ERROR: "三轴采集失败",
    SNAPSHOT_COLLECT_TIMEOUT: "三轴采集超时",
    SNAPSHOT_STALE: "三轴快照已过期",
    SNAPSHOT_GENERATION: "快照代次已失效（配置/网络已变化）",
    SNAPSHOT_INCOMPLETE: "快照结构不完整（轻量占位快照）",
    SNAPSHOT_EXTERNAL: "外部注入快照（未经新鲜度校验）",
}


@dataclass
class ObservationResult:
    """一次观测的结果（快照 + 可信度 + 证据）。"""

    contract_version: str = CONTRACT_VERSION
    snapshot: Optional[NetworkHealthSnapshot] = None
    trusted: bool = False
    reason: str = SNAPSHOT_NO_DATA
    detail: str = ""
    #: 快照发布时的代次（用于判定是否已被 ``invalidate()`` 作废）。
    generation: Optional[Tuple[Any, int]] = None
    #: 快照发布的单调时刻（``None`` = 从未发布）。
    collected_at: Optional[float] = None
    #: ``observe()`` 时的快照年龄（秒）。
    age: Optional[float] = None
    #: 两轴是否都已解析（完整三轴；不完整的快照即使可信也不建议用于切换）。
    complete: bool = False

    @property
    def reason_text(self) -> str:
        """带说明的可信度结论（日志/告警用）。"""
        text = STATE_TEXT.get(self.reason, self.reason)
        return text if not self.detail else "%s（%s）" % (text, self.detail)

    def to_dict(self) -> Dict[str, Any]:
        return {"contract_version": self.contract_version,
                "trusted": self.trusted, "reason": self.reason,
                "detail": self.detail, "complete": self.complete,
                "age": self.age,
                "collected_at": self.collected_at,
                "reason_text": self.reason_text}


# --------------------------------------------------------------------------- #
# 纯函数：结构判定与代次签名
# --------------------------------------------------------------------------- #
def _axis_identity(health: Any) -> Tuple[Any, Any]:
    """接口轴的身份（ifIndex + 源地址）；轴缺失返回 ``(None, None)``。"""
    if health is None:
        return (None, None)
    return (getattr(health, "if_index", None),
            getattr(health, "source_ip", None))


def snapshot_signature(snapshot: Optional[NetworkHealthSnapshot]
                       ) -> Tuple[Any, ...]:
    """快照的接口身份签名（用于识别 DHCP / 接口变化）。

    以太网与 Wi-Fi 的 ``ifIndex`` + ``源地址`` 组合成签名；任一变化即视为
    "网络身份已变"，此前在途的采集结果不得再被发布（P7.5-1 代次要求）。
    """
    if snapshot is None:
        return ()
    ethernet = getattr(snapshot, "ethernet", None)
    wifi = getattr(snapshot, "wifi", None)
    return (_axis_identity(ethernet), _axis_identity(wifi))


def is_full_snapshot(snapshot: Optional[NetworkHealthSnapshot]) -> bool:
    """是否为**完整三轴**观测结果（``light_snapshot`` 一律返回 ``False``）。

    判据（三条同时满足）：

    1. ``system`` 轴存在；
    2. ``ethernet`` 或 ``wifi`` **至少一轴已解析**（无一轴解析则无法判断走哪条路）；
    3. ``recommendation`` 与 ``generated_at`` 均非空 —— 这两项**只有**
       ``campus_network.evaluate()`` 会填写，因此可作为"确实来自真实三轴评估"
       的结构性证据，把 ``light_snapshot`` 明确排除在 AUTO 执行依据之外。
    """
    if snapshot is None:
        return False
    if getattr(snapshot, "system", None) is None:
        return False
    if getattr(snapshot, "ethernet", None) is None \
            and getattr(snapshot, "wifi", None) is None:
        return False
    if getattr(snapshot, "recommendation", None) is None:
        return False
    return bool(getattr(snapshot, "generated_at", ""))


def _is_complete(snapshot: Optional[NetworkHealthSnapshot]) -> bool:
    """两轴是否都已解析（比 :func:`is_full_snapshot` 更严格）。"""
    if snapshot is None:
        return False
    return (getattr(snapshot, "ethernet", None) is not None
            and getattr(snapshot, "wifi", None) is not None)


def is_auto_executable_snapshot(snapshot: Optional[NetworkHealthSnapshot]
                                ) -> Tuple[bool, str]:
    """判定快照是否**足以作为 AUTO 执行网络副作用的依据**。

    与 :func:`is_full_snapshot`（结构检查）和 :func:`_is_complete`（两轴检查）
    不同，本函数进一步验证**角色身份**：AUTO 执行 Metric 写入或 Portal 认证前，
    必须知道目标接口的物理身份（ifIndex），否则无法把动作约束到正确网卡。

    返回 ``(ok, reason)``：

    - ``ok=True``：三轴齐全 + 两轴都有有效 ifIndex + system 轴存在；
    - ``ok=False``：``reason`` 说明缺少哪一项（日志/告警用）。
    """
    if not is_full_snapshot(snapshot):
        return False, "快照结构不完整（缺少 recommendation/generated_at 或接口轴）"
    eth = getattr(snapshot, "ethernet", None)
    wifi = getattr(snapshot, "wifi", None)
    system = getattr(snapshot, "system", None)
    if eth is None or wifi is None:
        return False, "AUTO 执行需要 Ethernet 与 Wi-Fi 两轴同时存在"
    if system is None:
        return False, "缺少 SYSTEM 轴"
    eth_idx = getattr(eth, "if_index", None)
    wifi_idx = getattr(wifi, "if_index", None)
    if type(eth_idx) is not int or not 0 < eth_idx <= 0xFFFFFFFF:
        return False, "Ethernet 轴缺少有效的 if_index（无法定位目标接口）"
    if type(wifi_idx) is not int or not 0 < wifi_idx <= 0xFFFFFFFF:
        return False, "Wi-Fi 轴缺少有效的 if_index（无法定位目标接口）"
    if eth_idx == wifi_idx:
        return False, "Ethernet 与 Wi-Fi 不得指向同一接口"
    return True, ""


def _default_spawn(func: Callable[[], None]) -> Any:
    """默认后台启动器：守护线程（崩溃不拖垮托盘，退出不阻塞）。"""
    thread = threading.Thread(target=func, name="campus-netdiag", daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------- #
# 托盘三轴观测源
# --------------------------------------------------------------------------- #
class TraySnapshotSource:
    """后台三轴观测源：**限流 + 单飞 + 超时 + 新鲜度 + 代次 + 任务 ID 防覆盖**。

    用法（托盘工作线程里每轮一次）::

        source.request(now)                 # 非阻塞：必要时在后台发起一次采集
        observation = source.observe(now)   # 非阻塞：取当前可信快照与可信度
        if not observation.trusted:
            ...                             # AUTO 下拒绝执行任何网络副作用

    并发模型：

    - 同一时刻**至多一个**采集在途（``_collecting``），配合 ``min_interval``
      限流，避免 netdiag 的 PowerShell 进程堆积；
    - 采集线程只**写**结果，UI/工作线程只**读**结果，全部经 ``_lock``；
    - ``observe()`` 永不等待、永不 I/O，可在 Tk 主线程安全调用；
    - **任务 ID 匹配**：每个采集分配唯一递增 ID，完成时只修改与该 ID 匹配的
      状态，防止超时/过期的旧任务覆盖新任务的快照、错误、计数或代次。
    """

    def __init__(self, *, collect: Optional[Callable[[], Any]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 generation: Optional[Callable[[], Any]] = None,
                 max_age: float = DEFAULT_MAX_AGE,
                 collect_timeout: float = DEFAULT_COLLECT_TIMEOUT,
                 min_interval: float = DEFAULT_MIN_INTERVAL,
                 max_inflight: int = 3,
                 spawn: Optional[Callable[[Callable[[], None]], Any]] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self._collect = collect
        self._clock = clock or (lambda: time.monotonic())
        self._generation_provider = generation
        self.max_age = float(max(0.0, max_age))
        self.collect_timeout = float(max(0.1, collect_timeout))
        self.min_interval = float(max(0.0, min_interval))
        self._max_inflight = int(max(1, max_inflight))
        self._spawn = spawn or _default_spawn
        self._log = log
        self._lock = threading.Lock()

        self._snapshot: Optional[NetworkHealthSnapshot] = None
        self._snapshot_at: Optional[float] = None
        self._snapshot_generation: Optional[Tuple[Any, int]] = None
        self._signature: Tuple[Any, ...] = ()
        self._error: str = ""
        self._collecting = False
        self._collect_started_at: Optional[float] = None
        self._last_request_at: Optional[float] = None
        #: 内部代次（配置/网络变化或采集超时时自增）。
        self.generation = 0
        #: 任务 ID 序列（每次发起采集时递增；_run 只接受匹配 _active_task_id 的结果）。
        self._task_seq = 0
        self._active_task_id = 0
        #: 并发采集线程数（防止超时重试造成无限堆积）。
        self._inflight_count = 0
        #: 统计（便于验收与告警）。
        self.collect_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self.dropped_stale = 0
        self.dropped_task_mismatch = 0
        self.request_count = 0
        self.throttled = 0
        self.rejected_inflight_limit = 0

    # ---- 代次 ---- #
    def current_generation(self) -> Tuple[Any, int]:
        """当前代次 = ``(外部代次, 内部代次)``。

        外部代次由调用方提供（托盘传 ``config_version()``，覆盖 SSID / 模式 /
        运营商等配置变化）；内部代次由 :meth:`invalidate` 与网络身份变化自增。
        """
        external = None
        if self._generation_provider is not None:
            try:
                external = self._generation_provider()
            except Exception:                       # 代次故障不得阻断观测
                external = None
        return (external, self.generation)

    def invalidate(self, reason: str = "") -> int:
        """作废旧代次：配置 / SSID / 网络身份变化后调用。

        自增内部代次后，所有**在途**采集的结果都会被丢弃（不发布），
        已发布的旧快照也会在 :meth:`observe` 里被判 ``GENERATION_MISMATCH``。
        """
        with self._lock:
            self.generation += 1
            bumped = self.generation
        self._note("三轴观测代次失效（%s）：作废在途采集结果，新代次=%d"
                   % (reason or "未说明原因", bumped))
        return bumped

    # ---- 采集（非阻塞） ---- #
    def request(self, now: Optional[float] = None) -> bool:
        """必要时发起一次后台采集；返回是否真的发起。

        - 已有采集在途且未超时 → 直接返回 ``False``（单飞）；
        - 在途已超时 → 记为超时、**提升任务 ID** 丢弃该在途结果，并允许重新发起；
        - 并发采集线程数已达 ``max_inflight`` → 拒绝，防止超时重试造成堆积；
        - 距上次发起不足 ``min_interval`` → 限流，返回 ``False``。
        """
        stamp = self._clock() if now is None else float(now)
        with self._lock:
            self.request_count += 1
            if self._collecting:
                # BUGFIX P7.5-1A：起始时间为 0.0 时不能用 ``or``（0.0 是 falsy）。
                started = (self._collect_started_at
                           if self._collect_started_at is not None else stamp)
                elapsed = stamp - started
                if elapsed <= self.collect_timeout:
                    return False
                # 超时：提升任务 ID + 自增代次，确保旧线程结果一定被丢弃
                self._collecting = False
                self.timeout_count += 1
                self.generation += 1
                self._task_seq += 1
                self._active_task_id = self._task_seq
                self._error = "采集超时（>%.0fs）" % self.collect_timeout
                self._note("三轴采集超时（%.0fs）：丢弃本轮结果"
                           % self.collect_timeout)
            if self._inflight_count >= self._max_inflight:
                self.rejected_inflight_limit += 1
                return False
            if (self._last_request_at is not None
                    and (stamp - self._last_request_at) < self.min_interval):
                self.throttled += 1
                return False
            self._last_request_at = stamp
            self._collecting = True
            self._collect_started_at = stamp
            self._task_seq += 1
            task_id = self._task_seq
            self._active_task_id = task_id
            self._inflight_count += 1
            started_generation = self.current_generation()
        try:
            self._spawn(lambda: self._run(task_id, started_generation, stamp))
        except Exception as exc:                    # 起线程失败不得拖垮调用方
            with self._lock:
                self._inflight_count = max(0, self._inflight_count - 1)
                if task_id == self._active_task_id:
                    self._collecting = False
                    self.error_count += 1
                    self._error = "采集线程启动失败：%s" % type(exc).__name__
            self._note("三轴采集线程启动失败：%s" % type(exc).__name__)
            return False
        return True

    def collect_now(self) -> Optional[NetworkHealthSnapshot]:
        """同步采集（预热/测试）；与后台入口共用单飞和在途上限。"""
        stamp = self._clock()
        with self._lock:
            if self._collecting or self._inflight_count >= self._max_inflight:
                return self._snapshot
            if (self._last_request_at is not None
                    and stamp - self._last_request_at < self.min_interval):
                self.throttled += 1
                return self._snapshot
            self._last_request_at = stamp
            self._task_seq += 1
            task_id = self._task_seq
            self._active_task_id = task_id
            self._collecting = True
            self._collect_started_at = stamp
            self._inflight_count += 1
            generation = self.current_generation()
        self._run(task_id, generation, stamp)
        with self._lock:
            return self._snapshot

    def _run(self, task_id: int, generation: Tuple[Any, int],
             started_at: float) -> None:
        """采集线程主体：异常一律转为 ``_error``，绝不向上抛。

        P7.5-1A 安全：完成时先核对 ``task_id == _active_task_id``，
        不匹配则**不修改任何状态**（快照/错误/计数/代次均保持原样）。
        """
        error = ""
        snapshot = None
        if self._collect is None:
            error = "未注入三轴采集能力"
        else:
            try:
                snapshot = self._collect()
            except Exception as exc:
                error = "%s: %s" % (type(exc).__name__, exc)
        with self._lock:
            self._inflight_count = max(0, self._inflight_count - 1)
            # 任务 ID 匹配：只有当前活跃任务才能修改状态
            if task_id != self._active_task_id:
                self.dropped_task_mismatch += 1
                self._note("丢弃过期采集结果（任务 %d != 当前 %d）"
                           % (task_id, self._active_task_id))
                return
            self._collecting = False
            if self._clock() - started_at > self.collect_timeout:
                self.timeout_count += 1
                self.generation += 1
                self._task_seq += 1
                self._active_task_id = self._task_seq
                self._error = "采集超时（>%.0fs）" % self.collect_timeout
                return
            if error:
                self.error_count += 1
                self._error = error
                self._note("三轴采集失败：%s" % error)
                return
            if generation != self.current_generation():
                # 代次已变（配置/SSID/DHCP 变化或超时作废）：丢弃，不发布
                self.dropped_stale += 1
                self._note("丢弃过期采集结果（代次 %r != 当前 %r）"
                           % (generation, self.current_generation()))
                return
            signature = snapshot_signature(snapshot)
            if self._signature and signature != self._signature:
                # 网络身份变化（DHCP / 出口接口变化）：作废此后所有在途结果
                self.generation += 1
                self._task_seq += 1
                self._active_task_id = self._task_seq
                self._note("检测到网络身份变化 %r → %r：作废旧代次"
                           % (self._signature, signature))
            self._signature = signature
            self._snapshot = snapshot
            self._snapshot_at = self._clock()
            self._snapshot_generation = self.current_generation()
            self._error = ""
            self.collect_count += 1

    # ---- 读取（非阻塞） ---- #
    def observe(self, now: Optional[float] = None) -> ObservationResult:
        """取当前可信快照与可信度结论（**永不阻塞、永不 I/O**）。"""
        stamp = self._clock() if now is None else float(now)
        with self._lock:
            snapshot = self._snapshot
            collected_at = self._snapshot_at
            snap_generation = self._snapshot_generation
            error = self._error
            collecting = self._collecting
            current = self.current_generation()
        age = None if collected_at is None else max(0.0, stamp - collected_at)

        # 注意：``contract_version`` 是首字段，一律用**关键字**构造，避免错位。
        if snapshot is None:
            if collecting:
                reason = SNAPSHOT_COLLECTING
            elif error:
                reason = (SNAPSHOT_COLLECT_TIMEOUT if "超时" in error
                          else SNAPSHOT_COLLECT_ERROR)
            else:
                reason = SNAPSHOT_NO_DATA
            return ObservationResult(snapshot=None, trusted=False, reason=reason,
                                     detail=error, generation=current,
                                     collected_at=collected_at, age=age,
                                     complete=False)
        if snap_generation != current:
            return ObservationResult(
                snapshot=snapshot, trusted=False, reason=SNAPSHOT_GENERATION,
                detail="快照代次 %r 已失效（当前 %r）" % (snap_generation, current),
                generation=snap_generation, collected_at=collected_at, age=age,
                complete=_is_complete(snapshot))
        if age is not None and age > self.max_age:
            return ObservationResult(
                snapshot=snapshot, trusted=False, reason=SNAPSHOT_STALE,
                detail="已 %.0fs（>%.0fs）" % (age, self.max_age),
                generation=snap_generation, collected_at=collected_at, age=age,
                complete=_is_complete(snapshot))
        if error:
            return ObservationResult(
                snapshot=snapshot, trusted=False, reason=SNAPSHOT_COLLECT_ERROR,
                detail=error, generation=snap_generation,
                collected_at=collected_at, age=age,
                complete=_is_complete(snapshot))
        if not is_full_snapshot(snapshot):
            return ObservationResult(
                snapshot=snapshot, trusted=False, reason=SNAPSHOT_INCOMPLETE,
                detail="缺少 recommendation/generated_at 或无任何接口轴",
                generation=snap_generation, collected_at=collected_at, age=age,
                complete=_is_complete(snapshot))
        return ObservationResult(snapshot=snapshot, trusted=True,
                                 reason=SNAPSHOT_OK, detail="",
                                 generation=snap_generation,
                                 collected_at=collected_at, age=age,
                                 complete=_is_complete(snapshot))

    def snapshot(self) -> Optional[NetworkHealthSnapshot]:
        """最近一次发布的快照（**不判可信度**；展示用）。"""
        with self._lock:
            return self._snapshot

    def _note(self, message: str) -> None:
        if self._log is not None:
            self._log(message)


# --------------------------------------------------------------------------- #
# 不可信快照门禁执行器
# --------------------------------------------------------------------------- #
class RefusingExecutor:
    """**拒绝执行**的占位执行器（P7.5-1A 可信度门禁）。

    AUTO 模式下若本轮快照不可信（缺失 / 过期 / 代次失效 / 结构不完整），
    把单一副作用门的执行器替换为它。

    P7.5-1A 语义修正：

    - ``gate_passed=True``：通过了互斥串行和模式门禁；
    - ``attempted=False``：**不是一次真正的执行尝试**，因此
      **不消耗** Portal 认证冷却、Wi-Fi 重连冷却或 Metric 切换冷却；
    - ``executed=False``：Portal POST / Wi-Fi 重连 / Metric 写入
      三类副作用**一个都不会发生**；
    - 审计日志通过 ``refused`` 列表和 ``_log`` 保留，便于排查。

    这意味着：当快照重新变为可信后，状态机不会因为"虚假的冷却记录"
    而被阻止执行必要的动作。
    """

    def __init__(self, reason: str = "", *,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.reason = reason or "快照不可信"
        self._log = log
        #: 被拒绝的动作请求（供审计与测试断言）。
        self.refused: list = []

    def execute(self, request: Any) -> Any:
        from campus_failover import OpOutcome

        kind = getattr(request, "kind", None)
        self.refused.append(kind)
        if self._log is not None:
            self._log("拒绝执行 %s：%s"
                      % (getattr(kind, "value", "?"), self.reason))
        # P7.5-1A：attempted=False → 状态机 _decide 中不会调用
        # _note_auth / _note_switch，冷却不被消耗。
        return OpOutcome(kind, gate_passed=True, attempted=False,
                         executed=False,
                         detail="拒绝执行：%s" % self.reason)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def format_observation(observation: ObservationResult) -> str:
    """人类可读的观测结论（日志 / 托盘详情用）。"""
    if observation.snapshot is None:
        return "三轴观测：%s" % observation.reason_text
    ethernet = getattr(observation.snapshot, "ethernet", None)
    wifi = getattr(observation.snapshot, "wifi", None)
    lines = [
        "三轴观测：%s" % observation.reason_text,
        "  可信      : %s" % ("是" if observation.trusted else "否"),
        "  完整三轴  : %s" % ("是" if observation.complete else "否"),
        "  年龄      : %s"
        % ("-" if observation.age is None else "%.0fs" % observation.age),
        "  以太网    : %s" % (getattr(ethernet, "state", None).value
                              if ethernet is not None
                              and getattr(ethernet, "state", None) else "未解析"),
        "  Wi-Fi     : %s" % (getattr(wifi, "state", None).value
                              if wifi is not None
                              and getattr(wifi, "state", None) else "未解析"),
    ]
    return "\n".join(lines)
