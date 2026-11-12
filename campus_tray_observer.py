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

阶段 A（V0.7.1，2026-11-12）：观测预算自洽
==========================================
旧默认 ``(max_age=8, timeout=12, interval=3)`` 与真机实测一次采集 ~21.6s 矛盾：
每轮必然超时、快照永不发布，叠加「不可信 ⇒ 零网络副作用」形成长期自锁。
本阶段改为 ``(30, 45, 3)`` 并新增纯函数 :func:`check_snapshot_budget` 把不变量
固化为可回归的规格。**fail-closed 语义完全不变**：结构不完整 / 代次失效 /
真过期 / 采集失败 / 采集超时仍然一律 ``trusted=False``。
证据与残余风险见 ``docs/阶段A_观测预算自洽_2026-11-12.md``。

阶段 B（V0.7.1，2026-11-12）：有界降级观测
==========================================
阶段 A 只解决「慢采集必然超时」。真实断网时 ``netdiag`` 的
``adapters`` / ``default_routes`` 会抛 ``RuntimeError``，没有新快照发布，
旧快照在 ``max_age`` 后过期 ⇒ 仍回到「零网络副作用」自锁（15:50 / 16:20
两次 10 分钟断连的另一半根因）。

本阶段把「一刀切不可信」细分为三个**观测模式**：

* ``FULL``：本轮快照可信（``trusted=True``），语义与阶段 A 完全相同；
* ``DEGRADED``：本轮采集/新鲜度出问题，但存在**最近一次可信快照
  （last-known-good, LKG）**，且代次/接口身份未变、宽限未耗尽；
* ``REFUSED``：结构不完整 / 配置代次变化 / 接口身份变化 / 从未成功采集 /
  宽限耗尽 —— 严格 fail-closed。

**本阶段不改变 ``trusted`` 语义**：``DEGRADED`` 依旧 ``trusted=False``，
``RefusingExecutor`` 依旧零副作用，既有消费方零行为变化。分级信息
（``mode`` / ``stale_for`` / ``grace_left`` / ``lkg_snapshot``）只是为阶段 C
的白名单执行器准备的依据；宽限窗口有界（默认 60s），不会形成永久自锁。

阶段 C（V0.7.1，2026-11-12）：分级执行器
========================================
阶段 B 只给出「是否可以有限降级」的判定；状态机 ``NetworkFailoverManager.tick``
在 ``trusted=False`` 时**根本不调用 ``_decide()``**，动作计划恒为空，所以仅替换
执行器无法解除自锁。阶段 C 在用户确认的三条边界内第一次放宽 fail-closed：

* 只有调用方**显式**登记 ``MODE_DEGRADED`` 才启用；缺省登记路径仍是
  ``FULL`` / ``REFUSED`` 二元语义（阶段 A/B 的全部既有行为零变化）；
* ``DEGRADED`` 时把 ``usable_snapshot``（LKG，年龄 <= 90s）作为**只读上下文**
  喂给状态机，动作仍只经单一副作用门；``GradedExecutor`` 只放行
  ``PORTAL_POST`` / ``WIFI_RECONNECT``，其余一律 ``attempted=False`` 拒绝；
* 每个 ``network_epoch`` 限次 + 全局 >=180s 冷却；宽限耗尽 / 无 LKG /
  非 AUTO 授权 / 冷却中立即退化回 ``RefusingExecutor``；
* 双链路整合层与时间线仍收到 ``trusted=False``：**不安排验证、不重启 UU**。

观测数据结构未变，故 ``CONTRACT_VERSION`` 保持 ``v03-p75-obs-2``；新增的
``GRADED_CONTRACT_VERSION`` 只标记分级执行器契约。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from campus_network import NetworkHealthSnapshot

__all__ = [
    "CONTRACT_VERSION", "DEFAULT_MEASURED_COLLECT", "DEFAULT_MAX_AGE",
    "DEFAULT_COLLECT_TIMEOUT", "DEFAULT_MIN_INTERVAL", "SNAPSHOT_STATES",
    "SNAPSHOT_OK", "SNAPSHOT_NO_DATA", "SNAPSHOT_COLLECTING",
    "SNAPSHOT_COLLECT_ERROR", "SNAPSHOT_COLLECT_TIMEOUT", "SNAPSHOT_STALE",
    "SNAPSHOT_GENERATION", "SNAPSHOT_INCOMPLETE", "SNAPSHOT_EXTERNAL",
    "MODE_FULL", "MODE_DEGRADED", "MODE_REFUSED", "OBSERVATION_MODES",
    "DEFAULT_DEGRADED_GRACE", "MAX_DEGRADED_GRACE", "DEGRADABLE_REASONS",
    "ObservationResult", "snapshot_signature", "is_full_snapshot",
    "check_snapshot_budget", "check_degraded_window", "check_graded_budget",
    "GRADED_CONTRACT_VERSION", "DEGRADED_ALLOWED_KINDS",
    "DEFAULT_GRADED_MAX_PER_EPOCH", "MIN_GRADED_COOLDOWN",
    "DEFAULT_GRADED_COOLDOWN", "MAX_GRADED_COOLDOWN",
    "TraySnapshotSource", "RefusingExecutor", "GradedExecutor",
    "format_observation", "is_auto_executable_snapshot",
]

