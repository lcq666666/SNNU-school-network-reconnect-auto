# -*- coding: utf-8 -*-
"""V0.3 P5 受控路由切换与路由卫士（campus_route_guard）。

本模块实现主方案 §7.2「路由变更需要事务和回滚」、§4.2「阶段 B：正式切换」，
即执行计划 §0.2 中 P5 的行级定义：

> **P5 受控路由切换与路由卫士**：新增 ``campus_route_guard.py``，快照/切换/验证/回滚；
> 允许真实副作用（受控、单人值守）；前置依赖 P4。

职责边界
========

1. **事务式路由变更**（方案 §7.2）：每次切换前保存完整快照（目标网卡标识、原始
   InterfaceMetric 与 AutomaticMetric 状态、原始默认路由及 RouteMetric、当前主路径、
   唯一操作编号）→ 条件校验 → **最小变更**（只写与目标值不同的项）→ 结果检查
   （**实际出口**是否已改变，IPv4 与 IPv6 均须检查，AGENTS §3.7）→ 失败即按快照回滚。
2. **崩溃恢复**：变更前先把"进行中"记录写入 :class:`RouteJournal`（持久化 JSON）；
   主程序若在切换途中崩溃，可由**独立于主监控线程**的入口（``route --recover`` 或
   下次启动时的 :meth:`RouteGuard.recover`）读取日志并恢复。
3. **回滚不等于"永远恢复到有线优先"**（方案 §7.2 末段）：:meth:`RouteGuard.recover`
   先判断当前出口是否已等于本次变更的目标 —— 已生效则提交，否则才按快照回退；
   有线确实不可用时不会被强行切回。
4. **危险操作黑名单**（AGENTS §3.12）：本模块**没有**禁用/重置网卡、删除默认路由、
   清空 DNS、重启网络服务、断开全部连接、强杀远控、改 Clash TUN/代理 的任何入口；
   :func:`assert_operation_allowed` 对黑名单 fail-closed。
5. **权限分离**（AGENTS §3.11）：读写分离 —— 只读经 P0 ``netdiag`` 只读管道，
   写入经 :class:`WindowsMetricWriter`（白名单 ``Set-NetIPInterface``）；权限不足时
   **如实报告并停止**，不反复重试。
6. **不实现 Clash 联动**（方案 §9）：核心链路管理独立于 Clash；``CLASH_TOUCH``
   在本模块被**显式拒绝**（需用户显式授权后按事务改动，P5 不提供自动入口）。
7. **P3 执行器协议落地**：:class:`MetricExecutor` 实现 ``METRIC_WRITE``；
   :class:`CompositeExecutor` 把 P4 的 ``SnnuBackupExecutor``（``PORTAL_POST`` /
   ``WIFI_RECONNECT``）与 P5 的路由执行器组合成单一副作用门背后的**唯一执行器**。

依赖注入
========

所有副作用（读 Metric / 读路由 / 读出口 / 写 Metric / 恢复自动 Metric）都经
:class:`RouteOps` 注入；默认测试集合注入假件即可实现**零真实网络 / 零真实路由写入 /
零真实注册表 / 零真实 sleep**。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from campus_failover import FailoverPolicy, OpKind, OpOutcome

__all__ = [
    "CONTRACT_VERSION", "JOURNAL_CONTRACT_VERSION", "ROUTE_TARGETS",
    "EGRESS_TARGET_V4", "EGRESS_TARGET_V6", "TAKEOVER_METRIC_GAP",
    "DANGEROUS_OPERATIONS", "ALLOWED_OPERATIONS", "ALLOWED_SET_PARAMETERS",
    "EgressVerdict", "RouteStatus", "RouteGuardError", "EgressCheck",
    "EgressVerification", "MetricState", "RouteSnapshot", "RouteChangeRecord",
    "RouteJournal", "RouteOps", "RouteGuard", "MetricExecutor",
    "CompositeExecutor", "WindowsMetricWriter", "assert_operation_allowed",
    "assert_write_allowed", "build_windows_route_ops", "desired_metrics",
    "normalize_outcome", "format_route_snapshot", "format_route_change",
]

CONTRACT_VERSION = "v03-p5-routeguard-1"
JOURNAL_CONTRACT_VERSION = "v03-p5-routejournal-1"

#: 允许作为切换目标的方向（其余一律拒绝）。
ROUTE_TARGETS: Tuple[str, ...] = ("ethernet", "wifi")

#: 出口验证的目标地址（IPv4 与 IPv6 **都必须**检查，AGENTS §3.7）。
EGRESS_TARGET_V4 = "8.8.8.8"
EGRESS_TARGET_V6 = "2606:4700:4700::1111"

#: 切换接管时，非目标接口 Metric 相对目标接口的抬升量（确保严格劣后）。
TAKEOVER_METRIC_GAP = 100

#: 危险操作黑名单（AGENTS §3.12）—— 路由卫士拒绝执行其中任何一项。
DANGEROUS_OPERATIONS: Tuple[str, ...] = (
    "disable_adapter",
    "reset_adapter",
    "remove_all_default_routes",
    "flush_dns",
    "restart_network_service",
    "disconnect_all",
    "kill_remote_client",
    "clash_tun",
    "clash_proxy",
)

#: 本模块允许执行的写入操作（白名单，其余一律拒绝）。
ALLOWED_OPERATIONS: Tuple[str, ...] = (
    "set_interface_metric",
    "restore_automatic_metric",
)

#: 写入命令允许出现的参数（``Set-NetIPInterface`` 的受限子集）。
ALLOWED_SET_PARAMETERS = frozenset({
    "-InterfaceIndex", "-AddressFamily", "-InterfaceMetric", "-AutomaticMetric",
    "-Confirm", "-WhatIf",
})

#: 写入命令允许的首段 cmdlet（仅此一个）。
_WRITE_LEAD = "Set-NetIPInterface"


class EgressVerdict(Enum):
    """出口验证结论（AGENTS §3.7：先证实再切换，IPv4 + IPv6 均须检查）。"""

    CONFIRMED = "CONFIRMED"      # IPv4 出口已改为目标接口，IPv6 未指向其他可用接口
    IPV6_STALE = "IPV6_STALE"    # IPv4 已改，但 IPv6 仍指向其他接口（未通过）
    UNCHANGED = "UNCHANGED"      # IPv4 出口未改变（未通过）
    UNKNOWN = "UNKNOWN"          # 无法证明实际出口（不假定成功）

    @property
    def ok(self) -> bool:
        return self is EgressVerdict.CONFIRMED


class RouteStatus(Enum):
    """一次路由变更事务的状态（写入 journal 的 ``status`` 字段）。"""

    PENDING = "PENDING"            # 已开始、尚未完成（崩溃恢复依据）
    COMPLETED = "COMPLETED"        # 变更生效并通过出口验证
    ROLLED_BACK = "ROLLED_BACK"    # 变更失败/未通过验证，已按快照回滚
    REFUSED = "REFUSED"            # 未授权/非法目标/冷却中，未产生任何写入


class RouteGuardError(str, Enum):
    """P5 结构化错误码（不含任何凭据）。"""

    NOT_AUTHORIZED = "E_ROUTE_NOT_AUTHORIZED"
    INVALID_TARGET = "E_ROUTE_INVALID_TARGET"
    INTERFACE_UNRESOLVED = "E_ROUTE_INTERFACE_UNRESOLVED"
    SWITCH_COOLDOWN = "E_ROUTE_SWITCH_COOLDOWN"
    PRECHECK_FAILED = "E_ROUTE_PRECHECK_FAILED"
    SNAPSHOT_FAILED = "E_ROUTE_SNAPSHOT_FAILED"
    APPLY_FAILED = "E_ROUTE_APPLY_FAILED"
    PERMISSION_DENIED = "E_ROUTE_PERMISSION_DENIED"
    EGRESS_UNCHANGED = "E_ROUTE_EGRESS_UNCHANGED"
    EGRESS_UNKNOWN = "E_ROUTE_EGRESS_UNKNOWN"
    EGRESS_IPV6_STALE = "E_ROUTE_EGRESS_IPV6_STALE"
    DANGEROUS_OPERATION = "E_ROUTE_DANGEROUS_OPERATION"
    ROLLBACK_FAILED = "E_ROUTE_ROLLBACK_FAILED"
    RECOVERED = "E_ROUTE_RECOVERED"


class RouteGuardRefused(Exception):
    """危险/非法操作被拒绝（fail-closed）。"""


# --------------------------------------------------------------------------- #
# 纯函数：操作白名单与黑名单
# --------------------------------------------------------------------------- #
def assert_operation_allowed(operation: str) -> None:
    """拒绝危险操作与白名单外操作（AGENTS §3.12，fail-closed）。"""
    name = str(operation or "").strip()
    if name in DANGEROUS_OPERATIONS:
        raise RouteGuardRefused(
            "危险操作被拒绝（AGENTS §3.12）：%s" % name)
    if name not in ALLOWED_OPERATIONS:
        raise RouteGuardRefused(
            "操作不在路由卫士白名单（仅允许 %s）：%s"
            % ("/".join(ALLOWED_OPERATIONS), name or "(空)"))


def assert_write_allowed(script: str) -> None:
    """写入命令自检：仅允许 ``Set-NetIPInterface`` 的受限参数子集（fail-closed）。

    与 P0 ``netdiag.assert_read_only`` 互补：只读管道**拒绝一切写命令**，
    本函数则**只放行**这一条受限写命令，杜绝"顺手"引入禁用网卡/删除路由等操作。
    """
    text = (script or "").strip()
    if not text:
        raise ValueError("空写入命令不允许执行")
    parts = text.split()
    if parts[0] != _WRITE_LEAD:
        raise ValueError("写入命令不在白名单（仅允许 %s）：%r"
                         % (_WRITE_LEAD, parts[0]))
    for token in parts[1:]:
        if token.startswith("-"):
            name = token.split(":", 1)[0]      # ``-Confirm:$false`` → ``-Confirm``
            if name not in ALLOWED_SET_PARAMETERS:
                raise ValueError("写入命令包含未授权参数 %s：%r" % (name, text))
    lowered = text.lower()
    for banned in ("disable-netadapter", "restart-netadapter", "reset-netadapter",
                   "remove-netroute", "remove-netipaddress", "new-netroute",
                   "set-dnsclient", "clear-dnsclientcache", "restart-service",
                   "stop-service", "netsh", "ipconfig", "taskkill", "reg add",
                   "sc config"):
        if banned in lowered:
            raise ValueError("写入命令包含禁止操作 %s：%r" % (banned, text))


# --------------------------------------------------------------------------- #
# 数据结构：出口验证
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EgressCheck:
    """单个地址族的出口识别结果（``Find-NetRoute``，只读、不发包）。"""

    family: str
    target: str
    if_index: Optional[int] = None
    source_ip: Optional[str] = None
    error: Optional[str] = None

    @property
    def available(self) -> bool:
        """是否识别到出口接口。"""
        return self.if_index is not None

    def to_dict(self) -> Dict[str, Any]:
        return {"family": self.family, "target": self.target,
                "if_index": self.if_index, "source_ip": self.source_ip,
                "error": self.error}


@dataclass(frozen=True)
class EgressVerification:
    """IPv4 + IPv6 双族出口验证结论（方案 §4.2 阶段 B 4、AGENTS §3.7）。"""

    target_if_index: Optional[int]
    ipv4: Optional[EgressCheck] = None
    ipv6: Optional[EgressCheck] = None
    verdict: EgressVerdict = EgressVerdict.UNKNOWN
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict.ok

    def to_dict(self) -> Dict[str, Any]:
        return {"target_if_index": self.target_if_index,
                "ipv4": self.ipv4.to_dict() if self.ipv4 else None,
                "ipv6": self.ipv6.to_dict() if self.ipv6 else None,
                "verdict": self.verdict.value, "detail": self.detail}


# --------------------------------------------------------------------------- #
# 数据结构：快照与变更记录
# --------------------------------------------------------------------------- #
@dataclass
class MetricState:
    """单张接口的 Metric 快照（IPv4/IPv6 分开，不混淆）。"""

    if_index: int
    alias: str = ""
    ipv4_metric: Optional[int] = None
    ipv6_metric: Optional[int] = None
    ipv4_automatic: Optional[str] = None
    ipv6_automatic: Optional[str] = None

    def metric(self, family: str) -> Optional[int]:
        return self.ipv4_metric if family == "IPv4" else self.ipv6_metric

    def automatic(self, family: str) -> Optional[str]:
        return self.ipv4_automatic if family == "IPv4" else self.ipv6_automatic

    def to_dict(self) -> Dict[str, Any]:
        return {"if_index": self.if_index, "alias": self.alias,
                "ipv4_metric": self.ipv4_metric, "ipv6_metric": self.ipv6_metric,
                "ipv4_automatic": self.ipv4_automatic,
                "ipv6_automatic": self.ipv6_automatic}


@dataclass
class RouteSnapshot:
    """一次路由变更前的完整快照（方案 §7.2 要求保存的全部要素）。"""

    contract_version: str = CONTRACT_VERSION
    operation_id: str = ""
    created_at: str = ""
    target: str = ""
    primary_egress_if_index: Optional[int] = None
    interfaces: Dict[str, MetricState] = field(default_factory=dict)
    default_routes: List[Dict[str, Any]] = field(default_factory=list)
    interface_ids: Dict[str, Optional[int]] = field(default_factory=dict)

    def metric_state(self, if_index: Optional[int]) -> Optional[MetricState]:
        if if_index is None:
            return None
        return self.interfaces.get(str(if_index))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "operation_id": self.operation_id,
            "created_at": self.created_at,
            "target": self.target,
            "primary_egress_if_index": self.primary_egress_if_index,
            "interfaces": {k: v.to_dict() for k, v in self.interfaces.items()},
            "default_routes": [dict(r) for r in self.default_routes],
            "interface_ids": dict(self.interface_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteSnapshot":
        if not data:
            return cls()
        interfaces = {}
        for key, value in (data.get("interfaces") or {}).items():
            interfaces[str(key)] = MetricState(
                if_index=int(value.get("if_index", key)),
                alias=str(value.get("alias") or ""),
                ipv4_metric=value.get("ipv4_metric"),
                ipv6_metric=value.get("ipv6_metric"),
                ipv4_automatic=value.get("ipv4_automatic"),
                ipv6_automatic=value.get("ipv6_automatic"))
        return cls(
            contract_version=str(data.get("contract_version") or CONTRACT_VERSION),
            operation_id=str(data.get("operation_id") or ""),
            created_at=str(data.get("created_at") or ""),
            target=str(data.get("target") or ""),
            primary_egress_if_index=data.get("primary_egress_if_index"),
            interfaces=interfaces,
            default_routes=[dict(r) for r in (data.get("default_routes") or [])],
            interface_ids=dict(data.get("interface_ids") or {}))


@dataclass
class RouteChangeRecord:
    """一次路由变更事务的完整记录（写入 journal，供崩溃恢复与审计）。"""

    contract_version: str = CONTRACT_VERSION
    operation_id: str = ""
    created_at: str = ""
    target: str = ""
    reason: str = ""
    status: RouteStatus = RouteStatus.PENDING
    ok: bool = False
    error: Optional[str] = None
    detail: str = ""
    before: Optional[RouteSnapshot] = None
    after: Optional[RouteSnapshot] = None
    changes: List[Dict[str, Any]] = field(default_factory=list)
    verification: Optional[EgressVerification] = None
    rolled_back: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "operation_id": self.operation_id,
            "created_at": self.created_at,
            "target": self.target,
            "reason": self.reason,
            "status": self.status.value,
            "ok": self.ok,
            "error": self.error,
            "detail": self.detail,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "changes": [dict(c) for c in self.changes],
            "verification": (self.verification.to_dict()
                             if self.verification else None),
            "rolled_back": self.rolled_back,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteChangeRecord":
        verification = data.get("verification")
        return cls(
            contract_version=str(data.get("contract_version") or CONTRACT_VERSION),
            operation_id=str(data.get("operation_id") or ""),
            created_at=str(data.get("created_at") or ""),
            target=str(data.get("target") or ""),
            reason=str(data.get("reason") or ""),
            status=RouteStatus(str(data.get("status") or "PENDING")),
            ok=bool(data.get("ok")),
            error=data.get("error"),
            detail=str(data.get("detail") or ""),
            before=RouteSnapshot.from_dict(data.get("before") or {}),
            after=(RouteSnapshot.from_dict(data["after"])
                   if data.get("after") else None),
            changes=[dict(c) for c in (data.get("changes") or [])],
            verification=(EgressVerification(
                target_if_index=verification.get("target_if_index"),
                ipv4=(EgressCheck(**verification["ipv4"])
                      if verification.get("ipv4") else None),
                ipv6=(EgressCheck(**verification["ipv6"])
                      if verification.get("ipv6") else None),
                verdict=EgressVerdict(str(verification.get("verdict")
                                          or "UNKNOWN")),
                detail=str(verification.get("detail") or ""))
                if verification else None),
            rolled_back=bool(data.get("rolled_back")))


# --------------------------------------------------------------------------- #
# 变更日志（崩溃恢复的持久化依据）
# --------------------------------------------------------------------------- #
class RouteJournal:
    """路由变更日志（持久化 JSON）。

    - ``record_pending``：**任何写入之前**调用 —— 这是崩溃恢复的锚点；
    - ``record_finished``：事务结束（成功/回滚）后调用，清空 ``pending`` 并追加历史；
    - ``pending()``：读取未完成事务（``None`` 表示无待恢复项）；
    - ``path=None`` 时为**内存日志**（离线测试用），不触碰文件系统。
    """

    def __init__(self, path: Optional[Any] = None) -> None:
        self.path = Path(path) if path else None
        self._memory: Dict[str, Any] = self._empty()

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {"contract_version": JOURNAL_CONTRACT_VERSION,
                "pending": None, "history": []}

    def _read(self) -> Dict[str, Any]:
        if self.path is None:
            return self._memory
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return self._empty()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("pending", None)
        data.setdefault("history", [])
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        if self.path is None:
            self._memory = data
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # 日志写入失败不得让路由事务继续"盲跑"：上抛由 RouteGuard 转为失败。
            raise

    def pending(self) -> Optional[Dict[str, Any]]:
        value = self._read().get("pending")
        return dict(value) if isinstance(value, dict) else None

    def history(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in (self._read().get("history") or [])]

    def record_pending(self, record: RouteChangeRecord) -> None:
        data = self._read()
        data["pending"] = record.to_dict()
        self._write(data)

    def record_finished(self, record: RouteChangeRecord) -> None:
        data = self._read()
        data["pending"] = None
        history = list(data.get("history") or [])
        history.append(record.to_dict())
        data["history"] = history[-50:]
        self._write(data)


# --------------------------------------------------------------------------- #
# 注入式能力集合
# --------------------------------------------------------------------------- #
@dataclass
class RouteOps:
    """P5 依赖的只读 + 受限写入能力（全部可注入，便于离线测试）。

    - ``read_metrics() -> {if_index: MetricState}``：读各接口 IPv4/IPv6 Metric 与
      AutomaticMetric（只读，复用 P0 ``netdiag`` 管道）；
    - ``read_default_routes() -> [row]``：读 0.0.0.0/0 与 ::/0 默认路由（只读）；
    - ``read_primary_egress() -> ifIndex|None``：读当前公网出口（只读）；
    - ``set_metric(if_index, family, metric) -> bool``：受限写入（仅
      ``Set-NetIPInterface -InterfaceMetric``）；
    - ``restore_automatic(if_index, family) -> bool``：恢复 AutomaticMetric=Enabled；
    - ``egress_check(target) -> EgressCheck``：按目标地址查询实际出口（只读）。
    """

    read_metrics: Callable[[], Dict[int, MetricState]]
    read_default_routes: Callable[[], List[Dict[str, Any]]]
    read_primary_egress: Callable[[], Optional[int]]
    set_metric: Callable[[int, str, int], bool]
    restore_automatic: Callable[[int, str], bool]
    egress_check: Callable[[str], EgressCheck]


# --------------------------------------------------------------------------- #
# 目标 Metric 计算（纯函数）
# --------------------------------------------------------------------------- #
def desired_metrics(target: str, *, ethernet_if: Optional[int],
                    wifi_if: Optional[int],
                    policy: Optional[FailoverPolicy] = None
                    ) -> Dict[int, int]:
    """计算一次切换的目标 InterfaceMetric（纯函数，便于审计与单测）。

    规则（方案 §4.1 / §4.2 阶段 B 3）：

    - ``target="ethernet"``：以太网 = 基线值（默认 10），Wi-Fi = 基线值（默认 50）
      —— 即"有线优先"基线；
    - ``target="wifi"``：Wi-Fi = 基线值（默认 50），以太网 = 基线值 + ``TAKEOVER_METRIC_GAP``
      —— 仅抬高**故障侧**的 Metric，使 Wi-Fi 严格优先（Windows 取更小 Metric 的接口）。

    未解析出 ifIndex 的角色不进入结果（不臆断接口）。
    """
    if target not in ROUTE_TARGETS:
        raise RouteGuardRefused("非法切换目标：%s" % target)
    policy = policy or FailoverPolicy()
    eth_base = int(policy.ethernet_interface_metric)
    wifi_base = int(policy.wifi_interface_metric)
    result: Dict[int, int] = {}
    if target == "wifi":
        if wifi_if is not None:
            result[wifi_if] = wifi_base
        if ethernet_if is not None:
            result[ethernet_if] = wifi_base + TAKEOVER_METRIC_GAP
    else:
        if ethernet_if is not None:
            result[ethernet_if] = eth_base
        if wifi_if is not None:
            result[wifi_if] = wifi_base
    return result


# --------------------------------------------------------------------------- #
# 路由卫士
# --------------------------------------------------------------------------- #
class RouteGuard:
    """受控路由切换与路由卫士（方案 §7.2）。

    事务顺序：**授权 → 目标/接口校验 → 切换冷却 → 前置条件 → 快照 → 写 journal
    (pending) → 最小变更 → 出口验证（IPv4+IPv6）→ 提交或按快照回滚**。

    默认 ``authorized=False``：:meth:`apply` **不产生任何写入**，直接返回
    :attr:`RouteGuardError.NOT_AUTHORIZED`（fail-closed）。
    """

    def __init__(self, *, ops: RouteOps, ethernet_if: Optional[int],
                 wifi_if: Optional[int], policy: Optional[FailoverPolicy] = None,
                 journal: Optional[RouteJournal] = None,
                 timers: Optional[Any] = None, authorized: bool = False,
                 log: Optional[Callable[[str], None]] = None,
                 now: Callable[[], float] = time.monotonic,
                 stamp: Optional[Callable[[], str]] = None) -> None:
        self.ops = ops
        self.ethernet_if = ethernet_if
        self.wifi_if = wifi_if
        self.policy = policy or FailoverPolicy()
        self.journal = journal if journal is not None else RouteJournal()
        self.timers = timers
        self.authorized = bool(authorized)
        self._log = log
        self._now = now
        self._stamp = stamp or _iso_now

    # ---- 外部查询 ---- #
    def interface_for(self, target: str) -> Optional[int]:
        """目标方向对应的接口 ifIndex（``None`` = 未解析）。"""
        if target == "ethernet":
            return self.ethernet_if
        if target == "wifi":
            return self.wifi_if
        return None

    def snapshot(self, target: str = "") -> RouteSnapshot:
        """只读快照（不写 journal、不改路由）。"""
        operation_id = uuid.uuid4().hex[:12]
        try:
            metrics = dict(self.ops.read_metrics() or {})
        except Exception as exc:
            self._note("读取接口 Metric 失败：%s" % type(exc).__name__)
            metrics = {}
        try:
            routes = list(self.ops.read_default_routes() or [])
        except Exception as exc:
            self._note("读取默认路由失败：%s" % type(exc).__name__)
            routes = []
        try:
            egress = self.ops.read_primary_egress()
        except Exception as exc:
            self._note("读取当前出口失败：%s" % type(exc).__name__)
            egress = None
        return RouteSnapshot(
            operation_id=operation_id, created_at=self._stamp(), target=target,
            primary_egress_if_index=egress,
            interfaces={str(k): v for k, v in metrics.items()},
            default_routes=routes,
            interface_ids={"ethernet": self.ethernet_if, "wifi": self.wifi_if})

    def verify(self, target_if_index: Optional[int]) -> EgressVerification:
        """验证实际出口是否已改为目标接口（IPv4 + IPv6 均检查）。"""
        ipv4 = self._egress(EGRESS_TARGET_V4, "IPv4")
        ipv6 = self._egress(EGRESS_TARGET_V6, "IPv6")
        if target_if_index is None:
            return EgressVerification(None, ipv4, ipv6, EgressVerdict.UNKNOWN,
                                      "目标接口 ifIndex 未解析，无法验证出口")
        if not ipv4.available:
            return EgressVerification(
                target_if_index, ipv4, ipv6, EgressVerdict.UNKNOWN,
                "IPv4 出口无法确认（%s）：不假定成功（AGENTS §3.7）"
                % (ipv4.error or "无结果"))
        if ipv4.if_index != target_if_index:
            return EgressVerification(
                target_if_index, ipv4, ipv6, EgressVerdict.UNCHANGED,
                "IPv4 实际出口仍为 ifIndex=%s，未变为目标 ifIndex=%s"
                % (ipv4.if_index, target_if_index))
        if ipv6.available and ipv6.if_index != target_if_index:
            return EgressVerification(
                target_if_index, ipv4, ipv6, EgressVerdict.IPV6_STALE,
                "IPv4 已切至目标 ifIndex=%s，但 IPv6 出口仍为 ifIndex=%s"
                "（不得只切 IPv4）" % (target_if_index, ipv6.if_index))
        ipv6_note = ("；IPv6 出口未识别（视为该接口无 IPv6 默认路由）"
                     if not ipv6.available else "；IPv6 出口同为 ifIndex=%s"
                     % ipv6.if_index)
        return EgressVerification(
            target_if_index, ipv4, ipv6, EgressVerdict.CONFIRMED,
            "已证实实际出口为 ifIndex=%s（IPv4 源地址=%s）%s"
            % (target_if_index, ipv4.source_ip or "-", ipv6_note))

    # ---- 主流程：受控切换 ---- #
    def apply(self, target: str, reason: str = "",
              *, precheck: Optional[Callable[[], Tuple[bool, str]]] = None
              ) -> RouteChangeRecord:
        """执行一次**事务式**路由切换（未授权时零写入）。"""
        record = RouteChangeRecord(operation_id=uuid.uuid4().hex[:12],
                                   created_at=self._stamp(), target=str(target),
                                   reason=str(reason or ""))
        if not self.authorized:
            return self._refuse(record, RouteGuardError.NOT_AUTHORIZED,
                                "未授权真实路由变更（需 --yes 显式授权、前台上人）")
        if target not in ROUTE_TARGETS:
            return self._refuse(record, RouteGuardError.INVALID_TARGET,
                                "非法切换目标：%r（允许 %s）"
                                % (target, "/".join(ROUTE_TARGETS)))
        target_if = self.interface_for(target)
        if target_if is None:
            return self._refuse(
                record, RouteGuardError.INTERFACE_UNRESOLVED,
                "%s 接口未解析（adapters.json 或物理分类未给出 ifIndex），"
                "拒绝臆断接口" % target)
        if self.timers is not None and not self.timers.switch_allowed(self._now()):
            return self._refuse(
                record, RouteGuardError.SWITCH_COOLDOWN,
                "切换冷却未过（剩余 %.0fs），本轮不执行路由变更"
                % (self.timers.switch_ready_at - self._now()))
        if precheck is not None:
            try:
                ok, why = precheck()
            except Exception as exc:
                ok, why = False, "前置条件检查异常：%s" % type(exc).__name__
            if not ok:
                return self._refuse(record, RouteGuardError.PRECHECK_FAILED,
                                    "前置条件不满足：%s" % (why or "未说明"))

        before = self.snapshot(target)
        record.before = before
        changes = self._plan_changes(target, before)
        record.changes = changes
        if not changes:
            # 目标状态已满足：不产生任何写入，但仍须**验证出口**才可判成功。
            verification = self.verify(target_if)
            record.verification = verification
            if verification.ok:
                record.status = RouteStatus.COMPLETED
                record.ok = True
                record.detail = ("当前 Metric 已满足 %s 优先（无需写入）且出口已验证：%s"
                                 % (target, verification.detail))
                self._note(record.detail)
                self._finish(record)
                return record
            record.status = RouteStatus.REFUSED
            record.error = _EGRESS_ERROR[verification.verdict].value
            record.detail = ("当前 Metric 已满足 %s 优先，但出口验证未通过（%s）：%s"
                             % (target, verification.verdict.value,
                                verification.detail))
            self._note(record.detail)
            return record

        try:
            self.journal.record_pending(record)     # 崩溃恢复锚点（写入之前）
        except Exception as exc:
            return self._refuse(
                record, RouteGuardError.SNAPSHOT_FAILED,
                "变更日志写入失败（%s）：为避免无法恢复，拒绝执行路由变更"
                % type(exc).__name__)

        applied: List[Dict[str, Any]] = []
        for change in changes:
            ok = self._write(change)
            if not ok:
                rollback_ok = self._restore(applied)
                record.rolled_back = rollback_ok
                record.status = RouteStatus.ROLLED_BACK
                record.error = RouteGuardError.PERMISSION_DENIED.value
                record.detail = ("写入 InterfaceMetric 失败（ifIndex=%s %s）："
                                 "可能权限不足（需管理员）；已按快照回滚=%s"
                                 % (change["if_index"], change["family"],
                                    rollback_ok))
                self._note(record.detail)
                self._finish(record)
                return record
            applied.append(change)

        self._note_switch()
        verification = self.verify(target_if)
        record.verification = verification
        if not verification.ok:
            rollback_ok = self._restore(applied)
            record.rolled_back = rollback_ok
            record.status = RouteStatus.ROLLED_BACK
            record.error = _EGRESS_ERROR[verification.verdict].value
            record.detail = ("出口验证未通过（%s）：%s；已按快照回滚=%s"
                             % (verification.verdict.value, verification.detail,
                                rollback_ok))
            self._note(record.detail)
            self._finish(record)
            return record

        record.after = self.snapshot(target)
        record.status = RouteStatus.COMPLETED
        record.ok = True
        record.detail = "已切换为 %s 优先并通过出口验证：%s" % (target,
                                                             verification.detail)
        self._note(record.detail)
        self._finish(record)
        return record

    # ---- 回滚与崩溃恢复 ---- #
    def rollback(self, record: RouteChangeRecord,
                 reason: str = "") -> RouteChangeRecord:
        """按记录中的快照回滚（仅回滚**本次已写入**的项）。"""
        if not self.authorized:
            return self._refuse(record, RouteGuardError.NOT_AUTHORIZED,
                                "未授权：拒绝回滚路由变更")
        ok = self._restore(record.changes)
        record.rolled_back = ok
        record.status = RouteStatus.ROLLED_BACK
        record.ok = False
        record.error = None if ok else RouteGuardError.ROLLBACK_FAILED.value
        record.detail = ("按快照回滚%s：%s" % ("成功" if ok else "失败",
                                             reason or "手动回滚"))
        self._note(record.detail)
        self._finish(record)
        return record

    def recover(self) -> Optional[RouteChangeRecord]:
        """崩溃恢复：读取 journal 中未完成事务并按策略处置。

        - 无 ``pending`` → 返回 ``None``（无需恢复）；
        - 当前出口已等于本次变更目标 → **提交**（变更其实已生效，不无谓回滚）；
        - 否则 → 按快照回滚到变更前状态（方案 §7.2）。

        本方法**不需要** ``authorized``（恢复是安全动作，且是崩溃后唯一出口）；
        但它只回滚**日志中已记录的**变更，不会引入新的路由修改。
        """
        pending = self.journal.pending()
        if not pending:
            return None
        record = RouteChangeRecord.from_dict(pending)
        target_if = self.interface_for(record.target)
        verification = self.verify(target_if)
        if verification.ok:
            record.status = RouteStatus.COMPLETED
            record.ok = True
            record.error = None
            record.verification = verification
            record.detail = ("崩溃恢复：检测到未完成事务 %s，但当前出口已等于目标"
                             "（%s），标记为已完成"
                             % (record.operation_id, verification.detail))
            self._note(record.detail)
            self._finish(record)
            return record
        rollback_ok = self._restore(record.changes)
        record.status = RouteStatus.ROLLED_BACK
        record.ok = False
        record.rolled_back = rollback_ok
        record.error = RouteGuardError.RECOVERED.value
        record.detail = ("崩溃恢复：检测到未完成事务 %s，当前出口未达目标，"
                         "已按快照回滚（回滚成功=%s）"
                         % (record.operation_id, rollback_ok))
        self._note(record.detail)
        self._finish(record)
        return record

    # ---- 内部 ---- #
    def _note(self, message: str) -> None:
        if self._log is not None:
            self._log(message)

    def _note_switch(self) -> None:
        if self.timers is not None:
            self.timers.note_switch(self._now(),
                                    self.policy.switch_min_interval)

    def _finish(self, record: RouteChangeRecord) -> None:
        try:
            self.journal.record_finished(record)
        except Exception as exc:
            self._note("变更日志收尾失败：%s" % type(exc).__name__)

    def _refuse(self, record: RouteChangeRecord, error: RouteGuardError,
                detail: str) -> RouteChangeRecord:
        record.status = RouteStatus.REFUSED
        record.ok = False
        record.error = error.value
        record.detail = detail
        self._note("拒绝路由变更：%s" % detail)
        return record

    def _egress(self, target: str, family: str) -> EgressCheck:
        try:
            result = self.ops.egress_check(target)
        except Exception as exc:
            return EgressCheck(family, target,
                               error="%s: %s" % (type(exc).__name__, exc))
        if result is None:
            return EgressCheck(family, target, error="路由查询无结果")
        if isinstance(result, EgressCheck):
            return EgressCheck(family, result.target or target, result.if_index,
                               result.source_ip, result.error)
        if_index = getattr(result, "if_index", None)
        source_ip = getattr(result, "source_ip", None)
        error = getattr(result, "error", None)
        return EgressCheck(family, target, if_index, source_ip, error)

    def _plan_changes(self, target: str,
                      before: RouteSnapshot) -> List[Dict[str, Any]]:
        """计算**最小变更**清单（只含与目标值不同的项）。"""
        desired = desired_metrics(target, ethernet_if=self.ethernet_if,
                                  wifi_if=self.wifi_if, policy=self.policy)
        changes: List[Dict[str, Any]] = []
        for if_index in sorted(desired):
            state = before.metric_state(if_index)
            for family in ("IPv4", "IPv6"):
                new_value = desired[if_index]
                old_value = state.metric(family) if state is not None else None
                old_auto = state.automatic(family) if state is not None else None
                if old_value == new_value and old_auto != "Enabled":
                    continue        # 已是目标状态：最小变更原则，跳过
                changes.append({"if_index": if_index, "family": family,
                                "old": old_value, "new": new_value,
                                "old_automatic": old_auto})
        return changes

    def _write(self, change: Dict[str, Any]) -> bool:
        assert_operation_allowed("set_interface_metric")
        try:
            return bool(self.ops.set_metric(int(change["if_index"]),
                                            str(change["family"]),
                                            int(change["new"])))
        except RouteGuardRefused:
            raise
        except Exception as exc:
            self._note("写入 Metric 异常：%s" % type(exc).__name__)
            return False

    def _restore(self, changes: Sequence[Dict[str, Any]]) -> bool:
        """按快照恢复：原本 AutomaticMetric=Enabled 的项恢复自动，否则写回原值。"""
        ok = True
        for change in reversed(list(changes)):
            if_index = int(change["if_index"])
            family = str(change["family"])
            old = change.get("old")
            old_auto = change.get("old_automatic")
            try:
                if old_auto == "Enabled" or old is None:
                    if not self.ops.restore_automatic(if_index, family):
                        ok = False
                else:
                    if not self.ops.set_metric(if_index, family, int(old)):
                        ok = False
            except Exception as exc:
                self._note("恢复 Metric 异常：%s" % type(exc).__name__)
                ok = False
        return ok


_EGRESS_ERROR: Dict[EgressVerdict, RouteGuardError] = {
    EgressVerdict.UNCHANGED: RouteGuardError.EGRESS_UNCHANGED,
    EgressVerdict.UNKNOWN: RouteGuardError.EGRESS_UNKNOWN,
    EgressVerdict.IPV6_STALE: RouteGuardError.EGRESS_IPV6_STALE,
    EgressVerdict.CONFIRMED: RouteGuardError.APPLY_FAILED,
}


# --------------------------------------------------------------------------- #
# P3 执行器协议：路由侧实现与组合执行器
# --------------------------------------------------------------------------- #
def normalize_outcome(kind: Any, raw: Any, *,
                      attempted: Optional[bool] = None) -> OpOutcome:
    """把执行器原始返回值归一化为 :class:`OpOutcome`（P3 协议落地）。

    **关键约定**：``str`` 一律视为**失败详情**（P3 文档 §6 的冻结语义），
    ``False`` 为失败，``True``/``None`` 为成功。P3 的
    ``NetworkOpScheduler._invoke`` 把 ``str`` 当作"执行成功并携带说明"，与文档
    不一致；P5 的执行器**不依赖该歧义**，一律返回显式 :class:`OpOutcome`，
    使"失败"不会被记成"已执行"（否则状态机会在动作失败时进入执行态）。
    """
    if isinstance(raw, OpOutcome):
        return raw
    if raw is None or raw is True:
        return OpOutcome(kind, gate_passed=True, attempted=True, executed=True,
                         detail="已执行")
    if raw is False:
        return OpOutcome(kind, gate_passed=True, attempted=True,
                         executed=False, detail="执行失败")
    return OpOutcome(kind, gate_passed=True,
                     attempted=True if attempted is None else attempted,
                     executed=False, detail=str(raw))


class MetricExecutor:
    """P3 冻结的执行器协议在**路由侧**的实现（``METRIC_WRITE``）。

    - ``METRIC_WRITE`` → :meth:`RouteGuard.apply`：成功（含"已满足目标且出口已验证"）
      返回 ``executed=True``；失败返回 ``executed=False`` 并携带失败详情；
      ``attempted`` 仅在**确实发出过写入**时为真（供状态机登记切换冷却）；
    - ``CLASH_TOUCH`` → 显式拒绝（方案 §9：核心链路管理独立于 Clash，需用户显式
      授权后按事务改动；P5 不提供自动入口），``attempted=False``；
    - 其余动作 → 拒绝。

    本类**不做授权判断**：授权由 :class:`RouteGuard`（``authorized``）与运行入口
    共同保证；执行器只负责协议适配与结果翻译。
    """

    def __init__(self, guard: RouteGuard,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.guard = guard
        self._log = log

    def execute(self, request: Any) -> OpOutcome:
        kind = getattr(request, "kind", None)
        if kind is OpKind.METRIC_WRITE:
            target = (getattr(request, "payload", {}) or {}).get("target")
            record = self.guard.apply(target, getattr(request, "reason", ""))
            attempted = bool(record.changes)
            if record.ok:
                return OpOutcome(kind, gate_passed=True, attempted=attempted,
                                 executed=True,
                                 detail=record.detail or "路由切换已执行")
            return OpOutcome(
                kind, gate_passed=True, attempted=attempted, executed=False,
                detail="路由切换未成功：%s（%s）"
                       % (record.error or "未授权", record.detail))
        if kind is OpKind.CLASH_TOUCH:
            return OpOutcome(
                kind, gate_passed=True, attempted=False, executed=False,
                detail="拒绝修改 Clash（方案 §9：核心链路管理独立于 Clash，"
                       "需用户显式授权后按事务改动）")
        return OpOutcome(kind, gate_passed=True, attempted=False, executed=False,
                         detail="路由执行器不处理该动作：%s"
                                % getattr(kind, "value", kind))


class CompositeExecutor:
    """把 P4（SNNU 侧）与 P5（路由侧）执行器组合为**单一副作用门背后的唯一执行器**。

    分流规则（执行计划 §0.4：四类动作互斥串行由调度器保证，本类只做路由分发）：

    - ``PORTAL_POST`` / ``WIFI_RECONNECT`` → ``snnu``（P4 ``SnnuBackupExecutor``）；
    - ``METRIC_WRITE`` → ``route``（P5 :class:`MetricExecutor`）；
    - ``CLASH_TOUCH`` → 显式拒绝（方案 §9 / AGENTS §3.12）。

    委派结果一律经 :func:`normalize_outcome` 归一化：P4 执行器以 ``str`` 表示失败，
    在此被正确翻译为 ``executed=False``，避免"认证失败却被记为已执行"。
    """

    def __init__(self, *, snnu: Optional[Any] = None,
                 route: Optional[Any] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.snnu = snnu
        self.route = route
        self._log = log

    def execute(self, request: Any) -> OpOutcome:
        kind = getattr(request, "kind", None)
        if kind in (OpKind.PORTAL_POST, OpKind.WIFI_RECONNECT):
            if self.snnu is None:
                return normalize_outcome(kind, "SNNU 执行器未接入：拒绝 %s"
                                         % getattr(kind, "value", kind))
            return normalize_outcome(kind, self.snnu.execute(request))
        if kind is OpKind.METRIC_WRITE:
            if self.route is None:
                return normalize_outcome(kind, "路由执行器未接入：拒绝 metric_write")
            return normalize_outcome(kind, self.route.execute(request))
        if kind is OpKind.CLASH_TOUCH:
            return OpOutcome(
                kind, gate_passed=True, attempted=False, executed=False,
                detail="拒绝修改 Clash（方案 §9：链路管理独立于 Clash；"
                       "需用户显式授权后按事务改动）")
        return normalize_outcome(kind, "未知动作：%s"
                                 % getattr(kind, "value", kind))


# --------------------------------------------------------------------------- #
# 真实 Windows 适配层（读经 P0 只读管道；写经受限白名单）
# --------------------------------------------------------------------------- #
class WindowsMetricWriter:
    """真实 InterfaceMetric 写入器（Windows only；**仅白名单写命令**）。

    与只读管道分离：``netdiag.PowerShellRunner`` 拒绝一切写命令，本类只允许
    ``Set-NetIPInterface`` 的受限参数子集（:func:`assert_write_allowed`）。
    权限不足（非管理员）时返回 ``False``，由上层**如实报告并停止**，不反复重试
    （AGENTS §3.11）。
    """

    SET_METRIC = ("Set-NetIPInterface -InterfaceIndex {if_index} "
                  "-AddressFamily {family} -InterfaceMetric {metric} "
                  "-Confirm:$false")
    RESTORE_AUTOMATIC = ("Set-NetIPInterface -InterfaceIndex {if_index} "
                         "-AddressFamily {family} -AutomaticMetric Enabled "
                         "-Confirm:$false")

    def __init__(self, runner: Optional[Callable[[str], Tuple[int, str, str]]]
                 = None, timeout: float = 25.0) -> None:
        self.runner = runner or (lambda script: _default_write_runner(script,
                                                                     timeout))

    def _execute(self, script: str) -> bool:
        assert_write_allowed(script)
        try:
            rc, _out, _err = self.runner(script)
        except Exception:
            return False
        return int(rc) == 0

    def set_metric(self, if_index: int, family: str, metric: int) -> bool:
        return self._execute(self.SET_METRIC.format(
            if_index=int(if_index), family=str(family), metric=int(metric)))

    def restore_automatic(self, if_index: int, family: str) -> bool:
        return self._execute(self.RESTORE_AUTOMATIC.format(
            if_index=int(if_index), family=str(family)))


def _default_write_runner(script: str, timeout: float) -> Tuple[int, str, str]:
    """默认写入执行器（Windows only；``powershell -Command``）。"""
    from tools import netdiag

    if sys.platform != "win32":
        return 1, "", "非 Windows 平台不支持路由写入"
    args = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
            netdiag._POWERSHELL_PRELUDE + "\n" + script]
    try:
        done = subprocess.run(args, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout,
                              creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", "%s: %s" % (type(exc).__name__, exc)
    return done.returncode, netdiag._decode(done.stdout), \
        netdiag._decode(done.stderr)


def build_windows_route_ops(observer: Any, runner: Any,
                            writer: Optional[WindowsMetricWriter] = None
                            ) -> RouteOps:
    """组装真实 :class:`RouteOps`：读经 P0 ``netdiag`` 只读管道，写经受限写入器。

    ``observer`` 为 :class:`campus_network.NetworkObserver`（提供 ``route_lookup``），
    ``runner`` 为只读命令执行器（``netdiag.PowerShellRunner`` 或假件）。
    """
    from tools import netdiag

    writer = writer or WindowsMetricWriter()

    def _read_metrics() -> Dict[int, MetricState]:
        rc, out, _err = runner.run("ip_interfaces",
                                   netdiag.COMMANDS["ip_interfaces"])
        rows = netdiag.parse_ip_interfaces(out)
        metrics: Dict[int, MetricState] = {}
        for row in rows:
            if_index = row.get("if_index")
            if if_index is None:
                continue
            state = metrics.setdefault(int(if_index), MetricState(
                if_index=int(if_index), alias=str(row.get("interface_alias")
                                                 or "")))
            family = row.get("address_family")
            if family == "IPv4":
                state.ipv4_metric = row.get("interface_metric")
                state.ipv4_automatic = row.get("automatic_metric")
            elif family == "IPv6":
                state.ipv6_metric = row.get("interface_metric")
                state.ipv6_automatic = row.get("automatic_metric")
        return metrics

    def _read_routes() -> List[Dict[str, Any]]:
        rc, out, _err = runner.run("default_routes",
                                   netdiag.COMMANDS["default_routes"])
        return netdiag.parse_default_routes(out)

    def _read_primary_egress() -> Optional[int]:
        info = observer.route_lookup(EGRESS_TARGET_V4)
        return getattr(info, "if_index", None) if info is not None else None

    def _egress_check(target: str) -> EgressCheck:
        info = observer.route_lookup(target)
        if info is None:
            return EgressCheck("", target, error="路由查询无结果")
        return EgressCheck("", target, getattr(info, "if_index", None),
                           getattr(info, "source_ip", None),
                           getattr(info, "error", None))

    return RouteOps(
        read_metrics=_read_metrics,
        read_default_routes=_read_routes,
        read_primary_egress=_read_primary_egress,
        set_metric=writer.set_metric,
        restore_automatic=writer.restore_automatic,
        egress_check=_egress_check,
    )


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def format_route_snapshot(snapshot: RouteSnapshot, *,
                          journal: Optional[RouteJournal] = None) -> str:
    """人类可读的路由快照（``route`` 命令只读输出）。"""
    lines: List[str] = []
    add = lines.append
    add("=" * 72)
    add("V0.3 P5 路由卫士快照（只读：不切换、不写 Metric）")
    add("契约: %s    时间: %s" % (snapshot.contract_version,
                                  snapshot.created_at or "-"))
    add("=" * 72)
    add("")
    add("[接口角色] 以太网 ifIndex=%s    Wi-Fi ifIndex=%s"
        % (snapshot.interface_ids.get("ethernet"),
           snapshot.interface_ids.get("wifi")))
    add("[当前公网出口] ifIndex=%s" % snapshot.primary_egress_if_index)
    add("")
    add("[接口 Metric]")
    if snapshot.interfaces:
        for key in sorted(snapshot.interfaces, key=lambda k: int(k)):
            state = snapshot.interfaces[key]
            add("  ifIndex=%s（%s）IPv4=%s(自动=%s)  IPv6=%s(自动=%s)"
                % (state.if_index, state.alias or "-", state.ipv4_metric,
                   state.ipv4_automatic or "-", state.ipv6_metric,
                   state.ipv6_automatic or "-"))
    else:
        add("  （未读取到接口 Metric）")
    add("")
    add("[默认路由]")
    if snapshot.default_routes:
        for row in snapshot.default_routes:
            add("  %-10s via %-16s ifIndex=%-4s RouteMetric=%s InterfaceMetric=%s"
                % (row.get("destination_prefix"), row.get("next_hop") or "-",
                   row.get("if_index"), row.get("route_metric"),
                   row.get("interface_metric")))
    else:
        add("  （无默认路由数据）")
    if journal is not None:
        pending = journal.pending()
        add("")
        add("[崩溃恢复] 未完成事务: %s"
            % ("无" if not pending else
               "%s（目标=%s，状态=%s）—— 请执行 route --recover"
               % (pending.get("operation_id"), pending.get("target"),
                  pending.get("status"))))
    add("=" * 72)
    return "\n".join(lines)


def format_route_change(record: RouteChangeRecord) -> str:
    """人类可读的一次路由变更结果（``route --apply`` / ``--recover`` 输出）。"""
    lines: List[str] = []
    add = lines.append
    add("=" * 72)
    add("V0.3 P5 受控路由变更（事务式：快照 → 最小变更 → 出口验证 → 回滚）")
    add("契约: %s    操作编号: %s    时间: %s"
        % (record.contract_version, record.operation_id, record.created_at))
    add("=" * 72)
    add("")
    add("目标: %s    状态: %s    结果: %s"
        % (record.target or "-", record.status.value,
           "成功" if record.ok else "未成功"))
    add("原因: %s" % (record.reason or "-"))
    add("说明: %s" % (record.detail or "-"))
    if record.error:
        add("错误码: %s" % record.error)
    add("是否回滚: %s" % ("是" if record.rolled_back else "否"))
    add("")
    add("[最小变更清单] %s" % ("（无变更）" if not record.changes
                              else "%d 项" % len(record.changes)))
    for change in record.changes:
        add("  - ifIndex=%s %s: %s → %s（原自动=%s）"
            % (change.get("if_index"), change.get("family"), change.get("old"),
               change.get("new"), change.get("old_automatic") or "-"))
    if record.verification is not None:
        add("")
        add("[出口验证] %s" % record.verification.verdict.value)
        add("  %s" % record.verification.detail)
        for check in (record.verification.ipv4, record.verification.ipv6):
            if check is not None:
                add("  - %s 目标=%s 出口 ifIndex=%s 源地址=%s%s"
                    % (check.family, check.target, check.if_index,
                       check.source_ip or "-",
                       "" if not check.error else "（%s）" % check.error))
    add("=" * 72)
    return "\n".join(lines)
