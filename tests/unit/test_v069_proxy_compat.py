"""Portable proxy compatibility: all files and registry operations are isolated."""
import copy

import pytest
import yaml

from campus_proxy_compat import (Compatibility, merge_bypass, enhance_config,
                                 patch_script, export_rules, CAMPUS_RULES, local_proxy_endpoint)


def fixture(tmp_path):
    root = tmp_path / "portable-verge"
    (root / "profiles").mkdir(parents=True)
    (root / "verge.yaml").write_text("use_default_bypass: true\nsystem_proxy_bypass: localhost\n", encoding="utf-8")
    (root / "profiles.yaml").write_text("items:\n- {uid: Script, type: script, file: Script.js}\n", encoding="utf-8")
    (root / "profiles" / "Script.js").write_text("function main(config) { return config; }", encoding="utf-8")
    reg = {"ProxyOverride": {"value": "<local>;old.example", "type": 1},
           "ProxyServer": {"value": "127.0.0.1:10999", "type": 1},
           "ProxyEnable": {"value": 1, "type": 4}}
    def write(item):
        if item is None:
            reg.pop("ProxyOverride", None)
        else:
            reg["ProxyOverride"] = copy.deepcopy(item)
    compat = Compatibility(tmp_path / "data", lambda: copy.deepcopy(reg), write,
                           refresh=lambda: True, client_running=lambda: False)
    return compat, root, reg


def test_bypass_preserves_custom_and_is_idempotent():
    result = merge_bypass("<local>;old.example;NET.SNNU.EDU.CN")
    assert "old.example" in result and result.count("202.117.144.205") == 1
    assert merge_bypass(result) == result
    assert result.lower().count("net.snnu.edu.cn") == 1


def test_tun_and_dns_preserve_existing_config_and_rule_priority():
    original = {"rules": ["MATCH,Proxy"], "tun": {"enable": True, "route-exclude-address": ["10.1.0.0/16"]},
                "dns": {"nameserver": ["1.1.1.1"], "fake-ip-filter": ["*.lan"]}}
    result = enhance_config(original)
    assert original["rules"] == ["MATCH,Proxy"]
    assert result["rules"][:2] == list(CAMPUS_RULES)
    assert "10.1.0.0/16" in result["tun"]["route-exclude-address"]
    assert result["dns"]["nameserver"] == ["1.1.1.1"]
    assert "*.lan" in result["dns"]["fake-ip-filter"]
    assert enhance_config(result) == result


def test_dns_rule_mode_uses_real_ip_rule():
    result = enhance_config({"dns": {"fake-ip-filter-mode": "rule", "fake-ip-filter": ["MATCH,fake-ip"]}})
    assert result["dns"]["fake-ip-filter"][0] == "DOMAIN,net.snnu.edu.cn,real-ip"


def test_whitelist_and_bad_types_fail_before_writes():
    with pytest.raises(ValueError):
        enhance_config({"dns": {"fake-ip-filter-mode": "whitelist"}})
    with pytest.raises(ValueError):
        enhance_config({"rules": "MATCH,Proxy"})


def test_managed_script_preserves_user_main_and_replaces_only_own_block():
    source = "function main(config) { config.userFlag = true; return config; }\n"
    updated = patch_script(source)
    assert source in updated
    assert patch_script(updated) == updated
    assert "route-exclude-address" in updated and "fake-ip-filter" in updated
    assert updated.count("BEGIN CAMPUS RECONNECT") == 1


def test_backup_restore_preserves_port_and_original_bytes(tmp_path):
    compat, root, reg = fixture(tmp_path)
    originals = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    registry_before = copy.deepcopy(reg)
    backup = compat.apply(root)
    assert reg["ProxyServer"] == registry_before["ProxyServer"]
    assert reg["ProxyEnable"] == registry_before["ProxyEnable"]
    assert "old.example" in reg["ProxyOverride"]["value"]
    modified = yaml.safe_load((root / "verge.yaml").read_text(encoding="utf-8"))
    assert modified["use_default_bypass"] is False
    assert "172.31.*" in modified["system_proxy_bypass"]
    compat.restore(backup)
    assert reg == registry_before
    assert all(p.read_bytes() == contents for p, contents in originals.items())


