# -*- coding: utf-8 -*-
"""V0.6 Block 2：Wi-Fi 自启链（campus_wifi_bringup）离线测试。

覆盖：plan 各分支（已连、网卡禁用、射频关、无 profile、全缺）、profile XML 结构、
run_bringup 成功 / 中途失败 / 管理员失败、failback 稳定判定边界。
全程假 ops，零真实网络 / 注册表 / 凭据 / 浏览器 / 真实 sleep。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from campus_wifi_bringup import (
    BringupPlan,
    BringupStep,
    WifiAdapterState,
    adapter_state_from_netsh_text,
    build_snnu_open_profile_xml,
    is_stable_for_failback,
    plan,
    run_bringup,
)

NS = "{http://www.microsoft.com/networking/WLAN/profile/v1}"


def _kinds(a_plan: BringupPlan):
    """取计划的步骤种类列表（便于断言顺序）。"""
    return [step.kind for step in a_plan.steps]


def _state(**kwargs):
    """默认“网卡启用、射频未知、未连接”的接口快照，调用方按需覆盖字段。"""
    base = {"name": "Wi-Fi", "admin_state": "enabled",
            "state": "disconnected", "ssid": None, "radio_on": None}
    base.update(kwargs)
    return WifiAdapterState(**base)


class FakeOps:
    """假注入执行器：按队列返回 (returncode, stdout)，记录全部调用。"""

    def __init__(self, run_results=(), wait_ip_result=True):
        self._queue = list(run_results)
        self.wait_ip_result = wait_ip_result
        self.run_calls = []
        self.wait_ip_calls = 0

    def run(self, cmd):
        self.run_calls.append(list(cmd))
        if self._queue:
            return self._queue.pop(0)
        return 0, ""

    def wait_ip(self):
        self.wait_ip_calls += 1
        return self.wait_ip_result


class RaisingOps(FakeOps):
    """run 一律抛异常的假执行器：验证执行异常不被静默吞掉。"""

    def run(self, cmd):
        self.run_calls.append(list(cmd))
        raise OSError("netsh 启动失败")


# --------------------------------------------------------------------------- #
# plan：已连 / 仅连接
# --------------------------------------------------------------------------- #
def test_plan_already_connected_returns_empty_ok_plan():
    result = plan(_state(state="connected", ssid="SNNU"), has_profile=True)
    assert result.ok is True
    assert result.steps == ()
    assert "已连接" in result.reason


def test_plan_already_connected_chinese_state_counts():
    result = plan(_state(state="已连接", ssid="SNNU"), has_profile=False)
    assert result.ok is True and result.steps == ()


def test_plan_connected_to_other_ssid_still_connects():
    result = plan(_state(state="connected", ssid="Other"), has_profile=True)
    assert _kinds(result) == ["connect", "wait_ip"]


def test_plan_disconnected_with_profile_only_connects_and_waits():
    result = plan(_state(), has_profile=True)
    assert result.ok is True
    assert _kinds(result) == ["connect", "wait_ip"]
    connect = result.steps[0]
    assert list(connect.command) == ["netsh", "wlan", "connect", "name=SNNU"]
    assert result.steps[1].command == ()


def test_plan_case_difference_is_not_target():
    """SSID 大小写不同不得视为已连接（与 F17 同源）。"""
    result = plan(_state(state="connected", ssid="snnu"), has_profile=True)
    assert _kinds(result) == ["connect", "wait_ip"]


# --------------------------------------------------------------------------- #
# plan：各缺失分支
# --------------------------------------------------------------------------- #
def test_plan_disabled_adapter_first_step_is_enable_adapter():
    result = plan(_state(admin_state="disabled"), has_profile=True)
    assert _kinds(result) == ["enable_adapter", "connect", "wait_ip"]
    step = result.steps[0]
    assert list(step.command) == ["netsh", "interface", "set", "interface",
                                  "name=Wi-Fi", "admin=enabled"]
    assert "网卡已禁用" in result.reason


def test_plan_radio_off_adds_radio_on_step_with_task_command():
    result = plan(_state(radio_on=False), has_profile=True)
    assert _kinds(result) == ["radio_on", "connect", "wait_ip"]
    step = result.steps[0]
    assert list(step.command) == ["netsh", "wlan", "set", "autoconfig",
                                  "enabled=yes", "interface=Wi-Fi"]
    assert "射频已关闭" in result.reason


def test_plan_radio_unknown_adds_no_radio_step():
    assert _kinds(plan(_state(radio_on=None), has_profile=True)) == [
        "connect", "wait_ip"]
    assert _kinds(plan(_state(radio_on=True), has_profile=True)) == [
        "connect", "wait_ip"]


def test_plan_missing_profile_adds_ensure_profile_step():
    result = plan(_state(), has_profile=False)
    assert _kinds(result) == ["ensure_profile", "connect", "wait_ip"]
    step = result.steps[0]
    assert list(step.command) == ["netsh", "wlan", "add", "profile",
                                  "filename=SNNU.xml"]
    assert step.profile_xml is not None
    assert "<authentication>open</authentication>" in step.profile_xml
    assert "<encryption>none</encryption>" in step.profile_xml
    assert "缺少 SNNU 配置文件" in result.reason


def test_plan_all_missing_in_order():
    result = plan(_state(admin_state="disabled", radio_on=False),
                  has_profile=False)
    assert _kinds(result) == ["enable_adapter", "radio_on", "ensure_profile",
                              "connect", "wait_ip"]


def test_plan_no_interface_is_not_executable():
    result = plan(None, has_profile=True)
    assert result.ok is False and result.steps == ()


def test_plan_empty_ssid_is_not_executable():
    result = plan(_state(), has_profile=True, ssid="  ")
    assert result.ok is False and result.steps == ()


def test_plan_disabled_adapter_without_name_is_not_executable():
    result = plan(_state(name="", admin_state="disabled"), has_profile=True)
    assert result.ok is False and result.steps == ()


def test_plan_radio_off_without_name_is_not_executable():
    result = plan(_state(name="", radio_on=False), has_profile=True)
    assert result.ok is False and result.steps == ()


def test_plan_custom_ssid_flows_through():
    result = plan(_state(), has_profile=False, ssid="SNNU-5G")
    assert list(result.steps[0].command) == ["netsh", "wlan", "add", "profile",
                                             "filename=SNNU-5G.xml"]
    assert list(result.steps[1].command) == ["netsh", "wlan", "connect",
                                             "name=SNNU-5G"]


# --------------------------------------------------------------------------- #
# adapter_state_from_netsh_text：复用 F17 解析
# --------------------------------------------------------------------------- #
EN_OUTPUT = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : connected
    SSID                   : SNNU
    BSSID                  : aa:bb:cc:dd:ee:01
    Profile                : SNNU
"""

