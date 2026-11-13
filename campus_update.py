# -*- coding: utf-8 -*-
"""Release discovery, version comparison and SHA256-verified EXE downloads.

Sessions are injected by the caller. Installation and rollback are handled by
campus_update_install after the user clicks the local update button.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re

UPDATE_REPO = "lcq666666/SNNU-school-network-reconnect-auto"
UPDATE_API_URL = "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO
#: 首次检查：启动后 60 秒（等网络稳定，不抢启动流程）。
UPDATE_FIRST_CHECK_DELAY_S = 60.0
#: 之后每 6 小时检查一次。
UPDATE_CHECK_INTERVAL_S = 6 * 3600.0
#: 单次 GitHub API 请求超时（秒）。
UPDATE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class UpdateInfo:
    """一次成功的最新 Release 查询结果。"""

    tag: str
    name: str
    html_url: str
    published_at: str = ""

    @property
    def version(self) -> "tuple[int, int, int] | None":
        return parse_version(self.tag)


def parse_version(text) -> "tuple[int, int, int] | None":
    """解析 ``"v0.6.0"`` / ``"0.6.0"`` 为 ``(0, 6, 0)``；非法返回 ``None``。

    接受 1–3 段纯数字（缺段补 0，如 ``"0.6"`` → ``(0, 6, 0)``）；
    带 ``-`` / ``+`` 后缀、空段、非数字一律不猜。
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    if not cleaned:
        return None
    parts = cleaned.split(".")
    if len(parts) > 3:
        return None
    numbers = []
    for part in parts:
        if not part.isdigit():
            return None
        numbers.append(int(part))
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


def is_newer(latest, current) -> bool:
    """``latest`` 是否严格新于 ``current``；任一无法解析即返回 ``False``。"""
    latest_v = parse_version(latest)
    current_v = parse_version(current)
    if latest_v is None or current_v is None:
        return False
    return latest_v > current_v


def should_notify(info, current_version, notified_tag) -> bool:
    """这次检查到新版本时，气泡该不该弹（纯函数，可单测）。

    三个条件同时成立才弹：确实有新版本、且这一版之前没弹过。
    横幅是否显示另由 ``update_dismissed_tag`` 决定（见托盘接线），
    与气泡去重分开：气泡只弹一次，横幅在用户点“以后再说”前一直留着。
    """
    if info is None:
        return False
    if not is_newer(info.tag, current_version):
        return False
    return info.tag != (notified_tag or "")


def _check_api_latest(session, repo: str = UPDATE_REPO
                 ) -> "tuple[UpdateInfo | None, str]":
    """查询 GitHub 最新 Release；成功返回 ``(UpdateInfo, "")``。

    任何失败都返回 ``(None, 原因)``：网络异常、非 200、JSON 异常、
    缺 ``tag_name``、tag 无法解析为版本号。调用方只写日志，不弹窗。
    """
    url = "https://api.github.com/repos/%s/releases/latest" % repo
    try:
        resp = session.get(url, timeout=UPDATE_TIMEOUT_S,
                           headers={"Accept": "application/vnd.github+json"})
    except Exception as exc:  # noqa: BLE001 - 网络异常全部转原因，不抛
        return None, "请求失败：%s" % type(exc).__name__
    try:
        status = resp.status_code
    except Exception:  # noqa: BLE001 - 假 session 也可能没状态码
        return None, "响应无状态码"
    if status != 200:
        return None, "GitHub 返回 HTTP %s" % status
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - 坏 JSON 不抛
        return None, "响应不是合法 JSON"
    if not isinstance(data, dict):
        return None, "响应不是合法 JSON 对象"
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        return None, "最新 Release 缺少 tag_name"
    tag = tag.strip()
    if parse_version(tag) is None:
        return None, "tag 无法解析为版本号：%s" % tag
    name = data.get("name")
    html_url = data.get("html_url")
    published = data.get("published_at")
    return UpdateInfo(
        tag=tag,
        name=name if isinstance(name, str) and name else tag,
        html_url=(html_url if isinstance(html_url, str) and html_url
                  else "https://github.com/%s/releases" % repo),
        published_at=published if isinstance(published, str) else "",
    ), ""


def check_latest(session, repo: str = UPDATE_REPO):
    info, reason = _check_api_latest(session, repo)
    if info is not None:
        return info, reason
    # Public API rate limiting must not disable updates on a shared campus exit.
    try:
        url = "https://github.com/%s/releases/latest/download/build_info.json" % repo
        response = session.get(url, timeout=UPDATE_TIMEOUT_S)
        if response.status_code != 200:
            return None, reason
        data = response.json()
        version = data.get("version") if isinstance(data, dict) else None
        if parse_version(version) is None:
            return None, reason
        tag = "v" + version.lstrip("vV")
        return UpdateInfo(tag, tag, "https://github.com/%s/releases/tag/%s" % (repo, tag)), ""
    except Exception:
        return None, reason


def _download_verified_asset(session, info, directory, filename, progress=None) -> Path:
    """Download one release asset and validate its sidecar and PE header."""
    if parse_version(info.tag) is None or not re.fullmatch(r"v?\d+(?:\.\d+){0,2}", info.tag):
        raise ValueError("更新版本号无效")
    base = "https://github.com/%s/releases/download/%s/" % (UPDATE_REPO, info.tag)
    checksum = session.get(base + filename + ".sha256", timeout=(5, 20))
    try:
        if checksum.status_code != 200:
            raise ValueError("无法获取更新 SHA256 校验文件")
        match = re.fullmatch(
            r"\s*([a-fA-F0-9]{64})\s+\*?" + re.escape(filename)
            + r"\s*(?:size=(\d+)\s*)?",
            checksum.text)
        if match is None:
            raise ValueError("更新 SHA256 文件格式无效")
        expected = match.group(1).lower()
        expected_size = int(match.group(2)) if match.group(2) else 0
    finally:
        checksum.close()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / (filename + ".download")
    target = directory / filename
    response = session.get(base + filename, timeout=(5, 30), stream=True)
    try:
        if response.status_code != 200:
            raise ValueError("更新下载失败 HTTP %s" % response.status_code)
        total = int(response.headers.get("Content-Length", "0"))
        digest = hashlib.sha256()
        received = 0
        with temporary.open("wb") as stream:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                received += len(chunk)
                if received > 512 * 1024 * 1024:
                    raise ValueError("更新文件超过大小上限")
                digest.update(chunk)
                stream.write(chunk)
                if progress:
                    progress(received, total)
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != expected:
            raise ValueError("更新 SHA256 校验失败")
        if total and received != total:
            raise ValueError("更新下载长度不完整")
        if expected_size and received != expected_size:
            raise ValueError("更新文件与发布大小不符")
        with temporary.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError("更新文件不是 Windows 可执行文件")
        os.replace(temporary, target)
        return target
    finally:
        response.close()
        temporary.unlink(missing_ok=True)


def download_update(session, info, directory, progress=None) -> Path:
    """Download the tagged portable executable and verify its sidecar."""
    return _download_verified_asset(session, info, directory, "CampusReconnect.exe", progress)


def download_installer(session, info, directory, progress=None) -> Path:
    """Download the standard installer and verify its sidecar."""
    return _download_verified_asset(session, info, directory,
                                    "CampusReconnectSetup.exe", progress)
