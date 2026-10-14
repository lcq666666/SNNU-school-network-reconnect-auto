# -*- coding: utf-8 -*-
"""共享契约、统一错误码与配置验证（阶段 1 草案）。

本模块只放「类型 + 验证 + 错误码 + 调度接口」，**不含业务流程**
（不发起认证、不做网络探测、不写注册表）。目的：

- 让 CLI（``campus_reconnect.py``）与托盘（``campus_reconnect_tray.py``）
  共享同一套配置语义、状态枚举与错误分类；
- 为后续阶段（2/3/4）的每一项修复提供明确的回归目标与断言锚点。

安全约定：
- :class:`Config` **不含密码字段**，密码只留在 Windows 凭据管理器；
- 秘密值用 :class:`Secret` 容器包裹，其 ``repr``/``str`` 永不输出明文；
- 非法配置一律回退到「安全默认值」，且**绝不**因此启用系统代理联动。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (Any, Callable, Dict, List, Mapping, Optional, Sequence,
                    Tuple)
from urllib.parse import urljoin, urlsplit

__all__ = [
    "__version__", "SCHEMA_VERSION", "OPERATORS", "ACCOUNT_FORMATS",
    "INTERVAL_RANGE", "THRESHOLD_RANGE", "COOLDOWN_RANGE",
    "ERROR_CODES", "ERROR_MESSAGES",
    "E_CONFIG_INVALID", "E_CONFIG_IO", "E_CRED_MISSING", "E_CRED_BACKEND",
    "E_PROBE_UNRELIABLE", "E_PORTAL_UNREACHABLE", "E_AUTH_REJECTED",
    "E_AUTH_CAPTCHA", "E_PORTAL_STRUCTURE_UNKNOWN", "E_PROXY_ENDPOINT_INVALID",
    "E_PROXY_WRITE", "E_NETS_FAILED", "E_CANCELLED", "E_MUTEX_FAILED",
    "MISSING", "Secret", "Config", "ConfigIssue", "ConfigLoadResult",
    "ProbeResult", "PortalStatus", "RepairResult", "PortalResolution",
    "BrowserLaunchResult", "ProxyApplyResult", "CredentialLoad",
    "CredentialSave", "CredentialClear", "SingleInstanceResult", "Scheduler",
    "TimerTable",
    "validate_config", "load_config_text", "load_config_file", "default_config",
    # ---- 阶段 3：共享重连服务、状态页解析、重定向与探测分类 ----
    "ONLINE_MARKER", "MAX_BODY_BYTES", "PROBE_EXACT", "PROBE_HTML_TITLE",
    "LoginOutcome", "StatusPageResult", "ProbeSpec", "RepairOptions",
    "RepairAdapters",
    "normalize_account", "parse_status_page", "extract_error_hint",
    "probe_matches", "classify_network", "resolve_url",
    "check_redirect_target", "repair_service",
]


# --------------------------------------------------------------------------- #
# 版本与取值范围
# --------------------------------------------------------------------------- #
#: 应用版本（唯一来源；``pyproject.toml`` 与打包版本资源均应与之一致，F22）。
__version__ = "0.2.0"

SCHEMA_VERSION = 1

#: 运营商（对应 Portal 表单字段 ``yys``）；空串是**合法值**（默认校园网线路）。
OPERATORS: Tuple[str, ...] = ("mobile", "unicom", "telecom", "")

#: 账号提交格式：plain=学号（同浏览器表单）；isp=学号@运营商。
ACCOUNT_FORMATS: Tuple[str, ...] = ("plain", "isp")

INTERVAL_RANGE: Tuple[int, int] = (5, 3600)
THRESHOLD_RANGE: Tuple[int, int] = (1, 20)
COOLDOWN_RANGE: Tuple[int, int] = (60, 86400)


# --------------------------------------------------------------------------- #
# 统一错误码表
# --------------------------------------------------------------------------- #
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_IO = "E_CONFIG_IO"
E_CRED_MISSING = "E_CRED_MISSING"
E_CRED_BACKEND = "E_CRED_BACKEND"
E_PROBE_UNRELIABLE = "E_PROBE_UNRELIABLE"
E_PORTAL_UNREACHABLE = "E_PORTAL_UNREACHABLE"
E_AUTH_REJECTED = "E_AUTH_REJECTED"
E_AUTH_CAPTCHA = "E_AUTH_CAPTCHA"
E_PORTAL_STRUCTURE_UNKNOWN = "E_PORTAL_STRUCTURE_UNKNOWN"
E_PROXY_ENDPOINT_INVALID = "E_PROXY_ENDPOINT_INVALID"
E_PROXY_WRITE = "E_PROXY_WRITE"
E_NETS_FAILED = "E_NETS_FAILED"
E_CANCELLED = "E_CANCELLED"
#: 单实例互斥体创建失败（fail-closed：拒绝后台认证 / 降级只读，F18）。
E_MUTEX_FAILED = "E_MUTEX_FAILED"

#: 错误码 -> 面向用户的中文说明（不含任何敏感信息）。
ERROR_MESSAGES: Dict[str, str] = {
    E_CONFIG_INVALID: "配置内容不合法，已回退到安全默认值",
    E_CONFIG_IO: "配置读取失败（文件损坏或不可读），已回退到安全默认值",
    E_CRED_MISSING: "尚未保存凭据，请先执行 configure",
    E_CRED_BACKEND: "凭据后端不可用（读取/写入失败）",
    E_PROBE_UNRELIABLE: "连通性探测结果不可信（探测站点受限或 DNS 异常）",
    E_PORTAL_UNREACHABLE: "Portal 不可达（可能是 Wi-Fi 或网关故障）",
    E_AUTH_REJECTED: "Portal 拒绝认证（账号或密码错误）",
    E_AUTH_CAPTCHA: "Portal 要求验证码，需要人工处理",
    E_PORTAL_STRUCTURE_UNKNOWN: "无法识别 Portal 页面结构（疑似改版），需要人工确认",
    E_PROXY_ENDPOINT_INVALID: "代理端点无效或未在监听，未启用代理联动",
    E_PROXY_WRITE: "写入系统代理设置失败",
    E_NETS_FAILED: "Wi-Fi 重连失败",
    E_CANCELLED: "操作已取消（暂停或退出）",
    E_MUTEX_FAILED: "单实例互斥体创建失败，已拒绝后台认证（fail-closed）",
}

ERROR_CODES = frozenset(ERROR_MESSAGES)


# --------------------------------------------------------------------------- #
# 秘密容器
# --------------------------------------------------------------------------- #
class Secret:
    """秘密容器：``repr``/``str`` 永不输出明文。

    约定：任何可能承载密码/Cookie 的对象都应把秘密字段放进本容器，
    或使用 ``dataclasses.field(repr=False)``，以免日志、异常、序列化泄漏明文。
    """

    __slots__ = ("_value",)

    def __init__(self, value: str = "") -> None:
        self._value = value

    def reveal(self) -> str:
        """显式取出明文（仅在真正需要提交给 Portal 的瞬间调用）。"""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    """运行配置（**不含密码**）。

    字段含义：
    - ``schema_version``：配置结构版本，用于迁移与拒绝未知版本；
    - ``ssid``：目标校园 Wi-Fi 名称（完整匹配）；
    - ``profile`` / ``interface``：可选的 profile 名与网卡名（阶段 4 使用）；
    - ``operator``：运营商（``yys``），取值见 :data:`OPERATORS`，空串合法；
    - ``account_format``：账号提交格式，见 :data:`ACCOUNT_FORMATS`；
    - ``interval`` / ``threshold`` / ``cooldown``：探测间隔秒 / 失败阈值 / 重连冷却秒；
    - ``browser_fallback``：失败时是否允许 Edge 兜底；
    - ``proxy_consent``：代理联动授权三态（``None`` 未知 / ``True`` 同意 / ``False`` 拒绝）；
    - ``proxy_endpoint``：用户显式确认的代理端点（形如 ``127.0.0.1:7897``）。
    """

    schema_version: int = SCHEMA_VERSION
    ssid: str = "SNNU"
    profile: Optional[str] = None
    interface: Optional[str] = None
    operator: str = "mobile"
    account_format: str = "plain"
    interval: int = 15
    threshold: int = 3
    cooldown: int = 180
    browser_fallback: bool = True
    proxy_consent: Optional[bool] = None
    proxy_endpoint: Optional[str] = None

    @property
    def proxy_enabled(self) -> bool:
        """仅当用户**显式同意**（``proxy_consent is True``）才允许启用代理联动。"""
        return self.proxy_consent is True


def default_config() -> Config:
    """返回安全默认配置（代理联动默认关闭）。"""
    return Config()


@dataclass(frozen=True)
class ConfigIssue:
    """一条配置问题记录（脱敏：只含键名、错误码与说明，不含原始值）。"""

    key: str
    code: str
    detail: str


@dataclass(frozen=True)
class ConfigLoadResult:
    """配置加载结果。

    - ``config``：校验后的安全配置（非法输入已回退到默认值）；
    - ``issues``：发现的问题列表（供 UI 显示可见错误）；
    - ``unknown_keys``：被隔离的未知键名（已从 config 中剔除）；
    - ``source_ok``：原始来源是否可信（``False`` 表示整体回退）。
    """

    config: Config
    issues: Tuple[ConfigIssue, ...] = ()
    unknown_keys: Tuple[str, ...] = ()
    source_ok: bool = True

    @property
    def ok(self) -> bool:
        """无任何问题且来源可信时为 ``True``。"""
        return self.source_ok and not self.issues


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #
class _Missing:
    """「原本不存在」哨兵：用于区分「注册表值缺失」与「值为 None」。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<missing>"

    def __bool__(self) -> bool:
        return False


