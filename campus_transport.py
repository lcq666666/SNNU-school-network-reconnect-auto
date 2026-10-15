# -*- coding: utf-8 -*-
"""Fail-closed Windows interface-scoped transport for P7.5-2."""
from __future__ import annotations

import ipaddress
import random
import socket
import struct
from types import SimpleNamespace
from typing import Iterable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.poolmanager import PoolManager

IP_UNICAST_IF = 31


def _qname(name: str) -> bytes:
    labels = str(name).rstrip(".").split(".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise ValueError("invalid DNS name")
    return b"".join(bytes([len(label)]) + label.encode("idna") for label in labels) + b"\0"


class _ScopedHTTPConnection(HTTPConnection):
    def __init__(self, *args, **kwargs):
        self.transport = kwargs.pop("transport", None)
        for key in ("cert_reqs", "ca_certs", "ca_cert_dir", "ca_cert_data",
                    "ssl_context", "server_hostname"):
            kwargs.pop(key, None)
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        from urllib3.exceptions import NewConnectionError
        transport = self.transport
        if transport is None:
            raise NewConnectionError(
                self, "interface-scoped connection without transport (fail-closed)")
        try:
            return transport._connect_socket(self.host, self.port)
        except OSError as exc:
            raise NewConnectionError(self, str(exc)) from exc


class _ScopedHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        self.transport = kwargs.pop("transport", None)
        # urllib3 performs certificate validation in connect; do not pass pool-only keywords.
        for key in ("cert_reqs", "ca_certs", "ca_cert_dir", "ca_cert_data",
                    "ssl_context", "server_hostname"):
            kwargs.pop(key, None)
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        from urllib3.exceptions import NewConnectionError
        transport = self.transport
        if transport is None:
            raise NewConnectionError(
                self, "interface-scoped connection without transport (fail-closed)")
        try:
            return transport._connect_socket(self.host, self.port)
        except OSError as exc:
            raise NewConnectionError(self, str(exc)) from exc
    def connect(self):
        import ssl
        try:
            return super().connect()
        except NotImplementedError as exc:
            raise ssl.SSLError(str(exc)) from exc

class _ScopedPool(HTTPConnectionPool):
    ConnectionCls = _ScopedHTTPConnection

    def __init__(self, host, port=None, transport=None, **kwargs):
        self.transport = transport
        super().__init__(host, port=port, **kwargs)
    def _new_conn(self):
        conn_kw = dict(self.conn_kw)
        conn_kw["transport"] = self.transport
        return self.ConnectionCls(host=self.host, port=self.port, **conn_kw)


class _ScopedHTTPSPool(_ScopedPool):
    ConnectionCls = _ScopedHTTPSConnection


class _ScopedPoolManager(PoolManager):
    def __init__(self, transport, *args, **kwargs):
        self.transport = transport
        super().__init__(*args, **kwargs)
        # F75-11：urllib3 的 ``PoolManager.__init__`` 把**模块级共享 dict**
        # ``pool_classes_by_scheme`` 直接挂到实例上；就地修改它会污染同进程内
        # 所有普通 ``HTTPAdapter``（它们随后建出的受限 pool 因缺 transport 而抛
        # AttributeError）。这里改为给本实例一份独立副本。
        self.pool_classes_by_scheme = dict(self.pool_classes_by_scheme)
        self.pool_classes_by_scheme["http"] = _ScopedPool
        self.pool_classes_by_scheme["https"] = _ScopedHTTPSPool

    def _new_pool(self, scheme, host, port, request_context=None):
        context = dict(request_context or {})
        context.pop("scheme", None)
        context.pop("host", None)
        context.pop("port", None)
        context.pop("transport", None)
        pool_cls = self.pool_classes_by_scheme[scheme]
        return pool_cls(host, port=port, transport=self.transport, **context)

class _ScopedAdapter(HTTPAdapter):
    def __init__(self, transport):
        self.transport = transport
        super().__init__()

    def send(self, request, stream=False, timeout=None, verify=True,
             cert=None, proxies=None):
        from requests.exceptions import ProxyError
        if proxies:
            raise ProxyError("interface-scoped transport rejects proxies")
        return super().send(request, stream=stream, timeout=timeout,
                            verify=verify, cert=cert, proxies=proxies)
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        self.poolmanager = _ScopedPoolManager(
            self.transport, num_pools=connections, maxsize=maxsize,
            block=block, **pool_kwargs)


class InterfaceTransport:
    """Every created socket is constrained to one physical Windows interface."""

    def __init__(self, if_index: int, source_ip: str,
                 dns_servers: Iterable[str], *, timeout: float = 3.0,
                 platform: Optional[str] = None, socket_factory=socket.socket):
        if platform is not None and platform.lower() != "win32":
            raise OSError("interface binding requires Windows")
        if type(if_index) is not int or not 0 < if_index < 2**24:
            raise ValueError("invalid interface index")
        try:
            ip = ipaddress.IPv4Address(source_ip)
        except (ValueError, TypeError):
            raise ValueError("invalid source address")
        if ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            raise ValueError("invalid source address")
        servers = list(dns_servers)
        if not servers or any(not self._is_ipv4(value) for value in servers):
            raise ValueError("explicit IPv4 DNS servers required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.if_index = if_index
        self.source_ip = str(ip)
        self.dns_servers = servers
        self.timeout = float(timeout)
        self.socket_factory = socket_factory
        self.interface_bound = True

    @staticmethod
    def _is_ipv4(value):
        try:
            ipaddress.IPv4Address(value)
            return True
        except (ValueError, TypeError):
            return False

    def _new_socket(self, sock_type=socket.SOCK_STREAM):
        sock = self.socket_factory(socket.AF_INET, sock_type)
        try:
            packed = struct.pack("!I", self.if_index)
            sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, packed)
            if sock.getsockopt(socket.IPPROTO_IP, IP_UNICAST_IF) != self.if_index:
                raise OSError("IP_UNICAST_IF readback mismatch")
            sock.bind((self.source_ip, 0))
            return sock
        except Exception:
            sock.close()
            raise

    def _connect_socket(self, host, port):
        addresses = self.resolve(host) if not self._is_ipv4(host) else [host]
        last = None
        for address in addresses:
            sock = self._new_socket()
            try:
                sock.settimeout(self.timeout)
                sock.connect((address, port))
                return sock
            except Exception as exc:
                last = exc
                sock.close()
        raise OSError("scoped connection failed") from last

    def tcp_connect(self, host, port, timeout=None):
        try:
            sock = self._connect_socket(host, port)
            sock.close()
            return True
        except OSError:
            return False

    def _dns_query(self, name, server):
        query_id = random.randrange(1, 65535)
        qname = _qname(name)
        query = (struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
                 + qname + struct.pack("!HH", 1, 1))
        sock = self._new_socket(socket.SOCK_DGRAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect((server, 53))
            sock.send(query)
            data = sock.recv(4096)
            if len(data) < 12 or data[:2] != struct.pack("!H", query_id):
                return []
            flags, questions, answers = struct.unpack("!HHH", data[2:8])
            if (not flags & 0x8000 or flags & 0x0200
                    or flags & 0x000F != 0 or questions != 1):
                return []
            if data[12:12 + len(qname)] != qname:
                return []
            offset = 12 + len(qname) + 4
            result = []
            for _ in range(answers):
                if offset + 12 > len(data):
                    return []
                if data[offset] & 0xC0 == 0xC0:
                    pointer = struct.unpack("!H", data[offset:offset + 2])[0] & 0x3FFF
                    if pointer < 12 or pointer >= len(data):
                        return []
                    offset += 2
                else:
                    while offset < len(data) and data[offset]:
                        step = data[offset]
                        if step > 63 or offset + step >= len(data):
                            return []
                        offset += step + 1
                    offset += 1
                if offset + 10 > len(data):
                    return []
                rtype, rclass, _, rdlength = struct.unpack(
                    "!HHIH", data[offset:offset + 10])
                offset += 10
                if offset + rdlength > len(data):
                    return []
                if rtype == 1 and rclass == 1 and rdlength == 4:
                    result.append(socket.inet_ntoa(data[offset:offset + 4]))
                offset += rdlength
            return result
        finally:
            sock.close()

    def resolve(self, name):
        for server in self.dns_servers:
            try:
                result = self._dns_query(name, server)
                if result:
                    return result
            except OSError:
                continue
        return []

    def dns_resolve(self, name):
        return bool(self.resolve(name))

    def bind_session(self, session: requests.Session) -> bool:
        session.trust_env = False
        adapter = _ScopedAdapter(self)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return True

    def http_probe(self, url, timeout=None):
        session = requests.Session()
        response = None
        try:
            self.bind_session(session)
            response = session.get(url, allow_redirects=False,
                                   timeout=timeout or self.timeout,
                                   headers={"User-Agent": "CampusReconnect-netdiag/1.0"})
            raw = getattr(response, "content", None)
            if raw is None:
                raw = response.raw.read(512)
            body = raw[:512].decode(response.encoding or "utf-8", errors="replace")
            return SimpleNamespace(status_code=response.status_code,
                                   redirected=bool(300 <= response.status_code < 400),
                                   final_url=response.url or "", body=body, error="")
        except requests.RequestException as exc:
            return SimpleNamespace(status_code=0, redirected=False,
                                   final_url="", body="", error=type(exc).__name__)
        finally:
            if response is not None:
                response.close()
            session.close()
