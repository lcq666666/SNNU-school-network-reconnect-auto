# -*- coding: utf-8 -*-
"""离线测试用可注入假件（阶段 1）。

覆盖全部 Windows / 网络副作用点，保证默认测试集合：
- 不访问真实校园网 Portal；
- 不读取真实凭据（Windows 凭据管理器）；
- 不写系统注册表；
- 不启动真实浏览器；
- 不真实 ``sleep``。

包含：:class:`FakeRegistry`、:class:`FakeKeyring`、:class:`FakeResponse`、
:class:`FakeSession`、:class:`FakeLauncher`、:class:`VirtualClock`、
:class:`FakeCommandRunner`（V0.3 P0 只读诊断用的假命令执行器）、
:class:`FakeRemoteOps`（V0.3 P7 远控探测用的假能力集合）。
"""
from __future__ import annotations

import types
from typing import Any, Dict, List, Optional, Tuple

import requests
from types import SimpleNamespace


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
class FakeRegistryKey:
    """模拟 ``winreg`` 的句柄：记录访问权限，支持上下文管理器。"""

    def __init__(self, registry: "FakeRegistry", access: int, sub_key: str) -> None:
        self._reg = registry
        self.access = access
        self.sub_key = sub_key
        self.closed = False

    def __enter__(self) -> "FakeRegistryKey":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.closed = True
        return False


class FakeRegistry:
    """模拟 ``winreg`` 的最小接口。

    - 句柄按 ``access`` 位判定读写权限：缺少 ``KEY_QUERY_VALUE`` 时
      ``QueryValueEx`` 抛 ``PermissionError``（复现 F04 的权限缺陷）；
    - 可注入 ``deny_open`` / ``deny_query`` / ``deny_set`` 模拟拒绝；
    - ``set_failures`` 按 ``SetValueEx`` 调用顺序脚本化失败（``True`` 表示该次抛错），
      用于构造「写入中途失败 / 回滚失败」（F04/T09）；
    - 记录每次写入，便于断言“部分写入”。
    """

    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 0x0001
    KEY_SET_VALUE = 0x0002
    KEY_READ = 0x20019
    REG_SZ = 1
    REG_DWORD = 4

    def __init__(self, values: Optional[Dict[str, Any]] = None,
                 deny_open: bool = False, deny_query: bool = False,
                 deny_set: bool = False,
                 set_failures: Optional[List[bool]] = None) -> None:
        self.values: Dict[str, Any] = dict(values or {})
        self.deny_open = deny_open
        self.deny_query = deny_query
        self.deny_set = deny_set
        self.set_failures: List[bool] = list(set_failures or [])
        self.set_calls = 0
        self.open_access: List[int] = []
        self.writes: List[Tuple[str, Any]] = []
        self.deletes: List[str] = []

    # ---- winreg 接口 ----
    def OpenKey(self, hkey: Any, sub_key: str, reserved: int = 0,
                access: int = 0) -> FakeRegistryKey:
        if self.deny_open:
            raise PermissionError("OpenKey denied")
        self.open_access.append(access)
        return FakeRegistryKey(self, access, sub_key)

    def QueryValueEx(self, key: FakeRegistryKey, name: str) -> Tuple[Any, int]:
        allowed = (key.access in (0, self.KEY_READ)) or bool(key.access & self.KEY_QUERY_VALUE)
        if self.deny_query or not allowed:
            raise PermissionError("QueryValueEx denied")
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_DWORD

    def SetValueEx(self, key: FakeRegistryKey, name: str, reserved: int,
                   type_: int, value: Any) -> None:
        self.set_calls += 1
        if self.deny_set or not (key.access & self.KEY_SET_VALUE):
            raise PermissionError("SetValueEx denied")
        index = self.set_calls - 1
        if index < len(self.set_failures) and self.set_failures[index]:
            raise PermissionError("SetValueEx denied (scripted)")
        self.values[name] = value
        self.writes.append((name, value))

    def DeleteValue(self, key: FakeRegistryKey, name: str) -> None:
        if self.deny_set or not (key.access & self.KEY_SET_VALUE):
            raise PermissionError("DeleteValue denied")
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]
        self.deletes.append(name)

    # ---- 断言辅助 ----
    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)