#: 哨兵实例：表示某个注册表值在修改前**不存在**（回滚时应删除）。
MISSING = _Missing()


@dataclass(frozen=True)
class ProbeResult:
    """单个连通性探测结果（**不含响应正文**）。

    ``source`` 为探测地址标识；``status_code`` 为 HTTP 状态；``error`` 为
    错误类别（超时/DNS/连接）；``elapsed`` 为耗时秒；``redirected`` 表示是否发生跳转。
    """

    ok: bool
    source: str = ""
    status_code: Optional[int] = None
    error: Optional[str] = None
    elapsed: float = 0.0
    redirected: bool = False


class PortalStatus(str, Enum):
    """Portal / 网络状态枚举（两入口共享）。

    - ``STARTING``：启动中；``ONLINE``：公网与认证均正常；
    - ``SUSPECT``：探测可疑（不等于认证失败）；
    - ``WIFI_DISCONNECTED``：Wi-Fi 未连接；``NON_TARGET``：非目标网络；
    - ``AUTH_REQUIRED``：明确需要认证；``REPAIRING``：正在重连；
    - ``AUTHENTICATED_DEGRADED``：Portal 已认证但公网异常；
    - ``NEEDS_USER``：需要人工处理（验证码/密码错误/结构未知）；
    - ``ERROR``：内部错误；``PAUSED`` / ``STOPPING``：暂停 / 正在退出。
    """

    STARTING = "STARTING"
    ONLINE = "ONLINE"
    SUSPECT = "SUSPECT"
    WIFI_DISCONNECTED = "WIFI_DISCONNECTED"
    NON_TARGET = "NON_TARGET"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    REPAIRING = "REPAIRING"
    AUTHENTICATED_DEGRADED = "AUTHENTICATED_DEGRADED"
    NEEDS_USER = "NEEDS_USER"
    ERROR = "ERROR"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"


@dataclass(frozen=True)
class RepairResult:
    """一次重连的结果（联网 / 认证 / 代理三项独立）。

    ``network_ok``、``auth_ok``、``proxy_ok`` 各自独立，不得互相谎报；
    ``error_code`` 取自 :data:`ERROR_CODES`；``retryable`` 表示是否可自动重试；
    ``needs_user`` 表示需要人工介入（验证码/密码错误等）。
    """

    op_id: str = ""
    network_ok: Optional[bool] = None
    auth_ok: Optional[bool] = None
    proxy_ok: Optional[bool] = None
    error_code: Optional[str] = None
    retryable: bool = True
    elapsed: float = 0.0
    port: Optional[int] = None
    needs_user: bool = False
    #: 细分类别（F08）；``None`` 表示未分类。
    status: Optional["PortalStatus"] = None
    #: 面向用户的说明（不含密码/Cookie）。
    detail: str = ""
    #: 是否复用已有会话（``True`` 表示本次**未**验证新凭据，F06）。
    already_authenticated: bool = False

    @property
    def ok(self) -> bool:
        """联网与认证均成功且无错误码时视为整体成功。"""
        return self.network_ok is True and self.auth_ok is True and self.error_code is None


