# -*- coding: utf-8 -*-
"""V0.6 Block 1：双轴探测 + Clash 假死自愈判定（campus_clash_health）。

与 V0.5 直连探测的关系
----------------------
V0.5 的真值是**直连轴**：``campus_reconnect.internet_ok`` /
``verify_internet`` 与 ``campus_network.InterfaceHealthChecker`` 都走
``trust_env=False``、绕过系统代理的直连 HTTP 探测（Baidu 闸门 + 交叉站，
任一严格匹配即在线）。它能回答“本地能不能上网”，但回答不了
“Clash 代理本身是不是假死”——用户 2026-11-11 的实测正是这个缺口：
本地能上网，但 Clash 全 timeout，日志里“Portal 已在线但公网探测仍未
通过”出现 26 次，只有断网重连 + 重开系统代理才能恢复。

本模块补上**代理轴**，与直连轴组成双轴判定：

- 直连轴（调用方传入，已由 V0.5 测得）：``direct_ok``；
- 代理轴（本模块测）：调用方传入**已设好 proxies 的会话**（``trust_env``
  可为 True，由调用方定），对同一批 ``ProbeSpec`` 做严格内容匹配
  （复用 ``campus_core.probe_matches``），任一匹配即 ``proxy_ok``；
- 双轴对照见 :func:`classify`：直连通 + 代理 timeout/connection 才算
  Clash 假死；直连不通时一律不判假死，DNS 错误也不判假死。

纯逻辑模块（无副作用）
----------------------
- 不创建真实网络会话、不发真实请求、不做真实 socket、不读注册表、
  不读凭据、不启动进程、不真实 sleep；
- 会话、TCP 探针一律由调用方**注入**（假件 / 实机探针），默认不探测；
- 所有 Windows 副作用（开关系统代理、重启 Clash Verge）只以计划
  字符串返回（见 :func:`heal_plan`），由调用方经注入的 ops 执行，
  本模块永远不执行它们。

V0.6 新默认值（限幅见 :func:`clamp_interval` 等）
------------------------------------------------
``interval=5``、``threshold=2``、``cooldown=60``；旧值（V0.5 及以前）
为 ``interval=15``、``threshold=3``、``cooldown=180``。非法 / 缺失值
回退到 V0.6 新默认值，再按各字段上下限夹紧。
"""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from campus_core import PROBE_EXACT, ProbeSpec, probe_matches

__all__ = [
    "VERDICT_HEALTHY", "VERDICT_CLASH_FAKE_DEAD", "VERDICT_OFFLINE",
    "VERDICT_UNKNOWN",
    "PROXY_ERROR_TIMEOUT", "PROXY_ERROR_DNS", "PROXY_ERROR_CONNECTION",
    "PROXY_ERROR_HTTP_STATUS", "PROXY_ERROR_MISMATCH",
    "PROXY_ERROR_NO_PROBES", "PROXY_ERROR_UNKNOWN",
    "ACTION_TOGGLE_SYSTEM_PROXY", "ACTION_RESTART_VERGE", "ACTION_NONE",
    "DEFAULT_CLASH_HOST", "DEFAULT_CLASH_PORT", "DEFAULT_PROXY_TIMEOUT",
    "DEFAULT_INTERVAL", "DEFAULT_THRESHOLD", "DEFAULT_COOLDOWN",
    "MIN_INTERVAL", "MAX_INTERVAL", "MIN_THRESHOLD", "MAX_THRESHOLD",
    "MIN_COOLDOWN", "MAX_COOLDOWN",
    "V06_DEFAULTS", "V05_DEFAULTS",
    "ProxyAxisResult", "ClashHealth", "ClashHealPlan",
    "classify", "assess_dual_axis", "probe_via_proxy", "check_endpoint",
    "heal_plan", "clamp_interval", "clamp_threshold", "clamp_cooldown",
    "clamp_v06_defaults",
]

# --------------------------------------------------------------------------- #
# 结论与错误种类
# --------------------------------------------------------------------------- #
#: 双轴判定结论（字符串，便于日志 / UI / 配置透传）。
VERDICT_HEALTHY = "healthy"
VERDICT_CLASH_FAKE_DEAD = "clash_fake_dead"
VERDICT_OFFLINE = "offline"
VERDICT_UNKNOWN = "unknown"

