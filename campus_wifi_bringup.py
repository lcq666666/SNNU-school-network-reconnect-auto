# -*- coding: utf-8 -*-
"""V0.6 Block 2：Wi-Fi 自启链（纯逻辑层）。

解决 V0.6 实测问题（2）：有线坏了不自动转 Wi-Fi，Wi-Fi 关着也不会自动打开并连 SNNU。
旧 ``wifi_reconnect``（``campus_reconnect.py`` F17）只会 ``netsh wlan connect``，
调用前假定网卡已启用、射频已开、profile 已存在；本模块补齐这三步缺失的前置链。

设计约束：

- **纯逻辑、零副作用**：不调 ``subprocess``、不写注册表、不写文件、不做真实
  ``sleep``、不发真实网络请求。所有命令只以 :class:`BringupPlan` /
  :class:`BringupStep` 的形式产出，或经注入的 ``ops`` 执行；
- **复用已有解析，不重写**：接口状态直接用 ``campus_windows`` 的
  :func:`parse_netsh_interfaces` / :func:`select_wifi_interface`
  （F17，与 ``wifi_state_from_text`` 同源），本模块只在其上补管理状态与射频信息；
- **fail-closed**：计划不可生成（无接口 / SSID 为空 / 缺接口名却要做接口级操作）
  时返回 ``ok=False`` 的计划，:func:`run_bringup` 不执行任何命令；
- **不静默吞错**：任一步失败即停，:class:`BringupResult` 的 ``detail`` 写明命令与
  返回码；疑似权限不足时明确写“需要管理员权限”。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as _xml_escape

from campus_windows import (
    CONNECTED_STATES,
    parse_netsh_interfaces,
    select_wifi_interface,
)

__all__ = [
    "DEFAULT_SSID",
    "STEP_ENABLE_ADAPTER",
    "STEP_RADIO_ON",
    "STEP_ENSURE_PROFILE",
    "STEP_CONNECT",
    "STEP_WAIT_IP",
    "DEFAULT_FAILBACK_REQUIRED",
    "DEFAULT_FAILBACK_WINDOW_SECONDS",
    "WifiAdapterState",
    "BringupStep",
    "BringupPlan",
    "BringupResult",
    "enable_adapter_command",
    "radio_on_command",
    "add_profile_command",
    "connect_command",
    "build_snnu_open_profile_xml",
    "adapter_state_from_netsh_text",
    "plan",
    "run_bringup",
    "is_stable_for_failback",
]

#: 默认目标校园 Wi-Fi（与全仓库约定一致）。
DEFAULT_SSID = "SNNU"

#: 自启步骤种类（按执行顺序排列）。
STEP_ENABLE_ADAPTER = "enable_adapter"
STEP_RADIO_ON = "radio_on"
STEP_ENSURE_PROFILE = "ensure_profile"
STEP_CONNECT = "connect"
STEP_WAIT_IP = "wait_ip"

#: 切回有线稳定判据默认值（AGENTS §3.8）：连续成功 ≥5 次且持续 ≥120 秒。
DEFAULT_FAILBACK_REQUIRED = 5
DEFAULT_FAILBACK_WINDOW_SECONDS = 120.0

#: 疑似“需要管理员权限”的失败输出特征（中 / 英 netsh 常见措辞，不区分大小写）。
_ADMIN_HINTS = (
    "administrator",
    "elevation",
    "run as administrator",
    "requires elevation",
    "以管理员身份",
    "需要提升",
    "access is denied",
    "拒绝访问",
    "权限不足",
)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WifiAdapterState:
    """一次无线接口快照（plan 的输入）。

    ``admin_state`` 取 ``enabled`` / ``disabled``（来自
    ``netsh interface show interface`` 的“管理状态”）；
    ``state`` 取 ``connected`` / ``disconnected``（来自
    ``netsh wlan show interfaces`` 的“状态”，中英文皆可）；
    ``radio_on`` 为射频开关：``True`` 开 / ``False`` 关 / ``None`` 未知
    （``netsh wlan show interfaces`` 不报告射频，解析入口会填 ``None``）。
    """

    name: str = ""
    admin_state: str = "enabled"
    state: str = "disconnected"
    ssid: Optional[str] = None
    radio_on: Optional[bool] = None

    @property
    def is_disabled(self) -> bool:
        """网卡是否被禁用（管理状态为 disabled）。"""
        return self.admin_state.strip().lower() == "disabled"

    @property
    def connected(self) -> bool:
        """接口状态是否为已连接（中 / 英文皆认，与 F17 同源）。"""
        return self.state.strip().lower() in CONNECTED_STATES

    def is_connected_to(self, target: str) -> bool:
        """是否已连接目标 SSID（**完整**匹配，不做大小写折叠，与 F17 同源）。"""
        return bool(self.connected and self.ssid is not None
                    and self.ssid.strip() == (target or "").strip())


@dataclass(frozen=True)
class BringupStep:
    """自启链中的一步。

    ``command`` 是要执行的命令（参数列表形式，不拼 shell 字符串）；
    ``wait_ip`` 没有命令，走注入 ``ops.wait_ip()``；
    ``ensure_profile`` 的 ``profile_xml`` 是要导入的配置文件内容，
    调用方须先把它写入与 ``command`` 里 ``filename=`` 同名的文件再执行，
    本模块本身不写文件。
    """

    kind: str
    command: Tuple[str, ...] = ()
    profile_xml: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class BringupPlan:
    """自启计划：按顺序执行的步骤 + 生成原因。

    ``ok=False`` 表示计划不可生成（见 :func:`plan`），此时 ``steps`` 为空，
    :func:`run_bringup` 不会执行任何命令；已连接目标 SSID 时 ``steps`` 为空
    但 ``ok=True``，表示“无需自启”。
    """

    steps: Tuple[BringupStep, ...] = ()
    reason: str = ""
    ok: bool = True


@dataclass(frozen=True)
class BringupResult:
    """自启执行结果。

    ``steps_done`` 是已完成的步骤种类（按执行顺序）；``failed_step`` 是失败的
    步骤种类（成功时为 ``None``）；``detail`` 写明成败原因，失败时含命令与返回码。
    """

    ok: bool
    steps_done: Tuple[str, ...] = ()
    failed_step: Optional[str] = None
    detail: str = ""


# --------------------------------------------------------------------------- #
# 命令构造（纯字符串拼接，不执行）
# --------------------------------------------------------------------------- #
def enable_adapter_command(name: str) -> List[str]:
    """启用网卡的 netsh 命令。"""
    return ["netsh", "interface", "set", "interface", "name=%s" % name,
            "admin=enabled"]


def radio_on_command(name: str) -> List[str]:
    """打开射频自动配置的 netsh 命令（任务指定形式）。"""
    return ["netsh", "wlan", "set", "autoconfig", "enabled=yes",
            "interface=%s" % name]


def add_profile_command(ssid: str) -> List[str]:
    """导入配置文件的 netsh 命令；XML 内容见 :func:`build_snnu_open_profile_xml`。"""
    return ["netsh", "wlan", "add", "profile", "filename=%s.xml" % ssid]


def connect_command(ssid: str) -> List[str]:
    """连接目标网络的 netsh 命令（与 F17 ``wifi_reconnect`` 同形）。"""
    return ["netsh", "wlan", "connect", "name=%s" % ssid]


# --------------------------------------------------------------------------- #
# 开放式 SNNU 配置文件
# --------------------------------------------------------------------------- #
def build_snnu_open_profile_xml(ssid: str) -> str:
    """生成开放式（无加密）目标网络的 WLANProfile XML。

    SNNU 校园开放网络无密码：``authentication=open``、``encryption=none``；
    ``connectionType=ESS``、``connectionMode=auto``（开机/断线后自动连）。
    SSID 做 XML 转义；为空时抛 ``ValueError``，不生成非法配置文件。
    """
    name = (ssid or "").strip()
    if not name:
        raise ValueError("SSID 不能为空")
    escaped = _xml_escape(name)
    hex_name = name.encode("utf-8").hex().upper()
    return (
        "<?xml version=\"1.0\"?>\n"
        "<WLANProfile xmlns=\"http://www.microsoft.com/networking/WLAN/profile/v1\">\n"
        "    <name>%s</name>\n"
        "    <SSIDConfig>\n"
        "        <SSID>\n"
        "            <hex>%s</hex>\n"
        "            <name>%s</name>\n"
        "        </SSID>\n"
        "    </SSIDConfig>\n"
        "    <connectionType>ESS</connectionType>\n"
        "    <connectionMode>auto</connectionMode>\n"
        "    <MSM>\n"
        "        <security>\n"
        "            <authEncryption>\n"
        "                <authentication>open</authentication>\n"
        "                <encryption>none</encryption>\n"
        "                <useOneX>false</useOneX>\n"
        "            </authEncryption>\n"
        "        </security>\n"
        "    </MSM>\n"
        "</WLANProfile>\n" % (escaped, hex_name, escaped)
    )


# --------------------------------------------------------------------------- #
# netsh 文本 → 接口快照（复用 F17 解析）
# --------------------------------------------------------------------------- #
def adapter_state_from_netsh_text(
    text: str, ssid: str = "", interface: Optional[str] = None,
) -> Optional[WifiAdapterState]:
    """从 ``netsh wlan show interfaces`` 文本取接口快照（复用 F17 解析）。

    无任何无线接口时返回 ``None``（调用方应据此判“不支持自动认证”，不误报）。
    注意：该命令**不报告**管理状态与射频开关——列出的接口视为已启用
    （``admin_state="enabled"``），射频未知（``radio_on=None``）；
    要区分“网卡禁用 / 射频关闭”，调用方须用
    ``netsh interface show interface`` / 射频查询补齐后再调 :func:`plan`。
    """
    interfaces = parse_netsh_interfaces(text or "")
    if not interfaces:
        return None
    iface = select_wifi_interface(interfaces, ssid, interface)
    if iface is None:
        return None
    return WifiAdapterState(
        name=iface.name,
        admin_state="enabled",
        state=(iface.state or "").strip() or "disconnected",
        ssid=iface.ssid,
        radio_on=None,
    )


# --------------------------------------------------------------------------- #
# 计划：按顺序生成自启步骤
# --------------------------------------------------------------------------- #
def plan(state: Optional[WifiAdapterState], has_profile: bool,
         ssid: str = DEFAULT_SSID) -> BringupPlan:
    """按顺序生成 Wi-Fi 自启步骤。

    顺序：网卡禁用 → ``enable_adapter``；射频关闭 → ``radio_on``；
    无配置文件 → ``ensure_profile``；最后 ``connect`` + ``wait_ip``。
    已连接目标 SSID 时返回空步骤（``ok=True``，无需自启）。
    """
    target = (ssid or "").strip()
    if not target:
        return BringupPlan(steps=(), reason="SSID 为空，无法生成自启计划", ok=False)
    if state is None:
        return BringupPlan(steps=(), reason="未发现无线接口，无法生成自启计划",
                           ok=False)
    if state.is_connected_to(target):
        return BringupPlan(steps=(), reason="已连接 %s，无需自启" % target, ok=True)

    name = (state.name or "").strip()
    causes: List[str] = []
    steps: List[BringupStep] = []
    if state.is_disabled:
        if not name:
            return BringupPlan(steps=(), reason="无线网卡已禁用，但缺少接口名，无法启用",
                               ok=False)
        steps.append(BringupStep(
            kind=STEP_ENABLE_ADAPTER,
            command=tuple(enable_adapter_command(name)),
            detail="启用无线网卡 %s" % name))
        causes.append("网卡已禁用")
    if state.radio_on is False:
        if not name:
            return BringupPlan(steps=(), reason="射频已关闭，但缺少接口名，无法打开射频",
                               ok=False)
        steps.append(BringupStep(
            kind=STEP_RADIO_ON,
            command=tuple(radio_on_command(name)),
            detail="打开无线网卡 %s 的射频自动配置" % name))
        causes.append("射频已关闭")
    if not has_profile:
        steps.append(BringupStep(
            kind=STEP_ENSURE_PROFILE,
            command=tuple(add_profile_command(target)),
            profile_xml=build_snnu_open_profile_xml(target),
            detail="导入开放式 %s 配置文件（先写入 %s.xml 再执行）" % (target, target)))
        causes.append("缺少 %s 配置文件" % target)
    steps.append(BringupStep(
        kind=STEP_CONNECT, command=tuple(connect_command(target)),
        detail="连接 %s" % target))
    steps.append(BringupStep(
        kind=STEP_WAIT_IP, command=(), detail="等待获得可用 IP"))
    if causes:
        reason = "%s；连接 %s 并等待 IP 就绪" % ("；".join(causes), target)
    else:
        reason = "未连接 %s，连接并等待 IP 就绪" % target
    return BringupPlan(steps=tuple(steps), reason=reason, ok=True)


# --------------------------------------------------------------------------- #
# 执行：按计划顺序走注入的 ops，任一步失败即停
# --------------------------------------------------------------------------- #
def _looks_like_admin_failure(stdout: Optional[str]) -> bool:
    """失败输出是否像权限不足（中 / 英 netsh 常见措辞）。"""
    text = (stdout or "").lower()
    return any(hint in text for hint in _ADMIN_HINTS)


def _failure_detail(step: BringupStep, cmd: Sequence[str],
                    returncode: Optional[int], stdout: Optional[str]) -> str:
    """失败详情：步骤 + 命令 + 返回码，权限不足时明确写“需要管理员权限”。

    不静默吞错：输出原文（截断 200 字）也一并记下，供上层如实报告。
    """
    detail = "%s 失败（命令：%s，返回码：%s）" % (
        step.kind, " ".join(cmd), returncode)
    if _looks_like_admin_failure(stdout):
        detail += "：需要管理员权限，请以管理员身份运行后再试"
    output = (stdout or "").strip()
    if output:
        detail += "；输出：%s" % output[:200]
    return detail


def run_bringup(plan_: BringupPlan, ops: object) -> BringupResult:
    """按计划顺序执行自启链。

    ``ops`` 为注入对象，须提供 ``run(cmd: list) -> (returncode, stdout)`` 与
    ``wait_ip() -> bool``；本函数不执行任何真实命令。
    任一步 ``returncode != 0``（含 ``None``）或 ``wait_ip()`` 为 ``False`` 即停，
    返回 ``ok=False`` 及失败步骤；执行异常也转为失败结果记入 ``detail``，不吞错。
    """
    if not plan_.ok:
        return BringupResult(ok=False, steps_done=(), failed_step=None,
                             detail="自启计划不可执行：%s" % plan_.reason)
    done: List[str] = []
    for step in plan_.steps:
        if step.kind == STEP_WAIT_IP:
            try:
                ready = ops.wait_ip()  # type: ignore[attr-defined]
            except Exception as exc:  # 执行异常也如实记入结果，不静默吞掉
                return BringupResult(
                    ok=False, steps_done=tuple(done), failed_step=step.kind,
                    detail="wait_ip 执行异常：%s：%s" % (type(exc).__name__, exc))
            if not ready:
                return BringupResult(
                    ok=False, steps_done=tuple(done), failed_step=step.kind,
                    detail="wait_ip 失败：连接后未获得可用 IP")
            done.append(step.kind)
            continue
        cmd = list(step.command)
        try:
            returncode, stdout = ops.run(cmd)  # type: ignore[attr-defined]
        except Exception as exc:  # 执行异常也如实记入结果，不静默吞掉
            return BringupResult(
                ok=False, steps_done=tuple(done), failed_step=step.kind,
                detail="%s 执行异常：%s：%s（命令：%s）" % (
                    step.kind, type(exc).__name__, exc, " ".join(cmd)))
        if returncode != 0:
            return BringupResult(
                ok=False, steps_done=tuple(done), failed_step=step.kind,
                detail=_failure_detail(step, cmd, returncode, stdout))
        done.append(step.kind)
    if not done:
        return BringupResult(ok=True, steps_done=(), detail="无需执行任何步骤")
    return BringupResult(ok=True, steps_done=tuple(done), detail="Wi-Fi 自启完成")


# --------------------------------------------------------------------------- #
# 切回有线稳定判定（纯函数）
# --------------------------------------------------------------------------- #
def is_stable_for_failback(successes: int, required: int = DEFAULT_FAILBACK_REQUIRED,
                           window_seconds: float = DEFAULT_FAILBACK_WINDOW_SECONDS,
                           elapsed: float = 0.0) -> bool:
    """有线是否稳定到可以切回（AGENTS §3.8 滞回判据）。

    连续成功次数 ``successes`` ≥ ``required``（默认 5）**且**持续 ``elapsed`` 秒
    ≥ ``window_seconds``（默认 120）时才为 ``True``；任一条件不满足即 ``False``。
    非法输入（无法转成数字）返回 ``False``，不抛异常。
    """
    try:
        count = int(successes)
        need = int(required)
        window = float(window_seconds)
        span = float(elapsed)
    except (TypeError, ValueError):
        return False
    return count >= need and span >= window
