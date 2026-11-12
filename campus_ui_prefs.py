"""Presentation-only UI preferences. Never read by the network state machine.

Stored in a file of its own so the existing config schema, credentials and
guard/autostart settings stay untouched across upgrades.
"""
from __future__ import annotations

import json
from pathlib import Path

from campus_install import atomic_json, data_directory

THEME_FILE = "ui-theme.json"
VALID_MODES = ("light", "dark")


def _path() -> Path:
    return Path(data_directory()) / THEME_FILE


def load_theme() -> str:
    """Read the saved appearance; fall back to light on any missing/bad value."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "light"
    mode = raw.get("theme") if isinstance(raw, dict) else None
    return mode if mode in VALID_MODES else "light"


def save_theme(mode: str) -> bool:
    """Persist an appearance choice. Returns False for invalid modes or IO errors."""
    if mode not in VALID_MODES:
        return False
    try:
        atomic_json(_path(), {"theme": mode})
    except OSError:
        return False
    return True
