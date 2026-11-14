"""Explicitly authorized, interface-scoped renewal of a stale SNNU session."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

from campus_install import atomic_json

_LOCK = threading.Lock()
RESET_INTERVAL = 6 * 3600


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.frames = []
        self.rows = []
        self.row = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "iframe":
            self.frames.append(values.get("src", ""))
        if tag == "tr":
            self.row = []

    def handle_endtag(self, tag):
        if tag == "tr" and self.row is not None:
            self.rows.append(" ".join(self.row))
            self.row = None

    def handle_data(self, data):
        self.text.append(data)
        if self.row is not None:
            self.row.append(data)


def _read(session, url):
    response = session.get(url, timeout=(3, 6), allow_redirects=False)
    try:
        from campus_reconnect import decode_body
        return decode_body(response) if response.status_code == 200 else None
    finally:
        response.close()


def _identity_matches(session, base, wifi, mac, account):
    from campus_reconnect import normalize_account, parse_status_page

    body = _read(session, base + "/snnuportal/userstatus.jsp")
    if not body:
        return False
    status = parse_status_page(body)
    if not account or normalize_account(status.account) != normalize_account(account):
        return False
    page = _Page()
    page.feed(body)
    current = re.search(r"当前\s*IP\s*[:：]\s*([0-9.]+)", " ".join(page.text), re.I)
    if current is None or current.group(1) != wifi.source_ip:
        return False
    expected_mac = re.sub(r"[^0-9a-f]", "", mac.lower())
    if len(expected_mac) != 12:
        return False
    for src in page.frames:
        url = urljoin(base + "/snnuportal/userstatus.jsp", src)
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "202.117.144.205"
                or parsed.port not in (None, 80)
                or parsed.path != "/zili/auth_my_state.php"
                or parsed.username is not None or parsed.password is not None):
            continue
        backend = _read(session, url)
        if not backend:
            continue
        details = _Page()
        details.feed(backend)
        for row in details.rows:
            ips = re.findall(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])", row)
            macs = re.findall(r"(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}", row, re.I)
            if wifi.source_ip in ips and any(
                    re.sub(r"[:-]", "", value.lower()) == expected_mac for value in macs):
                return True
    return False


def reset_wireless_session(*, session, wifi, mac, account, journal, primary_ready,
                           relogin, enabled=False, cancelled=lambda: False,
                           clock=time.time, log=lambda message: None):
    """Return the relogin result, or None when renewal cannot be safely attempted.

    The caller owns the single recovery worker. A second thread is refused here.
    Reservation is persisted before logoff, including when its reply is lost.
    """
    if not enabled or cancelled() or not _LOCK.acquire(blocking=False):
        return None
    try:
        now = clock()
        if journal.exists():
            try:
                last = json.loads(journal.read_text(encoding="utf-8"))["last_attempt"]
                if type(last) not in (int, float) or not math.isfinite(last):
                    return None
            except (OSError, ValueError, KeyError, TypeError):
                log("无线会话恢复记录无法读取，保留现有会话")
                return None
            if now - last < RESET_INTERVAL:
                return None
        adapter = session.get_adapter("http://202.117.144.205/")
        transport = getattr(adapter, "transport", None)
        if (session.trust_env or getattr(session, "proxies", {})
                or not getattr(transport, "interface_bound", False)
                or transport.if_index != wifi.if_index
                or transport.source_ip != wifi.source_ip):
            return None
        from campus_reconnect import resolve_port, portal_base
        port, _ = resolve_port(session, None)
        if port is None:
            return None
        base = portal_base(port)
        if not _identity_matches(session, base, wifi, mac, account):
            log("无线 IP、MAC 或账号未能与校园记录核对，保留现有会话")
            return None
        if cancelled() or not primary_ready():
            return None
        if cancelled():
            return None
        atomic_json(journal, {"last_attempt": now, "stage": "reserved"})
        log("无线旧会话公网未恢复，已核对本机身份，开始重建无线会话")
        try:
            response = session.get(base + "/snnuportal/logoff", timeout=(3, 8),
                                   allow_redirects=False)
            response.close()
        except requests.RequestException:
            log("无线断开响应未确认，仍尝试恢复登录")
        # Logoff may have succeeded even if the response was lost. Always try to
        # restore login; the existing pipeline checks cancellation before POST.
        result = relogin()
        atomic_json(journal, {"last_attempt": now, "stage": "finished",
                              "restored": bool(getattr(result, "ok", False))})
        return result
    finally:
        _LOCK.release()