#: 代理轴失败种类：只有 timeout / connection 能佐证 Clash 假死。
PROXY_ERROR_TIMEOUT = "timeout"
PROXY_ERROR_DNS = "dns"
PROXY_ERROR_CONNECTION = "connection"
#: HTTP 状态码非 200（正文再对也按失败计，由 probe_matches 判定）。
PROXY_ERROR_HTTP_STATUS = "http_status"
#: 200 但正文结构失配，或发生了重定向（严格匹配不通过）。
PROXY_ERROR_MISMATCH = "mismatch"
#: 调用方没有给任何探测点。
PROXY_ERROR_NO_PROBES = "no_probes"
#: 无法归类的异常 / 失败（fail-closed：不据此判 Clash 假死）。
PROXY_ERROR_UNKNOWN = "unknown"

#: 只有 timeout / connection 才算 Clash 假死的代理错误。
FAKE_DEAD_ERRORS = frozenset({PROXY_ERROR_TIMEOUT, PROXY_ERROR_CONNECTION})

# --------------------------------------------------------------------------- #
# 自愈动作（只作计划字符串返回，本模块不执行）
# --------------------------------------------------------------------------- #
ACTION_TOGGLE_SYSTEM_PROXY = "toggle_system_proxy"
ACTION_RESTART_VERGE = "restart_verge"
#: “无动作”的语义常量：:func:`heal_plan` 无动作时返回空列表 ``[]``，
#: 而不是 ``["none"]``，本常量只供调用方 / 日志表述“无动作”时使用。
ACTION_NONE = "none"

# --------------------------------------------------------------------------- #
# Clash 端点与探测默认值
# --------------------------------------------------------------------------- #
#: Clash Verge 默认混合端口（与 AGENTS.md 第 4 节系统代理 127.0.0.1:7897 一致）。
DEFAULT_CLASH_HOST = "127.0.0.1"
DEFAULT_CLASH_PORT = 7897
#: 代理轴单点 GET 超时（秒），与 V0.5 直连探测的 3 秒量级一致。
DEFAULT_PROXY_TIMEOUT = 3.0

# --------------------------------------------------------------------------- #
# V0.6 新默认值与限幅范围（旧值见 V05_DEFAULTS）
# --------------------------------------------------------------------------- #
#: V0.6 新默认值：interval=5, threshold=2, cooldown=60。
DEFAULT_INTERVAL = 5
DEFAULT_THRESHOLD = 2
DEFAULT_COOLDOWN = 60
#: V0.5 及以前的旧默认值：interval=15, threshold=3, cooldown=180。
V05_DEFAULTS: Dict[str, int] = {"interval": 15, "threshold": 3,
                                 "cooldown": 180}
V06_DEFAULTS: Dict[str, int] = {"interval": DEFAULT_INTERVAL,
                                 "threshold": DEFAULT_THRESHOLD,
                                 "cooldown": DEFAULT_COOLDOWN}
#: 限幅范围（V0.6 收紧后的范围，比 campus_core 的通用范围更窄）。
MIN_INTERVAL, MAX_INTERVAL = 5, 120
MIN_THRESHOLD, MAX_THRESHOLD = 1, 10
MIN_COOLDOWN, MAX_COOLDOWN = 60, 3600
#: 自愈尝试达到该次数仍假死即停手，转人工处理。
MAX_HEAL_ATTEMPTS = 2


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProxyAxisResult:
    """双轴探测的一次记录（直连轴由 V0.5 测得，代理轴由本模块测得）。

    - ``direct_ok``：直连轴是否通；``None`` 表示本次只测了代理轴，
      直连轴尚未填入（:func:`probe_via_proxy` 返回时即为此状态）；
    - ``proxy_ok``：代理轴是否通（任一探测点严格匹配）；
    - ``proxy_error``：代理轴全败时首个失败的错误种类
      （timeout / dns / connection / http_status / mismatch /
      no_probes / unknown），通过时为 ``None``；
    - ``elapsed``：代理轴探测总耗时（秒，单调时钟，不含 sleep）。
    """

    direct_ok: Optional[bool] = None
    proxy_ok: bool = False
    proxy_error: Optional[str] = None
    elapsed: float = 0.0

    def with_direct(self, direct_ok: bool) -> "ProxyAxisResult":
        """填入直连轴结果，返回一份新的记录（原记录不变）。"""
        return ProxyAxisResult(bool(direct_ok), self.proxy_ok,
                               self.proxy_error, self.elapsed)

    def to_dict(self) -> Dict[str, Any]:
        """序列化（只含普通值，可进日志 / 快照，不含凭据）。"""
        return {"direct_ok": self.direct_ok, "proxy_ok": self.proxy_ok,
                "proxy_error": self.proxy_error, "elapsed": self.elapsed}


