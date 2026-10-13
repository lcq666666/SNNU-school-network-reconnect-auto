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
  成功的判定 = userstatus.jsp 出现"当前登录账号"且包含账号 且 直连公网探测通过。

命令:
  configure   交互式保存账号密码到 Windows 凭据管理器
  status      诊断: Wi-Fi / Portal / 公网探测 / 系统代理绕过列表
  once        执行一次重连 (--dry-run 预检; --force 在线强制, 会提交真实凭据)
  watch       常驻监测, 掉线自动重连 (单实例保护, 重复启动自动退出)
  fix-bypass  把 Portal 地址写入系统代理绕过列表(注册表), Edge 无需关代理即可打开登录页

安全边界: 不执行注销(logoff), 不修改 Clash Verge 配置, 不明文保存密码, 日志不含密码。
"""
from __future__ import annotations

import argparse
import ctypes
import getpass
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import keyring
    import requests
except ImportError:
    print("缺少依赖，请先运行: py -m pip install requests keyring")
    raise SystemExit(2)

if os.name == "nt":
    import winreg

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
ONLINE_MARKER = "当前登录账号"
FAILURE_HINTS = ("密码错误", "密码不正确", "密码有误", "用户不存在", "账号不存在",
                 "账户不存在", "欠费", "冻结", "绑定", "验证码", "登录失败", "失败")
DEFAULT_PROBES = (
    ("http://www.msftconnecttest.com/connecttest.txt", "Microsoft Connect Test"),
    ("http://captive.apple.com/hotspot-detect.html", "Success"),
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")


def setup_logging(verbose: bool) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("campus")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "campus_reconnect.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": UA, "Cache-Control": "no-cache"})
    return session


def decode_body(resp: requests.Response) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return resp.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return resp.content.decode("utf-8", errors="replace")


def internet_ok(session: requests.Session,
                probes=DEFAULT_PROBES) -> Tuple[bool, str]:
    """直连(不走系统代理)探测公网。任一探测通过即视为在线。"""
    for url, expect in probes:
        try:
            resp = session.get(url, timeout=(2.5, 4), allow_redirects=False)
            if resp.status_code == 200 and (expect is None or expect in resp.text):
                return True, url
        except requests.RequestException:
            continue
    return False, ""


def portal_base(port: int) -> str:
    return "http://%s:%d" % (PORTAL_IP, port)


def detect_portal_port(session: requests.Session) -> Optional[int]:
    """解析 Portal 根页面 JS 跳转声明的认证端口；识别不到返回 None，不假装成功。"""
    try:
        resp = session.get(PORTAL_ROOT, timeout=(3, 6), allow_redirects=False)
        m = re.search(r":(\d{2,5})/snnuportal/login\.jsp", decode_body(resp))
        if m:
            port = int(m.group(1))
            if 1 <= port <= 65535:
                return port
    except requests.RequestException:
        pass
    return None


def resolve_port(session: requests.Session,
                 requested: Optional[int]) -> Tuple[int, str]:
    """确定认证端口及其来源。优先级: 用户指定 > 首页动态识别 > 已知端口试探 > 8601。"""
    if requested is not None:
        return requested, "用户手动指定"
    detected = detect_portal_port(session)
    if detected is not None:
        return detected, "从 Portal 首页动态识别"
    for port in KNOWN_PORTS:
        if portal_reachable(session, port):
            return port, "首页跳转解析失败，试探已知端口 %d 可用" % port
    return 8601, "无法识别端口且已知端口均不可达，回退 8601（不保证有效）"


def portal_reachable(session: requests.Session, port: int) -> bool:
    """检查实际认证端口的登录页；只访问根地址不能证明该端口可用。"""
    try:
        resp = session.get(portal_base(port) + LOGIN_JSP,
                           timeout=(2.5, 4), allow_redirects=False)
        return 200 <= resp.status_code < 400
    except requests.RequestException:
        return False


def wifi_state() -> Tuple[Optional[str], str]:
    """返回 (ssid, 状态)。未连接无线时 ssid 为 None。"""
    if os.name != "nt":
        return None, "non-windows"
    try:
        done = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=5, check=False)
        data = done.stdout
        text = None
        for enc in ("utf-8", "gbk", "locale"):
            try:
                if enc == "locale":
                    import locale
                    text = data.decode(locale.getpreferredencoding(False), errors="replace")
                else:
                    text = data.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            text = data.decode("utf-8", errors="replace")
        ssid, state = None, "unknown"
        for line in text.splitlines():
            low = line.strip().lower()
            if low.startswith("ssid") and "bssid" not in low and ssid is None:
                ssid = line.split(":", 1)[1].strip()
            elif low.startswith("状态") or low.startswith("state"):
                state = line.split(":", 1)[1].strip()
        return ssid, state
    except (OSError, subprocess.TimeoutExpired):
        return None, "error"


def wifi_reconnect(ssid: str, log: logging.Logger) -> bool:
    try:
        done = subprocess.run(["netsh", "wlan", "connect", "name=%s" % ssid],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=10, check=False)
        ok = done.returncode == 0
        log.info("netsh 重连 Wi-Fi %s: %s", ssid, "已发起" if ok else "失败")
        return ok
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("netsh 重连 Wi-Fi 异常: %s", type(exc).__name__)
        return False


def get_credentials() -> Optional[Tuple[str, str]]:
    try:
        username = keyring.get_password(SERVICE, "username")
        password = keyring.get_password(SERVICE, "password")
    except Exception:
        return None
    return (username, password) if username and password else None


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
    keyring.set_password(SERVICE, "username", username)
    keyring.set_password(SERVICE, "password", password)
    print("凭据保存完毕（未尝试登录）。")
    print("运营商由 watch/once 的 --yys 参数指定，默认 mobile（移动）。")


def portal_login(session: requests.Session, account: str, password: str,
                 yys: str, port: int, log: logging.Logger) -> Tuple[bool, str]:
    """按浏览器真实流程登录：先取登录页建立会话，再 POST 表单。"""
    base = portal_base(port)
    try:
        session.get(base + LOGIN_JSP, timeout=(3, 6), allow_redirects=False)
    except requests.RequestException as exc:
        return False, "无法打开登录页: %s" % type(exc).__name__
    payload = {"sourceurl": "null", "account": account, "password": password,
               "yys": yys, "issave": ""}
    headers = {"Referer": base + LOGIN_JSP, "Origin": base,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    try:
        resp = session.post(base + LOGIN_ACTION, data=payload, headers=headers,
                            timeout=(3, 8), allow_redirects=True)
    except requests.RequestException as exc:
        return False, "登录请求失败: %s" % type(exc).__name__
    body = decode_body(resp)
    if ONLINE_MARKER in body and account in body:
        return True, "Portal 确认已在线（账号出现在状态页）"
    hints = sorted({h for h in FAILURE_HINTS if h in body})
    if hints:
        return False, "Portal 返回失败信息: " + ", ".join(hints)
    return False, "登录响应未能确认（HTTP %d）" % resp.status_code


def edge_fallback(log: logging.Logger) -> None:
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
        return
    profile = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CampusReconnectEdge"
    try:
        subprocess.Popen([exe, "--user-data-dir=%s" % profile,
                          "--no-proxy-server", PORTAL_ROOT],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("已打开 Edge（不走系统代理）的 Portal 登录页，供人工处理。")
    except OSError as exc:
        log.warning("Edge 启动失败: %s", type(exc).__name__)


def attempt_repair(session: requests.Session, args: argparse.Namespace,
                   log: logging.Logger) -> bool:
    ssid, state = wifi_state()
    if args.ssid and not ssid and args.reconnect_wifi:
        log.warning("当前未连接 Wi-Fi，尝试重连 %s ...", args.ssid)
        wifi_reconnect(args.ssid, log)
        for _ in range(6):
            time.sleep(5)
            ssid, state = wifi_state()
            if ssid:
                break
    if args.ssid and ssid and ssid.casefold() != args.ssid.casefold():
        log.info("当前 Wi-Fi 为 %r，非目标 %r，跳过认证。", ssid, args.ssid)
        return False
    if args.ssid and not ssid:
        log.info("未检测到已连接的 Wi-Fi，跳过认证。")
        return False
    port, port_source = resolve_port(session, args.port)
    log.info("Portal 认证端口: %d（%s）", port, port_source)
    if not portal_reachable(session, port):
        log.warning("Portal 不可达：可能是 Wi-Fi/网关故障，不发送账号密码。")
        return False
    creds = get_credentials()
    if creds is None:
        log.error("尚未保存凭据，请先执行: py campus_reconnect.py configure")
        return False
    account, password = creds
    account_to_send = "%s@%s" % (account, args.yys) if args.account_format == "isp" else account
    ok, detail = portal_login(session, account_to_send, password,
                              args.yys, port, log)
    log.info("登录结果: %s（%s）", "成功" if ok else "失败", detail)
    if not ok:
        if args.browser_fallback:
            edge_fallback(log)
        return False
    for delay in (3, 5, 8):
        time.sleep(delay)
        online, via = internet_ok(session)
        if online:
            log.info("网络恢复成功（%s）。Clash 系统代理全程未被动过。", via)
            return True
    log.warning("Portal 已确认在线，但公网探测仍未通过："
                "可能是探测地址受限、DNS 或运营商链路问题。")
    if args.browser_fallback:
        edge_fallback(log)
    return False


def status(session: requests.Session, args: argparse.Namespace) -> None:
    ssid, state = wifi_state()
    print("无线网卡状态 : %s（SSID: %s）" % (state, ssid or "未连接"))
    port, port_source = resolve_port(session, args.port)
    print("Portal 端口  : %d（%s）" % (port, port_source))
    print("Portal 可达  : %s" % ("是" if portal_reachable(session, port) else "否"))
    for url, expect in DEFAULT_PROBES:
        try:
            resp = session.get(url, timeout=(2.5, 4), allow_redirects=False)
            ok = resp.status_code == 200 and (expect is None or expect in resp.text)
            print("公网探测      : %s -> %s" % (url, "通过" if ok else "未通过"))
        except requests.RequestException as exc:
            print("公网探测      : %s -> 失败(%s)" % (url, type(exc).__name__))
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


_MUTEX_HANDLE = None


def ensure_single_instance(log: logging.Logger) -> bool:
    """watch 单实例保护：防止 run.bat 重复双击或与任务计划叠加运行。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, "SNNU_CampusReconnect_Watch_Mutex")
    if not handle:
        log.warning("无法创建单实例互斥体，继续运行。")
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        log.error("已有一个监控实例在运行，本次退出（互斥体保护）。")
        return False
    _MUTEX_HANDLE = handle  # 保持引用，避免 GC 关闭句柄导致互斥体失效
    return True