DISCONNECTED_OUTPUT = """
    Name                   : Wi-Fi
    State                  : disconnected
    SSID                   : SNNU
"""


def test_adapter_state_from_netsh_text_reuses_f17_parsing():
    state = adapter_state_from_netsh_text(EN_OUTPUT)
    assert state is not None
    assert state.name == "Wi-Fi"
    assert state.ssid == "SNNU"
    assert state.state == "connected"
    assert state.is_connected_to("SNNU") is True
    # netsh show interfaces 不报告管理状态与射频：启用是推断，未知填 None
    assert state.admin_state == "enabled"
    assert state.radio_on is None


def test_adapter_state_from_netsh_text_disconnected():
    state = adapter_state_from_netsh_text(DISCONNECTED_OUTPUT)
    assert state is not None
    assert state.is_connected_to("SNNU") is False
    assert plan(state, has_profile=True).steps[0].kind == "connect"


def test_adapter_state_from_netsh_text_no_interface_is_none():
    assert adapter_state_from_netsh_text("totally unrelated text") is None
    assert adapter_state_from_netsh_text("") is None


# --------------------------------------------------------------------------- #
# build_snnu_open_profile_xml：结构与转义
# --------------------------------------------------------------------------- #
def test_profile_xml_structure():
    root = ET.fromstring(build_snnu_open_profile_xml("SNNU"))
    assert root.tag == NS + "WLANProfile"
    assert root.findtext(NS + "name") == "SNNU"
    assert root.findtext(NS + "SSIDConfig/" + NS + "SSID/" + NS + "name") == "SNNU"
    assert root.findtext(NS + "connectionType") == "ESS"
    assert root.findtext(NS + "connectionMode") == "auto"
    auth = root.find(NS + "MSM/" + NS + "security/" + NS + "authEncryption")
    assert auth.findtext(NS + "authentication") == "open"
    assert auth.findtext(NS + "encryption") == "none"
    assert auth.findtext(NS + "useOneX") == "false"


def test_profile_xml_escapes_ssid():
    xml_text = build_snnu_open_profile_xml("A&B<C>")
    assert "A&amp;B&lt;C&gt;" in xml_text
    root = ET.fromstring(xml_text)
    assert root.findtext(NS + "name") == "A&B<C>"


def test_profile_xml_empty_ssid_raises():
    with pytest.raises(ValueError):
        build_snnu_open_profile_xml("  ")


# --------------------------------------------------------------------------- #
# run_bringup：成功 / 中途失败 / 管理员失败
# --------------------------------------------------------------------------- #
def test_run_bringup_success_all_missing():
    a_plan = plan(_state(admin_state="disabled", radio_on=False),
                  has_profile=False)
    ops = FakeOps()
    result = run_bringup(a_plan, ops)
    assert result.ok is True
    assert result.steps_done == ("enable_adapter", "radio_on", "ensure_profile",
                                 "connect", "wait_ip")
    assert result.failed_step is None
    assert ops.run_calls == [
        ["netsh", "interface", "set", "interface", "name=Wi-Fi",
         "admin=enabled"],
        ["netsh", "wlan", "set", "autoconfig", "enabled=yes",
         "interface=Wi-Fi"],
        ["netsh", "wlan", "add", "profile", "filename=SNNU.xml"],
        ["netsh", "wlan", "connect", "name=SNNU"],
    ]
    assert ops.wait_ip_calls == 1


