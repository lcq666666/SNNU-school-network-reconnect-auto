import hashlib
import logging

import pytest

import campus_captcha as captcha
import campus_reconnect as engine
from campus_core import E_AUTH_CAPTCHA, PortalStatus
from campus_update import UpdateInfo, download_update
from tests.fakes import FakeResponse, FakeSession

BASE = "http://202.117.144.205:8601/snnuportal/"
FORM = ("<form action='login'><input type='hidden' name='nonce' value='opaque'>"
        "<input name='verifycode' maxlength='4'><img id='verifycode' src='captcha.jpg'></form>")


def test_real_login_entry_submits_recognized_code_in_same_session(monkeypatch):
    session = FakeSession()
    session.route(BASE + "login.jsp", FakeResponse(200, FORM))
    session.route(BASE + "captcha.jpg", FakeResponse(200, "sample"))
    session.route(BASE + "login", FakeResponse(200, "当前登录账号：123456"))
    monkeypatch.setattr(captcha, "recognize_digits", lambda image: "1234")
    # The runtime resolves the recognizer at call time, keeping the same session.
    outcome = engine.portal_login(session, "123456", "test-password", "mobile", 8601,
                                  logging.getLogger("campus"))
    assert outcome.ok
    payload = session.posts()[0]["kwargs"]["data"]
    assert payload["verifycode"] == "1234"
    assert payload["nonce"] == "opaque"


def test_form_rejects_cross_origin_image_and_action():
    with pytest.raises(ValueError):
        captcha.captcha_form(FORM.replace("captcha.jpg", "https://example.invalid/captcha.jpg"),
                             BASE + "login.jsp")
    with pytest.raises(ValueError):
        captcha.captcha_form(FORM.replace("action='login'", "action='https://example.invalid/login'"),
                             BASE + "login.jsp")


def test_stale_online_session_accepts_challenge_after_initial_post(monkeypatch):
    session = FakeSession()
    session.route(BASE + "login.jsp", [
        FakeResponse(302, "", headers={"Location": "userstatus.jsp"}),
        FakeResponse(200, FORM),
    ])
    session.route(BASE + "userstatus.jsp", FakeResponse(200, "当前登录账号：123456"))
    session.route(BASE + "captcha.jpg", FakeResponse(200, "sample"))
    session.route(BASE + "login", [FakeResponse(200, FORM), FakeResponse(200, "当前登录账号：123456")])
    monkeypatch.setattr(captcha, "recognize_digits", lambda image: "1234")
    result = engine.portal_login_detailed(
        session, "123456", "test-password", "mobile", 8601,
        logging.getLogger("campus"), force_login=True)
    assert result.ok
    assert not result.already_authenticated
    assert len(session.posts()) == 2
    assert session.posts()[1]["kwargs"]["data"]["verifycode"] == "1234"


def test_delayed_captcha_uses_configured_retries_after_first_post(monkeypatch):
    session = FakeSession()
    session.route(BASE + "login.jsp", [
        FakeResponse(302, "", headers={"Location": "userstatus.jsp"}),
        FakeResponse(200, FORM),
    ])
    session.route(BASE + "userstatus.jsp", FakeResponse(200, "当前登录账号：其他账号"))
    session.route(BASE + "captcha.jpg", [FakeResponse(200, "first"),
                                           FakeResponse(200, "second")])
    session.route(BASE + "login", [FakeResponse(200, FORM),
                                    FakeResponse(200, "验证码错误"),
                                    FakeResponse(200, "当前登录账号：123456")])
    monkeypatch.setattr(captcha, "recognize_digits", lambda image: "1234")

    result = engine.portal_login_detailed(
        session, "123456", "test-password", "mobile", 8601,
        logging.getLogger("campus"), force_login=True,
        captcha_max_attempts=2)

    assert result.ok
    assert len(session.posts()) == 3
    assert session.posts()[1]["kwargs"]["data"]["verifycode"] == "1234"
    assert session.posts()[2]["kwargs"]["data"]["verifycode"] == "1234"


def test_delayed_captcha_stops_after_configured_limit(monkeypatch):
    session = FakeSession()
    session.route(BASE + "login.jsp", FakeResponse(302, "", headers={"Location": "userstatus.jsp"}))
    session.route(BASE + "userstatus.jsp", FakeResponse(200, "当前登录账号：其他账号"))
    session.route(BASE + "captcha.jpg", [FakeResponse(200, "first"),
                                           FakeResponse(200, "second")])
    session.route(BASE + "login", [FakeResponse(200, FORM),
                                    FakeResponse(200, "验证码错误")])
    monkeypatch.setattr(captcha, "recognize_digits", lambda image: "1234")

    result = engine.portal_login_detailed(
        session, "123456", "test-password", "mobile", 8601,
        logging.getLogger("campus"), force_login=True,
        captcha_max_attempts=2)

    assert result.error_code == E_AUTH_CAPTCHA
    assert len(session.posts()) == 2


def test_ocr_failure_never_posts_credentials():
    session = FakeSession()
    session.route(BASE + "captcha.jpg", FakeResponse(200, "sample"))
    def fail(image):
        raise ValueError("bad recognition")
    result = captcha.login_with_captcha(
        session, FORM, BASE + "login.jsp", {"account": "123456"}, {},
        lambda response: (response.text, None), recognize=fail)
    assert result.error_code == E_AUTH_CAPTCHA
    assert session.posts() == []


def test_ocr_failure_refreshes_challenge_before_retry():
    session = FakeSession()
    session.route(BASE + "login.jsp", [FakeResponse(200, FORM)])
    session.route(BASE + "captcha.jpg", [FakeResponse(200, "bad"),
                                           FakeResponse(200, "good")])
    session.route(BASE + "login", FakeResponse(200, "当前登录账号：123456"))
    attempts = iter((ValueError("temporary OCR failure"), "1234"))

    def recognize(_image):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    result = captcha.login_with_captcha(
        session, FORM, BASE + "login.jsp", {"account": "123456"}, {},
        lambda response: (response.text, None), recognize=recognize)
    assert result.ok
    assert len(session.posts()) == 1


def test_captcha_attempts_bounded_to_two():
    session = FakeSession()
    session.route(BASE + "captcha.jpg", FakeResponse(200, "sample"))
    session.route(BASE + "login.jsp", FakeResponse(200, FORM))
    session.route(BASE + "login", FakeResponse(200, "验证码错误"))
    result = captcha.login_with_captcha(
        session, FORM, BASE + "login.jsp", {"account": "123456"}, {},
        lambda response: (response.text, None), recognize=lambda image: "1234")
    assert result.status is PortalStatus.NEEDS_USER
    assert len(session.posts()) == 2


def test_release_checksum_accepts_existing_size_format(tmp_path):
    binary = b"MZ" + b"synthetic-test-binary" * 100
    digest = hashlib.sha256(binary).hexdigest()
    class Response:
        status_code = 200
        headers = {"Content-Length": str(len(binary))}
        text = f"{digest}  CampusReconnect.exe\nsize={len(binary)}\n"
        def iter_content(self, chunk_size):
            yield binary
        def close(self):
            pass
    class Session:
        def get(self, url, **kwargs):
            assert "/releases/download/v99.0.0/" in url
            return Response()
    info = UpdateInfo("v99.0.0", "test", "https://example.invalid/releases/tag/")
    path = download_update(Session(), info, tmp_path)
    assert path.read_bytes() == binary