# --------------------------------------------------------------------------- #
# 凭据后端
# --------------------------------------------------------------------------- #
class FakeKeyring:
    """模拟 ``keyring``：可注入读取异常与「某个键的首次写入失败」。

    ``fail_set_on`` 指定的键**第一次**写入时抛错（之后可正常写入），用于构造
    「先写 username 成功、再写 password 失败」以及随后的恢复路径（F11/T22）。
    """

    def __init__(self, values: Optional[Dict[Tuple[str, str], str]] = None,
                 fail_set_on: Optional[str] = None, raise_get: bool = False) -> None:
        self.values: Dict[Tuple[str, str], str] = dict(values or {})
        self.fail_set_on = fail_set_on
        self.raise_get = raise_get
        self.set_calls: List[Tuple[str, str, str]] = []
        self.delete_calls: List[Tuple[str, str]] = []
        self._failed_keys: set = set()

    def get_password(self, service: str, username: str) -> Optional[str]:
        if self.raise_get:
            raise RuntimeError("credential backend unavailable")
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_calls.append((service, username, password))
        if (self.fail_set_on and username == self.fail_set_on
                and username not in self._failed_keys):
            self._failed_keys.add(username)
            raise RuntimeError("write denied")
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.delete_calls.append((service, username))
        self.values.pop((service, username), None)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class FakeResponse:
    """最小响应对象，提供 ``content`` / ``text`` / ``status_code`` / ``headers``。"""

    def __init__(self, status_code: int = 200, body: Any = "",
                 headers: Optional[Dict[str, str]] = None, url: str = "",
                 encoding: str = "utf-8") -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.url = url
        self.encoding = encoding
        self._content = body.encode(encoding) if isinstance(body, str) else bytes(body)

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return self._content.decode(self.encoding, errors="replace")


class FakeSession:
    """按脚本返回响应的假 Session，不产生真实连接。

    路由键支持 ``(method, url)``、``url`` 或 ``(None, url)``；值为
    :class:`FakeResponse`、``Exception``（抛出）或可调用对象；也可传列表按序弹出。
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.routes: Dict[Any, Any] = {}
        #: ``mount(prefix, adapter)`` 调用记录（V0.3 P4：断言"会话已绑定接口"）。
        self.mounts: List[Tuple[str, Any]] = []

    def route(self, url: str, response: Any, method: Optional[str] = None) -> "FakeSession":
        self.routes[(method, url)] = response
        return self

    def mount(self, prefix: str, adapter: Any) -> None:
        """记录接口绑定（不产生真实连接）。"""
        self.mounts.append((prefix, adapter))

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._handle("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._handle("POST", url, kwargs)

    def _handle(self, method: str, url: str, kwargs: Dict[str, Any]) -> FakeResponse:
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        handler = self.routes.get((method, url))
        if handler is None:
            handler = self.routes.get((None, url))
        if handler is None:
            handler = self.routes.get(url)
        if handler is None:
            raise requests.ConnectionError("no fake route for %s %s" % (method, url))
        if isinstance(handler, list):
            handler = handler.pop(0)
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(method, url, kwargs)
        return handler

    # ---- 断言辅助 ----
    def posts(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]


# --------------------------------------------------------------------------- #
# 浏览器启动
# --------------------------------------------------------------------------- #
class FakeLauncher:
    """记录浏览器启动调用，不真实启动进程。"""

    def __init__(self, which_result: Optional[str] = "C:/fake/msedge.exe",
                 popen_fail: bool = False) -> None:
        self.which_result = which_result
        self.popen_fail = popen_fail
        self.popen_calls: List[Tuple[Any, Dict[str, Any]]] = []

    def which(self, name: str) -> Optional[str]:
        return self.which_result

    def Popen(self, args: Any, **kwargs: Any) -> Any:
        self.popen_calls.append((args, kwargs))
        if self.popen_fail:
            raise OSError("launch failed")
        return object()

    def install(self, monkeypatch: Any, module: Any) -> "FakeLauncher":
        """把假件挂到 ``module`` 的 ``shutil`` / ``subprocess`` 上。"""
        monkeypatch.setattr(module.shutil, "which", self.which)
        monkeypatch.setattr(module.subprocess, "Popen", self.Popen)
        return self


# --------------------------------------------------------------------------- #
# 虚拟时钟
# --------------------------------------------------------------------------- #
class VirtualClock:
    """可推进的单调时钟，替代 ``time.monotonic`` / ``time.sleep``。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def install(self, monkeypatch: Any, module: Any) -> "VirtualClock":
        """把 ``module.time`` 替换为只暴露 monotonic/sleep/time 的假模块。"""
        fake = types.SimpleNamespace(monotonic=self.monotonic, sleep=self.sleep,
                                     time=self.time)
        monkeypatch.setattr(module, "time", fake)
        return self


