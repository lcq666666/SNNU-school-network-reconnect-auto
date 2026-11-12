"""Per-machine campus proxy compatibility, with reversible explicit changes."""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import yaml

from campus_install import atomic_json

CAMPUS_HOSTS = ("202.117.144.205", "net.snnu.edu.cn")
CAMPUS_RULES = ("IP-CIDR,202.117.144.205/32,DIRECT,no-resolve", "DOMAIN,net.snnu.edu.cn,DIRECT")
DEFAULT_BYPASS = ";".join(["localhost", "127.*", "192.168.*", "10.*"] +
                          [f"172.{n}.*" for n in range(16, 32)] + ["<local>"])
BEGIN = "// BEGIN CAMPUS RECONNECT MANAGED DIRECT RULES v1"
END = "// END CAMPUS RECONNECT MANAGED DIRECT RULES v1"


def merge_bypass(value):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("系统绕过列表类型无效，未修改")
    entries = [part.strip() for part in value.split(";") if part.strip()]
    known = {part.lower() for part in entries}
    for host in CAMPUS_HOSTS:
        if host not in known:
            entries.append(host)
    return ";".join(entries)


def local_proxy_endpoint(values, explicit=None):
    """Only return an unambiguous local HTTP endpoint; never invent a port."""
    from campus_windows import LOCAL_HOSTS, parse_endpoint
    if explicit:
        endpoint = explicit
    else:
        if (not values.get("ProxyEnable", {}).get("value")
                or values.get("AutoConfigURL", {}).get("value")):
            return None
        endpoint = values.get("ProxyServer", {}).get("value", "")
        if isinstance(endpoint, str) and "=" in endpoint:
            entries = dict(part.split("=", 1) for part in endpoint.split(";") if "=" in part)
            candidates = {entries[key].strip() for key in ("http", "https") if key in entries}
            if len(candidates) != 1:
                return None
            endpoint = candidates.pop()
    host, port = parse_endpoint(endpoint)
    if host is None or host.lower() not in LOCAL_HOSTS:
        return None
    return (f"[{host}]:{port}" if ":" in host else f"{host}:{port}")


def _strings(value, label):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError(f"{label} 不是字符串列表，未修改")
    return value


def _prepend(values, additions):
    return list(additions) + [v for v in values if v not in additions]


def enhance_config(config):
    result = copy.deepcopy(config)
    result["rules"] = _prepend(_strings(result.get("rules"), "rules"), CAMPUS_RULES)
    for section in ("tun", "dns"):
        if result.get(section) is None:
            result[section] = {}
        if not isinstance(result[section], dict):
            raise ValueError(f"{section} 配置类型无效，未修改")
    tun, dns = result["tun"], result["dns"]
    tun["route-exclude-address"] = _prepend(
        _strings(tun.get("route-exclude-address"), "TUN 排除"), ["202.117.144.205/32"])
    mode = dns.get("fake-ip-filter-mode", "blacklist")
    if mode not in ("blacklist", "rule"):
        raise ValueError("DNS fake-ip 白名单/未知模式需要手动确认，未自动修改")
    entry = "DOMAIN,net.snnu.edu.cn,real-ip" if mode == "rule" else "net.snnu.edu.cn"
    dns["fake-ip-filter"] = _prepend(_strings(dns.get("fake-ip-filter"), "DNS 过滤"), [entry])
    return result


