# -*- coding: utf-8 -*-
"""SNNU 校园网自动重连助手 (Windows / Python 3.10+)

2026-10-13 对线上 Portal 的实测结论:
  GET  http://202.117.144.205/                          -> JS 跳转到 :8601 登录页
  GET  http://202.117.144.205:8601/snnuportal/login.jsp -> 302 userstatus.jsp, 下发 JSESSIONID
  POST http://202.117.144.205:8601/snnuportal/login     -> 302 userstatus.jsp
  GET  http://202.117.144.205:8601/snnuportal/userstatus.jsp -> 在线状态页 (GB2312)
  登录字段: sourceurl=null, account, password, yys=mobile|unicom|telecom, issave=
  根页面会在 8601/8602/8603/8605/8609 等端口间轮询跳转, 脚本动态解析该端口;
  登录页当前无验证码; 失败的登录不会影响已有在线会话;
  成功的判定 = userstatus.jsp 出现"当前登录账号"且账号字段与本次提交**完整一致**,
  且直连公网探测通过(严格匹配响应结构, 不再用任意正文子串)。

命令:
  configure   交互式保存账号密码到 Windows 凭据管理器
  status      诊断: Wi-Fi / Portal / 公网探测 / 系统代理绕过列表
  once        执行一次重连 (--dry-run 预检; --force 在线强制, 会提交真实凭据)
  watch       常驻监测, 掉线自动重连 (单实例保护, 重复启动自动退出)
  netcheck    V0.3 P1 只读三轴网络健康检查(有线/无线/系统), 只给决策不切换
  failover    V0.3 P3 只读故障切换监测(运行 11 态状态机, 不执行任何动作)
  snnu        V0.3 P4 SNNU 备用链路与接口绑定认证(默认只读识别; --auth 受控认证)
  route       V0.3 P5 受控路由切换与路由卫士(默认只读快照; --recover/--apply/--auto 受控)
  fix-bypass  把 Portal 地址写入系统代理绕过列表(注册表), Edge 无需关代理即可打开登录页

安全边界: 不执行注销(logoff), 不修改 Clash Verge 配置, 不明文保存密码, 日志不含密码。
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import urlsplit

try:
    import keyring
    import requests
except ImportError:
    print("缺少依赖，请先运行: py -m pip install requests keyring")
    raise SystemExit(2)

from campus_core import (E_AUTH_REJECTED, E_PORTAL_STRUCTURE_UNKNOWN,
                         E_PORTAL_UNREACHABLE, MAX_BODY_BYTES, PROBE_EXACT,
                         PROBE_HTML_TITLE, BrowserLaunchResult, PortalResolution,
                         PortalStatus, ProbeResult, ProbeSpec, RepairAdapters,
                         RepairOptions, RepairResult, LoginOutcome,
                         StatusPageResult, TimerTable,
                         check_redirect_target, classify_network,
                         extract_error_hint, normalize_account,
                         parse_status_page, probe_matches,
                         repair_service, resolve_url)
from campus_failover import (FailoverMode, FailoverPolicy,
                             NetworkFailoverManager, format_tick)
from campus_network import (DEFAULT_TCP_TARGETS, NetworkObserver,
                            WindowsProbeEngine, evaluate, format_snapshot,
                            resolve_roles)
from campus_route_guard import (CompositeExecutor, MetricExecutor, RouteGuard,
                                RouteJournal, build_windows_route_ops,
                                format_route_change, format_route_snapshot)
from campus_snnu import (InterfaceBinding, SnnuBackupExecutor, SnnuBackupLink,
                         SnnuOps, format_backup_result)
from campus_windows import (CredentialStore, SingleInstanceGuard, WifiInterface,
                            parse_ipconfig_ipv4, parse_netsh_interfaces,
                            tcp_probe, wifi_state_from_text)

if os.name == "nt":
    import winreg

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent   # PyInstaller 单文件: 用 exe 所在目录
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"

SERVICE = "SNNU-Campus-Reconnect"
PORTAL_IP = "202.117.144.205"
PORTAL_ROOT = "http://%s/" % PORTAL_IP
LOGIN_JSP = "/snnuportal/login.jsp"
USERSTATUS_JSP = "/snnuportal/userstatus.jsp"
LOGIN_ACTION = "/snnuportal/login"
BYPASS_HOSTS = (PORTAL_IP, "net.snnu.edu.cn")
KNOWN_PORTS = (8601, 8602, 8603, 8605, 8609)
#: Portal 认证端口解析的总预算（秒）；各阶段检查剩余时间，超预算即失败（F16）。
PORTAL_BUDGET = 20.0

#: 连通性探测点：严格匹配响应结构，不再「任意 200 含 Success 即通过」（F08/T18）。
#:
#: P8/K-4（2026-10-15 真机取证）：原先首选的 ``www.msftconnecttest.com`` 在本校园网
#: **3/3 不可达**（DNS/连接失败），``captive.apple.com`` 又偶发首次失配；两点同时失败
#: 即产生「假掉线」。改为 3 个**实测可达**且互相独立的目标：任一严格匹配即判在线，
#: 单点抖动不再导致整轮误判（真实的 Portal 劫持返回 HTML，不会误匹配）。
DEFAULT_PROBES = (
    ProbeSpec("http://detectportal.firefox.com/success.txt",
              "success", PROBE_EXACT),
    ProbeSpec("http://www.msftncsi.com/ncsi.txt",
              "Microsoft NCSI", PROBE_EXACT),
    ProbeSpec("http://captive.apple.com/hotspot-detect.html",
              "Success", PROBE_HTML_TITLE),
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

#: 免 DNS 的 IP 级证据（F75-13②，2026-10-15 真机取证）。
#:
#: 本机**公网 IPv6 是黑洞**（全局 IPv6 只在 WLAN、IPv6 默认路由仅 WLAN；
#: ``ping -6`` 100% 丢包、``curl -6`` 10s 超时，而 ``curl -4`` 200），
#: 而三个 HTTP 探测点**全为域名**且 ``detectportal.firefox.com`` 会返回 AAAA：
#: 任一次 DNS 抖动都会让三点同时失败 ⇒ 误判"掉线"并触发修复/认证路径，
#: 而 IPv4 的 IP 级连通性可能完好。故补一条**不依赖 DNS** 的 TCP 证据
#: （目标沿用 K-6 实机验证可达的 ``DEFAULT_TCP_TARGETS``）。
IP_PROBE_TIMEOUT = 1.0
#: IP 级探测实现（默认 :func:`campus_windows.tcp_probe`；测试注入假件以保证零真实网络）。
IP_TCP_PROBE: Callable[[str, int, float], bool] = tcp_probe


def _prefer_ipv4() -> None:
    """让 requests/urllib3 只解析 IPv4（F75-13①）。

    见上文 ``IP_PROBE_TIMEOUT`` 的真机取证：本机公网 IPv6 为黑洞，DNS 一旦返回 AAAA
    就会先撞黑洞再回退（单点 3.25s；紧循环复现 5.12s 超时）。公网连通性语义本就是 IPv4
    （``DEFAULT_TCP_TARGETS`` 为 IPv4 字面量，Portal 目标亦为 IPv4），故探测会话统一只解析 A 记录。
    """
    try:
        from urllib3.util import connection as urllib3_connection
    except ImportError:  # pragma: no cover - urllib3 是 requests 的硬依赖
        return
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


def setup_logging(verbose: bool) -> logging.Logger:
    """配置 ``campus`` logger（幂等）。

    日志目录不可写（只读安装目录等，见 F13/T37）时**降级**为仅控制台输出并给出
    可见告警，绝不在 UI 出现前因日志初始化失败而崩溃。
    """
    logger = logging.getLogger("campus")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    # 幂等：先移除本函数此前添加的 handler，避免重复调用时堆叠（对应 T36）。
    for handler in list(logger.handlers):
        if getattr(handler, "_campus_handler", False):
            logger.removeHandler(handler)
            handler.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch._campus_handler = True  # type: ignore[attr-defined]
    logger.addHandler(ch)
    file_error = None
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_DIR / "campus_reconnect.log", maxBytes=1_000_000, backupCount=3,
            encoding="utf-8")
        fh.setFormatter(fmt)
        fh._campus_handler = True  # type: ignore[attr-defined]
        logger.addHandler(fh)
    except OSError as exc:
        file_error = exc
    if file_error is not None:
        logger.warning("日志文件不可写（%s），已降级为仅控制台输出；日志目录：%s",
                       type(file_error).__name__, LOG_DIR)
    return logger


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": UA, "Cache-Control": "no-cache"})
    _prefer_ipv4()
    return session


def decode_body(resp: requests.Response, limit: int = MAX_BODY_BYTES,
                strict: bool = False) -> Optional[str]:
    """解码响应正文；``limit`` 限制读取字节数（F16）；``strict=True`` 时无法
    解码返回 ``None``（供「解码失败 ⇒ 结构未知」路径使用，F06）。"""
    raw = resp.content
    if limit and len(raw) > limit:
        raw = raw[:limit]
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    if strict:
        return None
    return raw.decode("utf-8", errors="replace")


def probe_online(session: requests.Session, probes=None) -> List[ProbeResult]:
    """逐个探测并返回每个探测点的结构化结果（**不含响应正文**，F08）。"""
    specs = DEFAULT_PROBES if probes is None else probes
    results: List[ProbeResult] = []
    for spec in specs:
        start = time.monotonic()
        try:
            resp = session.get(spec.url, timeout=(2.5, 4), allow_redirects=False)
        except requests.exceptions.Timeout:
            results.append(ProbeResult(False, spec.url, error="timeout",
                                       elapsed=time.monotonic() - start))
            continue
        except requests.RequestException as exc:
            results.append(ProbeResult(False, spec.url, error=_error_kind(exc),
                                       elapsed=time.monotonic() - start))
            continue
        body = decode_body(resp) or ""
        redirected = resp.status_code in (301, 302, 303, 307, 308)
        ok = probe_matches(spec, resp.status_code, body, redirected=redirected)
        results.append(ProbeResult(ok, spec.url, status_code=resp.status_code,
                                   elapsed=time.monotonic() - start,
                                   redirected=redirected))
    return results


def _error_kind(exc: Exception) -> str:
    """把请求异常归类为 dns / timeout / connection（供探测分类）。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "name" in name or "dns" in text or "getaddrinfo" in text or "name resolution" in text:
        return "dns"
    if "timeout" in name:
        return "timeout"
    return "connection"


