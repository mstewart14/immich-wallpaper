"""Paths, configuration and rotation state shared by every module.

rotate.py, tray_app.py and config_ui.py agree on where things live and how
they are stored by going through this module.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
from pathlib import Path
from typing import IO, Any

APP_NAME = "immich-wallpaper"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = Path.home() / ".cache" / APP_NAME
IMAGES_DIR = CACHE_DIR / "images"
STATE_PATH = CACHE_DIR / "state.json"
LOG_PATH = CACHE_DIR / "rotate.log"
# Random access token of the running settings page; see config_ui.py.
UI_TOKEN_PATH = CACHE_DIR / "ui-token"

# Port the config UI listens on by default; the tray probes it to decide
# whether to reuse a running UI or start a new one.
CONFIG_UI_PORT = 8877

# Everything we store is private to the user: the config holds the API key,
# and the cache holds photos from a personal library plus logs.
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

DEFAULT_CONFIG: dict[str, Any] = {
    "immich_url": "",
    "api_key": "",
    "interval_minutes": 5,
    "keep_count": 2,
    "albums": [],
    "people": [],
    "person_match": "any",
    "show_photo_info": False,
    "show_date_overlay": False,
    # With more than one monitor: "same" (one photo everywhere, each screen
    # drawn at its own size), "different" (each screen its own photos) or
    # "span" (one mosaic across the monitors).
    "multi_monitor_mode": "same",
    # Connector names of the monitors to change; empty means all of them.
    "monitors": [],
    # Most photos on one screen. 1 never pairs; 2 pairs portraits (today's
    # behaviour); more fills a wide screen with several portraits.
    "max_photos_per_screen": 2,
}


# --------------------------------------------------------------------------
# Private storage
# --------------------------------------------------------------------------
def ensure_private_dir(path: Path) -> None:
    """Create `path` (and parents) if needed, readable by the owner only.

    An existing directory is tightened too, so a cache created before this
    was enforced stops being readable by other users.
    """
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)


def open_private(path: Path, exclusive: bool = False) -> IO[bytes]:
    """Open `path` for binary writing, created readable by the owner only.

    The permissions are set when the file is created, so there is no
    moment at which it exists with looser ones. With `exclusive` it is an
    error for the file to exist already.
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    return os.fdopen(descriptor, "wb")


def write_private(path: Path, text: str) -> None:
    """Atomically replace `path` with `text`, readable by the owner only."""
    temp_path = path.with_name(path.name + ".tmp")
    with open_private(temp_path) as handle:
        handle.write(text.encode())
    temp_path.replace(path)


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
    ensure_private_dir(CONFIG_DIR)
    write_private(CONFIG_PATH, json.dumps(config, indent=2))


# --------------------------------------------------------------------------
# Rotation state
# --------------------------------------------------------------------------
# `history` runs oldest -> newest; each entry has kind, path, assets[],
# size_bytes and created_at. `position` is the index into `history` that is
# currently applied to the desktop (-1 when there is no history yet).
DEFAULT_STATE: dict[str, Any] = {
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
    ensure_private_dir(CACHE_DIR)
    write_private(STATE_PATH, json.dumps(state))


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


def entry_files(entry: dict[str, Any]) -> list[str]:
    """Every image file a history entry owns, without duplicates.

    An entry has a main `path` and, when it was built for several monitors,
    an `images` mapping of monitor name to its own file.
    """
    paths = [entry["path"], *(entry.get("images") or {}).values()]
    return list(dict.fromkeys(paths))


def can_go_back(state: dict[str, Any] | None = None) -> bool:
    """Whether there is an older wallpaper in the history to step back to."""
    state = state or load_state()
    return (state.get("position") or 0) > 0


def can_go_forward(state: dict[str, Any] | None = None) -> bool:
    """Whether there is a newer wallpaper in the history to step forward to."""
    state = state or load_state()
    history = state.get("history") or []
    return state.get("position", -1) < len(history) - 1
