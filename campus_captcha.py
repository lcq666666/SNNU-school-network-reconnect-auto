"""Local numeric-image recognition and same-session Portal form submission."""
from __future__ import annotations

import re
import threading
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from campus_core import (E_AUTH_CAPTCHA, E_AUTH_REJECTED, LoginOutcome,
                         PortalStatus, parse_status_page)

_OCR = None
_OCR_LOCK = threading.Lock()
_CAPTCHA_FIELD = re.compile(r"captcha|(?:check|verify|valid(?:ate)?|auth|safe|v)?code|rand|验证码", re.I)


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []
        self.images = []
        self.action = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs.get("action", "")
        if tag == "input":
            self.inputs.append(attrs)
        if tag == "img":
            self.images.append(attrs)


def captcha_form(body, page_url):
    form = LoginForm()
    form.feed(body)
    fields = [row for row in form.inputs if row.get("type", "text").lower()
              not in ("hidden", "submit", "button", "password")
              and _CAPTCHA_FIELD.search(row.get("name", "") + row.get("id", ""))]
    images = [row for row in form.images if _CAPTCHA_FIELD.search(
        " ".join(row.get(key, "") for key in ("src", "id", "alt", "onclick")))]
    if len(fields) != 1 or len(images) != 1 or not fields[0].get("name"):
        return None
    action = urljoin(page_url, form.action or "login")
    image = urljoin(page_url, images[0].get("src", ""))
    origin = urlsplit(page_url)
    for url in (action, image):
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("验证码地址或表单跨源，已拒绝提交")
    if urlsplit(action).path != "/snnuportal/login":
        raise ValueError("验证码表单提交地址未通过校验")
    hidden = {row["name"]: row.get("value", "") for row in form.inputs
              if row.get("name") and row.get("type", "").lower() == "hidden"}
    return action, image, fields[0]["name"], hidden


def recognize_digits(image):
    global _OCR
    if not image or len(image) > 1024 * 1024:
        raise ValueError("验证码图片大小异常")
    with _OCR_LOCK:
        if _OCR is None:
            import ddddocr
            _OCR = ddddocr.DdddOcr(show_ad=False, use_gpu=False)
        value = _OCR.classification(image).strip()
    if not re.fullmatch(r"[0-9A-Za-z]{4}", value):
        raise ValueError("验证码识别结果不是有效四位字符")
    return value


def login_with_captcha(session, page_body, page_url, payload, headers,
                       interpret, *, recognize=None, decode=None, max_attempts=2):
    """Return None for ordinary forms. Recognition failures never submit guesses."""
    decode = decode or (lambda response: response.text)
    recognize = recognize or recognize_digits
    form = captcha_form(page_body, page_url)
    if form is None:
        return None
    for attempt in range(max_attempts):
        try:
            action, image_url, field, hidden = form
            image = session.get(image_url, timeout=(3, 5), allow_redirects=False)
            try:
                if image.status_code != 200:
                    raise ValueError("验证码图片获取失败")
                content_type = image.headers.get("Content-Type", "").lower()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError("验证码响应格式异常")
                digits = recognize(image.content)
            finally:
                image.close()
            values = dict(hidden)
            values.update(payload)
            values[field] = digits
            response = session.post(action, data=values, headers=headers,
                                    timeout=(3, 8), allow_redirects=False)
            body, error = interpret(response)
            response.close()
            if body is None:
                raise ValueError(error)
            result = parse_status_page(body, payload["account"])
            if result.status is PortalStatus.ONLINE:
                return LoginOutcome(True, "验证码认证成功，Portal 账号匹配",
                                    status=PortalStatus.ONLINE)
            if re.search(r"密码.{0,4}(错误|有误|不正确)|账号不存在|账户不存在", body):
                return LoginOutcome(False, "Portal 拒绝账号或密码",
                                    status=PortalStatus.NEEDS_USER, error_code=E_AUTH_REJECTED)
            if attempt + 1 < max_attempts:
                # Refresh the page and image using the same session, preserving cookies.
                page = session.get(page_url, timeout=(3, 5), allow_redirects=False)
                try:
                    form = captcha_form(decode(page), page_url)
                finally:
                    page.close()
                if form is None:
                    break
        except Exception as exc:
            # Never include OCR output, credentials, cookies or HTML in diagnostics.
            return LoginOutcome(False, "验证码自动认证未完成（%s），等待重试" % type(exc).__name__,
                                status=PortalStatus.NEEDS_USER, error_code=E_AUTH_CAPTCHA)
    return LoginOutcome(False, "验证码连续两次未通过，等待冷却重试",
                        status=PortalStatus.NEEDS_USER, error_code=E_AUTH_CAPTCHA)