@dataclass(frozen=True)
class ClashHealth:
    """双轴判定结论：直连轴 + 代理轴对照后的定性结果。"""

    direct_ok: bool
    proxy_ok: bool
    verdict: str
    proxy_error: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化（只含普通值，可进日志 / 快照，不含凭据）。"""
        return {"direct_ok": self.direct_ok, "proxy_ok": self.proxy_ok,
                "verdict": self.verdict, "proxy_error": self.proxy_error,
                "reason": self.reason}


@dataclass
class ClashHealPlan:
    """Clash 自愈计划：只返回动作字符串，调用方经注入的 ops 执行。

    - ``actions``：按顺序执行的动作列表，取值为
      ``toggle_system_proxy`` / ``restart_verge``；无动作时为空列表
      ``[]``（不是 ``["none"]``）；
    - ``reason``：为什么是这个计划（含端点状态与人工处理建议）。
    """

    actions: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化（动作列表复制一份，避免调用方改动内部状态）。"""
        return {"actions": list(self.actions), "reason": self.reason}


# --------------------------------------------------------------------------- #
# 双轴判定
# --------------------------------------------------------------------------- #
def _normalize_error(proxy_error: Any) -> str:
    """代理错误种类归一化：去空白转小写，非字符串先转字符串。"""
    if proxy_error is None:
        return ""
    return str(proxy_error).strip().lower()


def classify(direct_ok: bool, proxy_ok: bool,
             proxy_error: Optional[str] = None) -> ClashHealth:
    """双轴对照，判定 Clash 是否假死（fail-closed，不误判）。

    - 直连通 + 代理通 → ``healthy``；
    - 直连通 + 代理 ``timeout`` / ``connection`` → ``clash_fake_dead``；
    - 直连通 + 代理不通但错误是 ``dns`` / ``http_status`` / ``mismatch`` /
      未知 / 为空 → ``unknown``（DNS 问题、正文失配都不能算 Clash 假死）；
    - 直连不通 + 代理不通 → ``offline``（整网离线，不判 Clash 假死）；
    - 直连不通 + 代理通 → ``unknown``（结论矛盾，不误判，等重测）。
    """
    direct = bool(direct_ok)
    proxy = bool(proxy_ok)
    error = _normalize_error(proxy_error)
    if direct and proxy:
        return ClashHealth(direct, proxy, VERDICT_HEALTHY, None,
                           "直连通且代理通：Clash 正常，无需自愈。")
    if direct and not proxy:
        if error in FAKE_DEAD_ERRORS:
            return ClashHealth(
                direct, proxy, VERDICT_CLASH_FAKE_DEAD, error,
                "直连通但代理 %s：Clash 假死（本地能上网，代理轴失败）。"
                % error)
        return ClashHealth(
            direct, proxy, VERDICT_UNKNOWN, error or None,
            "直连通但代理不通（错误：%s）：无法判定 Clash 假死，"
            "DNS 问题 / 状态码 / 正文失配都可能是原因，不误判。"
            % (error or "未知"))
    if not direct and not proxy:
        return ClashHealth(
            direct, proxy, VERDICT_OFFLINE, error or None,
            "直连与代理都不通：整网离线，不是 Clash 单点假死，"
            "不触发 Clash 自愈。")
    return ClashHealth(
        direct, proxy, VERDICT_UNKNOWN, error or None,
        "直连不通但代理通：结论矛盾，探测不可信，不误判为 Clash 假死。")


