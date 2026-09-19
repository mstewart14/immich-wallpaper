"""Settings logic shared by every front-end (GTK window, web page).

Everything a settings screen needs that is not drawing widgets lives here:
validating what the user entered, working out which server credentials a
request should use, asking Immich for albums and people, describing the
monitors, and saving the result and applying it. The front-ends stay thin,
so they cannot disagree about what is valid, and none of this needs a
display, a browser or a network port to test.
"""
from __future__ import annotations

import re
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any

import desktops
import immich_api
import settings

HERE = Path(__file__).resolve().parent

IMMICH_TIMEOUT_SECONDS = 15
# Applying a saved config runs a full rotation, which downloads photos.
APPLY_TIMEOUT_SECONDS = 90

PEOPLE_PAGE_SIZE = 250
# Safety valve on paging through people: 40 pages of 250 is 10k people.
MAX_PEOPLE_PAGES = 40
THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024

PERSON_MATCH_MODES = ("any", "all", "both")
MULTI_MONITOR_MODES = ("same", "different", "span")
PHOTOS_PER_SCREEN_RANGE = (1, 6)
WHOLE_NUMBER_KEYS = ("interval_minutes", "keep_count")
FLAG_KEYS = ("show_photo_info", "show_date_overlay")

MAX_TEXT_LENGTH = 200
MAX_SELECTED_ITEMS = 20000
MAX_MONITORS = 32
MONITOR_NAME = re.compile(r"[A-Za-z0-9 _.:/+-]{1,64}")


class SettingsError(ValueError):
    """A submitted setting is not acceptable."""


# --------------------------------------------------------------------------
# Validating and applying settings
# --------------------------------------------------------------------------
def _clean_selection(items: Any) -> list[dict[str, str]]:
    """Reduce chosen albums or people to their id and name."""
    if not isinstance(items, list):
        raise SettingsError("albums and people must be lists")
    return [
        {"id": item["id"][:MAX_TEXT_LENGTH],
         "name": str(item.get("name") or "")[:MAX_TEXT_LENGTH]}
        for item in items[:MAX_SELECTED_ITEMS]
        if (isinstance(item, dict) and isinstance(item.get("id"), str)
            and item["id"])
    ]


def _clean_monitor_names(names: Any) -> list[str]:
    """Keep only plausible connector names, without duplicates."""
    if not isinstance(names, list):
        raise SettingsError("monitors must be a list")
    cleaned: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if MONITOR_NAME.fullmatch(name) and name not in cleaned:
            cleaned.append(name)
    return cleaned[:MAX_MONITORS]


def _normalised_url(url: Any) -> str | None:
    """`url` validated and tidied, or None if it isn't usable."""
    try:
        return immich_api.validate_base_url(str(url or ""))
    except immich_api.UnsafeUrlError:
        return None


def _apply_server(config: dict[str, Any], body: dict[str, Any]) -> None:
    """Update the server URL and API key from a save request.

    A blank key means "keep the saved one", but only while the URL is
    unchanged: pointing the app at a different server without entering
    that server's key would otherwise send the old key to it.
    """
    old_url = _normalised_url(config.get("immich_url"))
    if "immich_url" in body:
        text = str(body["immich_url"]).strip()
        if text:
            try:
                config["immich_url"] = immich_api.validate_base_url(text)
            except immich_api.UnsafeUrlError as error:
                raise SettingsError(str(error)) from error
        else:
            config["immich_url"] = ""
    submitted_key = str(body.get("api_key") or "").strip()
    if submitted_key:
        config["api_key"] = submitted_key
    elif (config["immich_url"]
          and _normalised_url(config["immich_url"]) != old_url):
        raise SettingsError("Enter the API key for the new server URL.")


def _apply_numbers(config: dict[str, Any], body: dict[str, Any]) -> None:
    for key in WHOLE_NUMBER_KEYS:
        if key in body:
            try:
                config[key] = max(1, int(body[key]))
            except (TypeError, ValueError) as error:
                raise SettingsError(
                    f"{key} must be a whole number") from error
    if "max_photos_per_screen" in body:
        low, high = PHOTOS_PER_SCREEN_RANGE
        try:
            config["max_photos_per_screen"] = min(
                high, max(low, int(body["max_photos_per_screen"])))
        except (TypeError, ValueError) as error:
            raise SettingsError(
                "max_photos_per_screen must be a whole number") from error


def apply_settings(config: dict[str, Any], body: dict[str, Any]) -> None:
    """Merge a save request into `config`, or raise SettingsError.

    Unknown keys are ignored and every accepted value is validated, so the
    stored config can't be filled with arbitrary data.
    """
    _apply_server(config, body)
    _apply_numbers(config, body)
    for key in ("albums", "people"):
        if key in body and isinstance(body[key], list):
            config[key] = _clean_selection(body[key])
    if isinstance(body.get("monitors"), list):
        config["monitors"] = _clean_monitor_names(body["monitors"])
    for key, allowed in (("person_match", PERSON_MATCH_MODES),
                         ("multi_monitor_mode", MULTI_MONITOR_MODES)):
        if body.get(key) in allowed:
            config[key] = body[key]
    for key in FLAG_KEYS:
        if key in body:
            config[key] = bool(body[key])