# --------------------------------------------------------------------------- #
# 只读命令执行（V0.3 P0 netdiag）
# --------------------------------------------------------------------------- #
class FakeCommandRunner:
    """按诊断项名称返回预置输出的假命令执行器，零真实子进程。

    ``responses`` 键为诊断项名称（由被测工具定义），值可为：

    - ``str``：视为 ``rc=0`` 的 stdout（PowerShell ConvertTo-Json 文本等）；
    - ``(rc, stdout, stderr)`` 元组：完整结局；
    - ``Exception``：调用时抛出（构造"命令执行器本身故障"）；
    - ``list``：同一诊断项多次调用时按序弹出（空列表表示未预期的额外调用）。

    未提供且未设 ``default`` 的诊断项会抛出 :class:`KeyError`，
    迫使测试显式覆盖全部诊断项（防止漏桩导致假绿）。

    记录 ``commands``（``(name, script)`` 全量调用记录），
    供 I-0-01 断言"全部命令只读、无任何写操作"。
    """

    def __init__(self, responses: Optional[Dict[str, Any]] = None,
                 default: Any = None) -> None:
        self.responses: Dict[str, Any] = dict(responses or {})
        self.default = default
        self.commands: List[Tuple[str, str]] = []
        self.calls: List[str] = []
        self._pop: Dict[str, List[Any]] = {}

    def run(self, name: str, script: str) -> Tuple[int, str, str]:
        self.commands.append((name, script))
        self.calls.append(name)
        value: Any
        if name in self._pop:
            queue = self._pop[name]
            if not queue:
                raise AssertionError("unexpected extra call: %r" % name)
            value = queue.pop(0)
        elif name in self.responses:
            scripted = self.responses[name]
            if isinstance(scripted, list):
                self._pop[name] = list(scripted)
                if not scripted:
                    raise AssertionError("empty response list for %r" % name)
                value = scripted[0]
                if isinstance(value, Exception):
                    raise value
            else:
                value = scripted
        elif self.default is not None:
            value = self.default
        else:
            raise KeyError("no fake response for command %r" % name)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return int(value[0]), str(value[1]), str(value[2])
        if isinstance(value, int):
            return value, "", ""
        return 0, str(value), ""


