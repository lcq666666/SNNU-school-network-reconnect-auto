"""Offline contract tests: no operating-system sockets or credentials."""
import socket
import struct
from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import NewConnectionError

from urllib3 import poolmanager
from urllib3.connectionpool import HTTPConnectionPool

from campus_transport import (_ScopedHTTPConnection, _ScopedPool,
                              InterfaceTransport)


class Socket:
    def __init__(self, readback=7, fail=False):
        self.events = []
        self.readback = readback
        self.fail = fail
        self.closed = False
        self.reply = b""

    def setsockopt(self, *args):
        self.events.append(("set", *args))

    def getsockopt(self, *args):
        self.events.append(("get", *args))
        return self.readback

    def bind(self, address):
        self.events.append(("bind", address))

    def settimeout(self, value):
        self.events.append(("timeout", value))

    def connect(self, address):
        self.events.append(("connect", address))
        if self.fail:
            raise ConnectionRefusedError()

    def getsockname(self):
        return ("192.0.2.2", 1234)

    def close(self):
        self.closed = True

    def send(self, query):
        self.query = query
        return len(query)

    def recv(self, size):
        if callable(self.reply):
            return self.reply(self.query)
        return self.reply


def transport(sock=None, **kwargs):
    sock = sock or Socket()
    return InterfaceTransport(7, "192.0.2.2", ["192.0.2.53"],
                              platform="win32", socket_factory=lambda *a: sock,
                              **kwargs)


def answer(query):
    return (query[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
            + query[12:] + b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4)
            + socket.inet_aton("198.51.100.1"))


def test_socket_order_and_close():
    sock = Socket()
    assert transport(sock).tcp_connect("198.51.100.1", 443)
    assert sock.events[:3] == [
        ("set", socket.IPPROTO_IP, 31, struct.pack("!I", 7)),
        ("get", socket.IPPROTO_IP, 31),
        ("bind", ("192.0.2.2", 0)),
    ]
    assert sock.closed


@pytest.mark.parametrize("readback,fail", [(8, False), (7, True)])
def test_fail_closed(readback, fail):
    sock = Socket(readback, fail)
    assert not transport(sock).tcp_connect("198.51.100.1", 443)
    assert sock.closed
    if readback != 7:
        assert not any(e[0] in ("bind", "connect") for e in sock.events)


@pytest.mark.parametrize("kwargs", [dict(platform="linux"), dict(if_index=0),
    dict(if_index=True), dict(if_index=2**24), dict(source_ip="0.0.0.0"),
    dict(source_ip="::1"), dict(dns_servers=["example.com"]), dict(timeout=0)])
def test_invalid_configuration(kwargs):
    args = dict(if_index=7, source_ip="192.0.2.2", dns_servers=["192.0.2.53"],
                platform="win32")
    args.update(kwargs)
    with pytest.raises((ValueError, OSError)):
        InterfaceTransport(**args)


def test_dns_uses_bound_udp_not_system_resolver(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: pytest.fail("global DNS"))
    sock = Socket()
    sock.reply = answer
    assert transport(sock).resolve("example.com") == ["198.51.100.1"]
    assert ("connect", ("192.0.2.53", 53)) in sock.events
    assert sock.closed


@pytest.mark.parametrize("mutation", [
    lambda b: b"\x00\x00" + b[2:],
    lambda b: b[:2] + b"\x01\x80" + b[4:],  # QR absent
    lambda b: b[:2] + b"\x81\x83" + b[4:],  # NXDOMAIN
    lambda b: b[:2] + b"\x83\x80" + b[4:],  # truncated
    lambda b: b[:13] + b"X" + b[14:],         # wrong question
    lambda b: b[:-1],                          # malformed RDATA
    lambda b: b[:12] + b"\xc0\x0c" + b[14:], # compression loop
])
def test_dns_rejects_untrusted_answers(mutation):
    sock = Socket()
    sock.reply = lambda q: mutation(answer(q))
    assert transport(sock).resolve("example.com") == []
    assert sock.closed