def test_refresh_failure_rolls_back_files_and_registry(tmp_path):
    compat, root, reg = fixture(tmp_path)
    original = (root / "verge.yaml").read_bytes()
    before = copy.deepcopy(reg)
    compat.refresh = lambda: False
    with pytest.raises(OSError):
        compat.apply(root)
    assert reg == before
    assert (root / "verge.yaml").read_bytes() == original


def test_restore_refuses_later_user_changes(tmp_path):
    compat, root, _ = fixture(tmp_path)
    backup = compat.apply(root)
    path = root / "profiles" / "Script.js"
    path.write_text("// User edited later", encoding="utf-8")
    with pytest.raises(ValueError, match="变更"):
        compat.restore(backup)
    assert path.read_text(encoding="utf-8") == "// User edited later"


def test_running_client_refuses_persistent_changes(tmp_path):
    compat, root, _ = fixture(tmp_path)
    compat.client_running = lambda: True
    with pytest.raises(ValueError, match="退出"):
        compat.apply(root)


def test_invalid_profile_path_does_not_escape_directory(tmp_path):
    compat, root, _ = fixture(tmp_path)
    (root / "profiles.yaml").write_text('items: [{uid: Script, type: script, file: ../../other.js}]', encoding="utf-8")
    with pytest.raises(ValueError):
        compat.apply(root)


def test_no_proxy_and_missing_override_can_restore_absence(tmp_path):
    compat, _, reg = fixture(tmp_path)
    reg.clear()
    backup = compat.apply()
    assert "202.117.144.205" in reg["ProxyOverride"]["value"]
    compat.restore(backup)
    assert reg == {}


def test_read_only_report_detects_pac_global_tun_and_does_not_leak(tmp_path):
    compat, root, reg = fixture(tmp_path)
    reg["AutoConfigURL"] = {"value": "https://pac.example/?secret=abc", "type": 1}
    (root / "clash-verge.yaml").write_text("mode: global\ntun: {enable: true}\nsecret: TOPSECRET\n", encoding="utf-8")
    report = compat.inspect([root])
    assert "PAC" in report and "全局" in report and "TUN" in report
    assert "TOPSECRET" not in report and "pac.example" not in report
    assert not compat.directory.exists()


def test_export_does_not_replace_existing_user_file(tmp_path):
    export_rules(tmp_path / "export")
    content = (tmp_path / "export" / "campus-direct.yaml").read_text(encoding="utf-8")
    assert "202.117.144.205/32" in content
    assert (tmp_path / "export" / "campus-direct.js").is_file()
    with pytest.raises(FileExistsError):
        export_rules(tmp_path / "export")


def test_ui_mutation_requires_pause_and_no_repair_in_flight():
    from types import SimpleNamespace
    import threading
    from campus_proxy_ui import mutation_allowed
    cfg = {"paused": False}
    app = SimpleNamespace(get_config=lambda: cfg, _repair_busy=threading.Event(),
                          _lifeline_busy=threading.Event())
    assert not mutation_allowed(app)
    cfg["paused"] = True
    assert mutation_allowed(app)
    app._lifeline_busy.set()
    assert not mutation_allowed(app)
    app._lifeline_busy.clear()
    app._clash_check_busy = threading.Event()
    app._clash_check_busy.set()
    assert not mutation_allowed(app)


def test_restore_reports_refresh_failure_and_keeps_retryable_backup(tmp_path):
    compat, _, reg = fixture(tmp_path)
    before = copy.deepcopy(reg)
    backup = compat.apply()
    compat.refresh = lambda: False
    with pytest.raises(OSError, match="刷新"):
        compat.restore(backup)
    assert reg == before
    assert compat.latest_backup() == backup


