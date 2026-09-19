"""Paths, configuration and rotation state shared by every module.

rotate.py, tray_app.py and config_ui.py agree on where things live and how
they are stored by going through this module.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import stat
from pathlib import Path
from typing import Any

APP_NAME = "immich-wallpaper"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = Path.home() / ".cache" / APP_NAME
IMAGES_DIR = CACHE_DIR / "images"
STATE_PATH = CACHE_DIR / "state.json"

# Port the config UI listens on by default; the tray probes it to decide
# whether to reuse a running UI or start a new one.
CONFIG_UI_PORT = 8877

DEFAULT_CONFIG = {
    "immich_url": "",
    "api_key": "",
    "interval_minutes": 5,
    "keep_count": 2,
    "albums": [],
    "people": [],
    "person_match": "any",
    "show_photo_info": False,
    "show_date_overlay": False,
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def read_stored_config() -> dict[str, Any]:
    """Return config.json exactly as stored, without applying defaults.

    Raises FileNotFoundError, OSError or json.JSONDecodeError if the file
    is missing or unreadable.
    """
    return json.loads(CONFIG_PATH.read_text())


def load_config() -> dict[str, Any]:
    """Load the config with defaults filled in for any missing keys.

    Never fails: a missing or corrupt file yields the defaults.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    with contextlib.suppress(json.JSONDecodeError, OSError):
        config.update(read_stored_config())
    return config


def save_config(config: dict[str, Any]) -> None:
    """Persist `config` atomically, readable by the owner only.

    The file holds the API key, hence the restricted permissions.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(config, indent=2))
    os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
    temp_path.replace(CONFIG_PATH)


# --------------------------------------------------------------------------
# Rotation state
# --------------------------------------------------------------------------
# `history` runs oldest -> newest; each entry has kind, path, assets[],
# size_bytes and created_at. `position` is the index into `history` that is
# currently applied to the desktop (-1 when there is no history yet).
DEFAULT_STATE = {
    "last_run": 0,
    "last_success": None,
    "last_error": None,
    "last_error_at": None,
    "paused": False,
    "history": [],
    "position": -1,
}


def load_state() -> dict[str, Any]:
    """Load the persisted rotation state.

    Falls back to the defaults if the state file is missing or unreadable.
    """
    # Deep copy: callers mutate the nested "history" list, and a shallow
    # copy would let that leak into DEFAULT_STATE and every later call.
    state = copy.deepcopy(DEFAULT_STATE)
    if STATE_PATH.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            state.update(json.loads(STATE_PATH.read_text()))
    return state


def save_state(state: dict[str, Any]) -> None:
    """Persist `state` atomically (write a temp file, then rename it)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state))
    temp_path.replace(STATE_PATH)


def set_paused(paused: bool) -> dict[str, Any]:
    """Pause or resume rotation and return the updated state."""
    state = load_state()
    state["paused"] = paused
    save_state(state)
    return state


def current_entry(state: dict[str, Any] | None = None) -> dict | None:
    """Return the history entry applied to the desktop, or None."""
    state = state or load_state()
    history = state.get("history") or []
    position = state.get("position", -1)
    if 0 <= position < len(history):
        return history[position]
    return None


def can_go_back(state: dict[str, Any] | None = None) -> bool:
    """Whether there is an older wallpaper in the history to step back to."""
    state = state or load_state()
    return (state.get("position") or 0) > 0


def can_go_forward(state: dict[str, Any] | None = None) -> bool:
    """Whether there is a newer wallpaper in the history to step forward to."""
    state = state or load_state()
    history = state.get("history") or []
    return state.get("position", -1) < len(history) - 1