SCRIPT_BLOCK = r'''
// BEGIN CAMPUS RECONNECT MANAGED DIRECT RULES v1
main = (function(previous) {
  return function(config, profileName) {
    var c = previous(config, profileName);
    if (!c || typeof c !== "object" || Array.isArray(c)) return c;
    var list = function(v) { return v == null || (Array.isArray(v) && v.every(function(x) { return typeof x === "string"; })); };
    var mapping = function(v) { return v == null || (typeof v === "object" && !Array.isArray(v)); };
    if (!list(c.rules) || !mapping(c.tun) || !mapping(c.dns)) return c;
    var t = c.tun || {}, d = c.dns || {};
    var mode = d["fake-ip-filter-mode"] || "blacklist";
    if (!list(t["route-exclude-address"]) || !list(d["fake-ip-filter"]) || (mode !== "blacklist" && mode !== "rule")) return c;
    var prepend = function(old, values) { return values.concat((old || []).filter(function(x) { return values.indexOf(x) < 0; })); };
    c.rules = prepend(c.rules, ["IP-CIDR,202.117.144.205/32,DIRECT,no-resolve", "DOMAIN,net.snnu.edu.cn,DIRECT"]);
    t["route-exclude-address"] = prepend(t["route-exclude-address"], ["202.117.144.205/32"]);
    d["fake-ip-filter"] = prepend(d["fake-ip-filter"], [mode === "rule" ? "DOMAIN,net.snnu.edu.cn,real-ip" : "net.snnu.edu.cn"]);
    c.tun = t; c.dns = d;
    return c;
  };
})(main);
// END CAMPUS RECONNECT MANAGED DIRECT RULES v1
'''


def patch_script(source):
    if BEGIN in source or END in source:
        if source.count(BEGIN) != 1 or source.count(END) != 1:
            raise ValueError("已有校园规则标记异常，请先恢复备份")
        before, rest = source.split(BEGIN)
        _, after = rest.split(END)
        if after.strip():
            raise ValueError("校园规则后有用户脚本变更，请先手动确认")
        source = before.rstrip("\r\n")
    if re.search(r"\basync\s+function\s+main\s*\(", source):
        raise ValueError("异步全局脚本需要手动合并，未修改用户脚本")
    if not re.search(r"\bfunction\s+main\s*\(", source):
        raise ValueError("未识别标准 function main 脚本，未修改用户脚本")
    return source.rstrip("\r\n") + "\n" + SCRIPT_BLOCK


def read_yaml(path, *, raw=None):
    raw = Path(path).read_bytes() if raw is None else raw
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("代理配置过大，请手动检查")
    class UniqueLoader(yaml.SafeLoader):
        pass
    def mapping(loader, node):
        explicit = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = loader.construct_object(key_node)
            if not isinstance(key, (str, int)) or key in explicit:
                raise ValueError("YAML 有重复或复杂键，请手动确认")
            explicit.add(key)
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            result[key] = loader.construct_object(value_node)
        return result
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        value = yaml.load(raw.decode("utf-8-sig"), Loader=UniqueLoader)
    except (yaml.YAMLError, UnicodeError) as error:
        raise ValueError("代理 YAML 解析失败，未修改文件") from error
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("代理 YAML 顶层不是对象，未修改文件")
    return value