def test_run_bringup_stops_at_first_failure():
    a_plan = plan(_state(radio_on=False), has_profile=True)
    ops = FakeOps(run_results=[(1, "出错")])
    result = run_bringup(a_plan, ops)
    assert result.ok is False
    assert result.failed_step == "radio_on"
    assert result.steps_done == ()
    # detail 含命令与返回码，且后续 connect / wait_ip 未执行
    assert "netsh wlan set autoconfig" in result.detail
    assert "返回码：1" in result.detail
    assert ops.run_calls == [["netsh", "wlan", "set", "autoconfig",
                              "enabled=yes", "interface=Wi-Fi"]]
    assert ops.wait_ip_calls == 0


def test_run_bringup_mid_failure_keeps_done_steps():
    a_plan = plan(_state(admin_state="disabled", radio_on=False),
                  has_profile=True)
    ops = FakeOps(run_results=[(0, ""), (2, "radio error")])
    result = run_bringup(a_plan, ops)
    assert result.ok is False
    assert result.failed_step == "radio_on"
    assert result.steps_done == ("enable_adapter",)
    assert "返回码：2" in result.detail
    assert ops.wait_ip_calls == 0


def test_run_bringup_admin_failure_says_so_explicitly():
    a_plan = plan(_state(), has_profile=True)
    ops = FakeOps(run_results=[
        (1, "请求的操作需要提升(请以管理员身份运行)。")])
    result = run_bringup(a_plan, ops)
    assert result.ok is False
    assert result.failed_step == "connect"
    assert "需要管理员权限" in result.detail
    assert "返回码：1" in result.detail


def test_run_bringup_admin_failure_english_hint_too():
    a_plan = plan(_state(), has_profile=True)
    ops = FakeOps(run_results=[
        (1, "The requested operation requires elevation (Run as administrator).")])
    result = run_bringup(a_plan, ops)
    assert "需要管理员权限" in result.detail


def test_run_bringup_wait_ip_false_fails_at_last_step():
    a_plan = plan(_state(), has_profile=True)
    ops = FakeOps(wait_ip_result=False)
    result = run_bringup(a_plan, ops)
    assert result.ok is False
    assert result.failed_step == "wait_ip"
    assert result.steps_done == ("connect",)
    assert "未获得可用 IP" in result.detail


def test_run_bringup_empty_plan_executes_nothing():
    a_plan = plan(_state(state="connected", ssid="SNNU"), has_profile=True)
    ops = FakeOps()
    result = run_bringup(a_plan, ops)
    assert result.ok is True
    assert result.steps_done == ()
    assert ops.run_calls == [] and ops.wait_ip_calls == 0


def test_run_bringup_unexecutable_plan_executes_nothing():
    a_plan = BringupPlan(steps=(), reason="无接口", ok=False)
    ops = FakeOps()
    result = run_bringup(a_plan, ops)
    assert result.ok is False
    assert result.failed_step is None
    assert "无接口" in result.detail
    assert ops.run_calls == [] and ops.wait_ip_calls == 0


def test_run_bringup_executor_exception_is_reported_not_swallowed():
    a_plan = plan(_state(), has_profile=True)
    ops = RaisingOps()
    result = run_bringup(a_plan, ops)
    assert result.ok is False
    assert result.failed_step == "connect"
    assert "OSError" in result.detail
    assert "netsh 启动失败" in result.detail


def test_run_bringup_step_objects_are_immutable_snapshots():
    step = BringupStep(kind="connect", command=("netsh", "wlan"))
    assert step.kind == "connect"


# --------------------------------------------------------------------------- #
# is_stable_for_failback：边界
# --------------------------------------------------------------------------- #
def test_failback_stable_exact_boundary():
    assert is_stable_for_failback(5, elapsed=120) is True
    assert is_stable_for_failback(5, elapsed=120.0) is True
    assert is_stable_for_failback(6, elapsed=300) is True


def test_failback_unstable_below_either_threshold():
    assert is_stable_for_failback(5, elapsed=119.9) is False
    assert is_stable_for_failback(4, elapsed=200) is False
    assert is_stable_for_failback(5, elapsed=0) is False
    assert is_stable_for_failback(0, elapsed=0) is False


def test_failback_custom_required_and_window():
    assert is_stable_for_failback(3, required=3, window_seconds=60,
                                  elapsed=60) is True
    assert is_stable_for_failback(3, required=3, window_seconds=60,
                                  elapsed=59) is False
    assert is_stable_for_failback(2, required=3, window_seconds=60,
                                  elapsed=120) is False


def test_failback_invalid_input_is_false_not_exception():
    assert is_stable_for_failback(None, elapsed=120) is False
    assert is_stable_for_failback("x", elapsed=120) is False