def save_settings(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and save `body`, then run one rotation to show the change.

    Returns {"ok": True, "applied": bool}, or {"ok": False, "error": str}
    if a setting was rejected (in which case nothing is saved).
    """
    config = settings.load_config()
    try:
        apply_settings(config, body)
    except SettingsError as error:
        return {"ok": False, "error": str(error)}
    settings.save_config(config)
    return {"ok": True, "applied": run_rotation_now()}


def run_rotation_now() -> bool:
    """Run one forced rotation as a separate process. True if it succeeded."""
    try:
        result = subprocess.run(
            [sys.executable, str(HERE / "rotate.py"), "--once"],
            capture_output=True, text=True, timeout=APPLY_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------
# Talking to Immich on a front-end's behalf
# --------------------------------------------------------------------------
def resolve_credentials(entered: dict[str, Any]) -> tuple[str, str]:
    """The (server URL, API key) a request should use.

    A blank URL means the saved one. A blank key means the saved key, but
    only when the URL is the saved one, so the saved key is never sent to
    an address the user just typed. Raises SettingsError for an unusable
    URL or a missing key.
    """
    stored = settings.load_config()
    url_text = (str(entered.get("immich_url") or "").strip()
                or stored["immich_url"])
    key = str(entered.get("api_key") or "").strip()
    try:
        url = immich_api.validate_base_url(url_text)
    except immich_api.UnsafeUrlError as error:
        raise SettingsError(str(error)) from error
    if not key and url == _normalised_url(stored["immich_url"]):
        key = stored["api_key"]
    if not key:
        raise SettingsError("Server URL and API key are both required.")
    return url, key


def check_connection(url: str, key: str) -> dict[str, Any]:
    """Check that the server is reachable and the API key is accepted."""
    try:
        ping = immich_api.get_json(
            url, key, "/server/ping", timeout=IMMICH_TIMEOUT_SECONDS)
    except urllib.error.URLError as error:
        return {"ok": False,
                "error": f"Could not reach {url}: {error.reason}"}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": f"Could not reach server: {error}"}
    if not isinstance(ping, dict) or "res" not in ping:
        return {"ok": False, "error": "Server responded but not with a "
                                      "valid Immich ping."}
    try:
        albums = immich_api.get_json(
            url, key, "/albums", timeout=IMMICH_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            message = ("Server reachable, but the API key was rejected "
                       "(401/403). Check the key and its permissions.")
        else:
            message = ("Server reachable, but the auth check failed: "
                       f"HTTP {error.code}")
        return {"ok": False, "error": message}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": "Server reachable, but auth check "
                                      f"failed: {error}"}
    return {"ok": True,
            "album_count": len(albums) if isinstance(albums, list) else None}


def list_albums(url: str, key: str) -> dict[str, Any]:
    """The server's albums, sorted by name."""
    try:
        albums = immich_api.get_json(
            url, key, "/albums", timeout=IMMICH_TIMEOUT_SECONDS)
        listing = sorted(
            [{"id": album["id"],
              "name": album.get("albumName") or "(untitled album)",
              "count": album.get("assetCount", 0)} for album in albums],
            key=lambda album: album["name"].lower())
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": str(error)}
    return {"ok": True, "albums": listing}


def list_people(url: str, key: str) -> dict[str, Any]:
    """The server's people (all pages), named ones first."""
    people: list[dict] = []
    try:
        for page in range(1, MAX_PEOPLE_PAGES + 1):
            reply = immich_api.get_json(
                url, key,
                f"/people?page={page}&size={PEOPLE_PAGE_SIZE}"
                "&withHidden=true",
                timeout=IMMICH_TIMEOUT_SECONDS)
            batch = reply.get("people", [])
            people.extend(batch)
            if not reply.get("hasNextPage") or not batch:
                break
        listing = sorted(
            [{"id": person["id"],
              "name": person.get("name") or "(unnamed person)",
              "hidden": person.get("isHidden", False)} for person in people],
            key=lambda person: (person["name"] == "(unnamed person)",
                                person["name"].lower()))
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": str(error)}
    return {"ok": True, "people": listing}


def fetch_person_thumbnail(url: str, key: str, person_id: str) -> bytes:
    """A person's face thumbnail (JPEG bytes). Raises on any failure."""
    data, _ = immich_api.get_bytes(
        url, key, f"/people/{immich_api.quote_segment(person_id)}/thumbnail",
        timeout=IMMICH_TIMEOUT_SECONDS, max_bytes=THUMBNAIL_MAX_BYTES)
    return data


# --------------------------------------------------------------------------
# The monitors
# --------------------------------------------------------------------------
def describe_monitors() -> dict[str, Any]:
    """The connected monitors and whether each can be set on its own."""
    return {
        "ok": True,
        "monitors": [
            {"name": monitor.name, "x": monitor.x, "y": monitor.y,
             "width": monitor.width, "height": monitor.height,
             "primary": monitor.primary}
            for monitor in desktops.get_monitors()],
        "per_monitor": desktops.supports_monitor_wallpapers(),
    }