@dataclass(frozen=True)
class PortalResolution:
    """Portal 认证端口解析结果。

    ``port`` 为解析出的端口（失败为 ``None``，**不假装**已选端口）；
    ``source`` 说明来源；``error`` 为错误码；``evidence`` 为脱敏证据摘要。
    """

    port: Optional[int] = None
    source: str = ""
    error: Optional[str] = None
    evidence: str = ""

    @property
    def ok(self) -> bool:
        """解析出有效端口且无错误时为 ``True``。"""
        return self.port is not None and self.error is None


@dataclass(frozen=True)
class BrowserLaunchResult:
    """Edge 兜底启动结果（供通知如实反馈，不虚报成功）。"""

    launched: bool = False
    executable: Optional[str] = None
    error: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class ProxyApplyResult:
    """系统代理写入结果（F04 / F05）。

    - ``ok``：整个事务（含 Windows 刷新通知）是否成功；
    - ``changed``：注册表是否实际发生了变更；
    - ``enabled`` / ``endpoint``：本次目标状态与端点；
    - ``error_code``：取自 :data:`ERROR_CODES`，失败时给出可区分原因；
    - ``rollback_ok``：发生失败后是否成功回滚（``None`` = 无需回滚）；
    - ``refresh_ok``：``InternetSetOptionW`` 等刷新通知是否成功（``None`` = 未尝试）；
    - ``changes``：本次实际写入的 ``(名称, 旧值, 新值)``，旧值为 :data:`MISSING` 表示原本不存在；
    - ``before`` / ``after``：修改前后的完整快照（脱敏，仅代理相关值）。

    约定：``ok is True`` 才可对外声称“已成功切换系统代理”；注册表写成功
    不等于所有应用立即采用，``refresh_ok`` 单独报告。
    """

    ok: bool = False
    changed: bool = False
    enabled: Optional[bool] = None
    endpoint: Optional[str] = None
    error_code: Optional[str] = None
    detail: str = ""
    rollback_ok: Optional[bool] = None
    refresh_ok: Optional[bool] = None
    changes: Tuple[Tuple[str, Any, Any], ...] = ()
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class CredentialLoad:
    """凭据读取结果（F11）。

    ``ok`` 为 ``True`` 时 ``username`` 与 ``password`` 有效；失败时
    ``error_code`` 区分「尚未配置」（``E_CRED_MISSING``）与「后端不可用」
    （``E_CRED_BACKEND``），不得把两者混同。
    """

    ok: bool = False
    username: Optional[str] = None
    password: Secret = field(default_factory=Secret)
    error_code: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class CredentialSave:
    """凭据写入结果（F11）。

    Windows 凭据管理器没有跨键事务：先写 username 再写 password，第二步失败
    时尝试恢复旧对。``rolled_back`` 单独报告恢复是否成功（``None`` = 未尝试）。
    """

    ok: bool = False
    error_code: Optional[str] = None
    detail: str = ""
    rolled_back: Optional[bool] = None


@dataclass(frozen=True)
class CredentialClear:
    """凭据清除结果（F11）。"""

    ok: bool = False
    error_code: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class SingleInstanceResult:
    """单实例互斥体获取结果（F18）。

    - ``ok=True``：成功持有互斥体（或非 Windows 平台直接放行）；
    - ``already_running=True``：已有实例在运行（``ERROR_ALREADY_EXISTS``），
      调用方应退出；
    - ``ok=False, already_running=False``：创建失败（fail-closed），调用方应
      **拒绝后台认证**或降级为只读，而不是继续。
    """

    ok: bool = False
    already_running: bool = False
    error_code: Optional[str] = None
    detail: str = ""

    @property
    def failed_closed(self) -> bool:
        return not self.ok and not self.already_running


# --------------------------------------------------------------------------- #
# 调度接口草案
# --------------------------------------------------------------------------- #
@dataclass
class Scheduler:
    """调度接口草案（阶段 1 只定义结构与语义，不含网络流程）。

    语义约定：
    - 以**单调时钟** ``clock`` 驱动，不使用墙钟；
    - 认证开始前调用 :meth:`register_attempt`，冷却在 ``finally`` 中也要生效；
    - ``cooldown`` 只禁止**新的认证**，不禁止探测 / UI / 只读诊断；
    - 不可重试失败（如密码错误）进入 ``needs_user``，禁止自动频繁重试；
    - 网络类失败指数退避并设上限；成功**不抹掉**最短间隔。
    """

    clock: Callable[[], float]
    interval: float = 15.0
    threshold: int = 3
    cooldown: float = 180.0
    max_backoff: float = 1800.0
    failure_streak: int = 0
    busy: bool = False
    last_attempt: Optional[float] = None
    next_attempt_at: float = 0.0
    backoff: float = 0.0
    config_version: int = 0
    needs_user: bool = False

    def can_attempt(self) -> bool:
        """当前是否允许发起一次新的认证。"""
        if self.busy or self.needs_user:
            return False
        return self.clock() >= self.next_attempt_at

    def register_attempt(self) -> None:
        """认证开始前登记（置忙并记录尝试时刻）。"""
        self.busy = True
        self.last_attempt = self.clock()

    def finish_attempt(self) -> None:
        """认证结束（无论成败）清理忙标志。"""
        self.busy = False

    def note_success(self) -> None:
        """网络恢复：重置失败计数与退避，但**保留**最短间隔（``next_attempt_at``）。"""
        self.failure_streak = 0
        self.backoff = 0.0
        self.needs_user = False

    def note_failure(self, retryable: bool = True) -> None:
        """记录一次失败。

        - ``retryable=False``：进入 ``needs_user``，不再自动尝试；
        - ``retryable=True``：指数退避（不小于 cooldown，不超过 max_backoff）。
        """
        self.failure_streak += 1
        if not retryable:
            self.needs_user = True
            self.next_attempt_at = float("inf")
            return
        if self.backoff <= 0:
            self.backoff = self.cooldown
        else:
            self.backoff = min(self.max_backoff, self.backoff * 2)
        self.next_attempt_at = self.clock() + max(self.cooldown, self.backoff)

    def bump_config_version(self, version: int) -> None:
        """配置版本变化：记录版本并重置失败计数（不解除冷却）。"""
        self.config_version = version
        self.failure_streak = 0