# --------------------------------------------------------------------------- #
# 网络探测引擎假件（V0.3 P1 campus_network 用）
# --------------------------------------------------------------------------- #
class FakeProbeEngine:
    """可编排的假探测引擎：TCP / HTTP / DNS / 路由查询，零真实网络。

    键语义（``*_`` 属性为可脚本化响应表，均支持 ``"default"`` 通配，
    http/tcp 另支持 ``(key, source_address)`` 元组键——用于"同一 URL 经
    不同源地址得到不同响应"的场景，如 Portal 只劫持 Wi-Fi 路径）：

    - ``tcp[(host, port)]``：``bool``（True=主机可达，含"连接被拒绝"）；
      或 ``Exception``（调用时抛出）；
    - ``http[url]``：``dict``（``status_code``/``redirected``/``final_url``/
      ``body``/``error``）或 :class:`types.SimpleNamespace`；
    - ``dns[name]``：``bool`` 或 ``Exception``；
    - ``routes[target]``：带 ``if_index`` 属性的对象、dict，或 ``None``
      （无法确认出口）。

    全部调用记录在 ``calls``（``tcp``/``http``/``dns``/``route``），供断言
    "确实按源地址绑定探测"与"探测次数/顺序"。
    """

    def __init__(self) -> None:
        self.tcp: Dict[Any, Any] = {}
        self.http: Dict[Any, Any] = {}
        self.dns: Dict[Any, Any] = {}
        self.routes: Dict[Any, Any] = {}
        self.proof = {"default": True}
        self.calls: Dict[str, List[Any]] = {"tcp": [], "http": [], "dns": [],
                                            "route": []}

    def _resolve(self, table: Dict[Any, Any], key: Any,
                 source: Any = None) -> Any:
        if source is not None and (key, source) in table:
            return table[(key, source)]
        if key in table:
            return table[key]
        return table.get("default")

    def tcp_connect(self, host: str, port: int, timeout: Any = None,
                    source_address: Any = None) -> bool:
        self.calls["tcp"].append((host, port, source_address))
        value = self._resolve(self.tcp, (host, port), source_address)
        if isinstance(value, Exception):
            raise value
        return bool(value)

    def http_probe(self, url: str, timeout: Any = None,
                   source_address: Any = None) -> Any:
        self.calls["http"].append((url, source_address))
        value = self._resolve(self.http, url, source_address)
        if isinstance(value, Exception):
            raise value
        if value is None:
            value = {}
        if isinstance(value, SimpleNamespace):
            return value
        if isinstance(value, dict):
            return SimpleNamespace(**value)
        return SimpleNamespace(body=str(value))

    def dns_resolve(self, name: str, source_address: Any = None) -> bool:
        self.calls["dns"].append((name, source_address))
        value = self._resolve(self.dns, name, source_address)
        if isinstance(value, Exception):
            raise value
        return bool(value)

    def route_lookup(self, target: str) -> Any:
        self.calls["route"].append(target)
        value = self._resolve(self.routes, target)
        if isinstance(value, Exception):
            raise value
        return value

    def for_interface(self, if_index, source_ip, dns_servers=()):
        source = (source_ip, 0)
        return SimpleNamespace(
            interface_bound=self.proof.get(if_index, self.proof.get("default")),
            tcp_connect=lambda host, port, timeout=None: self.tcp_connect(
                host, port, timeout, source),
            http_probe=lambda url, timeout=None: self.http_probe(url, timeout, source),
            dns_resolve=lambda name: self.dns_resolve(name, source))


def matching_http_response(spec, *, status_code=200, redirected=False,
                           final_url=None, error=""):
    """按 ``ProbeSpec`` 生成**能通过** ``probe_matches`` 的假响应 dict。

    夹具过去靠"写死某个正文恰好命中某个探测点"（如 ``body="Microsoft Connect Test"``），
    探测点表一变就静默失效（P8/K-5 实测踩坑）。改为从 ``spec`` 推导期望正文，夹具与真实
    探测点表**同源**：目标表变更时夹具自动跟随，不会悄悄退化成"永远是失败探测"。
    """
    from campus_core import PROBE_HTML_TITLE

    if spec.mode == PROBE_HTML_TITLE:
        body = "<HTML><HEAD><TITLE>%s</TITLE></HEAD><BODY>%s</BODY></HTML>" % (
            spec.expect, spec.expect)
    else:
        body = spec.expect
    return {"status_code": status_code, "redirected": redirected,
            "final_url": spec.url if final_url is None else final_url,
            "body": body, "error": error}


def healthy_http_table(specs=None) -> Dict[Any, Any]:
    """生成"分轴 HTTP 探测点**全部命中**"的假响应表。

    ``specs`` 缺省取 ``campus_network.DEFAULT_HTTP_TARGETS``（真实目标表）。
    """
    if specs is None:
        from campus_network import DEFAULT_HTTP_TARGETS

        specs = DEFAULT_HTTP_TARGETS
    return {spec.url: matching_http_response(spec) for spec in specs}