#: 阶段 B 新增 mode / stale_for / grace_left / lkg_snapshot 字段，契约升版。
CONTRACT_VERSION = "v03-p75-obs-2"

#: 实测一次完整三轴 ``evaluate()`` 的中位耗时（秒）。
# 证据：docs/P8真实环境验收手册_2026-10-15.md §2026-10-16 09:4x 受控实验，
# 21.1s / 22.0s（中位 21.6s）；无线 LINK_DOWN 已在链路层短路仍需该量级。
DEFAULT_MEASURED_COLLECT = 21.6
#: 单次采集的超时容忍（秒）：超时后放弃本轮并**丢弃**迟到的结果。
# 必须覆盖一次实测采集并留出抖动余量；旧值 12.0 < 21.6 ⇒ 每轮必然超时、
# 快照永不发布，P8 受控实验 100% 复现（timeouts=6 / collect=0）。
DEFAULT_COLLECT_TIMEOUT = 45.0
#: 两次采集之间的最小间隔（秒）：限流，避免 PowerShell 诊断进程堆积。
DEFAULT_MIN_INTERVAL = 3.0
#: 快照有效期（秒）：超过该年龄的快照不得用于 AUTO 决策。
# 必须覆盖「一次实测采集 + 一次重采间隔」：21.6 + 3 = 24.6 < 30 ⇒ 正常节拍下
# 无 STALE 空窗。受配置规格 [1, 30] 约束，取上限 30。旧值 8.0 < 24.6 ⇒
# 任何慢采集都产生周期性不可信，叠加采集失败即长期 fail-closed 自锁。
DEFAULT_MAX_AGE = 30.0

# --------------------------------------------------------------------------- #
# 阶段 B：三级观测模式与有界降级窗口
# --------------------------------------------------------------------------- #
#: 本轮快照可信，可完整授权 AUTO 决策（``trusted=True``）。
MODE_FULL = "FULL"
#: 本轮采集/新鲜度异常，但可凭 last-known-good 快照做**受限**判定。
# 本阶段仍 ``trusted=False``、零网络副作用；阶段 C 才接白名单执行器。
MODE_DEGRADED = "DEGRADED"
#: 严格 fail-closed：无任何可用依据。
MODE_REFUSED = "REFUSED"

OBSERVATION_MODES: Tuple[str, ...] = (MODE_FULL, MODE_DEGRADED, MODE_REFUSED)

#: ``max_age`` 到期后，LKG 快照仍可被标记为 ``DEGRADED`` 的宽限（秒）。
# 用户 2026-11-12 确认取 60s：总窗口 30 + 60 = 90s，能完整覆盖一次
# ``collect_timeout=45`` 的病态采集 + 一次重采间隔（48s）并留抖动余量；
# 同时严格有界，绝不会像旧实现那样永久自锁。
DEFAULT_DEGRADED_GRACE = 60.0
#: 宽限硬上限（秒）：防止调用方把「有界」配成事实上的永久宽限。
MAX_DEGRADED_GRACE = 300.0

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

#: 允许进入 ``DEGRADED`` 的**软失败**结论（采集链路抖动，网络身份未变）。
# 注意：``SNAPSHOT_GENERATION`` 不在此列 —— 只有确认「仅内部任务代次变化、
# 外部配置代次与接口签名均未变」时才按软失败处理（见 ``_is_soft_failure``）。
# ``NO_DATA`` / ``INCOMPLETE`` / ``EXTERNAL`` 一律硬拒绝。
DEGRADABLE_REASONS: frozenset = frozenset({
    SNAPSHOT_STALE, SNAPSHOT_COLLECT_ERROR, SNAPSHOT_COLLECT_TIMEOUT,
    SNAPSHOT_COLLECTING,
})

# --------------------------------------------------------------------------- #
# 阶段 C：分级执行器的安全参数
# --------------------------------------------------------------------------- #
#: 分级执行器契约版本（观测数据结构未变，故与 CONTRACT_VERSION 分开标记）。
GRADED_CONTRACT_VERSION = "v03-p75-graded-1"

#: DEGRADED 唯一允许执行的动作（按 ``OpKind.value`` 字符串比较，避免模块级
#: 循环依赖；``METRIC_WRITE`` / ``CLASH_TOUCH`` 与 UU 重启一律拒绝）。
# 用户 2026-11-12 确认：只放行「用来把网络修好」的 Portal 认证与 Wi-Fi 重连，
# 且这两类本来就不在 VerifiedActionGate 的验证前置门内。
DEGRADED_ALLOWED_KINDS: frozenset = frozenset({
    "portal_post", "wifi_reconnect",
})

#: 每个 ``network_epoch`` 最多放行的白名单动作次数（用户确认值）。
DEFAULT_GRADED_MAX_PER_EPOCH = 2
#: 冷却硬下限：阶段 A 文档 §6 要求 >=180s，构造期钳位，禁止配小。
MIN_GRADED_COOLDOWN = 180.0
#: 全局冷却默认值（用户确认值）。因 180s > 90s 降级窗口，
#: 实际每个降级窗口最多执行 1 次白名单动作。
DEFAULT_GRADED_COOLDOWN = 180.0
#: 冷却硬上限：防止把「限次」配成事实上的永久禁用。
MAX_GRADED_COOLDOWN = 3600.0

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