# --------------------------------------------------------------------------- #
# 三表分离：网络检测 / 认证冷却 / 切换冷却（V0.3 P2）
# --------------------------------------------------------------------------- #
@dataclass
class TimerTable:
    """三表分离的计时器组（V0.3 P2；AGENTS §3.4、执行计划 §0.2 P2）。

    缺陷背景（P0-T6 取证，基线 ``4900ca9``）：V0.2 把「Portal 认证冷却」
    错误地叠加到「网络检测」上 —— ``campus_reconnect.watch()`` 的
    ``wait = max(interval, next_attempt - now)`` 与托盘 ``_next_probe_time``
    的 ``max(now + interval, _next_attempt_at)``，都让 180s 认证冷却顺带把
    公网探测整体暂停；公网一旦在冷却期内恢复，发现被推迟最多一个冷却周期。

    本类把三个**互不干涉**的计时器显式分开，任何一处冷却都不得推迟网络检测：

    - :attr:`next_detection_at` —— **网络检测计时器**：持续运行，每个
      ``interval`` 到点即探测；只由 :meth:`schedule_detection` 推进，
      **不受**认证/切换冷却影响。
    - :attr:`auth_ready_at` —— **Portal 认证冷却**：只限制**新的认证尝试**
      （:meth:`auth_allowed`），不限制探测。
    - :attr:`switch_ready_at` —— **网卡切换冷却**：只限制**路由变更**
      （:meth:`switch_allowed`）。P2 无路由切换，该表由 P3 的路由控制器消费。

    纯数据结构：不持有时钟、不做 I/O；所有方法显式接收 ``now``，便于用虚拟
    时钟离线断言（零真实 sleep）。
    """

    next_detection_at: float = 0.0
    auth_ready_at: float = 0.0
    switch_ready_at: float = 0.0

    # ---- 网络检测计时器：持续运行，与任何冷却无关 ----
    def detection_due(self, now: float) -> bool:
        """检测计时器是否到点（与认证/切换冷却无关）。"""
        return now >= self.next_detection_at

    def schedule_detection(self, now: float, interval: float) -> float:
        """登记下一次检测时刻 = ``now + interval``（**恒**与冷却无关）。"""
        self.next_detection_at = now + max(0.0, float(interval))
        return self.next_detection_at

    # ---- Portal 认证冷却：只限制认证 ----
    def auth_allowed(self, now: float) -> bool:
        """当前是否已过认证冷却，允许发起一次新的认证尝试。"""
        return now >= self.auth_ready_at

    def note_auth(self, now: float, cooldown: float) -> float:
        """登记一次认证尝试后的冷却截止时刻（只影响认证）。"""
        self.auth_ready_at = now + max(0.0, float(cooldown))
        return self.auth_ready_at

    # ---- 网卡切换冷却：只限制路由变更（P3 消费） ----
    def switch_allowed(self, now: float) -> bool:
        """当前是否已过切换冷却，允许执行一次路由变更。"""
        return now >= self.switch_ready_at

    def note_switch(self, now: float, cooldown: float) -> float:
        """登记一次路由变更后的切换冷却截止时刻（只影响路由变更）。"""
        self.switch_ready_at = now + max(0.0, float(cooldown))
        return self.switch_ready_at


