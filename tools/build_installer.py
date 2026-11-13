"""Build the per-user Inno Setup installer around the portable EXE."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "installer" / "CampusReconnect.iss"


def find_iscc() -> str | None:
    found = shutil.which("ISCC.exe") or shutil.which("iscc.exe")
    if found:
        return found
    for candidate in (
            Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
            Path(r"C:\Program Files\Inno Setup 6\ISCC.exe")):
        if candidate.is_file():
            return str(candidate)
    return None


def build(version: str) -> Path | None:
    compiler = find_iscc()
    if compiler is None:
        return None
    command = [compiler, "/Qp", "/DMyAppVersion=" + version, str(SCRIPT)]
    subprocess.run(command, cwd=str(ROOT), check=True)
    return ROOT / "dist" / "CampusReconnectSetup.exe"


if __name__ == "__main__":
    from campus_core import __version__
    result = build(__version__)
    if result is None:
        raise SystemExit("ISCC.exe not found")
    print(result)