def assess_dual_axis(direct_ok: bool,
                     proxy_result: ProxyAxisResult) -> ClashHealth:
    """把直连轴结果与代理轴探测记录合成一次双轴判定。

    等价于 ``classify(direct_ok, proxy_result.proxy_ok,
    proxy_result.proxy_error)``，只是入口按“记录”组织，方便调用方
    先拿 :func:`probe_via_proxy` 的结果再补直连轴。
    """
    return classify(direct_ok, proxy_result.proxy_ok,
                    proxy_result.proxy_error)


# --------------------------------------------------------------------------- #
# 代理轴探测（注入会话，不自建真实会话）
# --------------------------------------------------------------------------- #
def _classify_proxy_error(exc: BaseException) -> str:
    """把一次请求异常归类为 timeout / dns / connection / unknown。

    只按异常类型名与文本判断，不依赖 requests 的具体异常类（本模块
    不 import requests，假件抛出的同名异常同样可归类）：
    timeout 优先于 dns，dns 优先于 connection，其余一律 unknown，
    避免把未知异常误认成 Clash 假死的证据。
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text or "timed out" in text:
        return PROXY_ERROR_TIMEOUT
    if ("dns" in text or "getaddrinfo" in text
            or "name resolution" in text
            or "name or service not known" in text
            or "nodename nor servname" in text
            or "failed to resolve" in text):
        return PROXY_ERROR_DNS
    if ("connection" in name or "proxy" in name
            or "connection refused" in text or "connection reset" in text
            or "connection aborted" in text or "connectionerror" in name):
        return PROXY_ERROR_CONNECTION
    return PROXY_ERROR_UNKNOWN


def _response_body(resp: Any) -> str:
    """取响应正文：优先 ``text``，没有时按 ``content`` 解码。"""
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text
    raw = getattr(resp, "content", b"")
    if isinstance(raw, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    return str(raw or "")


def _response_redirected(resp: Any) -> bool:
    """响应是否发生重定向：history 非空，或状态码本身是重定向码。"""
    history = getattr(resp, "history", None)
    if history:
        return True
    return getattr(resp, "status_code", 0) in (301, 302, 303, 307, 308)


def _as_probe(url_or_spec: Any) -> Tuple[ProbeSpec, bool]:
    """探测点归一化：返回 ``(ProbeSpec, 是否为裸 URL)``。

    裸 URL（字符串）没有期望正文可严格匹配，成功只判状态码 200；
    调用方想做严格匹配时应传 ``ProbeSpec``。
    """
    if isinstance(url_or_spec, ProbeSpec):
        return url_or_spec, False
    return ProbeSpec(str(url_or_spec), "", PROBE_EXACT), True


def probe_via_proxy(session_factory: Any,
                    probes: Optional[Sequence[Any]] = None,
                    timeout: Any = DEFAULT_PROXY_TIMEOUT,
                    *, direct_ok: Optional[bool] = None) -> ProxyAxisResult:
    """经代理轴探测公网：任一探测点严格匹配即 ``proxy_ok``。

    - ``session_factory``：调用方注入的会话工厂（调用后返回已设好
      ``proxies`` 的会话，``trust_env`` 由调用方定），也可以直接传
      会话对象本身；本函数**不自己创建**真实会话，默认不发请求；
    - ``probes``：``ProbeSpec`` 列表（严格匹配，复用
      ``campus_core.probe_matches``）或裸 URL 列表（只判 200）；
      为空时返回 ``proxy_error="no_probes"``；
    - ``timeout``：单点 GET 超时，原样透传给 ``session.get``；
    - ``direct_ok``：调用方已知的直连轴结果，可选填入返回记录；
      不填时为 ``None``（表示本次只测了代理轴）。

    多站点聚合：单点失败不结束探测，全部失败才取**首个**错误种类；
    异常只算该点失败，绝不向上抛出（监测循环不能被一次探测炸掉）。
    """
    start = time.monotonic()

    def done(proxy_ok: bool, proxy_error: Optional[str]) -> ProxyAxisResult:
        return ProxyAxisResult(direct_ok, proxy_ok, proxy_error,
                               time.monotonic() - start)

    specs = list(probes or [])
    if not specs:
        return done(False, PROXY_ERROR_NO_PROBES)
    if callable(session_factory):
        try:
            session = session_factory()
        except Exception as exc:  # 工厂本身失败：代理轴不可用
            return done(False, _classify_proxy_error(exc))
    else:
        session = session_factory
    if session is None or not hasattr(session, "get"):
        return done(False, PROXY_ERROR_CONNECTION)

    first_error: Optional[str] = None
    for item in specs:
        spec, bare_url = _as_probe(item)
        try:
            resp = session.get(spec.url, timeout=timeout,
                               allow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - 该点失败，继续测下一点
            if first_error is None:
                first_error = _classify_proxy_error(exc)
            continue
        if resp is None:
            if first_error is None:
                first_error = PROXY_ERROR_CONNECTION
            continue
        status_code = getattr(resp, "status_code", 0) or 0
        redirected = _response_redirected(resp)
        if bare_url:
            ok = status_code == 200
        else:
            ok = probe_matches(spec, status_code, _response_body(resp),
                               redirected=redirected)
        if ok:
            return done(True, None)
        if first_error is None:
            if redirected:
                # 重定向本身就是严格匹配失败（哪怕状态码是 302 而非 200），
                # 归为 mismatch，不与 http_status 混淆。
                first_error = PROXY_ERROR_MISMATCH
            elif status_code != 200:
                first_error = PROXY_ERROR_HTTP_STATUS
            else:
                first_error = PROXY_ERROR_MISMATCH
    return done(False, first_error or PROXY_ERROR_UNKNOWN)


# --------------------------------------------------------------------------- #
# Clash 端点 TCP 检查（注入探针，不做真实 socket）
# --------------------------------------------------------------------------- #
def _call_tcp_probe(tcp_probe: Callable[..., Any], host: str, port: int
                    ) -> bool:
    """按探针签名调用：接受 2 参 ``(host, port)`` 或 3 参 ``(host, port, timeout)``。

    仓库内既有探针多为 3 参（如 ``campus_windows.tcp_probe`` /
    ``internet_ok`` 的 ``tcp_probe``），测试假件常为 2 参；先按签名
    判断，签名读不到时先试 2 参，``TypeError`` 再试 3 参。
    """
    try:
        params = list(inspect.signature(tcp_probe).parameters.values())
    except (TypeError, ValueError):
        params = []
    takes_timeout = any(p.kind in (inspect.Parameter.VAR_POSITIONAL,
                                   inspect.Parameter.VAR_KEYWORD)
                        for p in params) or len(params) >= 3
    if takes_timeout:
        return bool(tcp_probe(host, port, DEFAULT_PROXY_TIMEOUT))
    try:
        return bool(tcp_probe(host, port))
    except TypeError:
        return bool(tcp_probe(host, port, DEFAULT_PROXY_TIMEOUT))


def check_endpoint(host: str = DEFAULT_CLASH_HOST,
                   port: int = DEFAULT_CLASH_PORT,
                   tcp_probe: Optional[Callable[..., Any]] = None) -> bool:
    """Clash 端点 TCP 是否可达（只问“Verge 在不在监听”，不判代理通不通）。

    - ``tcp_probe`` 未注入时返回 ``False``（fail-closed：本模块不做
      真实 socket，绝不因“没探针”而误报端点正常）；
    - 主机为空 / 端口越界时不调用探针，直接返回 ``False``；
    - 探针抛错时返回 ``False``（只算不可达，不中断调用方）。
    """
    if not isinstance(host, str) or not host.strip():
        return False
    if isinstance(port, bool) or not isinstance(port, int):
        return False
    if not 1 <= port <= 65535:
        return False
    if tcp_probe is None or not callable(tcp_probe):
        return False
    try:
        return bool(_call_tcp_probe(tcp_probe, host.strip(), port))
    except Exception:  # noqa: BLE001 - 探针异常只算端点不可达
        return False


# --------------------------------------------------------------------------- #
# 自愈计划（只返回动作字符串，不执行）
# --------------------------------------------------------------------------- #
def _verdict_of(health: Any) -> str:
    """从 ClashHealth / 结论字符串 / 代理轴记录中取出 verdict。"""
    if isinstance(health, ClashHealth):
        return _normalize_error(health.verdict)
    if isinstance(health, ProxyAxisResult):
        direct = health.direct_ok if health.direct_ok is not None else False
        return classify(direct, health.proxy_ok,
                        health.proxy_error).verdict
    if isinstance(health, str):
        return _normalize_error(health)
    return VERDICT_UNKNOWN


def _coerce_attempts(attempts: Any) -> int:
    """自愈尝试次数归一化：非法 / 负数按 0 计（即尚未尝试过）。"""
    if isinstance(attempts, bool) or attempts is None:
        return 0
    try:
        value = int(attempts)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def heal_plan(health: Any, endpoint_ok: bool = False,
              attempts: int = 0) -> ClashHealPlan:
    """Clash 假死的分步自愈计划（动作顺序只由 attempts 驱动）。

    - ``clash_fake_dead`` 且 ``attempts==0`` → ``[toggle_system_proxy]``
      （最轻的恢复手段：开关一次系统代理，不重启进程）；
    - 仍假死且 ``attempts==1`` → ``[restart_verge]``（进程级恢复）；
    - 仍假死且 ``attempts>=2`` → ``[]``，reason 建议人工处理；
    - ``healthy`` / ``offline`` / ``unknown`` → ``[]``（不触发自愈）。

    ``endpoint_ok`` 只影响 reason 里的端点状态说明，不改变动作顺序：
    端点单次抖动不应直接升级成重启进程，升级只看 attempts。
    """
    verdict = _verdict_of(health)
    tried = _coerce_attempts(attempts)
    endpoint_text = "正常" if endpoint_ok else "异常"
    if verdict != VERDICT_CLASH_FAKE_DEAD:
        reasons = {
            VERDICT_HEALTHY: "Clash 正常，无需自愈。",
            VERDICT_OFFLINE: "整网离线：先恢复网络，不触发 Clash 自愈。",
            VERDICT_UNKNOWN: "结论不明：不误判、不自动操作，等下轮探测或人工确认。",
        }
        return ClashHealPlan([], reasons.get(
            verdict, "结论不明：不误判、不自动操作，等下轮探测或人工确认。"))
    if tried == 0:
        return ClashHealPlan(
            [ACTION_TOGGLE_SYSTEM_PROXY],
            "Clash 假死第 1 步：先开关一次系统代理（端点 TCP %s），"
            "这是最轻的恢复手段，不重启进程。" % endpoint_text)
    if tried == 1:
        return ClashHealPlan(
            [ACTION_RESTART_VERGE],
            "Clash 假死第 2 步：开关系统代理无效，重启 Clash Verge"
            "（进程级恢复；端点 TCP %s）。" % endpoint_text)
    return ClashHealPlan(
        [],
        "Clash 假死已尝试开关代理与重启 Verge（%d 次）仍未恢复："
        "停止自动操作，建议人工处理（检查 Verge 是否在运行、订阅与"
        "系统代理设置）。" % tried)


# --------------------------------------------------------------------------- #
# 限幅工具（V0.6 新默认值：interval=5, threshold=2, cooldown=60）
# --------------------------------------------------------------------------- #
def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """整数限幅：非法值回退到默认值，再夹到 ``[low, high]``。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            number = int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    return max(low, min(high, number))


def clamp_interval(value: Any = None) -> int:
    """探测间隔限幅到 5..120 秒，缺失 / 非法默认 5（V0.6 新默认值，旧值 15）。"""
    return _clamp_int(value, MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL)


def clamp_threshold(value: Any = None) -> int:
    """失败阈值限幅到 1..10 次，缺失 / 非法默认 2（V0.6 新默认值，旧值 3）。"""
    return _clamp_int(value, MIN_THRESHOLD, MAX_THRESHOLD, DEFAULT_THRESHOLD)


def clamp_cooldown(value: Any = None) -> int:
    """重连冷却限幅到 60..3600 秒，缺失 / 非法默认 60（V0.6 新默认值，旧值 180）。"""
    return _clamp_int(value, MIN_COOLDOWN, MAX_COOLDOWN, DEFAULT_COOLDOWN)


def clamp_v06_defaults(interval: Any = None, threshold: Any = None,
                       cooldown: Any = None) -> Dict[str, int]:
    """一次限幅三个 V0.6 调度参数，返回 ``{"interval","threshold","cooldown"}``。"""
    return {"interval": clamp_interval(interval),
            "threshold": clamp_threshold(threshold),
            "cooldown": clamp_cooldown(cooldown)}