# --------------------------------------------------------------------------- #
# 配置验证器
# --------------------------------------------------------------------------- #
def _is_int(value: Any) -> bool:
    """真正整数（显式排除 ``bool``，因为 ``bool`` 是 ``int`` 的子类）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _opt_str(value: Any) -> Optional[str]:
    """把值规范为可选字符串：``None`` 原样返回，其余非字符串返回 ``None``。"""
    if value is None:
        return None
    return value if isinstance(value, str) else None


def _check_int(raw: Any, key: str, lo: int, hi: int, default: int,
               issues: list) -> int:
    """校验区间整数；非法则记录问题并回退默认值。"""
    if not _is_int(raw):
        issues.append(ConfigIssue(key, E_CONFIG_INVALID,
                                  "必须是整数（当前类型 %s）" % type(raw).__name__))
        return default
    if not (lo <= raw <= hi):
        issues.append(ConfigIssue(key, E_CONFIG_INVALID,
                                  "超出允许范围 [%d, %d]" % (lo, hi)))
        return default
    return raw


def _check_bool(raw: Any, key: str, default: bool, issues: list) -> bool:
    """校验布尔开关；仅接受真正的 ``bool``，字符串 ``"false"`` 等一律非法。"""
    if isinstance(raw, bool):
        return raw
    issues.append(ConfigIssue(key, E_CONFIG_INVALID,
                              "必须是布尔值（当前类型 %s）" % type(raw).__name__))
    return default


def _check_consent(raw: Any, issues: list) -> Optional[bool]:
    """校验代理授权三态：仅接受 ``None`` / ``True`` / ``False``。

    缺失或非法（含字符串 ``"false"``）一律回退 ``None``（未知），**不启用**代理。
    """
    if raw is None or isinstance(raw, bool):
        return raw
    issues.append(ConfigIssue("proxy_consent", E_CONFIG_INVALID,
                              "授权只能是 null/true/false（当前类型 %s）" % type(raw).__name__))
    return None


def validate_config(raw: Any) -> ConfigLoadResult:
    """把任意已解析的 JSON 对象校验为 :class:`Config`。

    规则（对应 F03）：
    - 顶层必须是 JSON object，否则整体回退安全默认值；
    - 数值只接受真正 ``int``（排除 ``bool``）并统一范围；
    - 开关只接受 ``bool``；代理授权只接受 ``None/True/False``；
    - 未知键隔离并记录**脱敏**告警（不记录原始值）；
    - 未知 ``schema_version`` 视为整体不可信，回退默认值。
    """
    issues: list = []

    if raw is None:
        issues.append(ConfigIssue("<root>", E_CONFIG_INVALID, "配置为空"))
        return ConfigLoadResult(default_config(), tuple(issues), (), False)

    if not isinstance(raw, dict):
        issues.append(ConfigIssue("<root>", E_CONFIG_INVALID,
                                  "顶层必须是 JSON 对象（当前类型 %s）" % type(raw).__name__))
        return ConfigLoadResult(default_config(), tuple(issues), (), False)

    # ---- schema_version ----
    if "schema_version" in raw:
        version = raw["schema_version"]
        if not _is_int(version):
            issues.append(ConfigIssue("schema_version", E_CONFIG_INVALID,
                                      "版本号必须是整数"))
            return ConfigLoadResult(default_config(), tuple(issues), (), False)
        if version != SCHEMA_VERSION:
            issues.append(ConfigIssue("schema_version", E_CONFIG_INVALID,
                                      "未知版本 %s（当前支持 %d）" % (version, SCHEMA_VERSION)))
            return ConfigLoadResult(default_config(), tuple(issues), (), False)
    else:
        issues.append(ConfigIssue("schema_version", E_CONFIG_INVALID, "缺少版本号，按当前版本解析"))

    base = default_config()

    # ---- 字符串字段 ----
    ssid = base.ssid
    if "ssid" in raw:
        value = raw["ssid"]
        if not isinstance(value, str):
            issues.append(ConfigIssue("ssid", E_CONFIG_INVALID, "必须是字符串"))
        else:
            stripped = value.strip()
            if stripped:
                ssid = stripped
            else:
                issues.append(ConfigIssue("ssid", E_CONFIG_INVALID, "不能为空"))

    operator = base.operator
    op_key = "yys" if "yys" in raw else ("operator" if "operator" in raw else None)
    if op_key is not None:
        value = raw[op_key]
        if isinstance(value, str) and value in OPERATORS:
            operator = value
        else:
            issues.append(ConfigIssue(op_key, E_CONFIG_INVALID,
                                      "必须是 %s 之一" % "/".join(repr(o) for o in OPERATORS)))

    account_format = base.account_format
    if "account_format" in raw:
        value = raw["account_format"]
        if isinstance(value, str) and value in ACCOUNT_FORMATS:
            account_format = value
        else:
            issues.append(ConfigIssue("account_format", E_CONFIG_INVALID,
                                      "必须是 %s 之一" % "/".join(ACCOUNT_FORMATS)))

    profile = _opt_str(raw.get("profile"))
    interface = _opt_str(raw.get("interface"))
    proxy_endpoint = _opt_str(raw.get("proxy_endpoint"))

    # ---- 数值字段 ----
    interval = _check_int(raw.get("interval", base.interval), "interval",
                          *INTERVAL_RANGE, base.interval, issues)
    threshold = _check_int(raw.get("threshold", base.threshold), "threshold",
                           *THRESHOLD_RANGE, base.threshold, issues)
    cooldown = _check_int(raw.get("cooldown", base.cooldown), "cooldown",
                          *COOLDOWN_RANGE, base.cooldown, issues)

    # ---- 开关与授权 ----
    browser_fallback = _check_bool(raw.get("browser_fallback", base.browser_fallback),
                                   "browser_fallback", base.browser_fallback, issues)

    consent_key = None
    for candidate in ("clash_auto_proxy", "proxy_consent"):
        if candidate in raw:
            consent_key = candidate
            break
    if consent_key is not None:
        proxy_consent = _check_consent(raw[consent_key], issues)
    else:
        proxy_consent = None

    known = {
        "schema_version", "ssid", "profile", "interface", "operator", "yys",
        "account_format", "interval", "threshold", "cooldown",
        "browser_fallback", "clash_auto_proxy", "proxy_consent", "proxy_endpoint",
        # 托盘本地状态键（非认证引擎字段，由托盘自行读写；此处登记以避免
        # 被误判为"未知配置键"）：暂停标志（P2）与 V0.3 P6 三模式/锁定/Clash。
        "paused", "failover_mode", "failover_lock", "clash_keep",
        "auto_switch_authorized",
    }
    unknown_keys = tuple(sorted(k for k in raw if k not in known))
    for key in unknown_keys:
        # 脱敏：只记录键名，不记录值
        issues.append(ConfigIssue(key, E_CONFIG_INVALID, "未知配置键，已隔离"))

    config = Config(
        schema_version=SCHEMA_VERSION,
        ssid=ssid,
        profile=profile,
        interface=interface,
        operator=operator,
        account_format=account_format,
        interval=interval,
        threshold=threshold,
        cooldown=cooldown,
        browser_fallback=browser_fallback,
        proxy_consent=proxy_consent,
        proxy_endpoint=proxy_endpoint,
    )
    return ConfigLoadResult(config, tuple(issues), unknown_keys, True)


def load_config_text(text: str) -> ConfigLoadResult:
    """解析配置文本（含 JSON 截断/损坏处理）并校验，返回安全结果。"""
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        issue = ConfigIssue("<root>", E_CONFIG_IO,
                            "JSON 解析失败（%s）" % type(exc).__name__)
        return ConfigLoadResult(default_config(), (issue,), (), False)
    return validate_config(raw)


def load_config_file(path: Path) -> ConfigLoadResult:
    """读取配置文件并校验；读取失败回退安全默认值，不抛异常给调用方。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        issue = ConfigIssue("<root>", E_CONFIG_IO, "读取失败（%s）" % type(exc).__name__)
        return ConfigLoadResult(default_config(), (issue,), (), False)
    return load_config_text(text)


# --------------------------------------------------------------------------- #
# 阶段 3：状态页解析、重定向校验、探测分类与共享重连服务
# --------------------------------------------------------------------------- #
#: Portal 状态页里标识「当前登录账号」的标记（两入口共享，F06）。
ONLINE_MARKER = "当前登录账号"

#: 单次响应体解析上限（字节），避免异常大页面拖垮预算（F16）。
MAX_BODY_BYTES = 200_000

#: 探测匹配模式：``exact`` = 归一化后完全相等；``html_title`` = 含精确 ``<title>`` 标签。
PROBE_EXACT = "exact"
PROBE_HTML_TITLE = "html_title"

#: 状态页账号字段：``当前登录账号：123456`` / ``当前登录账号 123456``。
_STATUS_ACCOUNT_RE = re.compile(r"当前登录账号\s*[:：]?\s*([^\s<>\"']+)")

#: 错误文字提取：只在「错误/失败/提示」等标记后的短片段里找，避免导航脚本误触发。
_ERROR_REGION_RE = re.compile(r"(?:错误|失败|提示|异常)\s*[:：]?\s*([^<>]{1,80})")

#: 验证码页特征（命中即需要人工处理，F06/T16）。
CAPTCHA_HINTS = ("验证码", "captcha", "checkcode", "verifycode", "vcode")

#: 明确的认证拒绝特征（命中 ⇒ ``E_AUTH_REJECTED``，进入需人工处理，禁止自动频繁重试）。
AUTH_REJECT_HINTS = ("密码错误", "密码不正确", "密码有误", "密码不匹配",
                     "用户不存在", "账号不存在", "账户不存在", "用户名或密码错误",
                     "欠费", "冻结", "已停机", "登录失败", "认证失败")


def normalize_account(value: Any) -> str:
    """规范化账号用于**完整比较**：去空白、去包裹引号（不做子串匹配）。"""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


