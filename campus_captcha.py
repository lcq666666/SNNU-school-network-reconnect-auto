"""Local numeric-image recognition and same-session Portal form submission."""
from __future__ import annotations

import re
import threading
from collections import Counter
import io
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from campus_core import (E_AUTH_CAPTCHA, E_AUTH_REJECTED, LoginOutcome,
                         PortalStatus, parse_status_page)

_OCR = None
_OCR_LOCK = threading.Lock()
_CAPTCHA_FIELD = re.compile(r"captcha|(?:check|verify|valid(?:ate)?|auth|safe|v)?code|rand|验证码", re.I)
CAPTCHA_MAX_ATTEMPTS = 3
CAPTCHA_CODE_RE = re.compile(r"^[0-9]{4}$")


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.outside = ("", [], [])
        self.forms = []
        self.current = self.outside

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = (attrs.get("action", ""), [], [])
            self.forms.append(self.current)
        if tag == "input":
            self.current[1].append(attrs)
        if tag == "img":
            self.current[2].append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = self.outside


def _challenge_fields(inputs, images):
    fields = [row for row in inputs if row.get("type", "text").lower()
              not in ("hidden", "submit", "button", "password")
              and _CAPTCHA_FIELD.search(row.get("name", "") + row.get("id", ""))]
    pictures = [row for row in images if _CAPTCHA_FIELD.search(
        " ".join(row.get(key, "") for key in ("src", "id", "alt", "onclick")))]
    return fields, pictures


def captcha_form(body, page_url):
    page = LoginForm()
    page.feed(body)
    candidates = []
    for action, inputs, images in page.forms:
        fields, pictures = _challenge_fields(inputs, images)
        if fields or pictures:
            candidates.append((action, inputs, fields, pictures))
    loose_fields, loose_images = _challenge_fields(*page.outside[1:])
    if not candidates and not loose_fields and not loose_images:
        return None
    if len(candidates) != 1 or loose_fields or loose_images:
        raise ValueError("验证码表单归属不明确")
    action, inputs, fields, images = candidates[0]
    if (len(fields) != 1 or len(images) != 1 or not fields[0].get("name")
            or not images[0].get("src")):
        raise ValueError("验证码表单字段不完整")
    action = urljoin(page_url, action or "login")
    image = urljoin(page_url, images[0]["src"])
    origin = urlsplit(page_url)
    for url in (action, image):
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("验证码地址或表单跨源，已拒绝提交")
    if urlsplit(action).path != "/snnuportal/login":
        raise ValueError("验证码表单提交地址未通过校验")
    hidden = {row["name"]: row.get("value", "") for row in inputs
              if row.get("name") and row.get("type", "").lower() == "hidden"}
    return action, image, fields[0]["name"], hidden


def _valid_code(value):
    value = str(value or "").strip()
    return value if CAPTCHA_CODE_RE.fullmatch(value) else None


def _image_variants(image):
    """Return bounded local preprocessing variants for noisy numeric images."""
    variants = [image]
    try:
        from PIL import Image, ImageFilter, ImageOps
        source = Image.open(io.BytesIO(image)).convert("L")
        width, height = source.size
        if width <= 0 or height <= 0 or width > 1200 or height > 600:
            return variants
        scale = 3 if width < 180 else 2
        enlarged = source.resize((width * scale, height * scale), Image.Resampling.LANCZOS)
        variants.append(_png_bytes(ImageOps.autocontrast(enlarged)))
        variants.append(_png_bytes(ImageOps.invert(enlarged).filter(ImageFilter.MedianFilter(3))))
        threshold = enlarged.point(lambda value: 255 if value > 160 else 0)
        variants.append(_png_bytes(threshold))
    except Exception:
        # The unmodified image remains a valid fail-closed input if optional
        # image preprocessing is unavailable.
        pass
    return variants