def _probe_detail(result: ProbeResult) -> str:
    """失败探测点的可读明细 ``主机(错误类型/状态码)``（F75-13③）。"""
    try:
        host = urlsplit(result.source).hostname or result.source
    except ValueError:  # pragma: no cover - source 恒为探测点 URL
        host = result.source
    return "%s(%s)" % (host, result.error or result.status_code)


def internet_ok(session: requests.Session,
                probes=DEFAULT_PROBES) -> Tuple[bool, str]:
    """直连(不走系统代理)判定公网可达，返回 ``(是否在线, 证据或失败明细)``。

    判据（F75-13②）：**任一** HTTP 探测点严格结构匹配，**或任一** 免 DNS 的 IP 级
    TCP 点（``DEFAULT_TCP_TARGETS``）可达 ⇒ 在线。前者防 Portal 劫持误判，后者防
    DNS 抖动导致三点同时失败而误判掉线（有线正常时误判掉线会触发修复/认证）。

    在线时第二项为通过的证据（探测点 URL 或 ``tcp://host:port``）；离线时为失败明细
    （各失败探测点 + 错误类型，``；`` 分隔），供 :func:`watch` 记入日志（F75-13③）。
    """
    details: List[str] = []
    for result in probe_online(session, probes):
        if result.ok:
            return True, result.source
        details.append(_probe_detail(result))
    for host, port in DEFAULT_TCP_TARGETS:
        try:
            reachable = bool(IP_TCP_PROBE(host, port, IP_PROBE_TIMEOUT))
        except Exception as exc:  # 探测自身异常只算"不可达"，不得中断监测循环
            details.append("tcp://%s:%d(%s)" % (host, port, type(exc).__name__))
            continue
        if reachable:
            return True, "tcp://%s:%d" % (host, port)
        details.append("tcp://%s:%d(unreachable)" % (host, port))
    return False, "；".join(details)


def portal_base(port: int) -> str:
    return "http://%s:%d" % (PORTAL_IP, port)


def _port_from_text(text: str) -> Optional[int]:
    """从 Location 或 JS 片段中提取 ``:端口/snnuportal/login.jsp`` 的端口。"""
    m = re.search(r":(\d{2,5})/snnuportal/login\.jsp", text or "")
    if not m:
        return None
    port = int(m.group(1))
    return port if 1 <= port <= 65535 else None


def _is_portal_url(url: str) -> bool:
    """URL 的 host 是否为学校 Portal（不接受任意主机）。"""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host in (PORTAL_IP, "net.snnu.edu.cn")


def resolve_portal(session: requests.Session, requested: Optional[int] = None,
                   budget: float = PORTAL_BUDGET) -> PortalResolution:
    """解析 Portal 认证端口并返回结构化结果（F16）。

    优先级：用户指定 > 根页面 Location/JS 动态识别 > 已知端口试探。
    **全失败返回失败**（``port=None``），不假装已选端口；不复用同一无效请求。
    """
    deadline = time.monotonic() + max(0.0, budget)

    def remaining() -> float:
        return deadline - time.monotonic()

    if requested is not None:
        if not 1 <= requested <= 65535:
            return PortalResolution(error=E_PORTAL_UNREACHABLE,
                                    evidence="指定的端口 %s 越界" % requested)
        if remaining() <= 0:
            return PortalResolution(error=E_PORTAL_UNREACHABLE, evidence="端口解析预算耗尽")
        if portal_reachable(session, requested):
            return PortalResolution(port=requested, source="用户手动指定",
                                    evidence="手动端口可达")
        return PortalResolution(error=E_PORTAL_UNREACHABLE,
                                evidence="用户指定端口 %d 不可达" % requested)

    # 1) 根页面：Location 或 JS 跳转
    try:
        resp = session.get(PORTAL_ROOT, timeout=(3, 6), allow_redirects=False)
    except requests.RequestException as exc:
        root_error = "根页面请求失败（%s）" % type(exc).__name__
        resp = None
    else:
        root_error = ""
    if resp is not None:
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "")
            port = _port_from_text(location)
            if port is not None and _is_portal_url(resolve_url(PORTAL_ROOT, location) or ""):
                return PortalResolution(port=port, source="根页面 Location",
                                        evidence="302 Location 指向学校 Portal")
        body = decode_body(resp) or ""
        port = _port_from_text(body)
        if port is not None:
            return PortalResolution(port=port, source="根页面 JS 跳转",
                                    evidence="JS 声明 :%d/snnuportal/login.jsp" % port)

    # 2) 已知端口试探（受总预算约束）
    for port in KNOWN_PORTS:
        if remaining() <= 0:
            break
        if portal_reachable(session, port):
            return PortalResolution(port=port, source="试探已知端口",
                                    evidence="已知端口 %d 可用" % port)

    evidence = root_error or "根页面未发现可识别跳转，已知端口均不可达"
    return PortalResolution(error=E_PORTAL_UNREACHABLE, evidence=evidence)


def resolve_port(session: requests.Session,
                 requested: Optional[int]) -> Tuple[Optional[int], str]:
    """兼容层：返回 ``(端口|None, 来源/失败原因)``；失败时端口为 ``None``。"""
    resolution = resolve_portal(session, requested)
    if resolution.port is not None:
        return resolution.port, resolution.source
    return None, resolution.evidence or (resolution.error or "端口解析失败")


def portal_reachable(session: requests.Session, port: int) -> bool:
    """检查实际认证端口的登录页；只访问根地址不能证明该端口可用。"""
    try:
        resp = session.get(portal_base(port) + LOGIN_JSP,
                           timeout=(2.5, 4), allow_redirects=False)
        return 200 <= resp.status_code < 400
    except requests.RequestException:
        return False