def test_invalid_yaml_and_duplicate_keys_do_not_modify_registry(tmp_path):
    compat, root, reg = fixture(tmp_path)
    before = copy.deepcopy(reg)
    (root / "verge.yaml").write_text("use_default_bypass: true\nuse_default_bypass: false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        compat.apply(root)
    assert reg == before


def test_multiple_repairs_restore_in_order_without_duplicate_rules(tmp_path):
    compat, root, reg = fixture(tmp_path)
    first = compat.apply(root)
    first_bytes = (root / "profiles" / "Script.js").read_bytes()
    second = compat.apply(root)
    assert (root / "profiles" / "Script.js").read_bytes() == first_bytes
    assert reg["ProxyOverride"]["value"].count("202.117.144.205") == 1
    compat.restore(second)
    assert compat.latest_backup() == first


@pytest.mark.parametrize("server, expected", [
    ("127.0.0.1:10999", "127.0.0.1:10999"),
    ("http=127.0.0.1:8118;https=127.0.0.1:8118", "127.0.0.1:8118"),
    ("http=127.0.0.1:8118;https=127.0.0.1:8119", None),
    ("[::1]:1080", "[::1]:1080"), ("remote.example:7897", None), ("", None),
])
def test_endpoint_detects_actual_local_port(server, expected):
    values = {"ProxyEnable": {"value": 1}, "ProxyServer": {"value": server}}
    assert local_proxy_endpoint(values) == expected
    values["AutoConfigURL"] = {"value": "https://example.invalid/pac"}
    assert local_proxy_endpoint(values) is None
    values.pop("AutoConfigURL")
    values["ProxyEnable"]["value"] = 0
    assert local_proxy_endpoint(values) is None


def test_unconfigured_computer_never_probes_default_proxy_port(monkeypatch):
    import logging
    import campus_reconnect_tray as tray
    import campus_resilience_windows as native
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG))
    monkeypatch.setattr(native, "proxy_snapshot", lambda: {})
    def forbidden(*args):
        raise AssertionError("No proxy exists; must not probe 7897")
    monkeypatch.setattr(tray, "probe_via_proxy", forbidden)
    app._dual_axis_clash_check(True)


def test_proxy_consent_does_not_overwrite_later_endpoint_choice(monkeypatch):
    import logging
    import campus_reconnect_tray as tray
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG))
    app.root = object()
    app._observed_proxy_endpoint = lambda: "127.0.0.1:8118"
    def answer(*args, **kwargs):
        app.update_config({"proxy_endpoint": "127.0.0.1:12345"})
        return True
    monkeypatch.setattr(app, "_ui_ask", answer)
    assert app._ask_proxy_consent() is None
    assert app.config["proxy_endpoint"] == "127.0.0.1:12345"


def test_compatibility_lock_rejects_second_writer_and_releases(tmp_path):
    compat, _, _ = fixture(tmp_path)
    with compat._lock():
        with pytest.raises(ValueError, match="正在进行"):
            with compat._lock():
                pass
    with compat._lock():
        pass


def test_proxy_dialog_is_renderable_without_host_io(tmp_path, monkeypatch):
    import tkinter as tk
    from types import SimpleNamespace
    import threading
    import campus_proxy_ui as ui
    root = tk.Tk()
    root.withdraw()
    compat, folder, _ = fixture(tmp_path)
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(ui, "windows_compatibility", lambda: compat)
    monkeypatch.setattr(ui, "discover_roots", lambda: [folder])
    monkeypatch.setattr(ui, "runtime_summary", lambda: "isolated runtime")
    monkeypatch.setattr(ui, "threading", SimpleNamespace(Thread=InlineThread))
    app = SimpleNamespace(root=root, _ui_post=lambda call: call(),
                          get_config=lambda: {"paused": True},
                          _repair_busy=threading.Event(), _lifeline_busy=threading.Event())
    try:
        ui.open_dialog(app)
        root.update_idletasks()
        assert app.proxy_compat_win.winfo_exists()
        texts = [w for w in app.proxy_compat_win.winfo_children()[0].winfo_children() if isinstance(w, tk.Text)]
        assert "系统绕过" in texts[0].get("1.0", "end")
    finally:
        root.destroy()