# --------------------------------------------------------------------------- #
# 远控探测能力假件（V0.3 P7 campus_remote 用）
# --------------------------------------------------------------------------- #
class FakeRemoteOps:
    """可编排的假远控探测能力（V0.3 P7），**零真实**子进程 / 注册表 / 凭据。

    - ``processes``：``[{"pid", "process_name", "path"}]``（与
      ``netdiag.parse_remote_processes`` 同构）；
    - ``ports``：``[{"state", "remote_address", "local_port", ...}]``；
    - ``udp`` / ``sleep`` / ``auto_logon`` / ``run_entries``：就绪自检输入；
    - ``credentials``：凭据存在性（可为 ``Exception`` 或按序弹出的 list）；
    - ``probe_error``：注入探测异常；``probe_calls`` / ``readiness_calls`` 记录调用。

    ``probe()`` 只返回进程/端口/UDP；``probe_readiness()`` 额外带上睡眠/登录/
    自启数据（与真实 ``build_windows_remote_ops`` 的两级探测一致）。
    """

    def __init__(self, *, processes=None, ports=None, udp=None, sleep=None,
                 auto_logon=None, run_entries=None, credentials=True,
                 probe_error=None) -> None:
        self.processes = [dict(row) for row in (processes or [])]
        self.ports = [dict(row) for row in (ports or [])]
        self.udp = [dict(row) for row in (udp or [])]
        self.sleep = dict(sleep or {})
        self.auto_logon = dict(auto_logon or {})
        self.run_entries = [dict(row) for row in (run_entries or [])]
        self.credentials = credentials
        self.probe_error = probe_error
        self.probe_calls: List[int] = []
        self.readiness_calls: List[int] = []

    def _base(self):
        from campus_remote import RemoteProbe

        if self.probe_error is not None:
            raise self.probe_error
        return RemoteProbe(clients=[dict(row) for row in self.processes],
                           ports=[dict(row) for row in self.ports],
                           udp_endpoints=[dict(row) for row in self.udp])

    def probe(self):
        self.probe_calls.append(1)
        return self._base()

    def probe_readiness(self):

        self.readiness_calls.append(1)
        if self.probe_error is not None:
            raise self.probe_error
        probe = self._base()
        probe.sleep_states = dict(self.sleep)
        probe.auto_logon = dict(self.auto_logon)
        probe.run_entries = [dict(row) for row in self.run_entries]
        return probe

    def credentials_present(self) -> bool:
        value = self.credentials
        if isinstance(value, list):
            value = value.pop(0) if value else True
        if isinstance(value, Exception):
            raise value
        return bool(value)


# --------------------------------------------------------------------------- #
# 副作用执行器假件（V0.3 P3 campus_failover 用）
# --------------------------------------------------------------------------- #
class FakeOpExecutor:
    """记录动作请求的假执行器，**零真实副作用**（V0.3 P3）。

    只做两件事：记录每个 :class:`campus_failover.OpRequest`，并按脚本返回结局。
    不切路由、不写 Metric、不发认证、不触碰 Clash、不重连 Wi-Fi。

    ``results`` 键可为 ``OpKind`` 成员或其 ``value`` 字符串；值语义与
    :meth:`campus_failover.NetworkOpScheduler._invoke` 一致：

    - ``True`` / ``None``：执行成功；
    - ``False``：执行失败（``attempted=True``，``executed=False``）；
    - ``str``：执行成功并携带说明；
    - ``Exception``：抛出（用于验证调度器捕获异常后转为失败结局）；
    - ``list``：同一动作多次提交时按序弹出。

    未命中且未设 ``default`` 时按成功处理。``requests`` 记录全部请求，
    ``calls`` 记录动作类型序列，供断言"互斥串行、一轮一类"。
    """

    def __init__(self, results: Optional[Dict[Any, Any]] = None,
                 default: Any = True) -> None:
        self.results: Dict[Any, Any] = dict(results or {})
        self.default = default
        self.requests: List[Any] = []
        self.calls: List[Any] = []

    def execute(self, request: Any) -> Any:
        self.requests.append(request)
        self.calls.append(request.kind)
        key = getattr(request.kind, "value", request.kind)
        if request.kind in self.results:
            value = self.results[request.kind]
        elif key in self.results:
            value = self.results[key]
        else:
            value = self.default
        if isinstance(value, list):
            value = value.pop(0) if value else self.default
        if isinstance(value, Exception):
            raise value
        return value


