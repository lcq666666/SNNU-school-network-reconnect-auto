# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建配置（F22）。

- 嵌入 Windows 版本资源（版本 + 源码 commit），使 exe 可对照源码；
- 依赖 ``tools/build_info.py`` 生成版本文件，避免版本号出现第二来源。
"""
import os
import sys

from PyInstaller.utils.hooks import collect_all

# 从 spec 所在目录向上找到项目根（含 campus_core.py），避免依赖调用时的 cwd。
_SPEC_DIR = os.path.abspath(SPECPATH or ".")
while _SPEC_DIR and not os.path.exists(os.path.join(_SPEC_DIR, "campus_core.py")):
    _parent = os.path.dirname(_SPEC_DIR)
    if _parent == _SPEC_DIR:
        break
    _SPEC_DIR = _parent
if _SPEC_DIR not in sys.path:
    sys.path.insert(0, _SPEC_DIR)

from tools import build_info  # noqa: E402

_version_file = build_info.write_version_file(
    os.path.join(_SPEC_DIR, "build", "version_info.txt"))
print("CampusReconnect version file:", _version_file)

datas = []
binaries = []
hiddenimports = ['pystray._win32']
tmp_ret = collect_all('keyring')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pystray')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['campus_reconnect_tray.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CampusReconnect',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_file,
)