def _decode_console(data: bytes) -> str:
    """解码 netsh/ipconfig 控制台输出（UTF-8 → GBK → 本地代码页兜底）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        import locale
        return data.decode(locale.getpreferredencoding(False), errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _run_console(args, timeout: float = 5.0) -> Tuple[Optional[int], str]:
    """运行控制台命令；返回 ``(returncode|None, 文本)``（``None`` 表示启动/超时失败）。"""
    try:
        done = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return done.returncode, _decode_console(done.stdout)


def wifi_interfaces() -> List[WifiInterface]:
    """读取并解析全部无线接口（F17）；无接口 / 命令失败返回空列表。"""
    if os.name != "nt":
        return []
    code, text = _run_console(["netsh", "wlan", "show", "interfaces"])
    if code != 0:
        return []
    return parse_netsh_interfaces(text)


def wifi_state(interface: Optional[str] = None) -> Tuple[Optional[str], str]:
    """返回 ``(当前 SSID|None, 状态)``（F17）。

    - 按接口块解析，SSID 与 BSSID 分开；**检查 netsh 返回码**；
    - 无无线接口 → ``(None, "no-wireless")``：明确“不支持自动认证”，不误报已连接；
    - 多网卡 / 有线场景不会凭公网可达推断校园链路。
    """
    if os.name != "nt":
        return None, "non-windows"
    code, text = _run_console(["netsh", "wlan", "show", "interfaces"])
    if code is None or code != 0:
        return None, "error"
    return wifi_state_from_text(text, interface=interface)


def _ipv4_ready(interface: Optional[str] = None) -> bool:
    """本机是否已获得可用 IPv4 地址（连接后等待就绪，F17）。"""
    code, text = _run_console(["ipconfig"])
    if code is None:
        return False
    return parse_ipconfig_ipv4(text, interface)


def wifi_reconnect(ssid: str, log: logging.Logger, profile: Optional[str] = None,
                   interface: Optional[str] = None,
                   wait: Optional[Callable[[], bool]] = None,
                   timeout: float = 30.0) -> bool:
    """用 netsh 连接目标网络，并等待 IP 就绪（F17）。

    - ``profile`` 给定时按 **profile 名**连接（``netsh wlan connect name=`` 实际按
      profile 操作，未必等于 SSID）；否则用 SSID 作为 profile 名；
    - 连接后轮询 ``wait``（默认本机 IPv4 就绪）直到 ``timeout``，避免刚发起就断言成功；
    - 命令失败 / 超时 / 未就绪均返回 ``False``，由上层如实报告。
    """
    name = (profile or ssid or "").strip()
    if not name:
        log.warning("Wi-Fi 重连失败：未提供 SSID/profile 名")
        return False
    args = ["netsh", "wlan", "connect", "name=%s" % name]
    if interface:
        args.append("interface=%s" % interface)
    try:
        done = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("netsh 重连 Wi-Fi 异常: %s", type(exc).__name__)
        return False
    if done.returncode != 0:
        log.info("netsh 重连 Wi-Fi %s: 失败（返回码 %s）", name, done.returncode)
        return False
    log.info("netsh 已发起连接 Wi-Fi %s，等待 IP 就绪…", name)
    ready = wait or (lambda: _ipv4_ready(interface))
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        try:
            if ready():
                log.info("Wi-Fi %s 已获得可用地址。", name)
                return True
        except Exception as exc:
            log.warning("等待 IP 就绪异常: %s", type(exc).__name__)
        time.sleep(2.0)
    log.warning("Wi-Fi %s 连接后 %d 秒内未获得可用 IP/路由。", name, int(timeout))
    return False


def get_credentials() -> Optional[Tuple[str, str]]:
    """读取凭据；未配置或后端不可用均返回 ``None``。

    需要区分「未配置」与「凭据库不可用」时请改用
    :meth:`campus_windows.CredentialStore.load`（F11）。
    """
    loaded = CredentialStore(keyring).load()
    if not loaded.ok:
        return None
    return (loaded.username, loaded.password.reveal())


def read_proxy_settings() -> dict:
    if os.name != "nt":
        return {}
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            def query(name):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return None
            return {"enable": query("ProxyEnable"),
                    "server": query("ProxyServer"),
                    "bypass": query("ProxyOverride")}
    except OSError:
        return {}


def fix_bypass(log: logging.Logger) -> bool:
    """把 Portal 地址追加到系统代理绕过列表(立即生效)。

    注意: Clash Verge 在切换"系统代理"开关时会用自身设置重写该列表,
    因此这只是即时生效的补丁; 永久生效请在 Verge 界面里配置(见 README)。
    """
    settings = read_proxy_settings()
    current = settings.get("bypass") or ""
    entries = [e.strip() for e in current.split(";") if e.strip()]
    missing = [h for h in BYPASS_HOSTS if not any(
        e.lower() == h.lower() or e.lower() == h.lower() + ":*" for e in entries)]
    if not missing:
        log.info("系统代理绕过列表已包含 %s，无需修改", ", ".join(BYPASS_HOSTS))
        return True
    new_value = ";".join(entries + missing)
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, new_value)
        log.info("已写入系统代理绕过列表，新增: %s", ", ".join(missing))
        log.warning("提示: Clash Verge 切换系统代理开关时可能重写此列表; "
                    "永久生效请在 Verge 设置中同样添加这些地址(见 README 第 1 步)")
        return True
    except OSError as exc:
        log.error("写入注册表失败: %s", type(exc).__name__)
        return False


def configure() -> None:
    print("首次设置：账号密码仅存入当前 Windows 用户的凭据管理器，不写入任何文件。")
    username = input("校园网账号：").strip()
    if not username:
        raise SystemExit("账号不能为空")
    password = getpass.getpass("校园网密码（输入时不显示）：")
    if not password:
        raise SystemExit("密码不能为空")
    result = CredentialStore(keyring).save(username, password)
    if not result.ok:
        raise SystemExit("凭据保存失败：%s" % result.detail)
    print("凭据保存完毕（未尝试登录）。")
    print("运营商由 watch/once 的 --yys 参数指定，默认 mobile（移动）。")


def read_status_page(session: requests.Session, port: int,
                     log: Optional[logging.Logger] = None
                     ) -> Optional[object]:
    """只读获取状态页并解析（不发凭据，F06/F08）。

    不可达/无法解析返回 ``None``。

    F75-10：未认证的 SNNU 链路上 ``userstatus.jsp`` 会 **302 跳转到 ``login.jsp``**
    （真机实测），这本身就是「需要认证」的确凿信号；旧实现只接受 2xx，把它当作
    「无法解析」返回 ``None``，导致只读模式在网络确实需要认证时恒报「未识别」，
    也让 ``classify_network`` 无法判为需认证。这里对**同源且落在 Portal 流程内**
    （``login.jsp`` / ``userstatus.jsp``）的 301/302/303 直接判为 ``AUTH_REQUIRED``，
    **不跟随跳转、不发送任何凭据**；非同源 / 流程外的跳转仍按不可解析处理
    （fail-closed，不臆断「需要认证」）。
    """
    base = portal_base(port)
    try:
        resp = session.get(base + USERSTATUS_JSP, timeout=(3, 6),
                           allow_redirects=False)
    except requests.RequestException:
        return None
    if resp.status_code in (301, 302, 303):
        ok, _target, reason = check_redirect_target(
            resp.headers.get("Location", ""), base,
            allowed_paths=(USERSTATUS_JSP, LOGIN_JSP))
        if ok:
            return StatusPageResult(PortalStatus.AUTH_REQUIRED, marker_found=False,
                                    detail="状态页跳转到登录页，判定需要认证")
        if log is not None:
            log.info("状态页重定向未被信任，按不可解析处理：%s", reason)
        return None
    if not 200 <= resp.status_code < 300:
        return None
    body = decode_body(resp, strict=True)
    if body is None:
        return None
    return parse_status_page(body)


def _interpret_auth_response(session: requests.Session, base: str, resp,
                             log: logging.Logger) -> Tuple[Optional[str], Optional[str]]:
    """把认证 POST 响应解释为最终状态页正文（F07）。

    返回 ``(body, error)``：``body`` 为状态页正文（成功路径），``error`` 为
    拒绝原因。规则：
    - 307/308：默认拒绝（避免凭据被重放）；
    - 302/303（含 301）：校验 Location 同源且在 Portal 流程内，**不带凭据** GET；
    - 200：直接作为状态页；
    - 其余：未确认成功。
    """
    code = resp.status_code
    if code in (307, 308):
        return None, "Portal 要求 %d 重放请求，已拒绝（避免凭据外发）" % code
    if code in (301, 302, 303):
        location = resp.headers.get("Location", "")
        ok, target, err = check_redirect_target(
            location, base, allowed_paths=(USERSTATUS_JSP, LOGIN_JSP))
        if not ok:
            return None, "重定向目标不被允许：%s" % err
        try:
            follow = session.get(target, timeout=(3, 6), allow_redirects=False)
        except requests.RequestException as exc:
            return None, "获取状态页失败（%s）" % type(exc).__name__
        if 300 <= follow.status_code < 400:
            return None, "状态页再次跳转，判定为循环/非预期重定向"
        if not 200 <= follow.status_code < 300:
            return None, "状态页返回 HTTP %d" % follow.status_code
        body = decode_body(follow, strict=True)
        if body is None:
            return None, "状态页解码失败，判定为结构未知"
        return body, None
    if code == 200:
        body = decode_body(resp, strict=True)
        if body is None:
            return None, "登录响应解码失败，判定为结构未知"
        return body, None
    return None, "登录响应 HTTP %d（未确认成功）" % code


def portal_login(session: requests.Session, account: str, password: str,
                 yys: str, port: int, log: logging.Logger):
    """按浏览器真实流程登录（F06/F07）。

    返回 :class:`LoginOutcome`（可解包为 ``(ok, detail)``）。判定要求：
    允许的状态码 + 可信目标 + 状态页账号字段**完整比较**；区分「已有会话」
    与本次认证成功；验证码页 ⇒ 需人工处理。
    """
    return portal_login_detailed(session, account, password, yys, port, log)


def portal_login_detailed(session: requests.Session, account: str, password: str,
                          yys: str, port: int, log: logging.Logger) -> LoginOutcome:
    base = portal_base(port)
    # 1) 打开登录页建立会话
    try:
        session.get(base + LOGIN_JSP, timeout=(3, 6), allow_redirects=False)
    except requests.RequestException as exc:
        return LoginOutcome(False, "无法打开登录页: %s" % type(exc).__name__,
                            status=PortalStatus.AUTH_REQUIRED,
                            error_code=E_PORTAL_UNREACHABLE)

    # 2) 只读预检：已有会话且就是本账号 → 不 POST、不声称验证了新密码
    #    （F75-12：状态页显示 `学号@运营商`、提交为纯学号，须按规范化账号比较）
    pre = read_status_page(session, port, log)
    if pre is not None and getattr(pre, "status", None) is PortalStatus.ONLINE \
            and normalize_account(getattr(pre, "account", None)) \
            and normalize_account(getattr(pre, "account", None)) \
            == normalize_account(account):
        return LoginOutcome(True, "Portal 显示该账号已在线（复用已有会话，未验证新凭据）",
                            status=PortalStatus.ONLINE, already_authenticated=True)

    # 3) 提交认证（禁止自动重定向）
    payload = {"sourceurl": "null", "account": account, "password": password,
               "yys": yys, "issave": ""}
    headers = {"Referer": base + LOGIN_JSP, "Origin": base,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    try:
        resp = session.post(base + LOGIN_ACTION, data=payload, headers=headers,
                            timeout=(3, 8), allow_redirects=False)
    except requests.RequestException as exc:
        return LoginOutcome(False, "登录请求失败: %s" % type(exc).__name__,
                            status=PortalStatus.AUTH_REQUIRED,
                            error_code=E_PORTAL_UNREACHABLE)

    # 4) 解释响应（重定向控制 + 状态码校验）
    body, error = _interpret_auth_response(session, base, resp, log)
    if body is None:
        status = PortalStatus.NEEDS_USER if "重定向" in error or "重放" in error \
            else PortalStatus.AUTH_REQUIRED
        return LoginOutcome(False, error, status=status,
                            error_code=E_PORTAL_STRUCTURE_UNKNOWN)

    # 5) 状态页结构化校验（完整账号比较）
    outcome = parse_status_page(body, account)
    if outcome.status is PortalStatus.ONLINE:
        return LoginOutcome(True, "Portal 确认已在线（状态页账号完整匹配）",
                            status=PortalStatus.ONLINE)
    if outcome.status is PortalStatus.NEEDS_USER:
        return LoginOutcome(False, outcome.detail, status=PortalStatus.NEEDS_USER,
                            error_code=outcome.error_code or E_PORTAL_STRUCTURE_UNKNOWN)
    hint = extract_error_hint(body)
    if hint:
        return LoginOutcome(False, "Portal 返回失败信息: " + hint,
                            status=PortalStatus.NEEDS_USER, error_code=E_AUTH_REJECTED)
    return LoginOutcome(False, "登录响应未能确认成功（状态页无有效登录账号）",
                        status=PortalStatus.AUTH_REQUIRED,
                        error_code=E_PORTAL_STRUCTURE_UNKNOWN)


def edge_fallback(log: logging.Logger, force: bool = False) -> BrowserLaunchResult:
    """打开不走系统代理的 Edge 登录页供人工处理（F19）。

    返回 :class:`BrowserLaunchResult`，**如实**反馈是否真的启动成功，
    不得在找不到/启动失败时谎报「已打开 Edge」。
    """
    exe = shutil.which("msedge")
    if not exe:
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.is_file():
                exe = str(candidate)
                break
    if not exe:
        log.warning("未找到 Edge，请手动访问 %s", PORTAL_ROOT)
        return BrowserLaunchResult(launched=False, error="not_found",
                                   reason="未找到 Edge 可执行文件")
    profile = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CampusReconnectEdge"
    try:
        subprocess.Popen([exe, "--user-data-dir=%s" % profile,
                          "--no-proxy-server", PORTAL_ROOT],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("Edge 启动失败: %s", type(exc).__name__)
        return BrowserLaunchResult(launched=False, executable=exe, error=type(exc).__name__,
                                   reason="Edge 启动失败")
    log.info("已打开 Edge（不走系统代理）的 Portal 登录页，供人工处理。")
    return BrowserLaunchResult(launched=True, executable=exe, reason="已打开 Edge 登录页")


def _cli_adapters() -> RepairAdapters:
    """CLI 侧的适配器集合（全部指向本模块函数，便于测试替换）。"""
    return RepairAdapters(
        wifi_state=wifi_state,
        wifi_reconnect=lambda ssid: wifi_reconnect(ssid, logging.getLogger("campus")),
        resolve_port=resolve_port,
        portal_reachable=portal_reachable,
        probe_online=internet_ok,
        login=portal_login,
    )


def attempt_repair(session: requests.Session, args: argparse.Namespace,
                   log: logging.Logger) -> RepairResult:
    """执行一次重连（CLI）：调用共享服务并按结果决定是否开 Edge 兜底（F14/F19）。"""
    options = RepairOptions.from_namespace(args)
    result = repair_service(session, options, time.monotonic, _cli_adapters(),
                            get_credentials, log=log)
    log.info("重连结果: %s（%s）", "成功" if result.ok else "未成功", result.detail)
    if not result.ok and options.browser_fallback \
            and result.status is PortalStatus.NEEDS_USER:
        edge_fallback(log)
    return result


def diagnose_network(session: requests.Session, target_ssid: str = "",
                     port: Optional[int] = None):
    """综合连通性探测与只读状态页，给出网络/认证状态分类（F08）。

    返回 ``(状态, 各探测结果, 状态页解析|None)``；**不发送任何凭据**。
    """
    results = probe_online(session)
    try:
        ssid, _state = wifi_state()
    except Exception:
        ssid = None
    portal = None
    if port is not None:
        try:
            if portal_reachable(session, port):
                portal = read_status_page(session, port)
        except Exception:
            portal = None
    status = classify_network(ssid, target_ssid, results, portal)
    return status, results, portal


def netcheck(session: requests.Session, args: argparse.Namespace,
             log: logging.Logger) -> int:
    """V0.3 P1 只读三轴健康检查（netcheck）：不切换、不认证、不写系统。

    复用 P0 ``netdiag`` 只读管道（PowerShellRunner 逐命令 ``assert_read_only``
    fail-closed）获取网卡/路由观察，再用 ``campus_network`` 的三轴健康检测给出
    ETHERNET_HEALTH / WIFI_HEALTH / SYSTEM_HEALTH 结论与"如果允许切换，将选择
    哪条路径"的**只读决策**（方案 §3.2/§3.3）。本命令无任何写入口。
    """
    from tools import netdiag

    runner = netdiag.PowerShellRunner()
    observer = NetworkObserver(runner)
    report = observer.observe(target_ssid=args.ssid)
    engine = WindowsProbeEngine()
    snapshot = evaluate(
        report, engine=engine,
        system_probe=lambda: internet_ok(session),
        route_lookup=observer.route_lookup,
    )
    print(format_snapshot(snapshot))
    if args.json:
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    log.info("netcheck 完成（只读）：建议=%s，是否切换=%s",
             snapshot.recommendation.preferred if snapshot.recommendation
             else None,
             snapshot.recommendation.would_switch if snapshot.recommendation
             else None)
    return 0


def failover(session: requests.Session, args: argparse.Namespace,
             log: logging.Logger) -> int:
    """V0.3 P3 只读故障切换监测（failover）：运行状态机，不执行任何动作。

    复用 P0 ``netdiag`` 只读管道 + P1 三轴健康评估，把每帧观测喂给
    :class:`campus_failover.NetworkFailoverManager`（**MONITOR 只读监测模式**），
    输出 11 态状态机的当前状态、优先出口、滞回计数与本轮计划。

    本命令**没有执行器**，且 MONITOR 模式不允许执行任何动作：不切换路由、
    不发 Portal 认证、不写 Metric、不触碰 Clash、不重连 Wi-Fi。
    """
    from tools import netdiag

    runner = netdiag.PowerShellRunner()
    observer = NetworkObserver(runner)
    engine = WindowsProbeEngine()
    policy = FailoverPolicy(
        detection_interval=float(max(1, args.interval)),
        failure_threshold=int(max(1, args.threshold)),
        auth_cooldown=float(max(0, args.auth_cooldown)),
        switch_min_interval=float(max(0, args.switch_min_interval)),
    )
    for issue in policy.validate():
        log.warning("策略参数告警：%s", issue)
    manager = NetworkFailoverManager(policy=policy, mode=FailoverMode.MONITOR,
                                     log=lambda message: log.info("%s", message))
    frames = max(1, int(args.frames))
    results: List[dict] = []
    now = time.monotonic()
    for index in range(frames):
        report = observer.observe(target_ssid=args.ssid)
        snapshot = evaluate(report, engine=engine,
                            system_probe=lambda: internet_ok(session),
                            route_lookup=observer.route_lookup)
        result = manager.tick(now, snapshot)
        results.append(result.to_dict())
        print(format_tick(result, title="帧 %d/%d" % (index + 1, frames)))
        if index + 1 < frames and args.interval > 0:
            time.sleep(args.interval)
            now += args.interval
    if args.json:
        print(json.dumps(results if frames > 1 else results[0],
                         ensure_ascii=False, indent=2))
    log.info("failover 完成（只读监测）：状态=%s，计划=%d 项，已执行=%d 项",
             manager.state.value, len(results[-1]["plan"]),
             len(results[-1]["executed"]))
    return 0


def _snnu_ops(session: requests.Session, observer: Any, log: logging.Logger,
              interface: Optional[str]) -> SnnuOps:
    """P4 CLI 侧的注入式能力集合（指向本模块真实函数，便于测试替换）。

    ``repair`` 封装调用 ``campus_core.repair_service``（**不重写认证引擎**，
    AGENTS §3.1），并把已绑定源地址的会话传入，使 Portal 解析/状态/认证/验证
    四步全部经 SNNU 接口（方案 §6.2）。
    """

    def _repair(bound_session: Any, options: RepairOptions) -> RepairResult:
        return repair_service(bound_session, options, time.monotonic,
                              _cli_adapters(), get_credentials, log=log)

    def _refresh_binding():
        report = observer.observe(target_ssid="SNNU")
        adapter = next((item for item in report.adapters
                        if getattr(item, "name", "") == interface), None)
        if adapter is None:
            raise RuntimeError("SNNU 接口不存在")
        source = _wifi_source_ip(report, adapter.if_index)
        return InterfaceBinding(interface=interface or "",
                                if_index=adapter.if_index, source_ip=source)
    def _bind_interface(bound_session, binding):
        from campus_network import _interface_dns
        report = observer.observe(target_ssid="SNNU")
        transport = WindowsProbeEngine().for_interface(
            binding.if_index, binding.source_ip,
            _interface_dns(report, binding.if_index))
        return transport.bind_session(bound_session)
    return SnnuOps(
        wifi_state=lambda: wifi_state(interface),
        wifi_reconnect=lambda ssid: wifi_reconnect(ssid, log,
                                                   interface=interface),
        ip_ready=lambda: _ipv4_ready(interface),
        route_lookup=observer.route_lookup,
        resolve_port=resolve_port,
        portal_reachable=portal_reachable,
        read_status=lambda sess, port: read_status_page(sess, port, log),
        repair=_repair,
        bind_interface=_bind_interface,
        refresh_binding=_refresh_binding,
    )

def snnu(session: requests.Session, args: argparse.Namespace,
         log: logging.Logger) -> int:
    """V0.3 P4 SNNU 备用链路与接口绑定认证（受控）。

    - 默认**只读识别**：关联检查/重连、等待 IP 就绪、**出口证明**、Portal 端口
      解析/可达/状态识别；全程不提交凭据；
    - ``--auth`` 才在**出口已确认为 SNNU 接口**后经绑定会话提交一次受控认证
      （须用户显式授权；复用单实例互斥体，避免并发认证）。

    无法证明出口经 SNNU 接口时，返回"无线认证路径未确认"且**不发送真实凭据**
    （AGENTS §3.6 / 方案 §6.2）。
    """
    from tools import netdiag

    # 受控认证前先取进程级互斥体（fail-closed）：拒绝并发认证（F18）。
    if args.auth and not ensure_single_instance(log):
        log.error("已有实例在运行：拒绝并发受控认证。")
        return 1
    if args.auth:
        log.warning("--auth：将经 SNNU 接口提交一次真实认证（受控、前台上人）。")

    runner = netdiag.PowerShellRunner()
    observer = NetworkObserver(runner)
    report = observer.observe(target_ssid=args.ssid)
    probe_engine = WindowsProbeEngine()
    snapshot = evaluate(report, engine=probe_engine,
                        system_probe=lambda: internet_ok(session),
                        route_lookup=observer.route_lookup)
    wifi = snapshot.wifi
    interface = wifi.interface if wifi is not None else None
    if_index = wifi.if_index if wifi is not None else None
    source_ip = wifi.source_ip if wifi is not None else None

    ops = _snnu_ops(session, observer, log, interface)
    link = SnnuBackupLink(
        ops=ops, session=session, target_ssid=args.ssid,
        interface=interface or "", if_index=if_index, source_ip=source_ip,
        allow_auth=bool(args.auth), reconnect_wifi=not args.no_reconnect,
        port=args.port, operator=args.yys, account_format=args.account_format,
        log=lambda message: log.info("%s", message))
    result = link.run()
    print(format_backup_result(result))
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    log.info("snnu 完成：ok=%s，stage=%s，提交凭据=%s",
             result.ok, result.stage.value, result.credentials_sent)
    return 0 if result.ok else 1


def _load_adapters_config() -> Optional[dict]:
    """读取 ``adapters.json``（P0 产物）；缺失/损坏时返回 ``None``（不臆断角色）。"""
    try:
        with open(SCRIPT_DIR / "adapters.json", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _route_roles(observer: Any, ssid: str) -> Tuple[Any, Any, Any]:
    """解析以太网/Wi-Fi 角色与只读诊断报告（配置优先，其次物理分类）。"""
    report = observer.observe(target_ssid=ssid)
    roles = resolve_roles(report, _load_adapters_config())
    return report, roles.get("ethernet"), roles.get("wifi")


def _build_route_guard(observer: Any, runner: Any, eth: Any, wifi: Any,
                       args: argparse.Namespace, log: logging.Logger, *,
                       timers: Optional[TimerTable] = None) -> RouteGuard:
    """构造路由卫士（journal 落盘到 ``logs/route_journal.json``，崩溃恢复用）。"""
    policy = FailoverPolicy(
        detection_interval=float(max(1, getattr(args, "interval", 15))),
        failure_threshold=int(max(1, getattr(args, "threshold", 3))),
        auth_cooldown=float(max(0, getattr(args, "auth_cooldown", 180))),
        switch_min_interval=float(max(0, getattr(args, "switch_min_interval", 300))))
    return RouteGuard(
        ops=build_windows_route_ops(
            observer, runner, allowed_if_indices=[value for value in (
                getattr(eth, "if_index", None), getattr(wifi, "if_index", None))
                if value is not None]),
        ethernet_if=eth.if_index if eth is not None else None,
        wifi_if=wifi.if_index if wifi is not None else None,
        policy=policy,
        journal=RouteJournal(LOG_DIR / "route_journal.json"),
        timers=timers,
        authorized=bool(getattr(args, "yes", False)),
        log=lambda message: log.info("%s", message))


def _wifi_source_ip(report: Any, if_index: Optional[int]) -> Optional[str]:
    """从只读诊断报告取无线接口的 IPv4 源地址（供 SNNU 会话绑定使用）。"""
    if if_index is None:
        return None
    for row in getattr(report, "ip_addresses", None) or []:
        if (row.get("if_index") == if_index
                and row.get("address_family") == "IPv4"):
            address = row.get("ip_address")
            if address:
                return str(address)
    return None


def _build_composite_executor(session: requests.Session, observer: Any,
                              report: Any, wifi: Any,
                              args: argparse.Namespace, log: logging.Logger,
                              guard: RouteGuard) -> CompositeExecutor:
    """组合 P4（SNNU）与 P5（路由）执行器为单一副作用门背后的唯一执行器。

    角色对象来自 ``resolve_roles``（``netdiag.AdapterRecord``）：接口名取 ``name``，
    无线 IPv4 源地址从只读报告的 IP 表取得（仅用于会话绑定，不做出口假定）。
    """
    interface = (getattr(wifi, "interface", None)
                 or getattr(wifi, "name", None)) if wifi is not None else None
    wifi_if = getattr(wifi, "if_index", None) if wifi is not None else None
    source_ip = _wifi_source_ip(report, wifi_if)
    ops = _snnu_ops(session, observer, log, interface)

    def _portal_action() -> Any:
        link = SnnuBackupLink(
            ops=ops, session=session, target_ssid=args.ssid,
            interface=interface or "", if_index=wifi_if, source_ip=source_ip,
            allow_auth=True, reconnect_wifi=False, port=args.port,
            operator=args.yys, account_format=args.account_format,
            log=lambda message: log.info("%s", message))
        return link.run()

    def _wifi_action() -> bool:
        return wifi_reconnect(args.ssid, log, interface=interface)

    snnu = SnnuBackupExecutor(portal_action=_portal_action,
                              wifi_action=_wifi_action,
                              log=lambda message: log.info("%s", message))
    return CompositeExecutor(snnu=snnu, route=MetricExecutor(guard),
                             log=lambda message: log.info("%s", message))


def _build_remote_guard(log: logging.Logger, *, defer_failback: bool = True,
                        probe_interval: float = 30.0):
    """构造 P7 远程保障守卫（读经 P0 ``netdiag`` 只读管道，零写命令）。"""
    from campus_remote import RemoteSafetyGuard, build_windows_remote_ops
    from tools import netdiag

    ops = build_windows_remote_ops(netdiag.PowerShellRunner())
    return RemoteSafetyGuard(ops=ops, probe_interval=probe_interval,
                             defer_failback=defer_failback,
                             log=lambda message: log.info("%s", message))


def _route_journal_pending() -> Optional[dict]:
    """读取路由变更日志中的未完成事务（只读；无日志/无待恢复项返回 ``None``）。"""
    from campus_route_guard import RouteJournal

    try:
        return RouteJournal(LOG_DIR / "route_journal.json").pending()
    except Exception:
        return None


def remote(session: requests.Session, args: argparse.Namespace,
           log: logging.Logger) -> int:
    """V0.3 P7 远程保障（默认**只读**）。

    复用 P0 ``netdiag`` 只读管道（已含 ``remote_control``：进程/端口/UDP/睡眠/
    自动登录/自启项）完成三件事：

    1. **远控探测与敏感期判定**（方案 §7.3）——是否存在已建立的远控会话；
    2. **无人值守就绪自检**（方案 §7.3）——睡眠/休眠、自动登录、远控与托盘自启、
       凭据在位、远控自动重连（人工项）；
    3. **崩溃恢复可见性**（方案 §7.2）——报告路由变更日志中是否有未完成事务
       （处置仍由 ``route --recover`` 或托盘启动时自动执行）。

    本命令**不切换路由、不认证、不触碰远控进程**，默认也不写任何文件
    （``--log`` 才追加关联事件日志）。
    """
    from campus_remote import (MonitorWatchdog, RemoteEventLog, RemoteProbe,
                               format_remote_status)
    from tools import netdiag

    runner = netdiag.PowerShellRunner()
    observer = NetworkObserver(runner)
    report = observer.observe(target_ssid=args.ssid)
    probe = RemoteProbe.from_report(report)
    guard = _build_remote_guard(log,
                                probe_interval=float(max(5, args.probe_interval)))
    now = time.monotonic()
    status = guard.observe(probe, now)
    readiness = guard.readiness(probe)
    watchdog = MonitorWatchdog()
    decision = watchdog.check(now, alive=True)

    events = None
    if args.log:
        events = RemoteEventLog(LOG_DIR / "remote_guard.jsonl")
        correlation = events.new_correlation_id()
        events.record("remote", "远控探测：%s" % status.reason,
                      correlation_id=correlation)
        for warning in readiness.warnings:
            events.record("system", "无人值守缺口：%s" % warning,
                          correlation_id=correlation)
    print(format_remote_status(status, readiness=readiness, events=events,
                               watchdog=decision))
    pending = _route_journal_pending()
    if pending:
        print("崩溃恢复：检测到未完成的路由变更事务 %s（目标=%s）—— 请执行 "
              "route --recover（托盘启动时也会自动恢复）。"
              % (pending.get("operation_id"), pending.get("target")))
    else:
        print("崩溃恢复：未发现未完成的路由变更事务。")
    if args.json:
        print(json.dumps({"status": status.to_dict(),
                          "readiness": readiness.to_dict(),
                          "watchdog": decision.to_dict(),
                          "pending_transaction": pending},
                         ensure_ascii=False, indent=2))
    log.info("remote 完成（只读）：远控=%s，敏感期=%s，就绪缺口=%d 项",
             status.activity, status.sensitive, len(readiness.warnings))
    return 0


def route(session: requests.Session, args: argparse.Namespace,
          log: logging.Logger) -> int:
    """V0.3 P5 受控路由切换与路由卫士（快照 / 崩溃恢复 / 受控切换 / 受控 AUTO）。

    - **默认只读**：输出路由快照（接口 Metric、默认路由、当前出口）与未完成事务，
      零写入、零路由变更；
    - ``--recover``：读取变更日志，处置未完成事务（已生效则提交，否则按快照回滚）；
    - ``--apply {ethernet,wifi}``：受控执行**一次**事务式切换（快照 → 最小变更 →
      出口验证 IPv4+IPv6 → 失败回滚）；须 ``--yes`` 显式授权，且取单实例互斥体；
    - ``--auto``：受控运行 11 态状态机（``FailoverMode.AUTO``）并接入真实组合执行器；
      须 ``--yes`` 显式授权，且取单实例互斥体。

    危险操作黑名单（AGENTS §3.12）在本命令无任何入口：只允许受限的
    ``Set-NetIPInterface -InterfaceMetric``（:func:`campus_route_guard.assert_write_allowed`）。
    """
    from tools import netdiag

    if (args.apply or args.auto) and not args.yes:
        log.error("受控路由变更需显式授权：请加 --yes（前台上人、单人值守）。")
        return 2
    if args.apply or args.auto:
        if not ensure_single_instance(log):
            log.error("已有实例在运行：拒绝并发路由变更。")
            return 1
        log.warning("--yes：允许真实路由/Metric 变更（受控、前台上人、单人值守）。")

    runner = netdiag.PowerShellRunner()
    observer = NetworkObserver(runner)
    report, eth, wifi = _route_roles(observer, args.ssid)
    log.info("角色解析：以太网 ifIndex=%s，Wi-Fi ifIndex=%s",
             eth.if_index if eth is not None else None,
             wifi.if_index if wifi is not None else None)

    # --recover 用无冷却、无授权的卫士（恢复是安全动作，不受授权门禁约束）。
    if args.recover:
        guard = _build_route_guard(observer, runner, eth, wifi, args, log)
        recovered = guard.recover()
        if recovered is None:
            print("崩溃恢复：未发现未完成的路由变更事务（无需恢复）。")
            if args.json:
                print(json.dumps({"recovered": None}, ensure_ascii=False,
                                 indent=2))
            log.info("route --recover 完成：无未完成事务。")
            return 0
        print(format_route_change(recovered))
        if args.json:
            print(json.dumps(recovered.to_dict(), ensure_ascii=False, indent=2))
        log.info("route --recover 完成：状态=%s，已回滚=%s",
                 recovered.status.value, recovered.rolled_back)
        return 0

    if args.apply:
        guard = _build_route_guard(observer, runner, eth, wifi, args, log,
                                   timers=TimerTable())
        record = guard.apply(args.apply, "命令行受控切换（--apply）")
        print(format_route_change(record))
        if args.json:
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        log.info("route --apply 完成：target=%s，状态=%s，ok=%s",
                 args.apply, record.status.value, record.ok)
        return 0 if record.ok else 1

    if args.auto:
        guard = _build_route_guard(observer, runner, eth, wifi, args, log)
        recovered = guard.recover()     # AUTO 启动前先做崩溃恢复
        if recovered is not None:
            print(format_route_change(recovered))
        executor = _build_composite_executor(session, observer, report, wifi,
                                             args, log, guard)
        # V0.3 P7：远程会话敏感期推迟自动切回（方案 §4.3）。受控 CLI 入口要求
        # **显式**加 --defer-failback 才接入门禁（默认不接入，行为与 P5 一致）；
        # 托盘侧默认开启（见 campus_tray_failover / 托盘配置 remote_defer_failback）。
        defer_failback = bool(getattr(args, "defer_failback", False))
        remote = (_build_remote_guard(log) if defer_failback else None)
        manager = NetworkFailoverManager(
            policy=guard.policy, mode=FailoverMode.AUTO, executor=executor,
            failback_gate=(remote.failback_gate if remote is not None else None),
            log=lambda message: log.info("%s", message))
        frames = max(1, int(args.frames))
        now = time.monotonic()
        for index in range(frames):
            if remote is not None:
                remote.poll(now)    # 每轮刷新远控状态（按 probe_interval 节流）
            report = observer.observe(target_ssid=args.ssid)
            snapshot = evaluate(report, engine=WindowsProbeEngine(),
                                system_probe=lambda: internet_ok(session),
                                route_lookup=observer.route_lookup)
            result = manager.tick(now, snapshot)
            print(format_tick(result, title="AUTO 帧 %d/%d" % (index + 1, frames)))
            if args.json:
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            if index + 1 < frames and args.interval > 0:
                time.sleep(args.interval)
                now += args.interval
        log.info("route --auto 完成：状态=%s，执行动作=%d 项",
                 manager.state.value, len(result.executed))
        return 0

    # 默认：只读快照（零写入）。
    guard = _build_route_guard(observer, runner, eth, wifi, args, log)
    snapshot = guard.snapshot()
    print(format_route_snapshot(snapshot, journal=guard.journal))
    if args.json:
        print(json.dumps({"snapshot": snapshot.to_dict(),
                          "pending": guard.journal.pending()},
                         ensure_ascii=False, indent=2))
    log.info("route 快照完成（只读）：以太网 ifIndex=%s，Wi-Fi ifIndex=%s，"
             "当前出口 ifIndex=%s", guard.ethernet_if, guard.wifi_if,
             snapshot.primary_egress_if_index)
    return 0


def status(session: requests.Session, args: argparse.Namespace) -> None:
    ssid, state = wifi_state()
    print("无线网卡状态 : %s（SSID: %s）" % (state, ssid or "未连接"))
    port, port_source = resolve_port(session, args.port)
    if port is None:
        print("Portal 端口  : 未解析出（%s）" % port_source)
        print("Portal 可达  : 否（端口未确定）")
    else:
        print("Portal 端口  : %d（%s）" % (port, port_source))
        print("Portal 可达  : %s" % ("是" if portal_reachable(session, port) else "否"))
    results = probe_online(session)
    for result in results:
        note = "通过" if result.ok else ("失败(%s)" % result.error if result.error else "未通过")
        print("公网探测      : %s -> %s" % (result.source, note))
    portal = read_status_page(session, port) if port is not None else None
    verdict = classify_network(ssid, getattr(args, "ssid", "") or "", results, portal)
    print("综合判定      : %s" % verdict.value)
    print("凭据已保存    : %s" % ("是" if get_credentials() else "否（先执行 configure）"))
    proxy = read_proxy_settings()
    if proxy:
        enabled = proxy.get("enable")
        print("系统代理      : %s（%s）" % ("开启" if enabled else "关闭", proxy.get("server") or "-"))
        bypass = proxy.get("bypass") or ""
        entries = [e.strip().lower() for e in bypass.split(";") if e.strip()]
        missing = [h for h in BYPASS_HOSTS if h not in entries]
        if missing:
            print("代理绕过列表  : 未包含 %s  <-- Edge 将无法直接打开登录页，"
                  "请执行 fix-bypass 或在 Clash Verge 中添加" % ", ".join(missing))
        else:
            print("代理绕过列表  : 已包含 Portal 地址（Edge 可直接登录）")
    print("说明：探测失败不必然代表校园账号掉线，可能是探测地址被限制；"
          "以本页 Portal 可达 + 直连探测综合判断。")


_SINGLE_INSTANCE_GUARD: Optional[SingleInstanceGuard] = None


def ensure_single_instance(log: logging.Logger) -> bool:
    """进程级单实例保护（F18）。

    使用**正确声明 ABI** 的命名互斥体（``CreateMutexW`` 的 ``argtypes``/``restype``
    已声明，``HANDLE`` 按指针宽度处理）；持有可关闭句柄避免 GC 失效。
    创建失败时 **fail-closed**：返回 ``False`` 拒绝启动后台认证，不再 fail-open 继续。
    ``once`` 与 ``watch``/托盘共用同一互斥体，避免 ``watch`` 单实例被 ``once`` 绕过。
    """
    global _SINGLE_INSTANCE_GUARD
    if os.name != "nt":
        return True
    guard = SingleInstanceGuard(log=log)
    result = guard.acquire()
    if result.already_running:
        log.error("已有一个监控实例在运行，本次退出（互斥体保护）。")
        return False
    if not result.ok:
        log.error("%s（fail-closed，拒绝后台认证）", result.detail)
        return False
    _SINGLE_INSTANCE_GUARD = guard   # 持有句柄引用，进程存活期间保持互斥
    return True


def _mask_account(account: str) -> str:
    """账号脱敏：仅保留首尾字符（``--dry-run`` 报告用，F20）。"""
    text = str(account or "")
    if len(text) <= 2:
        return "*" * len(text)
    return "%s%s%s" % (text[0], "*" * (len(text) - 2), text[-1])


def dry_run(session: requests.Session, args: argparse.Namespace,
            log: logging.Logger) -> None:
    """预检：除提交认证外的全部步骤，不发送账号密码（账号脱敏，F20）。"""
    ssid, state = wifi_state()
    print("SSID/状态    : %s / %s" % (ssid or "未连接", state))
    port, source = resolve_port(session, args.port)
    if port is None:
        print("认证端口     : 未解析出（%s）" % source)
        print("登录页可达   : 否（端口未确定）")
    else:
        print("认证端口     : %d（%s）" % (port, source))
        print("登录页可达   : %s" % ("是" if portal_reachable(session, port) else "否"))
    creds = get_credentials()
    print("凭据已保存   : %s" % ("是" if creds else "否（先执行 configure）"))
    if creds:
        account, _ = creds
        if args.account_format == "isp" and args.yys:
            sent = "%s@%s" % (account, args.yys)
        else:
            sent = account
        payload = {"sourceurl": "null", "account": _mask_account(sent),
                   "password": "***已隐藏***", "yys": args.yys, "issave": ""}
        print("将提交的载荷 : %s" % payload)
    print("dry-run 完成：未发送任何认证请求。")


def watch(session: requests.Session, args: argparse.Namespace,
          log: logging.Logger) -> None:
    """常驻监测：三表分离（V0.3 P2）。

    - **网络检测计时器持续运行**：无论是否处于认证冷却，每个 ``interval``
      都探测一次公网（修复 P0-T6 取证缺陷 —— 冷却期探测被整体暂停）；
    - **Portal 认证冷却只限制认证**：由 ``TimerTable.auth_allowed()`` 门控，
      不参与探测节奏；``next_attempt`` 一旦登记，**恢复不抹掉**最短间隔；
    - 需人工处理（凭据/验证码/结构未知）时停止自动频繁重试；
    - 全程使用 ``time.monotonic`` 与 ``time.sleep``（测试用虚拟时钟替换）。
    """
    if not ensure_single_instance(log):
        return
    log.info("开始监测：目标 Wi-Fi=%r，yys=%s，间隔 %d 秒，连续 %d 次失败触发，冷却 %d 秒。",
             args.ssid, args.yys or "(默认校园网)", args.interval,
             args.threshold, args.cooldown)
    failures = 0
    timers = TimerTable()
    needs_user = False
    while True:
        try:
            online, via = internet_ok(session)
            if online:
                if failures:
                    log.info("公网恢复。")
                failures = 0
                needs_user = False
                # 注意：**不**重置认证冷却 —— 恢复不抹掉最短重连间隔（F09）
            else:
                failures += 1
                if failures < args.threshold:
                    log.info("公网探测失败（%d/%d）：%s", failures, args.threshold,
                             via or "无明细")
                elif failures == args.threshold:
                    log.warning("连续 %d 次失败，判定疑似掉线（%s）。", failures,
                                via or "无明细")
                if needs_user:
                    log.warning("需人工处理（凭据/验证码/Portal 结构），暂停自动重连。")
                elif failures >= args.threshold and timers.auth_allowed(time.monotonic()):
                    result = attempt_repair(session, args, log)
                    timers.note_auth(time.monotonic(), args.cooldown)
                    if getattr(result, "needs_user", False):
                        needs_user = True
                    elif getattr(result, "network_ok", None) is True:
                        # 确认窗口内自愈：修复预检（复核探测）已确认公网连通 ⇒
                        # 判定为探测抖动而非真实掉线，清零失败计数，避免阈值之后
                        # 逐轮重复告警与重复修复（P8 K-4）。
                        log.info("确认窗口内公网已连通（修复预检），判定为探测抖动，"
                                 "已抑制本次掉线判定。")
                        failures = 0
            # 网络检测计时器：持续运行 —— 探测节奏恒为 interval，认证冷却（auth）
            # 与切换冷却（switch）都不得推迟它（V0.3 P2 三表分离）。
            timers.schedule_detection(time.monotonic(), args.interval)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("用户终止。")
            return
        except Exception as exc:
            log.warning("监测异常: %s；下一轮继续。", type(exc).__name__)
            time.sleep(args.interval)


def _port_type(value: str) -> int:
    """CLI 端口参数：接受 1..65535（与动态识别能力一致，F20）。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("端口必须是整数")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1..65535 之间")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SNNU 校园网自动重连助手（Windows）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="把账号密码存入 Windows 凭据管理器")
    p_status = sub.add_parser("status", help="查看 Wi-Fi、Portal、公网与代理绕过状态")
    p_status.add_argument("--port", type=_port_type, default=None,
                          help="默认动态识别端口；可强制指定 1..65535 的端口")
    sub.add_parser("fix-bypass", help="把 Portal 地址写入系统代理绕过列表")
    p_netcheck = sub.add_parser(
        "netcheck",
        help="V0.3 P1 只读三轴网络健康检查（不切换路由、不发认证）")
    p_netcheck.add_argument("--ssid", default="SNNU",
                            help="目标校园 Wi-Fi 名称，默认 SNNU")
    p_netcheck.add_argument("--json", action="store_true",
                            help="额外输出机器可读 JSON（三轴快照契约）")
    p_failover = sub.add_parser(
        "failover",
        help="V0.3 P3 只读故障切换监测（运行 11 态状态机，不执行任何动作）")
    p_failover.add_argument("--ssid", default="SNNU",
                            help="目标校园 Wi-Fi 名称，默认 SNNU")
    p_failover.add_argument("--frames", type=int, default=1,
                            help="监测帧数（默认 1；观察滞回/冷却需多帧，如 --frames 8）")
    p_failover.add_argument("--interval", type=int, default=15,
                            help="检测间隔秒数（同时是多帧之间的等待秒数），默认 15")
    p_failover.add_argument("--threshold", type=int, default=3,
                            help="主链路连续可靠失败阈值，默认 3")
    p_failover.add_argument("--auth-cooldown", type=int, default=180,
                            help="Portal 认证冷却秒数（只限制认证），默认 180")
    p_failover.add_argument("--switch-min-interval", type=int, default=300,
                            help="链路切换最短间隔秒数（只限制路由变更），默认 300")
    p_failover.add_argument("--json", action="store_true",
                            help="额外输出机器可读 JSON（TickResult 契约）")
    p_snnu = sub.add_parser(
        "snnu",
        help="V0.3 P4 SNNU 备用链路与接口绑定认证（默认只读识别，--auth 受控认证）")
    p_snnu.add_argument("--ssid", default="SNNU",
                        help="目标校园 Wi-Fi 名称，默认 SNNU")
    p_snnu.add_argument("--port", type=_port_type, default=None,
                        help="Portal 认证端口；默认动态识别，失效时可强制指定")
    p_snnu.add_argument("--yys", choices=("mobile", "unicom", "telecom", ""),
                        default="mobile",
                        help="运营商: mobile(移动)/unicom(联通)/telecom(电信)/''(默认校园网)")
    p_snnu.add_argument("--account-format", choices=("plain", "isp"),
                        default="plain",
                        help="账号提交格式: plain=学号(默认); isp=学号@运营商")
    p_snnu.add_argument("--no-reconnect", action="store_true",
                        help="不主动重连 Wi-Fi（仅观察关联状态）")
    p_snnu.add_argument("--auth", action="store_true",
                        help="受控提交真实凭据（默认关闭；须经用户授权，前台上人）")
    p_snnu.add_argument("--json", action="store_true",
                        help="额外输出机器可读 JSON（BackupLinkResult 契约）")
    p_route = sub.add_parser(
        "route",
        help="V0.3 P5 受控路由切换与路由卫士（默认只读快照；"
             "--recover 崩溃恢复；--apply/--auto 受控真实变更，需 --yes）")
    p_route.add_argument("--ssid", default="SNNU",
                         help="目标校园 Wi-Fi 名称，默认 SNNU")
    p_route.add_argument("--port", type=_port_type, default=None,
                         help="Portal 认证端口；默认动态识别，失效时可强制指定")
    p_route.add_argument("--yys", choices=("mobile", "unicom", "telecom", ""),
                         default="mobile",
                         help="运营商: mobile(移动)/unicom(联通)/telecom(电信)/''(默认校园网)")
    p_route.add_argument("--account-format", choices=("plain", "isp"),
                         default="plain",
                         help="账号提交格式: plain=学号(默认); isp=学号@运营商")
    p_route.add_argument("--apply", choices=("ethernet", "wifi"), default=None,
                         help="受控执行一次路由切换（快照→最小变更→出口验证→回滚）")
    p_route.add_argument("--auto", action="store_true",
                         help="受控运行 AUTO 状态机（接入真实组合执行器）")
    p_route.add_argument("--recover", action="store_true",
                         help="崩溃恢复：读取变更日志并处置未完成事务")
    p_route.add_argument("--yes", action="store_true",
                         help="显式授权真实路由变更（前台上人、单人值守）")
    p_route.add_argument("--frames", type=int, default=1,
                         help="--auto 的监测帧数（默认 1）")
    p_route.add_argument("--interval", type=int, default=15,
                         help="--auto 的检测间隔秒数，默认 15")
    p_route.add_argument("--threshold", type=int, default=3,
                         help="主链路连续可靠失败阈值，默认 3")
    p_route.add_argument("--auth-cooldown", type=int, default=180,
                         help="Portal 认证冷却秒数（只限制认证），默认 180")
    p_route.add_argument("--switch-min-interval", type=int, default=300,
                         help="链路切换最短间隔秒数（只限制路由变更），默认 300")
    p_route.add_argument("--defer-failback", action="store_true",
                         help="--auto 时接入 P7「远程会话敏感期推迟切回」门禁"
                              "（受控入口默认不接入；托盘侧默认开启）")
    p_route.add_argument("--json", action="store_true",
                         help="额外输出机器可读 JSON（路由快照/变更记录契约）")
    p_remote = sub.add_parser(
        "remote",
        help="V0.3 P7 远程保障（远控探测 / 敏感期 / 无人值守就绪自检，默认只读）")
    p_remote.add_argument("--ssid", default="SNNU",
                          help="目标校园 Wi-Fi 名称，默认 SNNU")
    p_remote.add_argument("--probe-interval", type=int, default=30,
                          help="远控探测最小间隔秒数（节流），默认 30")
    p_remote.add_argument("--log", action="store_true",
                          help="把本次事件追加到 logs/remote_guard.jsonl（默认不写文件）")
    p_remote.add_argument("--json", action="store_true",
                          help="额外输出机器可读 JSON（远程保障契约）")
    for command in ("once", "watch"):
        p = sub.add_parser(command, help="单次尝试" if command == "once" else "常驻自动重连")
        p.add_argument("--ssid", default="SNNU", help="目标校园 Wi-Fi 名称，默认 SNNU")
        p.add_argument("--port", type=_port_type, default=None,
                       help="Portal 认证端口；默认动态识别，失效时可强制指定")
        p.add_argument("--yys", choices=("mobile", "unicom", "telecom", ""), default="mobile",
                       help="运营商: mobile(移动)/unicom(联通)/telecom(电信)/''(默认校园网)")
        p.add_argument("--account-format", choices=("plain", "isp"), default="plain",
                       help="账号提交格式: plain=学号(默认, 同浏览器表单); isp=学号@运营商")
        p.add_argument("--browser-fallback", action="store_true",
                       help="失败时打开不走系统代理的 Edge 登录页")
        p.add_argument("--reconnect-wifi", action="store_true",
                       help="Wi-Fi 未连接时先用 netsh 尝试重连")
        if command == "once":
            p.add_argument("--force", action="store_true",
                           help="在线也强制执行一次认证（仅 once，会提交真实凭据）")
            p.add_argument("--dry-run", action="store_true",
                           help="只做端口/可达性/凭据预检，不发送认证请求")
        if command == "watch":
            p.add_argument("--interval", type=int, default=15, help="检测间隔秒数")
            p.add_argument("--threshold", type=int, default=3, help="连续失败次数阈值")
            p.add_argument("--cooldown", type=int, default=180, help="两次尝试最小间隔秒数")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    log = setup_logging(args.verbose)
    log.info("命令: %s", args.command)
    if args.command == "configure":
        configure()
        return
    # watch 参数校验放在建立会话/任何副作用之前（F20：无效参数无网络/系统副作用）
    if args.command == "watch":
        if args.interval < 5 or args.cooldown < 60 or args.threshold < 1:
            log.error("参数不合法: interval>=5, cooldown>=60, threshold>=1")
            raise SystemExit(2)
    session = direct_session()
    if args.command == "status":
        status(session, args)
    elif args.command == "netcheck":
        raise SystemExit(netcheck(session, args, log))
    elif args.command == "failover":
        raise SystemExit(failover(session, args, log))
    elif args.command == "snnu":
        raise SystemExit(snnu(session, args, log))
    elif args.command == "route":
        raise SystemExit(route(session, args, log))
    elif args.command == "remote":
        raise SystemExit(remote(session, args, log))
    elif args.command == "fix-bypass":
        if not fix_bypass(log):
            raise SystemExit(1)
    elif args.command == "once":
        if args.dry_run:
            dry_run(session, args, log)
            return
        # F18：once 认证与 watch/托盘共用同一进程级互斥体（fail-closed）
        if not ensure_single_instance(log):
            raise SystemExit(1)
        online, _ = internet_ok(session)
        if args.force and online:
            log.warning("--force：当前网络正常，仍将提交一次真实认证（仅用于接口验证）。")
        elif online:
            log.info("当前网络正常，无需认证（预检用 --dry-run，强制测试用 --force）。")
            return
        result = attempt_repair(session, args, log)
        if not result.ok:
            raise SystemExit(1)
    elif args.command == "watch":
        watch(session, args, log)


if __name__ == "__main__":
    main()