def test_real_readonly_adapter_scripts_pass_command_guard(monkeypatch):
    from tools import netdiag
    from campus_proxy_compat import windows_compatibility, runtime_summary
    calls = []
    class Runner:
        def __init__(self, **kwargs):
            pass
        def run(self, name, script):
            netdiag.assert_read_only(script)
            calls.append(name)
            return 0, '"clash-verge"' if name == "proxy_client_readonly" else "", ""
    monkeypatch.setattr(netdiag, "PowerShellRunner", Runner)
    assert windows_compatibility().client_running() is True
    assert "未发现" in runtime_summary()
    assert calls == ["proxy_client_readonly", "proxy_tun_readonly", "proxy_core_readonly"]


def test_atomic_file_write_failure_rolls_back_earlier_files(tmp_path, monkeypatch):
    import campus_proxy_compat as module
    compat, root, reg = fixture(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    registry_before = copy.deepcopy(reg)
    real_write = module._atomic_bytes
    calls = []
    def write(path, content):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("simulated second-file failure")
        real_write(path, content)
    monkeypatch.setattr(module, "_atomic_bytes", write)
    with pytest.raises(OSError):
        compat.apply(root)
    assert reg == registry_before
    assert all(p.read_bytes() == contents for p, contents in before.items())


def test_legacy_healing_restores_same_machine_proxy_and_journals_failures(tmp_path, monkeypatch):
    import json
    import logging
    import os
    import campus_reconnect_tray as tray
    import campus_resilience_windows as native
    from campus_clash_health import heal_plan, assess_dual_axis, ProxyAxisResult
    if os.name != "nt":
        pytest.skip("Windows adapter")
    app = tray.TrayApp(logging.getLogger("campus"), config=dict(tray.DEFAULT_CONFIG, clash_auto_proxy=True))
    original = {"ProxyServer": {"value": "127.0.0.1:8118", "type": 1},
                "AutoConfigURL": {"value": "private PAC URL", "type": 1},
                "ProxyEnable": {"value": 1, "type": 4}}
    calls = []
    monkeypatch.setattr(tray, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(tray, "_REAL_SLEEP", lambda seconds: None)
    monkeypatch.setattr(native, "proxy_snapshot", lambda: copy.deepcopy(original))
    monkeypatch.setattr(native, "proxy_disable", lambda: calls.append("off"))
    monkeypatch.setattr(native, "proxy_restore", lambda item: calls.append(item))
    plan = heal_plan(assess_dual_axis(True, ProxyAxisResult(proxy_ok=False, proxy_error="timeout")), attempts=0)
    assert app._clash_heal_execute(plan)
    assert calls == ["off", original]
    def fail_restore(item):
        raise OSError("simulated restore failure")
    monkeypatch.setattr(native, "proxy_restore", fail_restore)
    assert not app._clash_heal_execute(plan)
    state = json.loads((tmp_path / "recovery.json").read_text(encoding="utf-8"))
    assert state["proxy_before"] == original


def test_yaml_standard_merge_keys_preserve_explicit_override(tmp_path):
    from campus_proxy_compat import read_yaml
    path = tmp_path / "merged.yaml"
    path.write_text("defaults: &defaults\n  mtu: 1500\ntun:\n  <<: *defaults\n  mtu: 9000\n", encoding="utf-8")
    assert read_yaml(path)["tun"]["mtu"] == 9000


def test_interrupted_transaction_blocks_new_repair_until_restored(tmp_path):
    import json
    compat, root, _ = fixture(tmp_path)
    backup = compat.apply(root)
    state = json.loads(backup.read_text(encoding="utf-8"))
    state["status"] = "applying"
    backup.write_text(json.dumps(state), encoding="utf-8")
    assert "上次代理事务未完成" in compat.inspect([root])
    with pytest.raises(ValueError, match="先恢复"):
        compat.apply(root)
    compat.restore(backup)
    assert compat.pending_backup() is None
    compat.apply(root)
