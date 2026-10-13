# -*- coding: utf-8 -*-
"""受控构建脚本（F22）。

用法::

    .venv/Scripts/python.exe tools/build_release.py            # 测试 + ruff + 打包
    .venv/Scripts/python.exe tools/build_release.py --skip-checks

步骤：

1. 运行离线测试与 ``ruff``（``--skip-checks`` 可跳过，CI 中分别单独执行）；
2. 生成 Windows 版本资源文件（版本 + commit）；
3. 按 ``CampusReconnect.spec`` 调用 PyInstaller 构建 ``dist/CampusReconnect.exe``；
4. 写出 ``dist/build_info.json`` 与 ``dist/CampusReconnect.exe.sha256``（含大小）。

安全约定：不注入用户凭据，不上传日志 / 配置；只产出可核对的校验清单。
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import build_info  # noqa: E402

DIST_DIR = PROJECT_ROOT / "dist"
EXE_NAME = "CampusReconnect.exe"


def _run(args, **kwargs) -> int:
    print("+", " ".join(str(a) for a in args))
    return subprocess.run(args, cwd=str(PROJECT_ROOT), check=False, **kwargs).returncode


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_checks() -> int:
    """运行离线测试与静态检查（任一失败返回非 0）。"""
    if _run([sys.executable, "-m", "pytest"]) != 0:
        return 1
    return _run([sys.executable, "-m", "ruff", "check", "."])


def run_pyinstaller() -> int:
    """调用 PyInstaller 按 spec 构建。"""
    spec = PROJECT_ROOT / "CampusReconnect.spec"
    return _run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                 "--distpath", str(DIST_DIR), str(spec)])


def write_manifest() -> Path:
    """写出构建元数据与 exe 校验文件。"""
    exe = DIST_DIR / EXE_NAME
    build_info.write_build_info(DIST_DIR)
    if exe.is_file():
        digest = _sha256(exe)
        size = exe.stat().st_size
        (DIST_DIR / (EXE_NAME + ".sha256")).write_text(
            "%s  %s\nsize=%d\n" % (digest, EXE_NAME, size), encoding="utf-8")
        print("SHA256 %s  %s (%d 字节)" % (digest, EXE_NAME, size))
    else:
        print("警告：未找到 %s，仅写出 build_info.json" % exe)
    return DIST_DIR / "build_info.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CampusReconnect 受控构建")
    parser.add_argument("--skip-checks", action="store_true",
                        help="跳过测试与 ruff（CI 中单独执行时使用）")
    args = parser.parse_args(argv)

    if not args.skip_checks:
        code = build_checks()
        if code != 0:
            print("测试 / 静态检查未通过，终止构建。")
            return code

    version_file = build_info.write_version_file(PROJECT_ROOT / "build" / "version_info.txt")
    print("版本资源：", version_file)

    code = run_pyinstaller()
    if code != 0:
        return code
    write_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
