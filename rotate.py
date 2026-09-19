#!/usr/bin/env python3
"""Rotate the desktop wallpaper with photos pulled from Immich.

Photos are filtered by the albums/people chosen in the config UI. Two
portrait photos are paired side-by-side into a single composite when the
screen resolution can be detected; landscape/square photos are shown
singly. A bounded history of recent wallpapers is kept on disk (oldest
deleted as new ones arrive) so the tray app can step back/forward through
what it has shown.

Meant to be triggered repeatedly (e.g. every 15-30s) by the tray app; it
self-paces against config.json's interval_minutes via a state file, so
callers can invoke it often without worrying about over-rotating.

Desktop support: auto-detects KDE Plasma (via D-Bus scripting, no
QtWebEngine involved) and XFCE (via xfconf-query / xrandr). Only Pillow is
a non-stdlib dependency, needed for the portrait-pairing composite; it is
imported lazily so the control commands work without it.

Usage:
    python3 rotate.py             # normal run: no-ops if not due yet
    python3 rotate.py --once      # ignore the interval gate, rotate now
    python3 rotate.py --pause
    python3 rotate.py --resume
    python3 rotate.py --status
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import sys
import time
import urllib.error
import uuid
from datetime import datetime
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import desktops
import immich_api
import settings

logger = logging.getLogger(__name__)

EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/heic": ".heic", "image/heif": ".heif", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/tiff": ".tiff",
}

DOWNLOAD_TIMEOUT_SECONDS = 60

MIN_INTERVAL_SECONDS = 60
MIN_KEEP_COUNT = 2

LOG_FORMAT = "[%(asctime)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Failures are also appended to settings.LOG_PATH, since the tray discards
# this program's output; the file is capped so it can't grow without bound.
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUP_COUNT = 1
# Longest response-header value written to the log.
LOG_HEADER_VALUE_LIMIT = 120

# How many candidates one /search/random call asks Immich for.
RANDOM_BATCH_SIZE = 12

# Pixel gap between the two photos of a side-by-side portrait pair.
PAIR_GAP_PX = 6
JPEG_QUALITY = 92

# EXIF orientation values that rotate the image a quarter turn, swapping
# its stored width and height.
EXIF_QUARTER_TURN_ORIENTATIONS = (5, 6, 7, 8)
# A photo counts as portrait/landscape only if one side is at least this
# much longer than the other; anything closer is treated as square.
ASPECT_RATIO_TOLERANCE = 1.05


# --------------------------------------------------------------------------
# Config / history navigation
# --------------------------------------------------------------------------
def load_required_config() -> dict[str, Any]:
    """Read config.json as stored, exiting if it is missing/incomplete.

    Unlike settings.load_config() this applies no defaults: an incomplete
    config means setup was never finished, so a rotation can't proceed.
    """
    if not settings.CONFIG_PATH.exists():
        logger.error("No config at %s. Run the config UI and save a "
                     "configuration first.", settings.CONFIG_PATH)
        sys.exit(1)
    config = settings.read_stored_config()
    if not config.get("immich_url") or not config.get("api_key"):
        logger.error("Config is missing immich_url or api_key. "
                     "Run the config UI to finish setup.")
        sys.exit(1)
    return config


def navigate(direction: int) -> bool:
    """Apply the previous (-1) or next (+1) wallpaper from the history.

    Returns True if the wallpaper moved, False if there was nowhere to go
    or the target image is no longer on disk.
    """
    state = settings.load_state()
    history = state.get("history") or []
    if not history:
        return False
    position = state.get("position", len(history) - 1)
    new_position = position + direction
    if new_position < 0 or new_position >= len(history):
        return False
    entry = history[new_position]
    path = Path(entry["path"])
    if not path.exists():
        return False
    entry["wallpaper_applied"] = desktops.set_wallpaper(path)
    state["position"] = new_position
    settings.save_state(state)
    return True


def get_asset_details(config: dict, asset_id: str) -> dict | None:
    """Fetch the full asset record (exifInfo + people), or None on failure.

    Used only for the on-image caption: /search/random's per-asset payload
    isn't reliably this complete, so this is one extra small GET per chosen
    photo.
    """
    try:
        return immich_api.get_json(
            config["immich_url"], config["api_key"], f"/assets/{asset_id}")
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        # URLError also covers HTTPError.
        return None


def pick_image_batch(
    config: dict, size: int = RANDOM_BATCH_SIZE,
) -> list[dict]:
    """Ask Immich for `size` random image assets matching the config.

    One /search/random call, filtered by album/person selections, asking
    for exif so orientation can be judged without downloading anything.

    Immich's personIds filter is an AND (asset must contain every listed
    person). person_match controls how multiple selected people combine:
      "any"  -- each call filters to one randomly-chosen person, so over
                many rotations you get an OR across the whole group.
      "all"  -- every call requires all of them together (the raw AND).
      "both" -- each call is a coin flip between the two, so you get a
                genuine blend of solo and together photos over time.
    """
    body: dict[str, Any] = {"size": size, "withExif": True}
    album_ids = [album["id"] for album in config.get("albums", [])]
    people = config.get("people", [])
    if album_ids:
        body["albumIds"] = album_ids
    if people:
        person_match = config.get("person_match", "any")
        require_all = person_match == "all" or (
            person_match == "both" and random.random() < 0.5)
        if require_all or len(people) == 1:
            body["personIds"] = [person["id"] for person in people]
        else:
            body["personIds"] = [random.choice(people)["id"]]

    assets = immich_api.post_json(
        config["immich_url"], config["api_key"], "/search/random", body)
    if not assets:
        return []
    return [
        asset for asset in assets
        if (asset.get("originalMimeType") or "").startswith("image/")
    ]


def classify_orientation(asset: dict) -> str:
    """Classify an asset as portrait, landscape, square or unknown.

    Accounts for EXIF rotation; returns one of those four words.
    """
    exif = asset.get("exifInfo") or {}
    width, height = exif.get("exifImageWidth"), exif.get("exifImageHeight")
    if not width or not height:
        return "unknown"
    try:
        orientation = int(exif.get("orientation") or 1)
    except (TypeError, ValueError):
        orientation = 1
    if orientation in EXIF_QUARTER_TURN_ORIENTATIONS:
        width, height = height, width
    if height > width * ASPECT_RATIO_TOLERANCE:
        return "portrait"
    if width > height * ASPECT_RATIO_TOLERANCE:
        return "landscape"
    return "square"


def choose_assets_for_rotation(config: dict, allow_pair: bool) -> list[dict]:
    """Pick the asset(s) for one rotation.

    Two portraits when `allow_pair` and a second portrait is available,
    otherwise a single asset. Returns an empty list if Immich had no
    matching images.
    """
    batch = pick_image_batch(config)
    if not batch:
        return []
    random.shuffle(batch)
    first = batch[0]
    if allow_pair and classify_orientation(first) == "portrait":
        for candidate in batch[1:]:
            if (candidate["id"] != first["id"]
                    and classify_orientation(candidate) == "portrait"):
                return [first, candidate]
    return [first]


def download_asset_bytes(
    config: dict, asset: dict,
) -> tuple[bytes, str | None]:
    """Download the original file for `asset`; see immich_api.get_bytes()."""
    return immich_api.get_bytes(
        config["immich_url"], config["api_key"],
        f"/assets/{asset['id']}/original", timeout=DOWNLOAD_TIMEOUT_SECONDS)


def asset_meta(config: dict, asset: dict) -> dict[str, Any]:
    """Return the subset of an asset's fields kept in the history entry."""
    return {
        "id": asset["id"],
        "original_filename": asset.get("originalFileName"),
        "web_url": config["immich_url"].rstrip("/") + f"/photos/{asset['id']}",
    }