# --------------------------------------------------------------------------- #
# 纯函数：观测预算自洽性校验（阶段 A）
# --------------------------------------------------------------------------- #
def check_snapshot_budget(max_age: float, collect_timeout: float,
                          min_interval: float, *,
                          measured_collect: float = DEFAULT_MEASURED_COLLECT
                          ) -> Tuple[bool, Tuple[str, ...]]:
    """校验「新鲜度窗口 / 采集预算 / 重采间隔」是否自洽（纯函数，零 I/O）。

    三条不变量任一不满足，观测链路就会出现周期性或永久性的不可信：

    1. ``collect_timeout >= measured_collect`` —— 预算必须覆盖一次真实采集，
       否则**每轮必然超时**、快照永不发布（旧 12s 缺陷，P8 已量化）；
    2. ``max_age >= measured_collect + min_interval`` —— 新鲜度窗口必须覆盖
       「一次实测采集 + 一次重采间隔」，否则每次慢采集都产生 STALE 空窗；
    3. ``max_age >= min_interval`` —— 窗口不得短于重采节流，否则每轮必有空窗。

    注意：这里比较的是**实测**采集耗时，不是 ``collect_timeout``。规格允许
    ``max_age <= 30`` 而 ``collect_timeout`` 可达 60，故 ``max_age >= timeout``
    无法作为不变量；病态的「采集跑满 timeout」由后续阶段的有界降级兜底。

    返回 ``(coherent, reasons)``；``reasons`` 为可直出日志/测试断言的违规说明。
    """
    max_age = float(max_age)
    collect_timeout = float(collect_timeout)
    min_interval = float(min_interval)
    cycle = float(measured_collect) + min_interval
    reasons = []
    if collect_timeout < measured_collect:
        reasons.append("采集预算 %.1fs < 实测一次采集 %.1fs：每轮必然超时"
                       % (collect_timeout, measured_collect))
    if max_age < cycle:
        reasons.append("有效期 %.1fs < 采集周期 %.1fs：慢采集必然产生 STALE 空窗"
                       % (max_age, cycle))
    if max_age < min_interval:
        reasons.append("有效期 %.1fs < 重采间隔 %.1fs：每轮必有不可信空窗"
                       % (max_age, min_interval))
    return (not reasons, tuple(reasons))


def check_degraded_window(max_age: float, degraded_grace: float, *,
                          collect_timeout: float = DEFAULT_COLLECT_TIMEOUT,
                          min_interval: float = DEFAULT_MIN_INTERVAL
                          ) -> Tuple[bool, Tuple[str, ...]]:
    """校验「有界降级窗口」是否自洽（阶段 B，纯函数，零 I/O）。

    三条不变量任一不满足，降级要么形同虚设、要么重新变成永久自锁：

    1. ``grace`` 必须是有限正数 —— 否则「有界」不成立，且无法退出；
    2. ``grace <= MAX_DEGRADED_GRACE`` —— 防止把宽限配成事实上的永久宽限；
    3. ``max_age + grace >= collect_timeout + min_interval`` —— 总可复用窗口
       必须覆盖「一次跑满 timeout 的病态采集 + 一次重采间隔」，否则宽限会在
       病态采集期间提前耗尽，起不到缓解自锁的作用。

    默认 ``(30, 60, 45, 3)``：总窗口 90s ≥ 48s，满足全部不变量。
    返回 ``(coherent, reasons)``。
    """
    max_age = float(max_age)
    grace = float(degraded_grace)
    collect_timeout = float(collect_timeout)
    min_interval = float(min_interval)
    reasons = []
    if not math.isfinite(grace) or grace <= 0:
        reasons.append("宽限 %r 非正或非有限值：降级必须有限且可退出" % grace)
    elif grace > MAX_DEGRADED_GRACE:
        reasons.append("宽限 %.1fs 超过硬上限 %.1fs：不再是「有界」降级"
                       % (grace, MAX_DEGRADED_GRACE))
    cycle = collect_timeout + min_interval
    if math.isfinite(grace) and max_age + grace < cycle:
        reasons.append("总窗口 %.1fs < 病态采集周期 %.1fs：宽限会在采集期间提前耗尽"
                       % (max_age + grace, cycle))
    return (not reasons, tuple(reasons))