# --------------------------------------------------------------------------- #
# SNNU 无线链路假件（V0.3 P4 campus_snnu 用）
# --------------------------------------------------------------------------- #
class FakeSnnuLink:
    """假 SNNU 关联/IP 观察（V0.3 P4），零真实 ``netsh`` / ``ipconfig``。

    以 ``readings`` 序列（``(ssid, 状态文本, has_ipv4)``）驱动：每次
    :meth:`wifi_state` **消费**一条（并推进指针，尾部自动保持最后一条），
    :meth:`ip_ready` 返回同一条 reading 的 ``has_ipv4``（与 ``wifi_state`` 配对）。
    因此"观察一次 = 消费一条 reading"，可精确编排"重连前/后""等待 IP 前后"。

    - :meth:`wifi_reconnect`：记录调用并按 ``reconnect_result`` 返回（可为 list
      按序弹出）；
    - :meth:`sleep`：只记录时长（零真实 sleep），供 ``SnnuBackupLink`` 的有界等待；
    - ``wifi_calls`` / ``reconnect_calls`` / ``sleeps`` 供断言。
    """

    def __init__(self, readings: Optional[List[Tuple[Any, str, bool]]] = None,
                 reconnect_result: Any = True) -> None:
        self.readings: List[Tuple[Any, str, bool]] = list(
            readings or [("SNNU", "connected", True)])
        self.reconnect_result = reconnect_result
        self.wifi_calls: List[Any] = []
        self.reconnect_calls: List[str] = []
        self.sleeps: List[float] = []
        self._index = 0
        self._last: Optional[Tuple[Any, str, bool]] = None

    def _current(self) -> Tuple[Any, str, bool]:
        if not self.readings:
            return (None, "unknown", False)
        return self.readings[min(self._index, len(self.readings) - 1)]

    def wifi_state(self, interface: Any = None) -> Tuple[Any, str]:
        reading = self._current()
        self._index += 1
        self._last = reading
        self.wifi_calls.append(reading)
        return reading[0], reading[1]

    def ip_ready(self) -> bool:
        reading = self._last if self._last is not None else self._current()
        return bool(reading[2])

    def wifi_reconnect(self, ssid: str, *args: Any, **kwargs: Any) -> bool:
        self.reconnect_calls.append(ssid)
        value = self.reconnect_result
        if isinstance(value, list):
            value = value.pop(0) if value else False
        if isinstance(value, Exception):
            raise value
        return bool(value)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))

    def advance(self, steps: int = 1) -> None:
        self._index += int(steps)