# --------------------------------------------------------------------------
# Image composition (only used for the 2-portrait side-by-side layout)
# --------------------------------------------------------------------------
def _load_oriented(data: bytes):
    """Decode image `data` to an upright RGB PIL image.

    EXIF rotation is applied.
    """
    from PIL import Image, ImageOps
    image = Image.open(BytesIO(data))
    image = ImageOps.exif_transpose(image)
    return image.convert("RGB")


def _contain_resize(image, target_width: int, target_height: int):
    """Scale `image` to fit entirely within the target size.

    No cropping -- the caller's canvas shows through as letterbox bars
    around it.
    """
    from PIL import Image
    scale = min(target_width / image.width, target_height / image.height)
    new_width = max(1, round(image.width * scale))
    new_height = max(1, round(image.height * scale))
    return image.resize((new_width, new_height), Image.LANCZOS)


def _letterbox_single(
    image, target_width: int, target_height: int, background=(0, 0, 0),
):
    """Center `image` on a canvas of the target size (see _contain_resize)."""
    from PIL import Image
    canvas = Image.new("RGB", (target_width, target_height), background)
    fitted = _contain_resize(image, target_width, target_height)
    canvas.paste(fitted, ((target_width - fitted.width) // 2,
                          (target_height - fitted.height) // 2))
    return canvas


def compose_pair(
    data_left: bytes, data_right: bytes, target_width: int,
    target_height: int, gap: int = PAIR_GAP_PX, background=(0, 0, 0),
):
    """Place two photos side by side on one canvas of the target size.

    Each photo is letterboxed inside its own half.
    """
    from PIL import Image
    canvas = Image.new("RGB", (target_width, target_height), background)
    left_width = (target_width - gap) // 2
    right_width = target_width - gap - left_width

    left = _contain_resize(_load_oriented(data_left),
                           left_width, target_height)
    canvas.paste(left, ((left_width - left.width) // 2,
                        (target_height - left.height) // 2))

    right = _contain_resize(_load_oriented(data_right),
                            right_width, target_height)
    right_x = left_width + gap + (right_width - right.width) // 2
    canvas.paste(right, (right_x, (target_height - right.height) // 2))
    return canvas


# --------------------------------------------------------------------------
# On-image overlays: per-photo caption (date / location / people) and a
# today's-date corner overlay -- both "faked" at compose time since this is
# a static wallpaper, not a live web page like Immich Kiosk (which these
# are modeled on). Off by default; baked into the JPEG at each rotation.
# --------------------------------------------------------------------------
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
]

# One-pixel offsets in all eight directions, used to fake a text outline.
_OUTLINE_OFFSETS = (
    (-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1),
)

# Distance kept clear of every screen edge, on top of any taskbar/panel.
EDGE_MARGIN_INCHES = 0.5


def _load_font(size: int):
    """Load the first available known TrueType font at `size` pixels.

    Falls back to Pillow's built-in font if none of them can be opened.
    """
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()  # older Pillow: fixed small size


def _draw_outlined_text(
    draw, position: tuple[int, int], text: str, font,
    fill=(190, 190, 190), outline=(0, 0, 0),
) -> None:
    """Draw `text` at `position` in `fill` with a one-pixel `outline`."""
    x, y = position
    for offset_x, offset_y in _OUTLINE_OFFSETS:
        draw.text((x + offset_x, y + offset_y), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def _format_taken_date(iso_timestamp: str | None) -> str | None:
    """Format an ISO-8601 timestamp as e.g. "Aug 29, 2022", or None."""
    if not iso_timestamp:
        return None
    try:
        taken = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return taken.strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return None


def photo_caption_lines(details: dict | None) -> list[str]:
    """Build caption lines from a full asset record, top to bottom.

    The lines are people names, location and date taken. Missing parts are
    skipped; the list is empty if there is nothing to show.
    """
    if not details:
        return []
    exif = details.get("exifInfo") or {}
    lines = []
    visible_names = [
        person["name"] for person in (details.get("people") or [])
        if person.get("name") and not person.get("isHidden")
    ]
    if visible_names:
        lines.append(", ".join(visible_names))
    place_parts = (exif.get("city"), exif.get("state") or exif.get("country"))
    location = ", ".join(part for part in place_parts if part)
    if location:
        lines.append(location)
    date_taken = _format_taken_date(exif.get("dateTimeOriginal"))
    if date_taken:
        lines.append(date_taken)
    return lines


def edge_margin_px() -> int:
    """Return the base margin kept clear of screen edges, in pixels."""
    return round(desktops.get_screen_dpi() * EDGE_MARGIN_INCHES)


def draw_caption(
    canvas, lines: list[str], region_left: int, region_right: int,
    region_bottom: int, corner: str = "left", extra_x: int = 0,
    extra_bottom: int = 0,
) -> None:
    """Draw caption `lines` bottom-anchored inside a horizontal region.

    The region [region_left, region_right] is separate from the whole
    canvas so pair captions stay against their own half. Lines are
    right-aligned if `corner` is "right". `extra_x`/`extra_bottom` pad past
    a detected taskbar/panel on top of the base EDGE_MARGIN_INCHES -- see
    screen_insets().
    """
    if not lines:
        return
    from PIL import ImageDraw
    draw = ImageDraw.Draw(canvas)
    font_size = max(13, round(canvas.height * 0.022))
    font = _load_font(font_size)
    margin = edge_margin_px()
    line_gap = max(2, font_size // 6)
    measured = [(line, draw.textbbox((0, 0), line, font=font))
                for line in lines]
    total_height = (
        sum(box[3] - box[1] for _, box in measured)
        + line_gap * (len(lines) - 1)
    )
    y = region_bottom - (margin + extra_bottom) - total_height
    for line, box in measured:
        text_width = box[2] - box[0]
        if corner == "right":
            x = region_right - (margin + extra_x) - text_width
        else:
            x = region_left + margin + extra_x
        _draw_outlined_text(draw, (x, y), line, font)
        y += (box[3] - box[1]) + line_gap


def draw_date_overlay(canvas, extra_x: int = 0, extra_top: int = 0) -> None:
    """Draw today's date, top-left.

    Baked in at rotation time -- see the overlay note above on why this
    isn't a live clock. `extra_x`/`extra_top` pad past a detected
    taskbar/panel on top of the base EDGE_MARGIN_INCHES -- see
    screen_insets().
    """
    from PIL import ImageDraw
    draw = ImageDraw.Draw(canvas)
    font_size = max(15, round(canvas.height * 0.026))
    font = _load_font(font_size)
    margin = edge_margin_px()
    _draw_outlined_text(
        draw, (margin + extra_x, margin + extra_top),
        time.strftime("%A, %B %-d"), font)


# --------------------------------------------------------------------------
# Building the wallpaper file
# --------------------------------------------------------------------------
def _draw_photo_caption(
    canvas, config: dict, asset: dict, region_left: int, region_right: int,
    corner: str, insets: dict[str, int],
) -> None:
    """Draw `asset`'s caption in the bottom `corner` of a canvas region.

    Padded to stay clear of the taskbar on that side.
    """
    lines = photo_caption_lines(get_asset_details(config, asset["id"]))
    edge_inset = insets["right"] if corner == "right" else insets["left"]
    draw_caption(canvas, lines, region_left, region_right, canvas.height,
                 corner=corner, extra_x=edge_inset,
                 extra_bottom=insets["bottom"])


def _compose_pair_wallpaper(
    config: dict, assets: list[dict], screen_size: tuple[int, int],
    show_info: bool, show_date: bool,
):
    """Side-by-side canvas for two portrait assets, with optional overlays."""
    insets = desktops.screen_insets(screen_size)
    data_left, _ = download_asset_bytes(config, assets[0])
    data_right, _ = download_asset_bytes(config, assets[1])
    canvas = compose_pair(data_left, data_right, *screen_size)
    if show_info:
        screen_width = screen_size[0]
        left_width = (screen_width - PAIR_GAP_PX) // 2
        _draw_photo_caption(canvas, config, assets[0], 0, left_width,
                            "left", insets)
        _draw_photo_caption(canvas, config, assets[1],
                            left_width + PAIR_GAP_PX, screen_width,
                            "right", insets)
    if show_date:
        draw_date_overlay(
            canvas, extra_x=insets["left"], extra_top=insets["top"])
    return canvas


def _compose_single(
    data: bytes, config: dict, asset: dict,
    screen_size: tuple[int, int] | None, show_info: bool, show_date: bool,
):
    """Canvas for one photo: letterboxed to the screen, overlays drawn on.

    Letterboxing onto the real screen size (when known) means the desktop's
    own fill mode never matters -- some crop to fill, and they don't agree
    -- and also puts the taskbar insets, measured in real screen pixels, in
    the same coordinate space as what's drawn here.

    Returns None if the photo can't be decoded (e.g. HEIC without a Pillow
    plugin), so the caller can fall back to the original file.
    """
    try:
        canvas = _load_oriented(data)
        if screen_size:
            canvas = _letterbox_single(canvas, *screen_size)
        insets = desktops.screen_insets(screen_size)
        if show_info:
            _draw_photo_caption(canvas, config, asset, 0, canvas.width,
                                "left", insets)
        if show_date:
            draw_date_overlay(
                canvas, extra_x=insets["left"], extra_top=insets["top"])
    except (ImportError, OSError, ValueError) as error:
        logger.warning("Could not decode %s (%s); using the original file "
                       "instead", asset.get("originalFileName"), error)
        return None
    return canvas


def _save_original_file(
    asset: dict, data: bytes, content_type: str | None, stem: str,
) -> Path:
    """Save a photo's original bytes untouched, keeping its extension."""
    extension = Path(asset.get("originalFileName", "")).suffix.lower()
    if not extension or len(extension) > 6:
        extension = EXT_BY_MIME.get(
            asset.get("originalMimeType"),
            EXT_BY_MIME.get(content_type, ".jpg"))
    path = settings.IMAGES_DIR / f"{stem}{extension}"
    path.write_bytes(data)
    return path


def build_wallpaper_entry(
    config: dict, assets: list[dict], screen_size: tuple[int, int] | None,
) -> dict[str, Any]:
    """Download/compose the wallpaper for `assets` as a history entry.

    The entry holds kind, path, assets, size_bytes and created_at.

    Two assets become a side-by-side pair. A single photo is letterboxed to
    the screen (and gets any overlays) as a JPEG. The original file is saved
    as-is only when there is nothing to draw against -- the screen size is
    unknown and no overlay is enabled -- or when it can't be decoded.
    """
    settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    # Timestamp plus random suffix: unique even across same-second calls.
    stem = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    show_info = bool(config.get("show_photo_info"))
    show_date = bool(config.get("show_date_overlay"))

    original = None
    if len(assets) == 2 and screen_size:
        kind, chosen = "pair", assets
        canvas = _compose_pair_wallpaper(
            config, assets, screen_size, show_info, show_date)
    else:
        kind, chosen = "single", [assets[0]]
        # Downloaded once: the original bytes are kept for the fallback.
        original = download_asset_bytes(config, assets[0])
        canvas = None
        if screen_size or show_info or show_date:
            canvas = _compose_single(
                original[0], config, assets[0], screen_size,
                show_info, show_date)

    if canvas is None:
        path = _save_original_file(assets[0], *original, stem)
    else:
        path = settings.IMAGES_DIR / f"{stem}.jpg"
        canvas.save(path, "JPEG", quality=JPEG_QUALITY)

    return {
        "kind": kind,
        "path": str(path),
        "assets": [asset_meta(config, asset) for asset in chosen],
        "size_bytes": path.stat().st_size,
        "created_at": time.time(),
    }


def append_history(
    state: dict[str, Any], entry: dict[str, Any], keep_count: int,
) -> bool:
    """Add `entry` to the history and trim the oldest past `keep_count`.

    Keeps `position` pointing at the same logical spot (or the new live
    edge if it was already there). Returns True if the caller was at the
    live edge (i.e. this rotation should actually be applied to the
    desktop).

    Whatever's currently applied to the desktop is never deleted, even if
    it's outside the keep_count window -- e.g. the user has navigated back
    to an older photo and a background rotation happens while they're
    looking at it. keep_count is a soft bound in that case (briefly +1)
    rather than risk pointing the desktop at a file we just unlinked.
    """
    history = state.get("history") or []
    position = state.get("position", -1)
    was_live = position == -1 or position == len(history) - 1
    if 0 <= position < len(history):
        displayed_path = history[position]["path"]
    else:
        displayed_path = None

    history.append(entry)
    while len(history) > keep_count and history[0]["path"] != displayed_path:
        oldest = history.pop(0)
        with contextlib.suppress(OSError):
            Path(oldest["path"]).unlink()

    if was_live:
        position = len(history) - 1
    else:
        position = next(
            (index for index, item in enumerate(history)
             if item["path"] == displayed_path),
            len(history) - 1)

    state["history"] = history
    state["position"] = position
    return was_live


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def _configure_logging() -> None:
    """Send log output to stdout, and failures also to the log file.

    The file handler is best-effort: if the log file can't be created,
    rotation carries on with stdout only.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        settings.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.LOG_PATH, maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, delay=True)
    except OSError:
        pass
    else:
        file_handler.setLevel(logging.ERROR)
        handlers.append(file_handler)
    logging.basicConfig(
        level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT,
        handlers=handlers)


def _http_error_details(error: urllib.error.HTTPError) -> str:
    """Describe a failed HTTP request for the log: URL and response headers.

    The headers show which layer answered -- e.g. a reverse proxy's `Server`
    or `Via` versus Immich's own -- which the status code alone can't.
    """
    headers = "; ".join(
        f"{name}: {value[:LOG_HEADER_VALUE_LIMIT]}"
        for name, value in error.headers.items()
        if name.lower() != "set-cookie")
    return (f"HTTP {error.code} from {error.geturl()} -- "
            f"response headers: {headers}")


def _record_failure(
    state: dict[str, Any], message: str, details: str | None = None,
) -> None:
    """Log `message` and persist it as the last error (shown by the tray).

    `details` is written to the log only: it can be long, and the state's
    message is what the tray shows.
    """
    logger.error("%s", message)
    if details:
        logger.error("  details: %s", details)
    state["last_error"] = message
    state["last_error_at"] = time.time()
    settings.save_state(state)


def _run_control_command(args: list[str]) -> bool:
    """Handle the pause/resume/status/back/forward flags.

    Returns True if one was present (and handled), meaning no rotation
    should follow.
    """
    if "--pause" in args:
        settings.set_paused(True)
        logger.info("Paused.")
    elif "--resume" in args:
        settings.set_paused(False)
        logger.info("Resumed.")
    elif "--status" in args:
        print(json.dumps(settings.load_state(), indent=2))
    elif "--back" in args:
        print("moved" if navigate(-1) else "at oldest")
    elif "--forward" in args:
        print("moved" if navigate(1) else "at newest")
    else:
        return False
    return True


def main() -> None:
    """Command-line entry point: run a control command or one rotation."""
    _configure_logging()
    args = sys.argv[1:]
    if _run_control_command(args):
        return

    force = "--once" in args
    config = load_required_config()
    state = settings.load_state()

    if state.get("paused") and not force:
        return  # quiet no-op while paused

    interval_minutes = int(
        config.get("interval_minutes",
                   settings.DEFAULT_CONFIG["interval_minutes"]))
    interval_seconds = max(MIN_INTERVAL_SECONDS, interval_minutes * 60)
    seconds_since_last_run = time.time() - state.get("last_run", 0)
    if not force and seconds_since_last_run < interval_seconds:
        return  # not due yet -- quiet no-op, caller polls often

    state["last_run"] = time.time()
    screen_size = desktops.get_screen_size()

    try:
        assets = choose_assets_for_rotation(
            config, allow_pair=bool(screen_size))
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")[:200]
        _record_failure(
            state, f"Immich request failed: HTTP {error.code} {body}",
            details=_http_error_details(error))
        return
    except urllib.error.URLError as error:
        _record_failure(
            state, f"Could not reach Immich server: {error.reason}")
        return

    if not assets:
        _record_failure(
            state,
            "No matching image assets returned (check album/person filters).")
        return

    try:
        entry = build_wallpaper_entry(config, assets, screen_size)
    except Exception as error:  # noqa: BLE001
        # Any download/decode/compose failure is recorded for the tray to
        # show rather than crashing the periodic run.
        details = (_http_error_details(error)
                   if isinstance(error, urllib.error.HTTPError) else None)
        _record_failure(
            state, f"Download/compose failed: {error}", details=details)
        return

    keep_count = max(
        MIN_KEEP_COUNT,
        int(config.get("keep_count", settings.DEFAULT_CONFIG["keep_count"])))
    was_live = append_history(state, entry, keep_count)
    image_name = Path(entry["path"]).name

    if was_live or force:
        entry["wallpaper_applied"] = desktops.set_wallpaper(
            Path(entry["path"]))
        if was_live is False:
            # --once always jumps to the new live edge.
            state["position"] = len(state["history"]) - 1
        outcome = "applied" if entry["wallpaper_applied"] else "NOT applied"
        logger.info("Stored %s (%s, %d KB); wallpaper %s", image_name,
                    entry["kind"], entry["size_bytes"] // 1024, outcome)
    else:
        logger.info("Stored %s (%s) in the background (you've navigated "
                    "back in history, so it wasn't applied to the desktop)",
                    image_name, entry["kind"])

    state["last_error"] = None
    state["last_error_at"] = None
    state["last_success"] = time.time()
    settings.save_state(state)


if __name__ == "__main__":
    main()
