# -*- coding: utf-8 -*-
"""凭据存储适配器单元测试（阶段 2，任务 2.4，对应 T22 / T34）。

覆盖 F11：

- 「尚未配置」与「凭据后端不可用」必须可区分；
- 第一条写入失败 ⇒ 不留下半条记录；
- 第二条写入失败 ⇒ 尝试恢复旧对，恢复结果单独报告；
- 原本没有记录时 ⇒ 只清理本次写入的账号，不误删用户既有记录；
- 清除凭据只删除本程序的记录。

约束：全部使用 :class:`tests.fakes.FakeKeyring`，不读写真实 Windows 凭据。
"""
from __future__ import annotations

from campus_core import E_CRED_BACKEND, E_CRED_MISSING
from campus_windows import CredentialStore
from tests.fakes import FakeKeyring

SERVICE = "SNNU-Campus-Reconnect"
FAKE_PASSWORD = "FAKE_PASSWORD"


def test_default_service_name_matches_engine_constant():
    """凭据 service 名与引擎常量保持一致（兼容既有已保存凭据）。"""
    import campus_reconnect

    assert CredentialStore(None).service == campus_reconnect.SERVICE
    assert CredentialStore(None).service == SERVICE


# --------------------------------------------------------------------------- #
# T34：首跑 / 无凭据
# --------------------------------------------------------------------------- #
def test_t34_first_run_reports_missing_not_backend_error():
    store = CredentialStore(FakeKeyring())
    loaded = store.load()
    assert loaded.ok is False
    assert loaded.error_code == E_CRED_MISSING
    assert loaded.password.reveal() == ""


def test_t22_read_exception_is_backend_error_not_missing():
    store = CredentialStore(FakeKeyring(raise_get=True))
    loaded = store.load()
    assert loaded.ok is False
    assert loaded.error_code == E_CRED_BACKEND


# --------------------------------------------------------------------------- #
# T22：写入与部分失败恢复
# --------------------------------------------------------------------------- #
def test_t22_save_success_roundtrip_and_secret_is_masked():
    keyring = FakeKeyring()
    store = CredentialStore(keyring)
    assert store.save("2024001", FAKE_PASSWORD).ok is True

    loaded = store.load()
    assert loaded.ok is True
    assert loaded.username == "2024001"
    assert loaded.password.reveal() == FAKE_PASSWORD
    assert FAKE_PASSWORD not in repr(loaded)   # 秘密容器脱敏


def test_t22_first_write_failure_leaves_no_partial_record():
    keyring = FakeKeyring(fail_set_on="username")
    result = CredentialStore(keyring).save("2024001", FAKE_PASSWORD)
    assert result.ok is False
    assert result.error_code == E_CRED_BACKEND
    assert keyring.values == {}


def test_t22_second_write_failure_restores_old_pair():
    keyring = FakeKeyring(values={(SERVICE, "username"): "old",
                                  (SERVICE, "password"): "oldpw"},
                          fail_set_on="password")
    result = CredentialStore(keyring).save("new", "newpw")
    assert result.ok is False
    assert result.rolled_back is True
    assert keyring.values[(SERVICE, "username")] == "old"
    assert keyring.values[(SERVICE, "password")] == "oldpw"


def test_t22_second_write_failure_without_old_pair_cleans_up():
    """原本没有账号时，只清理本次写入的账号（不删除用户既有记录）。"""
    keyring = FakeKeyring(values={("OtherApp", "username"): "other"},
                          fail_set_on="password")
    result = CredentialStore(keyring).save("new", FAKE_PASSWORD)
    assert result.ok is False
    assert result.rolled_back is True
    assert (SERVICE, "username") not in keyring.values
    assert keyring.values[("OtherApp", "username")] == "other"


def test_t22_restore_failure_is_reported():
    class BrokenRestore(FakeKeyring):
        def set_password(self, service, username, password):
            if username == "username" and password == "old":
                raise RuntimeError("restore denied")
            return super().set_password(service, username, password)

    keyring = BrokenRestore(values={(SERVICE, "username"): "old",
                                    (SERVICE, "password"): "oldpw"},
                            fail_set_on="password")
    result = CredentialStore(keyring).save("new", "newpw")
    assert result.ok is False
    assert result.rolled_back is False
    assert "恢复原凭据失败" in result.detail


# --------------------------------------------------------------------------- #
# T22：清除
# --------------------------------------------------------------------------- #
def test_t22_save_tolerates_unreadable_old_pair():
    """读取旧凭据失败时仍可保存（旧对视为未知），且不误删任何记录。"""
    class BrokenRead(FakeKeyring):
        def get_password(self, service, username):
            raise RuntimeError("read denied")

    keyring = BrokenRead()
    result = CredentialStore(keyring).save("u", FAKE_PASSWORD)
    assert result.ok is True
    assert keyring.delete_calls == []


def test_t22_clear_only_removes_our_records():
    keyring = FakeKeyring(values={(SERVICE, "username"): "u",
                                  (SERVICE, "password"): "p",
                                  ("OtherApp", "username"): "other"})
    assert CredentialStore(keyring).clear().ok is True
    assert (SERVICE, "username") not in keyring.values
    assert (SERVICE, "password") not in keyring.values
    assert keyring.values[("OtherApp", "username")] == "other"


def test_t22_clear_on_empty_is_ok_and_deletes_nothing():
    keyring = FakeKeyring()
    assert CredentialStore(keyring).clear().ok is True
    assert keyring.delete_calls == []


def test_t22_clear_backend_failure_is_reported():
    class BrokenRead(FakeKeyring):
        def get_password(self, service, username):
            raise RuntimeError("backend down")

    result = CredentialStore(BrokenRead()).clear()
    assert result.ok is False
    assert result.error_code == E_CRED_BACKEND