def _png_bytes(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def recognize_digits(image):
    """Recognize a four-digit challenge using bounded local consensus.

    A single valid result is accepted for compatibility with simple Portal
    images; when preprocessing produces multiple valid results, the most
    frequent result wins and ties fail closed instead of submitting a guess.
    """
    global _OCR
    if not image or len(image) > 1024 * 1024:
        raise ValueError("验证码图片大小异常")
    with _OCR_LOCK:
        if _OCR is None:
            import ddddocr
            _OCR = ddddocr.DdddOcr(show_ad=False, use_gpu=False)
        candidates = []
        for variant in _image_variants(image):
            try:
                value = _valid_code(_OCR.classification(variant))
            except Exception:
                value = None
            if value is not None:
                candidates.append(value)
    if not candidates:
        raise ValueError("验证码识别结果不是有效四位数字")
    counts = Counter(candidates)
    top = counts.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        raise ValueError("验证码识别结果不一致")
    return top[0][0]


def login_with_captcha(session, page_body, page_url, payload, headers,
                       interpret, *, recognize=None, decode=None, max_attempts=2):
    """Return None for ordinary forms; retry transient challenge failures safely.

    A failed OCR/image fetch must not submit a guess, but it also must not end
    the recovery attempt immediately. Refreshing the login page in the same
    session obtains a new challenge while preserving the Portal cookies.
    """
    decode = decode or (lambda response: response.text)
    recognize = recognize or recognize_digits
    try:
        form = captcha_form(page_body, page_url)
    except ValueError:
        return LoginOutcome(False, "无法确认验证码表单结构或地址，需在 Portal 页面处理",
                            status=PortalStatus.NEEDS_USER, error_code=E_AUTH_CAPTCHA)
    if form is None:
        return None
    failure = "Portal 未确认验证码通过"
    for attempt in range(max_attempts):
        try:
            action, image_url, field, hidden = form
            failure = "验证码图片获取失败"
            image = session.get(image_url, timeout=(3, 5), allow_redirects=False)
            try:
                if image.status_code != 200:
                    raise ValueError("验证码图片获取失败")
                content_type = image.headers.get("Content-Type", "").lower()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError("验证码响应格式异常")
                failure = "验证码图片识别失败"
                digits = recognize(image.content)
            finally:
                image.close()
            values = dict(hidden)
            values.update(payload)
            values[field] = digits
            failure = "验证码提交或响应读取失败"
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
            from campus_reconnect import portal_device_limit
            limited = portal_device_limit(body)
            if limited is not None:
                return limited
            if re.search(r"密码.{0,4}(错误|有误|不正确)|账号不存在|账户不存在", body):
                return LoginOutcome(False, "Portal 拒绝账号或密码",
                                    status=PortalStatus.NEEDS_USER, error_code=E_AUTH_REJECTED)
            failure = "Portal 未确认验证码通过"
            if attempt + 1 < max_attempts:
                # Refresh the page and image using the same session, preserving cookies.
                page = session.get(page_url, timeout=(3, 5), allow_redirects=False)
                try:
                    form = captcha_form(decode(page), page_url)
                finally:
                    page.close()
                if form is None:
                    failure = "刷新后未找到验证码表单"
                    break
        except Exception as exc:
            # Never include OCR output, credentials, cookies or HTML in diagnostics.
            # A malformed/cross-origin form remains fail-closed; ordinary image,
            # OCR, and transport failures get one bounded same-session retry.
            if attempt + 1 >= max_attempts:
                return LoginOutcome(
                    False, "%s（%s），等待重试" % (failure, type(exc).__name__),
                    status=PortalStatus.NEEDS_USER, error_code=E_AUTH_CAPTCHA)
            try:
                page = session.get(page_url, timeout=(3, 5), allow_redirects=False)
                try:
                    form = captcha_form(decode(page), page_url)
                finally:
                    page.close()
                if form is None:
                    break
            except Exception:
                return LoginOutcome(
                    False, "%s，刷新验证码页面失败" % failure,
                    status=PortalStatus.NEEDS_USER, error_code=E_AUTH_CAPTCHA)
    return LoginOutcome(False, "%s，等待冷却重试" % failure,
                        status=PortalStatus.NEEDS_USER, error_code=E_AUTH_CAPTCHA)
