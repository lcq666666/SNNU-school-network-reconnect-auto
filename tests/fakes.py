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
:class:`FakeCommandRunner`（V0.3 P0 只读诊断用的假命令执行器）。
"""
from __future__ import annotations

import types
from typing import Any, Dict, List, Optional, Tuple

import requests


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

    def route(self, url: str, response: Any, method: Optional[str] = None) -> "FakeSession":
        self.routes[(method, url)] = response
        return self

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