def check_graded_budget(max_per_epoch: int, cooldown: float
                        ) -> Tuple[bool, Tuple[str, ...]]:
    """校验阶段 C「限次 + 冷却」是否自洽（纯函数，零 I/O）。

    三条不变量：

    1. ``max_per_epoch`` 必须是 1..10 的整数 —— 否则等于无限放行或形同虚设；
    2. ``cooldown >= MIN_GRADED_COOLDOWN``（180s）—— 阶段 A 文档 §6 的硬下限；
    3. ``cooldown <= MAX_GRADED_COOLDOWN`` 且为有限值 —— 不得变成永久禁用。

    默认 ``(2, 180)`` 通过。返回 ``(coherent, reasons)``。
    """
    reasons = []
    if isinstance(max_per_epoch, bool) or not isinstance(max_per_epoch, int) \
            or not 1 <= max_per_epoch <= 10:
        reasons.append("每 epoch 限次 %r 必须是 1..10 的整数" % (max_per_epoch,))
    try:
        value = float(cooldown)
    except (TypeError, ValueError):
        reasons.append("冷却 %r 不是数值" % (cooldown,))
        return (False, tuple(reasons))
    if not math.isfinite(value) or value < MIN_GRADED_COOLDOWN:
        reasons.append("冷却 %.1fs 低于下限 %.0fs：不得高频冲击 Portal / Wi-Fi"
                       % (value, MIN_GRADED_COOLDOWN))
    elif value > MAX_GRADED_COOLDOWN:
        reasons.append("冷却 %.1fs 超过上限 %.0fs：等同永久禁用降级恢复"
                       % (value, MAX_GRADED_COOLDOWN))
    return (not reasons, tuple(reasons))


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
    #: 阶段 B：观测模式。空串 = 未指定，由 ``__post_init__`` 按 ``trusted`` 推导，
    # 保证既有 ``ObservationResult(trusted=True)`` 等关键字构造语义完全不变。
    mode: str = ""
    #: 阶段 B：LKG 超过 ``max_age`` 的时长（秒）；FULL / REFUSED 恒为 0。
    stale_for: float = 0.0
    #: 阶段 B：有界宽限剩余（秒）；REFUSED 恒为 0。
    grace_left: float = 0.0
    #: 阶段 B：最近一次「可授权 AUTO 执行」的完整快照（DEGRADED 的依据）。
    lkg_snapshot: Optional[NetworkHealthSnapshot] = None

    def __post_init__(self) -> None:
        if not self.mode:
            self.mode = MODE_FULL if self.trusted else MODE_REFUSED
        self.stale_for = max(0.0, float(self.stale_for))
        self.grace_left = max(0.0, float(self.grace_left))

    @property
    def is_degraded(self) -> bool:
        """本轮是否处于有界降级（**不代表** ``trusted``，阶段 B 恒为 False）。"""
        return self.mode == MODE_DEGRADED

    @property
    def usable_snapshot(self) -> Optional[NetworkHealthSnapshot]:
        """阶段 C 的统一消费入口：FULL 用本轮快照，DEGRADED 用 LKG，其余为 None。

        ``REFUSED`` 恒返回 ``None`` —— 严格 fail-closed 的单一取值点。
        """
        if self.mode == MODE_FULL and self.trusted:
            return self.snapshot
        if self.mode == MODE_DEGRADED:
            return self.lkg_snapshot
        return None

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
                "reason_text": self.reason_text,
                "mode": self.mode, "stale_for": self.stale_for,
                "grace_left": self.grace_left,
                "has_lkg": self.lkg_snapshot is not None}


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

    - ``ok=True``：已识别的接口均有有效 ifIndex。确认只有 Wi-Fi 时允许无线恢复；
    - ``ok=False``：``reason`` 说明缺少哪一项（日志/告警用）。
    """
    if not is_full_snapshot(snapshot):
        return False, "快照结构不完整（缺少 recommendation/generated_at 或接口轴）"
    eth = getattr(snapshot, "ethernet", None)
    wifi = getattr(snapshot, "wifi", None)
    system = getattr(snapshot, "system", None)
    if system is None:
        return False, "缺少 SYSTEM 轴"
    wifi_idx = getattr(wifi, "if_index", None)
    if type(wifi_idx) is not int or not 0 < wifi_idx <= 0xFFFFFFFF:
        return False, "Wi-Fi 轴缺少有效的 if_index（无法定位目标接口）"
    if eth is None and getattr(snapshot, "wifi_only", False) is True:
        return True, ""
    eth_idx = getattr(eth, "if_index", None)
    if type(eth_idx) is not int or not 0 < eth_idx <= 0xFFFFFFFF:
        return False, "Ethernet 轴缺少有效的 if_index（无法定位目标接口）"
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
                 degraded_grace: float = DEFAULT_DEGRADED_GRACE,
                 max_inflight: int = 3,
                 spawn: Optional[Callable[[Callable[[], None]], Any]] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self._collect = collect
        self._clock = clock or (lambda: time.monotonic())
        self._generation_provider = generation
        self.max_age = float(max(0.0, max_age))
        self.collect_timeout = float(max(0.1, collect_timeout))
        self.min_interval = float(max(0.0, min_interval))
        # 阶段 B：宽限必须严格有界，构造时即钳到硬上限，防止配置成永久降级。
        self.degraded_grace = float(min(MAX_DEGRADED_GRACE,
                                        max(0.0, degraded_grace)))
        self._max_inflight = int(max(1, max_inflight))
        self._hard_inflight_limit = self._max_inflight * 2
        self._spawn = spawn or _default_spawn
        self._log = log
        self._lock = threading.Lock()

        self._snapshot: Optional[NetworkHealthSnapshot] = None
        self._snapshot_at: Optional[float] = None
        self._snapshot_generation: Optional[Tuple[Any, int]] = None
        self._signature: Tuple[Any, ...] = ()
        #: 阶段 B：last-known-good —— 最近一次「结构完整且可定位接口」的快照。
        # 只由采集线程 ``_run`` 写入，``observe()`` 纯读；记录的是**曾经能
        # 授权 AUTO 执行**的数据，不是任意发布过的快照。
        self._lkg_snapshot: Optional[NetworkHealthSnapshot] = None
        self._lkg_at: Optional[float] = None
        #: LKG 发布时的**外部代次**（配置 / SSID / 模式）；内部任务代次因超时
        # 自增不应作废 LKG —— 那只是采集任务失败，网络身份并未改变。
        self._lkg_external: Any = None
        #: LKG 的接口身份签名；DHCP / 出口接口变化后旧 LKG 不可复用。
        self._lkg_signature: Tuple[Any, ...] = ()
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
        self._inflight_tasks: set[int] = set()
        self._retired_tasks: set[int] = set()
        # 超时的 PowerShell 线程不能被 Python 强制取消。若它们长期不退出，
        # 旧实现会把 max_inflight 永久占满，之后再也没有机会发布新快照。
        # 仅用任务 token 淘汰迟到结果，并在有界时间后重建逻辑采集窗口。
        self._inflight_blocked_since: Optional[float] = None
        self._inflight_rebuild_after = max(60.0, self.collect_timeout * 2.0)
        self.rebuild_count = 0
        self.retired_inflight = 0
        self.last_failure_reason = ""
        self.last_failure_at: Optional[float] = None
        self.consecutive_failures = 0
        self.last_task_id = 0
        self.last_generation_before: Optional[Tuple[Any, int]] = None
        self.last_generation_after: Optional[Tuple[Any, int]] = None
        self._last_invalidation = None
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
            before = self.current_generation()
            self._last_invalidation = (before[0], reason)
            self.generation += 1
            bumped = self.generation
            self.last_generation_before = before
            self.last_generation_after = self.current_generation()
            self._task_seq += 1
            self._active_task_id = self._task_seq
            self._collecting = False
            self._collect_started_at = None
            self._last_request_at = None
            # 阶段 B：配置 / SSID / 模式变化后，旧 LKG 的依据已失效，必须清空，
            # 否则降级会把「变化前的网络身份」当作当前依据（安全硬要求）。
            self._lkg_snapshot = None
            self._lkg_at = None
            self._lkg_external = None
            self._lkg_signature = ()
        self._note("三轴观测代次失效：reason=%s generation=%r->%r task=%d"
                   % (reason or "未说明原因", before,
                      self.last_generation_after, self._active_task_id))
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
                before = self.current_generation()
                self.generation += 1
                self._task_seq += 1
                self._active_task_id = self._task_seq
                self._error = "采集超时（>%.0fs）" % self.collect_timeout
                self.last_generation_before = before
                self.last_generation_after = self.current_generation()
                self._record_failure(self._error, stamp)
                self._note("三轴采集超时：task=%d elapsed=%.1fs generation=%r->%r inflight=%d"
                           % (self.last_task_id, elapsed, before,
                              self.last_generation_after, self._inflight_count))
            if len(self._retired_tasks) + self._inflight_count >= self._hard_inflight_limit:
                self.rejected_inflight_limit += 1
                if self.last_failure_reason != "采集任务达到硬上限":
                    self._note("三轴采集任务达到硬上限：active=%d retired=%d limit=%d；保留安全门，等待任务退出"
                               % (self._inflight_count, len(self._retired_tasks),
                                  self._hard_inflight_limit))
                self._record_failure("采集任务达到硬上限", stamp)
                return False
            if self._inflight_count >= self._max_inflight:
                if self._inflight_blocked_since is None:
                    self._inflight_blocked_since = stamp
                blocked_for = stamp - self._inflight_blocked_since
                if (blocked_for < self._inflight_rebuild_after
                        or len(self._retired_tasks) + self._inflight_count
                        >= self._hard_inflight_limit):
                    self.rejected_inflight_limit += 1
                    self._record_failure(
                        "采集并发槽位已满（已有 %d 个迟到任务，等待 %.0fs）"
                        % (self._inflight_count, blocked_for), stamp)
                    return False
                # 不能取消已卡住的线程，但可以安全地淘汰它们的结果。
                # 递增 task id 后，所有迟到完成都会走 mismatch 分支。
                before = self.current_generation()
                retired = self._inflight_count
                self._retired_tasks.update(self._inflight_tasks)
                self._inflight_tasks.clear()
                self.retired_inflight += retired
                self.rebuild_count += 1
                self._inflight_count = 0
                self._collecting = False
                self._collect_started_at = None
                self.generation += 1
                self._task_seq += 1
                self._active_task_id = self._task_seq
                self._inflight_blocked_since = None
                self.last_generation_before = before
                self.last_generation_after = self.current_generation()
                self._record_failure(
                    "采集器重建：并发槽位 %.0fs 未释放，淘汰 %d 个迟到任务"
                    % (blocked_for, retired), stamp)
                self._note("三轴采集器有界重建：淘汰 %d 个迟到任务，新代次=%d"
                           % (retired, self.generation))
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
            self._inflight_tasks.add(task_id)
            self._inflight_blocked_since = None
            started_generation = self.current_generation()
            self.last_task_id = task_id
        try:
            self._spawn(lambda: self._run(task_id, started_generation, stamp))
        except Exception as exc:                    # 起线程失败不得拖垮调用方
            with self._lock:
                self._inflight_count = max(0, self._inflight_count - 1)
                self._inflight_tasks.discard(task_id)
                if task_id == self._active_task_id:
                    self._collecting = False
                    self.error_count += 1
                    self._error = "采集线程启动失败：%s" % type(exc).__name__
                    self._record_failure(self._error, stamp)
            self._note("三轴采集线程启动失败：%s" % type(exc).__name__)
            return False
        return True

    def collect_now(self) -> Optional[NetworkHealthSnapshot]:
        """同步采集（预热/测试）；与后台入口共用单飞和在途上限。"""
        stamp = self._clock()
        with self._lock:
            if (self._collecting or self._inflight_count >= self._max_inflight
                    or len(self._retired_tasks) + self._inflight_count
                    >= self._hard_inflight_limit):
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
            self._inflight_tasks.add(task_id)
            self._inflight_blocked_since = None
            generation = self.current_generation()
            self.last_task_id = task_id
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
            if task_id in self._retired_tasks:
                self._retired_tasks.remove(task_id)
            elif task_id in self._inflight_tasks:
                self._inflight_tasks.remove(task_id)
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
                self._record_failure(self._error, self._clock())
                return
            if error:
                self.error_count += 1
                self._error = error
                self._record_failure(error, self._clock())
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
                # 阶段 B：旧接口身份下的 LKG 不可复用（硬拒绝依据之一）。
                self._lkg_snapshot = None
                self._lkg_at = None
                self._lkg_external = None
                self._lkg_signature = ()
                self._note("检测到网络身份变化 %r → %r：作废旧代次与 LKG"
                           % (self._signature, signature))
            self._signature = signature
            self._snapshot = snapshot
            self._snapshot_at = self._clock()
            self._snapshot_generation = self.current_generation()
            self._error = ""
            self.consecutive_failures = 0
            self.last_failure_reason = ""
            self.last_failure_at = None
            self.collect_count += 1
            self._last_invalidation = None
            self._note("三轴采集发布：task=%d elapsed=%.1fs generation=%r inflight=%d retired=%d"
                       % (task_id, self._clock() - started_at,
                          self._snapshot_generation, self._inflight_count,
                          len(self._retired_tasks)))
            # 阶段 B：只有「完整三轴 + 可定位目标接口」的快照才有资格成为 LKG，
            # 与 is_auto_executable_snapshot 的 AUTO 授权判据保持同一套真值。
            if is_full_snapshot(snapshot) and is_auto_executable_snapshot(snapshot)[0]:
                self._lkg_snapshot = snapshot
                self._lkg_at = self._snapshot_at
                self._lkg_external = self._snapshot_generation[0]
                self._lkg_signature = signature

    def _record_failure(self, reason: str, stamp: float) -> None:
        """记录可聚合的采集故障，避免只留下无原因的代次递增。"""
        self.last_failure_reason = str(reason or "")[:240]
        self.last_failure_at = float(stamp)
        self.consecutive_failures += 1

    # ---- 读取（非阻塞） ---- #
    @staticmethod
    def _classify(*, snapshot, collected_at, snap_generation, error,
                  collecting, current, age, max_age) -> ObservationResult:
        """阶段 A 的原始可信度判定（纯函数）。

        阶段 B **不改其中任何一条结论**：五维（存在性 / 新鲜度 / 代次 /
        采集错误 / 结构完整性）判定原样保留，只在 :meth:`observe` 之后
        对 ``trusted=False`` 做 ``DEGRADED`` / ``REFUSED`` 细分。
        """
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
        if age is not None and age > max_age:
            return ObservationResult(
                snapshot=snapshot, trusted=False, reason=SNAPSHOT_STALE,
                detail="已 %.0fs（>%.0fs）" % (age, max_age),
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

    @staticmethod
    def _is_soft_failure(reason: str, *, lkg_external: Any,
                         current_external: Any,
                         lkg_signature: Tuple[Any, ...],
                         signature: Tuple[Any, ...]) -> bool:
        """本轮不可信是否属于「采集链路软失败」（可凭 LKG 有界降级）。

        ``SNAPSHOT_GENERATION`` 只在**外部配置代次与接口签名均未变**时算软
        失败 —— 采集超时会自增内部任务代次，那只是任务失败，网络身份没变；
        而 SSID / 配置变化或 DHCP / 出口接口变化则是真失效，必须硬拒绝。
        """
        if reason in DEGRADABLE_REASONS:
            return True
        if reason == SNAPSHOT_GENERATION:
            return (lkg_external == current_external
                    and lkg_signature == signature)
        return False

    def _grade_untrusted(self, result: ObservationResult, *, stamp: float,
                         current: Tuple[Any, int], lkg: Any,
                         lkg_at: Optional[float], lkg_external: Any,
                         lkg_signature: Tuple[Any, ...],
                         signature: Tuple[Any, ...]) -> ObservationResult:
        """把 ``trusted=False`` 细分为 ``DEGRADED`` / ``REFUSED``（纯逻辑）。

        进入 ``DEGRADED`` 的**全部**条件（任一不满足即 ``REFUSED``）：

        1. 本轮结论是软失败（见 :meth:`_is_soft_failure`）；
        2. 存在 LKG，且结构完整、可定位目标接口；
        3. LKG 的外部配置代次与接口签名均与当前一致；
        4. LKG 年龄 ``<= max_age + degraded_grace``（严格有界）。

        **安全边界**：``result.trusted`` 始终保持 ``False``，``detail`` /
        ``reason`` / ``reason_text`` 均不改写，既有消费方零行为变化。
        降级依据只通过新增字段暴露，供阶段 C 的白名单执行器消费。
        """
        if not self._is_soft_failure(
                result.reason, lkg_external=lkg_external,
                current_external=current[0], lkg_signature=lkg_signature,
                signature=signature):
            return result
        if lkg is None or lkg_at is None:
            return result                        # 从未有过可信快照
        if not is_full_snapshot(lkg) or not is_auto_executable_snapshot(lkg)[0]:
            return result                        # LKG 不足以定位接口
        if lkg_external != current[0] or lkg_signature != signature:
            return result                        # 配置 / 网络身份已变
        lkg_age = max(0.0, stamp - lkg_at)
        window = self.max_age + self.degraded_grace
        if lkg_age > window:
            return result                        # 宽限耗尽 ⇒ 严格 fail-closed
        result.mode = MODE_DEGRADED
        result.lkg_snapshot = lkg
        result.stale_for = max(0.0, lkg_age - self.max_age)
        result.grace_left = max(0.0, window - lkg_age)
        return result

    def observe(self, now: Optional[float] = None) -> ObservationResult:
        """取当前快照与三级观测结论（**永不阻塞、永不 I/O**）。

        ``FULL`` 时 ``mode`` 由 :meth:`ObservationResult.__post_init__` 推导；
        ``DEGRADED`` / ``REFUSED`` 由 :meth:`_grade_untrusted` 给出。三者的
        ``trusted`` 语义与阶段 A 完全一致。
        """
        stamp = self._clock() if now is None else float(now)
        with self._lock:
            snapshot = self._snapshot
            collected_at = self._snapshot_at
            snap_generation = self._snapshot_generation
            error = self._error
            collecting = self._collecting
            current = self.current_generation()
            lkg = self._lkg_snapshot
            lkg_at = self._lkg_at
            lkg_external = self._lkg_external
            lkg_signature = self._lkg_signature
            signature = self._signature
        age = None if collected_at is None else max(0.0, stamp - collected_at)
        result = self._classify(
            snapshot=snapshot, collected_at=collected_at,
            snap_generation=snap_generation, error=error,
            collecting=collecting, current=current, age=age,
            max_age=self.max_age)
        if result.trusted:
            return result
        return self._grade_untrusted(
            result, stamp=stamp, current=current, lkg=lkg, lkg_at=lkg_at,
            lkg_external=lkg_external, lkg_signature=lkg_signature,
            signature=signature)

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
# 阶段 C：DEGRADED 白名单执行器
# --------------------------------------------------------------------------- #
class GradedExecutor:
    """有界降级期间**只放行白名单动作**的执行器（阶段 C）。

    包住真实执行器（``delegate``），协议与 ``NetworkOpScheduler._invoke()``
    完全一致：``execute(request)`` 可返回 ``OpOutcome`` / ``True|None`` /
    ``False`` / ``str``。桥接层只在**显式登记** ``MODE_DEGRADED`` 且本执行器
    :attr:`active` 为真时把它接入单一副作用门；其余情况一律换回
    :class:`RefusingExecutor`。

    安全边界（用户 2026-11-12 确认）：

    * 白名单仅 ``PORTAL_POST`` / ``WIFI_RECONNECT``；``METRIC_WRITE`` /
      ``CLASH_TOUCH`` 一律拒绝，UU 重启由整合层的 ``trusted=False`` 拒绝；
    * 拒绝时沿用 P7.5-1A 语义：``attempted=False``、``executed=False``，
      **不消耗**状态机的认证 / 重连 / 切换冷却，也不谎报状态迁移；
    * 每个 ``network_epoch`` 最多 ``max_per_epoch`` 次（默认 2），全局
      ``cooldown``（默认 180s）内不重复尝试；参数由
      :func:`check_graded_budget` 在构造期钳位；
    * ``grace_left <= 0``（宽限耗尽）/ 无 LKG / 无真实执行层 ⇒ ``active=False``，
      调用方应立即退化回 ``RefusingExecutor``。

    所有判定数据都由调用方通过 :meth:`update` 每轮显式注入（``grace_left`` /
    ``epoch`` / ``now``），本类**不读时钟、不做 I/O**，可被虚拟时钟精确测试。
    """

    def __init__(self, delegate: Any, *, grace_left: float = 0.0,
                 epoch: int = 0, now: float = 0.0,
                 max_per_epoch: int = DEFAULT_GRADED_MAX_PER_EPOCH,
                 cooldown: float = DEFAULT_GRADED_COOLDOWN,
                 log: Optional[Callable[[str], None]] = None) -> None:
        coherent, reasons = check_graded_budget(max_per_epoch, cooldown)
        if not coherent:
            raise ValueError("分级执行器预算不合法：%s" % "；".join(reasons))
        self.delegate = delegate
        self.grace_left = max(0.0, float(grace_left))
        self.epoch = int(epoch)
        self.now = float(now)
        self.max_per_epoch = int(max_per_epoch)
        self.cooldown = float(cooldown)
        self._log = log
        #: epoch -> 已放行（attempted）的动作次数。
        self._epoch_counts: Dict[int, int] = {}
        self._last_attempt_at: Optional[float] = None
        #: 审计台账：放行的 ``(epoch, kind, now, outcome)`` 与被拒的 ``(kind, 理由)``。
        self.allowed: list = []
        self.refused: list = []

    # ---- 每轮由桥接层注入上下文 ---- #
    def update(self, *, grace_left: float, epoch: int, now: float) -> bool:
        """登记本轮 DEGRADED 上下文，返回分级窗口是否仍 ``active``。"""
        self.grace_left = max(0.0, float(grace_left))
        self.epoch = int(epoch)
        self.now = float(now)
        return self.active

    @property
    def actions_left(self) -> int:
        """当前 ``network_epoch`` 还剩几次白名单动作名额。"""
        return max(0, self.max_per_epoch - self._epoch_counts.get(self.epoch, 0))

    @property
    def cooldown_left(self) -> float:
        """距下一次可尝试还剩多少秒（从未尝试则为 0）。"""
        if self._last_attempt_at is None:
            return 0.0
        elapsed = max(0.0, self.now - self._last_attempt_at)
        return max(0.0, self.cooldown - elapsed)

    @property
    def active(self) -> bool:
        """当前是否允许桥接层把 LKG 交给状态机并接入本执行器。

        宽限耗尽 / 限次用尽 / 冷却中 / 无真实执行层都会使窗口立即关闭，
        调用方据此把单一副作用门换回 ``RefusingExecutor``（fail-closed）。
        """
        return bool(self.delegate is not None and self.grace_left > 0
                    and self.actions_left > 0 and self.cooldown_left <= 0.0)

    # ---- 门禁 ---- #
    def block_reason(self, kind_value: str) -> str:
        """返回拒绝理由；空串表示可放行（纯函数，不改状态）。"""
        if kind_value not in DEGRADED_ALLOWED_KINDS:
            return "非 DEGRADED 白名单动作（%s）" % (kind_value or "未知")
        if self.delegate is None:
            return "真实执行层未接入"
        if self.grace_left <= 0:
            return "DEGRADED 有界宽限已耗尽"
        if self.actions_left <= 0:
            return "本 network_epoch 限次已用尽"
        if self.cooldown_left > 0:
            return "降级动作冷却剩余 %.0fs" % self.cooldown_left
        return ""

    def execute(self, request: Any) -> Any:
        """通过白名单与预算门禁后才调用真实执行器。"""
        from campus_failover import OpOutcome

        kind = getattr(request, "kind", None)
        label = str(getattr(kind, "value", kind) or "")
        reason = self.block_reason(label)
        if reason:
            self.refused.append((kind, reason))
            self._note("拒绝执行 %s：%s" % (label or "?", reason))
            return OpOutcome(kind, gate_passed=True, attempted=False,
                             executed=False, detail="拒绝执行：%s" % reason)
        outcome = self._invoke_delegate(request, kind)
        self.allowed.append((self.epoch, kind, self.now, outcome))
        if outcome.attempted:
            self._epoch_counts[self.epoch] = self._epoch_counts.get(self.epoch, 0) + 1
            self._last_attempt_at = self.now
        self._note("DEGRADED 放行 %s：%s" % (label, outcome.detail or "已执行"))
        return outcome

    def _invoke_delegate(self, request: Any, kind: Any) -> Any:
        """复用 ``NetworkOpScheduler._invoke`` 的返回值语义（含异常兜底）。"""
        from campus_failover import OpOutcome

        try:
            if hasattr(self.delegate, "execute"):
                raw = self.delegate.execute(request)
            else:
                raw = self.delegate(request)
        except Exception as exc:                       # 执行器故障不得上抛
            return OpOutcome(kind, gate_passed=True, attempted=True,
                             detail="执行器异常：%s: %s"
                                    % (type(exc).__name__, exc))
        if isinstance(raw, OpOutcome):
            return raw
        if raw is None or raw is True:
            return OpOutcome(kind, gate_passed=True, attempted=True,
                             executed=True, detail="已执行")
        if raw is False:
            return OpOutcome(kind, gate_passed=True, attempted=True,
                             detail="执行失败")
        return OpOutcome(kind, gate_passed=True, attempted=True,
                         executed=True, detail=str(raw))

    def _note(self, message: str) -> None:
        if self._log is not None:
            self._log(message)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def format_observation(observation: ObservationResult) -> str:
    """人类可读的观测结论（日志 / 托盘详情用）。"""
    if observation.snapshot is None:
        return "三轴观测：%s" % observation.reason_text
    ethernet = getattr(observation.snapshot, "ethernet", None)
    wifi = getattr(observation.snapshot, "wifi", None)
    mode_line = "  观测模式  : %s" % observation.mode
    if observation.is_degraded:
        mode_line += "（LKG 已过期 %.0fs，剩余宽限 %.0fs）" % (
            observation.stale_for, observation.grace_left)
    lines = [
        "三轴观测：%s" % observation.reason_text,
        "  可信      : %s" % ("是" if observation.trusted else "否"),
        mode_line,
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