class LoginOutcome(tuple):
    """登录结果：可解包为 ``(ok, detail)``，同时携带结构化状态（F06/F14）。

    兼容旧的两元组调用（``ok, detail = portal_login(...)``），并额外暴露
    ``status`` / ``error_code`` / ``already_authenticated``，供共享服务分类。
    """

    def __new__(cls, ok: bool, detail: str, status: Optional["PortalStatus"] = None,
                error_code: Optional[str] = None,
                already_authenticated: bool = False) -> "LoginOutcome":
        obj = super().__new__(cls, (bool(ok), str(detail)))
        obj.ok = bool(ok)
        obj.detail = str(detail)
        obj.status = status if status is not None else (
            PortalStatus.ONLINE if ok else PortalStatus.AUTH_REQUIRED)
        obj.error_code = error_code
        obj.already_authenticated = bool(already_authenticated)
        return obj

    def __repr__(self) -> str:  # pragma: no cover - 便于调试，不含秘密
        return "LoginOutcome(ok=%r, status=%s, detail=%r)" % (
            self.ok, getattr(self.status, "value", self.status), self.detail)


@dataclass(frozen=True)
class StatusPageResult:
    """只读状态页解析结果（F06/F08）。

    - ``status``：解析出的 Portal 状态；
    - ``account``：状态页声明的登录账号（**仅内部精确比较**，对外脱敏）；
    - ``error_code`` / ``detail``：失败原因；
    - ``marker_found``：是否出现「当前登录账号」标记。
    """

    status: "PortalStatus"
    account: Optional[str] = None
    error_code: Optional[str] = None
    detail: str = ""
    marker_found: bool = False


def parse_status_page(body: str, expected_account: Optional[str] = None) -> StatusPageResult:
    """解析 Portal 状态页（**纯函数**）。

    规则（F06）：
    - 命中验证码特征 ⇒ ``NEEDS_USER``（``E_AUTH_CAPTCHA``）；
    - 解析出明确的账号字段后**完整比较**（不是子串）：一致 ⇒ ``ONLINE``，
      不一致 ⇒ ``NEEDS_USER``（其他账号）；
    - 出现标记但无法解析账号 ⇒ 结构未知 ⇒ ``NEEDS_USER``；
    - 无标记 ⇒ 需要认证（``AUTH_REQUIRED``）。
    """
    text = body or ""
    low = text.lower()
    if any(h.lower() in low for h in CAPTCHA_HINTS):
        return StatusPageResult(PortalStatus.NEEDS_USER, error_code=E_AUTH_CAPTCHA,
                                detail="Portal 要求验证码，需要人工处理")
    marker = ONLINE_MARKER in text
    match = _STATUS_ACCOUNT_RE.search(text)
    if match:
        account = normalize_account(match.group(1))
        if expected_account is not None:
            if account and account == normalize_account(expected_account):
                return StatusPageResult(PortalStatus.ONLINE, account=account,
                                        detail="状态页账号与本次提交完整一致",
                                        marker_found=True)
            return StatusPageResult(
                PortalStatus.NEEDS_USER, account=account, error_code=E_AUTH_REJECTED,
                detail="状态页显示的账号与本次提交不一致（不视为本次认证成功）",
                marker_found=True)
        return StatusPageResult(PortalStatus.ONLINE, account=account,
                                detail="状态页已登录账号", marker_found=True)
    if marker:
        return StatusPageResult(PortalStatus.NEEDS_USER, marker_found=True,
                                error_code=E_PORTAL_STRUCTURE_UNKNOWN,
                                detail="出现登录标记但无法解析账号字段（疑似改版）")
    return StatusPageResult(PortalStatus.AUTH_REQUIRED, marker_found=False,
                            detail="状态页未显示登录账号，判定需要认证")


def extract_error_hint(body: str) -> Optional[str]:
    """仅在已识别的错误区域提取失败文字（避免导航脚本中的“失败/绑定”误触发）。"""
    text = body or ""
    hints: List[str] = []
    for match in _ERROR_REGION_RE.finditer(text):
        fragment = match.group(1).strip()
        for hint in AUTH_REJECT_HINTS:
            if hint in fragment and hint not in hints:
                hints.append(hint)
    return ", ".join(hints) if hints else None


def probe_matches(spec: "ProbeSpec", status_code: int, body: str,
                  redirected: bool = False) -> bool:
    """严格匹配探测响应结构（F08/T18）。

    - 必须是 200 且未发生跳转；
    - ``exact``：归一化后**完全相等**；
    - ``html_title``：正文包含精确的 ``<title>期望值</title>`` 标签。

    任意 200 正文里只是「含有 Success」不再视为通过。
    """
    if redirected or status_code != 200:
        return False
    text = (body or "").strip()
    if spec.mode == PROBE_EXACT:
        return text == spec.expect.strip()
    if spec.mode == PROBE_HTML_TITLE:
        pattern = r"<title>\s*%s\s*</title>" % re.escape(spec.expect.strip())
        return re.search(pattern, body or "", re.IGNORECASE) is not None
    return False


@dataclass(frozen=True)
class ProbeSpec:
    """一个连通性探测点：地址 + 期望结构 + 匹配模式。"""

    url: str
    expect: str = ""
    mode: str = PROBE_EXACT


def classify_network(wifi_ssid: Optional[str], target_ssid: Optional[str],
                     probe_results: Sequence[ProbeResult],
                     portal: Optional[StatusPageResult] = None,
                     wifi_present: bool = True) -> PortalStatus:
    """把探测/状态页信息归类为 :class:`PortalStatus`（F08）。

    先区分非目标网络 / Wi-Fi 断开；有任一探测通过即 ``ONLINE``；全部失败时结合
    只读状态页区分「已认证但公网异常 / 需要认证 / 需人工处理 / 探测不可信」。
    """
    target = (target_ssid or "").strip()
    if target:
        if not wifi_present or not wifi_ssid:
            return PortalStatus.WIFI_DISCONNECTED
        if wifi_ssid.strip() != target:   # F17：完整比较，大小写不同即非目标
            return PortalStatus.NON_TARGET
    if any(r.ok for r in probe_results):
        return PortalStatus.ONLINE
    if portal is not None:
        if portal.status is PortalStatus.ONLINE:
            return PortalStatus.AUTHENTICATED_DEGRADED
        if portal.status is PortalStatus.AUTH_REQUIRED:
            return PortalStatus.AUTH_REQUIRED
        if portal.status is PortalStatus.NEEDS_USER:
            return PortalStatus.NEEDS_USER
    return PortalStatus.SUSPECT


def resolve_url(base: str, location: str) -> Optional[str]:
    """把（可能是相对的）``Location`` 归一化为绝对 URL；缺失返回 ``None``。"""
    if not location:
        return None
    try:
        return urljoin(base.rstrip("/") + "/", location.strip())
    except ValueError:
        return None