def test_http_pool_preserves_hostname_and_tls(monkeypatch):
    sock = Socket()
    t = transport(sock)
    monkeypatch.setattr(t, "resolve", lambda host: ["198.51.100.1"])
    with requests.Session() as session:
        assert t.bind_session(session)
        assert not session.trust_env
        adapter = session.get_adapter("https://portal.example")
        pool = adapter.poolmanager.connection_from_url("https://portal.example")
        conn = pool._new_conn()
        assert conn.host == "portal.example"
        assert conn._new_conn() is sock
        assert ("connect", ("198.51.100.1", 443)) in sock.events
        conn.close()
        with pytest.raises(requests.exceptions.SSLError):
            adapter.send(requests.Request("GET", "https://portal.example").prepare(),
                         verify=False)
        with pytest.raises(requests.exceptions.ProxyError):
            adapter.send(requests.Request("GET", "http://portal.example").prepare(),
                         proxies={"http": "http://proxy.example"})


def test_https_connection_failure_has_no_fallback(monkeypatch):
    sock = Socket(readback=8)
    t = transport(sock)
    monkeypatch.setattr(t, "resolve", lambda host: ["198.51.100.1"])
    with requests.Session() as session:
        t.bind_session(session)
        conn = session.get_adapter("https://x").poolmanager.connection_from_url(
            "https://portal.example")._new_conn()
        with pytest.raises(NewConnectionError):
            conn._new_conn()
    assert sock.closed


def test_http_probe_closes_and_limits(monkeypatch):
    state = {}

    class Response:
        status_code = 302
        url = "http://portal.example/"
        encoding = "utf-8"
        raw = SimpleNamespace(read=lambda n, **kw: b"x" * n)

        def close(self):
            state["response_closed"] = True

    class Session:
        trust_env = True

        def mount(self, *a):
            pass

        def get(self, url, **kwargs):
            state.update(kwargs)
            state["trust_env"] = self.trust_env
            return Response()

        def close(self):
            state["session_closed"] = True

    monkeypatch.setattr(requests, "Session", Session)
    result = transport().http_probe("http://portal.example/")
    assert result.status_code == 302 and result.redirected
    assert len(result.body) == 512
    assert not state["allow_redirects"] and not state["trust_env"]
    assert state["response_closed"] and state["session_closed"]


def test_http_probe_error_closes_session(monkeypatch):
    with requests.Session() as session:
        monkeypatch.setattr(requests, "Session", lambda: session)
        closed = []
        monkeypatch.setattr(session, "get", lambda *a, **kw: (_ for _ in ()).throw(
            requests.ConnectionError("secret URL must not leak")))
        monkeypatch.setattr(session, "close", lambda: closed.append(True))
        result = transport().http_probe("http://portal.example/")
        assert result.error == "ConnectionError"
        assert closed


# --------------------------------------------------------------------------- #
# F75-11：接口绑定不得污染进程全局的 pool_classes_by_scheme
# --------------------------------------------------------------------------- #
#: 真机复现：``urllib3`` 的 ``pool_classes_by_scheme`` 是**模块级共享 dict**
#: （``PoolManager.__init__`` 直接把它挂到实例上），因此就地改它会波及同进程内
#: **所有**普通 ``HTTPAdapter``：它们随后建出的受限 pool 因缺少 transport 而抛
#: ``AttributeError: 'NoneType' object has no attribute '_connect_socket'``，
#: 被上层 ``except Exception`` 吞成"系统轴探测抛错 / 离线"，并制造 k 次假掉线。
def test_scoped_manager_does_not_pollute_global_pool_classes():
    before = dict(poolmanager.pool_classes_by_scheme)
    t = transport()
    with requests.Session() as session:
        assert t.bind_session(session)
        adapter = session.get_adapter("http://portal.example")
        # 该会话自己的管理器必须用受限 pool，且保留自身 transport
        assert adapter.poolmanager.pool_classes_by_scheme["http"] is _ScopedPool
        assert adapter.poolmanager.transport is t
    # 全局映射必须原样保留
    assert poolmanager.pool_classes_by_scheme == before
    assert poolmanager.pool_classes_by_scheme["http"] is HTTPConnectionPool


def test_plain_pool_manager_class_unaffected_after_binding():
    t = transport()
    with requests.Session() as scoped:
        assert t.bind_session(scoped)
    plain = poolmanager.PoolManager()
    assert plain.pool_classes_by_scheme["http"] is HTTPConnectionPool
    assert plain.pool_classes_by_scheme["https"] is poolmanager.HTTPSConnectionPool


def test_scoped_connection_without_transport_fails_closed():
    """没拿到 transport 的受限连接必须按 fail-closed 报连接错误，而非 AttributeError。"""
    conn = _ScopedHTTPConnection("portal.example")
    assert conn.transport is None
    with pytest.raises(NewConnectionError):
        conn._new_conn()