def dry_run(session: requests.Session, args: argparse.Namespace,
            log: logging.Logger) -> None:
    """预检：除提交认证外的全部步骤，不发送账号密码。"""
    ssid, state = wifi_state()
    print("SSID/状态    : %s / %s" % (ssid or "未连接", state))
    port, source = resolve_port(session, args.port)
    print("认证端口     : %d（%s）" % (port, source))
    print("登录页可达   : %s" % ("是" if portal_reachable(session, port) else "否"))
    creds = get_credentials()
    print("凭据已保存   : %s" % ("是" if creds else "否（先执行 configure）"))
    if creds:
        account, _ = creds
        sent = "%s@%s" % (account, args.yys) if args.account_format == "isp" else account
        payload = {"sourceurl": "null", "account": sent,
                   "password": "***已隐藏***", "yys": args.yys, "issave": ""}
        print("将提交的载荷 : %s" % payload)
    print("dry-run 完成：未发送任何认证请求。")


def watch(session: requests.Session, args: argparse.Namespace,
          log: logging.Logger) -> None:
    if not ensure_single_instance(log):
        return
    log.info("开始监测：目标 Wi-Fi=%r，yys=%s，间隔 %d 秒，连续 %d 次失败触发，冷却 %d 秒。",
             args.ssid, args.yys or "(默认校园网)", args.interval,
             args.threshold, args.cooldown)
    failures = 0
    next_attempt = 0.0
    while True:
        try:
            online, _ = internet_ok(session)
            if online or args.force:
                if failures and not args.force:
                    log.info("公网恢复。")
                if args.force:
                    attempt_repair(session, args, log)
                    return
                failures = 0
                next_attempt = 0.0
            else:
                failures += 1
                if failures < args.threshold:
                    log.info("公网探测失败（%d/%d）。", failures, args.threshold)
                elif failures == args.threshold:
                    log.warning("连续 %d 次失败，判定疑似掉线。", failures)
                if failures >= args.threshold and time.monotonic() >= next_attempt:
                    attempt_repair(session, args, log)
                    next_attempt = time.monotonic() + args.cooldown
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("用户终止。")
            return
        except Exception as exc:
            log.warning("监测异常: %s；下一轮继续。", type(exc).__name__)
            time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SNNU 校园网自动重连助手（Windows）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="把账号密码存入 Windows 凭据管理器")
    p_status = sub.add_parser("status", help="查看 Wi-Fi、Portal、公网与代理绕过状态")
    p_status.add_argument("--port", type=int, choices=KNOWN_PORTS, default=None,
                          help="默认动态识别端口；可强制指定已知端口")
    sub.add_parser("fix-bypass", help="把 Portal 地址写入系统代理绕过列表")
    for command in ("once", "watch"):
        p = sub.add_parser(command, help="单次尝试" if command == "once" else "常驻自动重连")
        p.add_argument("--ssid", default="SNNU", help="目标校园 Wi-Fi 名称，默认 SNNU")
        p.add_argument("--port", type=int, choices=KNOWN_PORTS, default=None,
                       help="Portal 认证端口；默认动态识别，失效时可强制指定")
        p.add_argument("--yys", default="mobile",
                       help="运营商: mobile(移动)/unicom(联通)/telecom(电信)/''(默认校园网)")
        p.add_argument("--account-format", choices=("plain", "isp"), default="plain",
                       help="账号提交格式: plain=学号(默认, 同浏览器表单); isp=学号@运营商")
        p.add_argument("--browser-fallback", action="store_true",
                       help="失败时打开不走系统代理的 Edge 登录页")
        p.add_argument("--reconnect-wifi", action="store_true",
                       help="Wi-Fi 未连接时先用 netsh 尝试重连")
        p.add_argument("--force", action="store_true",
                       help="在线也强制执行一次认证（仅 once，会提交真实凭据）")
        if command == "once":
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
    session = direct_session()
    if args.command == "status":
        status(session, args)
    elif args.command == "fix-bypass":
        fix_bypass(log)
    elif args.command == "once":
        if args.dry_run:
            dry_run(session, args, log)
            return
        online, _ = internet_ok(session)
        if args.force and online:
            log.warning("--force：当前网络正常，仍将提交一次真实认证（仅用于接口验证）。")
        elif online:
            log.info("当前网络正常，无需认证（预检用 --dry-run，强制测试用 --force）。")
            return
        if not attempt_repair(session, args, log):
            raise SystemExit(1)
    elif args.command == "watch":
        if args.interval < 5 or args.cooldown < 60 or args.threshold < 1:
            log.error("参数不合法: interval>=5, cooldown>=60, threshold>=1")
            raise SystemExit(2)
        watch(session, args, log)


if __name__ == "__main__":
    main()