def check_redirect_target(location: str, base: str,
                          allowed_paths: Optional[Sequence[str]] = None
                          ) -> Tuple[bool, Optional[str], str]:
    """校验重定向目标是否被允许（F07）。

    仅允许与 ``base`` 同 scheme / host / port 的 http(s) 目标，且路径在
    ``allowed_paths`` 之内；跨源、非 http(s)、越界路径一律拒绝。
    返回 ``(ok, target_url, detail)``。
    """
    target = resolve_url(base, location)
    if target is None:
        return False, None, "重定向缺少 Location"
    try:
        parsed = urlsplit(target)
        base_parsed = urlsplit(base)
    except ValueError:
        return False, None, "重定向 URL 无法解析"
    if parsed.scheme not in ("http", "https"):
        return False, None, "重定向 scheme 不被允许：%s" % (parsed.scheme or "?")
    if parsed.hostname != base_parsed.hostname:
        return False, None, "重定向跨 host（%s）不被允许" % (parsed.hostname or "?")
    if parsed.port != base_parsed.port:
        return False, None, "重定向跨 port（%s）不被允许" % (parsed.port,)
    if allowed_paths:
        path = parsed.path or "/"
        if not any(path == p or path.endswith(p) for p in allowed_paths):
            return False, None, "重定向路径 %s 不在允许范围" % path
    return True, target, ""


# --------------------------------------------------------------------------- #
# 重连选项与适配器（CLI / 托盘共用，F14）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RepairOptions:
    """一次重连请求的**不可变选项快照**（不含密码）。

    由 CLI 的 ``Namespace`` 或托盘的配置 dict / :class:`Config` 转换而来，
    保证两入口在同一输入下产生相同请求载荷（F14）。空运营商是**合法值**。
    """

    ssid: str = ""
    operator: str = "mobile"
    account_format: str = "plain"
    port: Optional[int] = None
    reconnect_wifi: bool = False
    browser_fallback: bool = True
    force: bool = False

    @classmethod
    def from_config(cls, config: Config) -> "RepairOptions":
        return cls(ssid=config.ssid or "", operator=config.operator,
                   account_format=config.account_format, port=None,
                   reconnect_wifi=False, browser_fallback=config.browser_fallback,
                   force=False)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "RepairOptions":
        """从托盘内部 dict 转换；**仅缺失键**用默认值，空串原样保留（F15）。"""
        raw_op = mapping["yys"] if "yys" in mapping else mapping.get("operator", "mobile")
        if raw_op is None or not isinstance(raw_op, str):
            raw_op = "mobile"
        return cls(
            ssid=(mapping.get("ssid") or "").strip() if isinstance(
                mapping.get("ssid"), str) else "",
            operator=raw_op,
            account_format=mapping.get("account_format", "plain")
            if isinstance(mapping.get("account_format"), str) else "plain",
            port=mapping.get("port") if isinstance(mapping.get("port"), int) else None,
            reconnect_wifi=bool(mapping.get("reconnect_wifi", False)),
            browser_fallback=bool(mapping.get("browser_fallback", True)),
            force=bool(mapping.get("force", False)),
        )

    @classmethod
    def from_namespace(cls, ns: Any) -> "RepairOptions":
        """从 argparse ``Namespace`` 转换（CLI 入口）。"""
        return cls(
            ssid=(getattr(ns, "ssid", "") or ""),
            operator=getattr(ns, "yys", "mobile"),
            account_format=getattr(ns, "account_format", "plain"),
            port=getattr(ns, "port", None),
            reconnect_wifi=bool(getattr(ns, "reconnect_wifi", False)),
            browser_fallback=bool(getattr(ns, "browser_fallback", False)),
            force=bool(getattr(ns, "force", False)),
        )


@dataclass
class RepairAdapters:
    """注入式适配器：把网络副作用抽象出来，便于两入口共用与离线测试（F14）。

    约定签名（与引擎现状保持一致）：
    - ``wifi_state() -> (ssid|None, state)``
    - ``wifi_reconnect(ssid) -> bool``
    - ``resolve_port(session, requested) -> (port|None, source)``
    - ``portal_reachable(session, port) -> bool``
    - ``probe_online(session) -> (bool, url)``
    - ``login(session, account, password, operator, port, log) -> LoginOutcome|(bool, str)``
    """

    wifi_state: Callable[[], Tuple[Optional[str], str]]
    wifi_reconnect: Callable[[str], bool]
    resolve_port: Callable[[Any, Optional[int]], Tuple[Optional[int], str]]
    portal_reachable: Callable[[Any, int], bool]
    probe_online: Callable[[Any], Tuple[bool, str]]
    login: Callable[..., Any]


def _as_login_outcome(raw: Any) -> LoginOutcome:
    """把适配器返回值规范为 :class:`LoginOutcome`（兼容旧的两元组）。"""
    if isinstance(raw, LoginOutcome):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2:
        ok, detail = raw
        return LoginOutcome(ok, detail)
    return LoginOutcome(False, "登录适配器返回了无法识别的结果")


def _ssid_matches(ssid: Optional[str], target: str) -> bool:
    """目标网络匹配（F17：**完整**比较，不做大小写折叠）。

    大小写不同视为**不同**网络，避免把 ``snnu`` 误当成 ``SNNU`` 而在非目标网络上
    提交凭据；SSID 本身也不是可信的网络身份证明，仅作必要条件。
    """
    if not ssid:
        return False
    return ssid.strip() == target.strip()


