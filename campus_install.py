"""Stable per-user configuration and upgrade migration."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


def data_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CampusReconnect"


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".campus-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def migrate_config(target: Path, candidates) -> None:
    if target.exists():
        return
    for source in candidates:
        source = Path(source)
        if source == target or not source.is_file():
            continue
        try:
            value = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            atomic_json(target, value)
            shutil.copy2(source, target.with_suffix(".legacy-backup.json"))
            return


def previous_install_directory(command: str | None) -> Path | None:
    if not command:
        return None
    from campus_windows import split_command_line
    parts = split_command_line(command)
    if not parts:
        return None
    executable = Path(parts[0])
    # Only migrate our own registered executable, never arbitrary startup entries.
    if not executable.name.lower().startswith("campusreconnect"):
        return None
    return executable.parent
