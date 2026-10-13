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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = [
    "SCHEMA_VERSION", "OPERATORS", "ACCOUNT_FORMATS",
    "INTERVAL_RANGE", "THRESHOLD_RANGE", "COOLDOWN_RANGE",
    "ERROR_CODES", "ERROR_MESSAGES",
    "E_CONFIG_INVALID", "E_CONFIG_IO", "E_CRED_MISSING", "E_CRED_BACKEND",
    "E_PROBE_UNRELIABLE", "E_PORTAL_UNREACHABLE", "E_AUTH_REJECTED",
    "E_AUTH_CAPTCHA", "E_PORTAL_STRUCTURE_UNKNOWN", "E_PROXY_ENDPOINT_INVALID",
    "E_PROXY_WRITE", "E_NETS_FAILED", "E_CANCELLED",
    "MISSING", "Secret", "Config", "ConfigIssue", "ConfigLoadResult",
    "ProbeResult", "PortalStatus", "RepairResult", "PortalResolution",
    "BrowserLaunchResult", "ProxyApplyResult", "CredentialLoad",
    "CredentialSave", "CredentialClear", "Scheduler",
    "validate_config", "load_config_text", "load_config_file", "default_config",
]


# --------------------------------------------------------------------------- #
# 版本与取值范围
# --------------------------------------------------------------------------- #
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