def repair_service(session: Any, options: RepairOptions, clock: Callable[[], float],
                   adapters: RepairAdapters,
                   credential_provider: Callable[[], Optional[Tuple[str, str]]],
                   cancelled: Optional[Callable[[], bool]] = None,
                   sleep: Optional[Callable[[float], None]] = None,
                   log: Optional[logging.Logger] = None) -> RepairResult:
    """CLI 与托盘共用的重连服务（F14）。

    只做「目标网络判定 → 端口解析 → Portal 可达 → 只读预检 → 认证 → 公网复核」，
    **不含** Edge / 系统代理 / UI —— 这些副作用由上层根据返回的
    :class:`RepairResult`（含 ``status`` / ``needs_user``）决定是否执行。
    """
    cancelled = cancelled or (lambda: False)
    sleep = sleep if sleep is not None else time.sleep
    log = log or logging.getLogger("campus")
    start = clock()

    def _result(**kw: Any) -> RepairResult:
        kw.setdefault("elapsed", clock() - start)
        return RepairResult(**kw)

    if cancelled():
        return _result(status=PortalStatus.STOPPING, error_code=E_CANCELLED,
                       retryable=False, detail="已退出或取消，未发起认证")

    # 1) 目标网络判定
    try:
        ssid, _state = adapters.wifi_state()
    except Exception as exc:  # 探测 Wi-Fi 失败按未连接处理，不静默失效
        log.warning("读取 Wi-Fi 状态异常：%s", type(exc).__name__)
        ssid = None
    target = (options.ssid or "").strip()
    if target and not _ssid_matches(ssid, target):
        if options.reconnect_wifi and not cancelled():
            log.warning("当前未连接 Wi-Fi，尝试重连 %s ...", target)
            try:
                adapters.wifi_reconnect(target)
            except Exception as exc:
                log.warning("Wi-Fi 重连异常：%s", type(exc).__name__)
            for _ in range(6):
                sleep(5)
                if cancelled():
                    break
                try:
                    ssid, _state = adapters.wifi_state()
                except Exception:
                    ssid = None
                if _ssid_matches(ssid, target):
                    break
        if cancelled():
            return _result(status=PortalStatus.STOPPING, error_code=E_CANCELLED,
                           retryable=False, detail="已取消，未发起认证")
        if not ssid:
            return _result(network_ok=False, status=PortalStatus.WIFI_DISCONNECTED,
                           error_code=E_NETS_FAILED, retryable=True,
                           detail="未检测到目标 Wi-Fi（%s）" % target)
        if not _ssid_matches(ssid, target):
            return _result(network_ok=False, status=PortalStatus.NON_TARGET,
                           error_code=E_NETS_FAILED, retryable=True,
                           detail="当前 Wi-Fi 为 %s，非目标 %s" % (ssid, target))

    # 2) 只读预检：已在线则不 POST（F06/F08；--force 时跳过）
    if not options.force:
        online = False
        try:
            online, _via = adapters.probe_online(session)
        except Exception as exc:
            log.warning("预检探测异常，按未在线处理：%s", type(exc).__name__)
        if online:
            return _result(network_ok=True, status=PortalStatus.ONLINE,
                           detail="公网已连通，无需认证")
        if cancelled():
            return _result(status=PortalStatus.STOPPING, error_code=E_CANCELLED,
                           retryable=False, detail="已取消，未发起认证")

    # 3) 端口解析
    try:
        port, source = adapters.resolve_port(session, options.port)
    except Exception as exc:
        return _result(error_code=E_PORTAL_UNREACHABLE, retryable=True,
                       status=PortalStatus.SUSPECT,
                       detail="端口解析异常：%s" % type(exc).__name__)
    if port is None:
        return _result(error_code=E_PORTAL_UNREACHABLE, retryable=True,
                       status=PortalStatus.SUSPECT,
                       detail="无法确定 Portal 认证端口：%s" % source)

    # 4) Portal 可达性（未验证可达前不取凭据、不提交）
    try:
        reachable = bool(adapters.portal_reachable(session, port))
    except Exception as exc:
        log.warning("Portal 可达性检查异常：%s", type(exc).__name__)
        reachable = False
    if not reachable:
        return _result(port=port, network_ok=False, retryable=True,
                       status=PortalStatus.SUSPECT, error_code=E_PORTAL_UNREACHABLE,
                       detail="Portal 登录页不可达（端口 %d）" % port)
    if cancelled():
        return _result(port=port, status=PortalStatus.STOPPING, error_code=E_CANCELLED,
                       retryable=False, detail="已取消，未提交认证")

    # 5) 凭据
    try:
        creds = credential_provider()
    except Exception as exc:
        return _result(port=port, error_code=E_CRED_BACKEND, retryable=False,
                       needs_user=True, status=PortalStatus.NEEDS_USER,
                       detail="凭据后端不可用：%s" % type(exc).__name__)
    if not creds:
        return _result(port=port, error_code=E_CRED_MISSING, retryable=False,
                       needs_user=True, status=PortalStatus.NEEDS_USER,
                       detail="尚未保存校园网凭据（请先运行 configure）")

    account, password = creds
    # isp 格式：运营商为空时**不**拼接尾部 "@"（F20）。
    if options.account_format == "isp" and options.operator:
        account_to_send = "%s@%s" % (account, options.operator)
    else:
        account_to_send = account

    # 6) 认证
    try:
        outcome = _as_login_outcome(adapters.login(
            session, account_to_send, password, options.operator, port, log))
    except Exception as exc:
        return _result(port=port, error_code=E_PORTAL_STRUCTURE_UNKNOWN,
                       retryable=True, status=PortalStatus.SUSPECT,
                       detail="认证请求异常：%s" % type(exc).__name__)
    if not outcome.ok:
        retryable = outcome.status is not PortalStatus.NEEDS_USER
        return _result(port=port, network_ok=False, auth_ok=False,
                       error_code=outcome.error_code or E_AUTH_REJECTED,
                       retryable=retryable,
                       needs_user=(outcome.status is PortalStatus.NEEDS_USER),
                       status=outcome.status, detail=outcome.detail)

    # 7) 公网复核（Portal 已认证但公网可能仍异常，两者独立报告）
    for delay in (3, 5, 8):
        if cancelled():
            return _result(port=port, network_ok=None, auth_ok=True,
                           error_code=E_CANCELLED, retryable=False,
                           status=PortalStatus.STOPPING,
                           detail="已取消，停止等待公网恢复")
        sleep(delay)
        if cancelled():
            return _result(port=port, network_ok=None, auth_ok=True,
                           error_code=E_CANCELLED, retryable=False,
                           status=PortalStatus.STOPPING,
                           detail="已取消，停止等待公网恢复")
        online = False
        try:
            online, _via = adapters.probe_online(session)
        except Exception as exc:
            log.warning("复核探测异常：%s", type(exc).__name__)
        if online:
            return _result(port=port, network_ok=True, auth_ok=True,
                           status=PortalStatus.ONLINE,
                           already_authenticated=outcome.already_authenticated,
                           detail="Portal 确认在线，公网已恢复（端口 %d · %s）"
                                  % (port, source))
    return _result(port=port, network_ok=False, auth_ok=True,
                   error_code=E_PROBE_UNRELIABLE, retryable=True,
                   status=PortalStatus.AUTHENTICATED_DEGRADED,
                   detail="Portal 已在线但公网探测仍未通过（可能是探测地址受限或 DNS 问题）")