# --------------------------------------------------------------------------- #
# 路由能力假件（V0.3 P5 campus_route_guard 用）
# --------------------------------------------------------------------------- #
class FakeRouteOps:
    """可编排的假路由能力集合（V0.3 P5），**零真实** netsh / PowerShell / 路由写入。

    与 :class:`campus_route_guard.RouteOps` 同构，模拟一个"按 InterfaceMetric 选出口"
    的简化 Windows 行为：

    - ``metrics``：``{if_index: dict}``，键为 if_index，值为
      ``alias`` / ``ipv4_metric`` / ``ipv6_metric`` / ``ipv4_automatic`` /
      ``ipv6_automatic``；``set_metric`` / ``restore_automatic`` 会就地更新它，
      因此"读到的状态"随写入真实变化（可断言快照/回滚）。
    - ``egress``：``{target: if_index|None}``，键为路由查询目标地址
      （``8.8.8.8`` / ``2606:4700:4700::1111``），值为该目标当前实际出口。
    - ``apply_egress=True``（默认）：每次写入后按"最小 Metric 优先"重算出口，
      模拟 Windows 依 Metric 选择出口；设 ``False`` 可构造"写入成功但出口未改变"。
    - ``set_ok`` / ``restore_ok``：写入是否成功（可为 ``list`` 按序弹出或 ``Exception``）。

    记录 ``writes``（``(if_index, family, metric)``）、``restores``、``reads``，
    供断言"最小变更""写入顺序""回滚内容"。
    """

    def __init__(self, metrics: Optional[Dict[int, Dict[str, Any]]] = None,
                 egress: Optional[Dict[str, Optional[int]]] = None,
                 default_routes: Optional[List[Dict[str, Any]]] = None,
                 set_ok: Any = True, restore_ok: Any = True,
                 apply_egress: bool = True) -> None:
        self.metrics: Dict[int, Dict[str, Any]] = {
            int(k): dict(v) for k, v in (metrics or {}).items()}
        self.egress: Dict[str, Optional[int]] = dict(egress or {})
        self.default_routes: List[Dict[str, Any]] = list(default_routes or [])
        self.set_ok = set_ok
        self.restore_ok = restore_ok
        self.apply_egress = apply_egress
        self.writes: List[Tuple[int, str, int]] = []
        self.restores: List[Tuple[int, str]] = []
        self.reads: int = 0

    # ---- 只读 ---- #
    def read_metrics(self) -> Dict[int, Any]:
        from campus_route_guard import MetricState

        self.reads += 1
        return {if_index: MetricState(if_index=if_index,
                                      alias=str(row.get("alias") or ""),
                                      ipv4_metric=row.get("ipv4_metric"),
                                      ipv6_metric=row.get("ipv6_metric"),
                                      ipv4_automatic=row.get("ipv4_automatic"),
                                      ipv6_automatic=row.get("ipv6_automatic"))
                for if_index, row in self.metrics.items()}

    def read_default_routes(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.default_routes]

    def read_primary_egress(self) -> Optional[int]:
        return self.egress.get("8.8.8.8")

    def egress_check(self, target: str) -> Any:
        from campus_route_guard import EgressCheck

        if target not in self.egress:
            return EgressCheck("", target, error="假件未提供该目标的出口")
        return EgressCheck("", target, self.egress[target])

    # ---- 写入 ---- #
    def _result(self, scripted: Any) -> bool:
        value = scripted
        if isinstance(value, list):
            value = value.pop(0) if value else True
        if isinstance(value, Exception):
            raise value
        return bool(value)

    def set_metric(self, if_index: int, family: str, metric: int) -> bool:
        ok = self._result(self.set_ok)
        if not ok:
            return False
        self.writes.append((int(if_index), str(family), int(metric)))
        row = self.metrics.setdefault(int(if_index), {})
        row["%s_metric" % family.lower()] = int(metric)
        row["%s_automatic" % family.lower()] = "Disabled"
        if self.apply_egress:
            self._recompute_egress()
        return True

    def restore_automatic(self, if_index: int, family: str) -> bool:
        ok = self._result(self.restore_ok)
        if not ok:
            return False
        self.restores.append((int(if_index), str(family)))
        row = self.metrics.setdefault(int(if_index), {})
        row["%s_automatic" % family.lower()] = "Enabled"
        if self.apply_egress:
            self._recompute_egress()
        return True

    def _recompute_egress(self) -> None:
        """按"最小 IPv4/IPv6 Metric 优先"重算两个目标地址的出口。"""
        for family, target in (("ipv4", "8.8.8.8"),
                               ("ipv6", "2606:4700:4700::1111")):
            if target not in self.egress:
                continue
            key = "%s_metric" % family
            candidates = [(row.get(key), if_index)
                          for if_index, row in self.metrics.items()
                          if row.get(key) is not None]
            if candidates:
                self.egress[target] = min(candidates)[1]


class FakeMetricWriter:
    """假 Metric 写入器（V0.3 P5）：记录写入脚本，**零真实** PowerShell。

    每次写入先经 :func:`campus_route_guard.assert_write_allowed` 自检
    （未通过即抛错），从而永久看守"路由卫士只会发出白名单内的写入命令"。
    """

    def __init__(self, set_ok: Any = True, restore_ok: Any = True) -> None:
        self.set_ok = set_ok
        self.restore_ok = restore_ok
        self.scripts: List[str] = []
        self.calls: List[Tuple[str, int, str]] = []

    def _record(self, script: str) -> None:
        from campus_route_guard import assert_write_allowed

        assert_write_allowed(script)
        self.scripts.append(script)

    def _result(self, scripted: Any) -> bool:
        value = scripted
        if isinstance(value, list):
            value = value.pop(0) if value else True
        if isinstance(value, Exception):
            raise value
        return bool(value)

    def set_metric(self, if_index: int, family: str, metric: int) -> bool:
        from campus_route_guard import WindowsMetricWriter

        self._record(WindowsMetricWriter.SET_METRIC.format(
            if_index=int(if_index), family=str(family), metric=int(metric)))
        self.calls.append(("set_metric", int(if_index), str(family)))
        return self._result(self.set_ok)

    def restore_automatic(self, if_index: int, family: str) -> bool:
        from campus_route_guard import WindowsMetricWriter

        self._record(WindowsMetricWriter.RESTORE_AUTOMATIC.format(
            if_index=int(if_index), family=str(family)))
        self.calls.append(("restore_automatic", int(if_index), str(family)))
        return self._result(self.restore_ok)