def discover_roots():
    roots = []
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if base:
            for folder in ("io.github.clash-verge-rev.clash-verge-rev", "io.github.clash-verge.clash-verge"):
                root = Path(base) / folder
                if (root / "verge.yaml").is_file() and root.resolve() not in roots:
                    roots.append(root.resolve())
    return roots


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _atomic_bytes(path, raw):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".campus-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Compatibility:
    def __init__(self, directory, read_registry, write_override, *, refresh, client_running):
        self.directory = Path(directory)
        self.read_registry = read_registry
        self.write_override = write_override
        self.refresh = refresh
        self.client_running = client_running

    @contextmanager
    def _lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = (self.directory / "proxy-compat.lock").open("a+b")
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            lock.close()
            raise ValueError("另一项代理配置操作正在进行，请稍后再试") from error
        try:
            yield
        finally:
            # The OS releases this handle lock even when the process crashes.
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def inspect(self, roots):
        values = self.read_registry()
        override = values.get("ProxyOverride", {}).get("value", "")
        missing = [host for host in CAMPUS_HOSTS if host.lower() not in
                   {v.strip().lower() for v in str(override).split(";")}]
        enabled = bool(values.get("ProxyEnable", {}).get("value"))
        lines = ["助手的认证请求默认直连；系统代理与 TUN 需要分别检查。",
                 "系统代理：" + ("已开启，保留本机端口配置" if enabled else "未开启"),
                 "系统绕过：" + ("缺少校园地址" if missing else "校园地址已配置")]
        if values.get("AutoConfigURL", {}).get("value"):
            lines.append("PAC：已配置。系统绕过不保证覆盖 PAC，请在 PAC 中为校园地址返回 DIRECT。助手不会改写 PAC。")
        if not roots:
            lines.append("未识别 Clash Verge 配置。便携版可选择配置目录；其他 Clash/Mihomo 可导出规则。其他 VPN/TUN 须按客户端确认。")
        for root in roots:
            lines.append(f"Clash Verge 配置：{root}")
            try:
                effective = read_yaml(Path(root) / "clash-verge.yaml")
                mode = effective.get("mode", "未知")
                if mode not in ("global", "rule", "direct"):
                    mode = "未知"
                lines.append("模式：" + ("全局（普通 DIRECT 规则不生效）" if mode == "global" else str(mode)))
                tun = effective.get("tun") or {}
                lines.append("TUN：" + ("配置为开启，需路由排除" if tun.get("enable") else "配置为关闭"))
                enhanced = enhance_config(effective)
                covered = all(effective.get(k) == enhanced[k] for k in ("rules", "tun", "dns"))
                lines.append("当前生成配置：" + ("包含校园规则；实际联网需检测" if covered else "校园规则不完整/尚未重载"))
            except (OSError, ValueError, TypeError, AttributeError):
                lines.append("当前生成配置不可确认，不能认定 TUN 已绕过。")
        lines.append("修复只写持久配置；重新启动代理客户端后再检查。此处不切断网络、不重启 UU。")
        if self.pending_backup() is not None:
            lines.append("上次代理事务未完成或刷新未确认，请先使用“恢复最近备份”，再进行新修复。")
        return "\n".join(lines)

    def _file_plan(self, root, *, require_stopped=True):
        if require_stopped and self.client_running():
            raise ValueError("请先从托盘退出 Clash Verge，再执行持久配置修复")
        root = Path(root).resolve()
        verge_path = root / "verge.yaml"
        originals = {}
        def snapshot(path):
            raw = path.read_bytes()
            originals[path] = raw
            return raw
        verge = read_yaml(verge_path, raw=snapshot(verge_path))
        profile_path = root / "profiles.yaml"
        profiles = read_yaml(profile_path, raw=snapshot(profile_path))
        items = profiles.get("items")
        if not isinstance(items, list):
            raise ValueError("未识别 Clash Verge 配置目录")
        matches = [i for i in items if isinstance(i, dict) and i.get("uid") == "Script" and i.get("type") == "script"]
        if len(matches) != 1 or not isinstance(matches[0].get("file"), str):
            raise ValueError("未找到 Clash Verge 全局 Script 配置，请使用规则导出")
        filename = matches[0]["file"]
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise ValueError("全局脚本路径无效")
        script_path = root / "profiles" / filename
        if not script_path.resolve().is_relative_to(root):
            raise ValueError("全局脚本路径越出配置目录")
        script_before = snapshot(script_path)
        effective_path = root / "clash-verge.yaml"
        if effective_path.is_file():
            enhance_config(read_yaml(effective_path, raw=snapshot(effective_path)))
        # Keep the client's default bypasses when converting to a custom list.
        existing = verge.get("system_proxy_bypass") or ""
        if verge.get("use_default_bypass", True):
            active = self.read_registry().get("ProxyOverride", {}).get("value", "")
            if not isinstance(active, str):
                raise ValueError("当前系统绕过列表类型异常，未修改")
            existing = DEFAULT_BYPASS + (";" + active if active else "") + (";" + existing if existing else "")
        verge["system_proxy_bypass"] = merge_bypass(existing)
        verge["use_default_bypass"] = False
        script = patch_script(script_before.decode("utf-8-sig"))
        return ([(verge_path, yaml.safe_dump(verge, allow_unicode=True, sort_keys=False).encode("utf-8")),
                 (script_path, script.encode("utf-8"))], originals)

    def persistent_ready(self, root):
        plans, originals = self._file_plan(root, require_stopped=False)
        return all(content == originals[path] for path, content in plans)

    def apply(self, root=None):
        with self._lock():
            if self.pending_backup() is not None:
                raise ValueError("上次代理修改未完成，请先恢复最近备份")
            plans, originals = self._file_plan(root) if root is not None else ([], {})
            values = self.read_registry()
            before = values.get("ProxyOverride")
            if before is not None and before.get("type") not in (1, 2):
                raise ValueError("系统绕过注册表类型异常，未修改")
            after = {"value": merge_bypass(before["value"] if before else ""),
                     "type": before["type"] if before else 1}
            files = [{"path": str(path), "before": base64.b64encode(originals[path]).decode("ascii"),
                      "after_hash": _digest(content)} for path, content in plans]
            backup = self.directory / "proxy-backups" / (uuid.uuid4().hex + ".json")
            journal = {"status": "applying", "root": str(root) if root else None,
                       "registry_before": before, "registry_after": after, "files": files}
            atomic_json(backup, journal)
            try:
                if root is not None and self.client_running():
                    raise ValueError("Clash Verge 已重新启动，请先退出客户端")
                if any(path.read_bytes() != raw for path, raw in originals.items()):
                    raise ValueError("代理配置发生并发变更，未覆盖")
                for (path, content), item in zip(plans, files):
                    if path.read_bytes() != base64.b64decode(item["before"]):
                        raise ValueError("代理文件发生并发变更，未覆盖")
                    _atomic_bytes(path, content)
                if self.read_registry().get("ProxyOverride") != before:
                    raise ValueError("系统绕过发生并发变更，未覆盖")
                self.write_override(after)
                if not self.refresh():
                    raise OSError("代理刷新失败，已尝试回滚，请重新检查")
                journal["status"] = "applied"
                atomic_json(backup, journal)
            except Exception:
                self._restore(journal, backup)
                raise
            return backup

    def _restore(self, journal, backup):
        # Check every target before restoring any, so a user's later edits survive.
        for item in journal["files"]:
            raw = Path(item["path"]).read_bytes()
            if raw != base64.b64decode(item["before"]) and _digest(raw) != item["after_hash"]:
                raise ValueError("代理文件已有后续变更，停止恢复；原始内容仍在备份中")
        current = self.read_registry().get("ProxyOverride")
        if current not in (journal["registry_before"], journal["registry_after"]):
            raise ValueError("系统绕过已有后续变更，停止恢复；原始内容仍在备份中")
        for item in journal["files"]:
            _atomic_bytes(Path(item["path"]), base64.b64decode(item["before"]))
        self.write_override(journal["registry_before"])
        refreshed = self.refresh()
        journal["status"] = "restored" if refreshed else "restored-refresh-pending"
        atomic_json(backup, journal)

    def restore(self, backup):
        with self._lock():
            backup = Path(backup).resolve()
            if not backup.is_relative_to((self.directory / "proxy-backups").resolve()):
                raise ValueError("只能恢复本助手创建的代理备份")
            journal = json.loads(backup.read_text(encoding="utf-8"))
            if journal.get("root") and self.client_running():
                raise ValueError("请先退出 Clash Verge 再恢复备份")
            self._restore(journal, backup)
            if journal["status"] == "restored-refresh-pending":
                raise OSError("备份已写回，但系统代理刷新失败，请重新检查或再次恢复")

    def latest_backup(self):
        backups = sorted((self.directory / "proxy-backups").glob("*.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for backup in backups:
            if json.loads(backup.read_text(encoding="utf-8")).get("status") in ("applied", "applying", "restored-refresh-pending"):
                return backup
        return None

    def pending_backup(self):
        for backup in (self.directory / "proxy-backups").glob("*.json"):
            if json.loads(backup.read_text(encoding="utf-8")).get("status") in ("applying", "restored-refresh-pending"):
                return backup
        return None


def export_rules(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "campus-direct.yaml").write_text(yaml.safe_dump(enhance_config({}), sort_keys=False), encoding="utf-8")
    (directory / "campus-direct.js").write_text(patch_script("function main(config) { return config; }"), encoding="utf-8")
    (directory / "README.txt").write_text(
        "校园认证规则（SNNU）\n\n"
        "Clash Verge Rev：优先用助手的持久修复。JS 文件也可作为全局扩展脚本使用；已有脚本时请在备份后合并，不能覆盖用户脚本。\n"
        "其他 Mihomo 客户端：将 YAML 中两条 rules 放在规则列表最前面；将 tun.route-exclude-address 和 dns.fake-ip-filter 合并进对应列表，保留原项目。此文件是片段，不能替换完整配置，也不能作为订阅导入。\n"
        "DNS fake-ip-filter-mode=rule 时，使用 DOMAIN,net.snnu.edu.cn,real-ip 放在过滤规则最前面；whitelist 模式需手动排除校园域名。\n"
        "普通 DIRECT 规则只在 rule 模式生效；全局模式需核对 TUN 路由排除。PAC 中为两校园地址返回 DIRECT。\n"
        "不要直接编辑订阅缓存或运行时生成文件；在客户端的全局覆写/脚本中合并以免订阅更新覆盖。重载后重新检查并访问校园认证页。其他 VPN/TUN 请按其客户端设置相同的地址排除。\n",
        encoding="utf-8")


def windows_compatibility():
    """Construct adapters without reading or changing the host during import."""
    from campus_install import data_directory
    from campus_resilience_windows import proxy_snapshot
    from campus_windows import PROXY_REG_PATH, default_refresh
    def read_registry():
        try:
            return proxy_snapshot()
        except FileNotFoundError:
            return {}
    def write_override(item):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, PROXY_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if item is None:
                try:
                    winreg.DeleteValue(key, "ProxyOverride")
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, "ProxyOverride", 0, item["type"], item["value"])
    def client_running():
        from tools.netdiag import PowerShellRunner
        code, output, _ = PowerShellRunner(timeout=5).run(
            "proxy_client_readonly", "Get-Process -ErrorAction Stop | Where-Object { $_.ProcessName -match '^(Clash Verge|clash-verge|clash-verge-rev|verge)$' } | Select-Object -ExpandProperty ProcessName | ConvertTo-Json -Compress")
        if code != 0:
            raise OSError("无法确认 Clash Verge 是否已退出，未修改持久配置")
        try:
            rows = json.loads(output) if output.strip() else []
            if isinstance(rows, str):
                rows = [rows]
            if rows is None:
                rows = []
            if not isinstance(rows, list) or any(not isinstance(v, str) for v in rows):
                raise ValueError("invalid process result")
            return bool(rows)
        except ValueError as error:
            raise OSError("无法确认 Clash Verge 是否已退出，未修改持久配置") from error
    return Compatibility(data_directory(), read_registry, write_override,
                         refresh=default_refresh, client_running=client_running)


def runtime_summary():
    from tools.netdiag import PowerShellRunner
    runner = PowerShellRunner(timeout=5)
    code, output, _ = runner.run(
        "proxy_tun_readonly", "Get-NetAdapter -ErrorAction Stop | Where-Object { $_.Status -eq 'Up' -and "
        "($_.InterfaceDescription -match 'Wintun|Mihomo|Clash|sing-box' -or $_.Name -match 'Mihomo|Clash|sing-box') } "
        "| Select-Object -ExpandProperty Name | ConvertTo-Json -Compress")
    try:
        adapters = json.loads(output) if output.strip() else []
        if code != 0:
            return "其他代理/TUN：无法确认网卡运行状态，请在其客户端核对。"
        if adapters:
            return "检测到活动的代理虚拟网卡：系统代理绕过不能单独覆盖 TUN，请核对路由排除。"
        code, output, _ = runner.run(
            "proxy_core_readonly", "Get-Process -ErrorAction Stop | Where-Object { $_.ProcessName -match 'mihomo|^clash|sing-box' } "
            "| Select-Object -ExpandProperty ProcessName | ConvertTo-Json -Compress")
        if code != 0:
            return "其他代理/TUN：无法确认代理进程状态，请在其客户端核对。"
        processes = json.loads(output) if output.strip() else []
        if processes:
            return "检测到代理内核进程：请核对客户端配置，网卡检查未确认 TUN 正在承载流量。"
        return "未发现已知代理内核或活动代理虚拟网卡；此检查不能排除其他 VPN。"
    except (ValueError, AttributeError):
        return "其他代理/TUN：读取结果不可确认，请在其客户端核对。"