class FakeInterfaceBinder:
    """显式模拟接口约束成功/失败；不创建 socket。"""

    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    def __call__(self, session, binding):
        self.calls.append(binding)
        if not self.allowed or not binding.ok:
            return False
        session.trust_env = False
        session.mount("http://", object())
        session.mount("https://", object())
        return True


# --------------------------------------------------------------------------- #
# UU 远程（GameViewer）用户态清退/重启能力假件（V0.4-2/3）
# --------------------------------------------------------------------------- #
class FakeRemoteAppOps:
    """可编排的假「UU 远程后台清退/重启」能力集合，**零真实进程操作**。

    - ``processes``：``[{"pid", "process_name", "path"}]``（与
      ``campus_remote_app.AppProcess`` / netdiag 解析同构）；
    - ``service`` / ``run_entries``：服务状态与自启项（只读发现用）；
    - ``kill_results`` / ``launch_results``：``bool``、``Exception`` 或按序弹出的
      ``list``；``False`` 表示该次操作**失败但未抛错**；
    - ``probe_error``：让 ``list_processes`` 抛错（构造"探测源故障"）；
    - 记录 ``killed``（pid 列表）、``launched``（命令列表）、各调用计数，
      供断言"只读发现不产生副作用""清退顺序""失败即中止"。
    """

    def __init__(self, processes=None, service=None, run_entries=None,
                 kill_results=True, launch_results=True, probe_error=None,
                 service_error=None, launch_spawns=True, spawn_pid=9001,
                 spawn_name="GameViewer.exe") -> None:
        # 内部存储刻意加下划线：属性名不得与方法名 ``run_entries`` 等冲突
        # （``campus_remote_app.discover`` 按 ``ops.run_entries()`` 调用）。
        self._processes = [dict(row) for row in (processes or [])]
        self._service = dict(service or {})
        self._run_entries = [dict(row) for row in (run_entries or [])]
        self.kill_results = kill_results
        self.launch_results = launch_results
        self.probe_error = probe_error
        self.service_error = service_error
        #: 启动命令成功执行后是否**真的**出现新进程（真机实测：stub 立刻退出、
        #: 主程序另起，故"命令成功"≠"起来了"；``False`` 用来构造假成功）。
        self.launch_spawns = launch_spawns
        self.spawn_pid = int(spawn_pid)
        self.spawn_name = spawn_name
        self.killed: List[int] = []
        self.launched: List[str] = []
        self.list_calls = 0
        self.service_calls = 0
        self.run_calls = 0

    def _result(self, scripted, *, pid=None):
        value = scripted
        if isinstance(value, list):
            value = value.pop(0) if value else True
        if isinstance(value, Exception):
            raise value
        return bool(value)

    # ---- 只读 ----
    def list_processes(self) -> List[Dict[str, Any]]:
        self.list_calls += 1
        if self.probe_error is not None:
            raise self.probe_error
        return [dict(row) for row in self._processes]

    def service_state(self) -> Dict[str, Any]:
        self.service_calls += 1
        if self.service_error is not None:
            raise self.service_error
        return dict(self._service)

    def run_entries(self) -> List[Dict[str, Any]]:
        self.run_calls += 1
        return [dict(row) for row in self._run_entries]

    # ---- 有副作用 ----
    def kill(self, pid: int) -> bool:
        self.killed.append(int(pid))
        success = self._result(self.kill_results, pid=pid)
        if success:                       # 真实语义：清退成功后该进程不再出现
            self._processes = [row for row in self._processes
                               if int(row.get("pid", -1)) != int(pid)]
        return success

    def launch(self, command: str) -> bool:
        self.launched.append(command)
        success = self._result(self.launch_results)
        if success and self.launch_spawns:   # 命令成功且**真的**起来了
            self._processes = [{"pid": self.spawn_pid,
                                "process_name": self.spawn_name,
                                "path": r"C:\_study\GameViewer\bin\%s"
                                        % self.spawn_name}]
        return success

