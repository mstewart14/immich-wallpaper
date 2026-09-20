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
import re
import sys
import time
import urllib.error
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import compose
import desktops
import immich_api
import layout
import screens
import settings

logger = logging.getLogger(__name__)

SAFE_EXTENSION = re.compile(r"\.[a-z0-9]{1,5}")

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

# How many candidates one /search/random call asks Immich for, and how many
# more to ask for per extra monitor (so each screen has fresh photos to use).
RANDOM_BATCH_SIZE = 12
EXTRA_BATCH_PER_MONITOR = 6
MAX_BATCH_SIZE = 30

MULTI_MONITOR_MODES = ("same", "different", "span")
# Most photos the "most photos" setting can allow (on one screen, or across
# a whole spanned picture).
MAX_PHOTOS_PER_SCREEN_LIMIT = 6

# Multi-monitor images don't try to keep clear of a taskbar: the desktop
# only reports one work area for all screens, so there is nothing reliable
# to measure per monitor. The base edge margin still applies.
_NO_INSETS = {"left": 0, "top": 0, "right": 0, "bottom": 0}

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
    try:
        config["immich_url"] = immich_api.validate_base_url(
            config["immich_url"])
    except immich_api.UnsafeUrlError as error:
        logger.error("Config has an unusable immich_url: %s", error)
        sys.exit(1)
    return config


def apply_entry(entry: dict[str, Any]) -> bool:
    """Put a history entry on the desktop. Returns True on success.

    An entry built for several monitors sets each monitor's own image (and
    leaves any other monitors alone) when the desktop can do that. In every
    other case the entry's main image is set the ordinary way.
    """
    images = entry.get("images")
    if images and desktops.supports_monitor_wallpapers():
        present = {name: Path(path) for name, path in images.items()
                   if Path(path).exists()}
        if present:
            return desktops.set_monitor_wallpapers(present)
    return desktops.set_wallpaper(Path(entry["path"]))


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
    if not all(Path(path).exists() for path in settings.entry_files(entry)):
        return False
    entry["wallpaper_applied"] = apply_entry(entry)
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
            config["immich_url"], config["api_key"],
            f"/assets/{immich_api.quote_segment(asset_id)}")
    except (urllib.error.URLError, ValueError, KeyError):
        # URLError also covers HTTPError; ValueError covers bad JSON and a
        # malformed id or URL. The caption is optional, so just go without.
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


def _displayed_size(asset: dict) -> tuple[int, int] | None:
    """Width and height of an asset as displayed (EXIF rotation applied).

    None if Immich has no dimensions for it.
    """
    exif = asset.get("exifInfo") or {}
    width, height = exif.get("exifImageWidth"), exif.get("exifImageHeight")
    if not width or not height:
        return None
    try:
        orientation = int(exif.get("orientation") or 1)
    except (TypeError, ValueError):
        orientation = 1
    if orientation in EXIF_QUARTER_TURN_ORIENTATIONS:
        width, height = height, width
    return width, height


def asset_aspect(asset: dict) -> float | None:
    """Width/height ratio of an asset as displayed, or None if unknown."""
    size = _displayed_size(asset)
    return size[0] / size[1] if size else None


def classify_orientation(asset: dict) -> str:
    """Classify an asset as portrait, landscape, square or unknown.

    Accounts for EXIF rotation; returns one of those four words.
    """
    size = _displayed_size(asset)
    if size is None:
        return "unknown"
    width, height = size
    if height > width * ASPECT_RATIO_TOLERANCE:
        return "portrait"
    if width > height * ASPECT_RATIO_TOLERANCE:
        return "landscape"
    return "square"


def choose_from_batch(batch: list[dict], allow_pair: bool) -> list[dict]:
    """Pick the asset(s) to show from an already shuffled, non-empty batch.

    Two portraits when `allow_pair` and the seed (the first asset) is a
    portrait with another portrait available, otherwise just the seed.
    """
    first = batch[0]
    if allow_pair and classify_orientation(first) == "portrait":
        for candidate in batch[1:]:
            if (candidate["id"] != first["id"]
                    and classify_orientation(candidate) == "portrait"):
                return [first, candidate]
    return [first]


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
    return choose_from_batch(batch, allow_pair)


def download_asset_bytes(
    config: dict, asset: dict,
) -> tuple[bytes, str | None]:
    """Download the original file for `asset`; see immich_api.get_bytes()."""
    return immich_api.get_bytes(
        config["immich_url"], config["api_key"],
        f"/assets/{immich_api.quote_segment(asset['id'])}/original",
        timeout=DOWNLOAD_TIMEOUT_SECONDS)


def asset_meta(config: dict, asset: dict) -> dict[str, Any]:
    """Return the subset of an asset's fields kept in the history entry."""
    return {
        "id": asset["id"],
        "original_filename": asset.get("originalFileName"),
        "web_url": (immich_api.validate_base_url(config["immich_url"])
                    + f"/photos/{immich_api.quote_segment(asset['id'])}"),
    }


# --------------------------------------------------------------------------
# Image composition (only used for the 2-portrait side-by-side layout)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# On-image overlays: per-photo caption (date / location / people) and a
# today's-date corner overlay -- both "faked" at compose time since this is
# a static wallpaper, not a live web page like Immich Kiosk (which these
# are modeled on). Off by default; baked into the JPEG at each rotation.
# --------------------------------------------------------------------------


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
    details = get_asset_details(config, asset["id"])
    edge_inset = insets["right"] if corner == "right" else insets["left"]
    compose.draw_caption(
        canvas, compose.photo_caption_lines(details), region_left,
        region_right, canvas.height, corner=corner, extra_x=edge_inset,
        extra_bottom=insets["bottom"])


def _compose_pair_wallpaper(
    config: dict, assets: list[dict], screen_size: tuple[int, int],
    show_info: bool, show_date: bool, download=None,
    insets: dict[str, int] | None = None,
):
    """Side-by-side canvas for two portrait assets, with optional overlays.

    `download` fetches an asset's bytes (shared and cached when several
    screens use the same photos); `insets` overrides taskbar detection.
    """
    if insets is None:
        insets = desktops.screen_insets(screen_size)
    if download is None:
        def download(asset):
            return download_asset_bytes(config, asset)
    data_left, _ = download(assets[0])
    data_right, _ = download(assets[1])
    canvas = compose.compose_pair(data_left, data_right, *screen_size)
    if show_info:
        screen_width = screen_size[0]
        left_width = (screen_width - compose.PAIR_GAP_PX) // 2
        _draw_photo_caption(canvas, config, assets[0], 0, left_width,
                            "left", insets)
        _draw_photo_caption(canvas, config, assets[1],
                            left_width + compose.PAIR_GAP_PX, screen_width,
                            "right", insets)
    if show_date:
        compose.draw_date_overlay(
            canvas, extra_x=insets["left"], extra_top=insets["top"])
    return canvas


def _compose_single(
    data: bytes, config: dict, asset: dict,
    screen_size: tuple[int, int] | None, show_info: bool, show_date: bool,
    insets: dict[str, int] | None = None,
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
        canvas = compose.load_oriented(data)
        if screen_size:
            canvas = compose.letterbox_single(canvas, *screen_size)
        if insets is None:
            insets = desktops.screen_insets(screen_size)
        if show_info:
            _draw_photo_caption(canvas, config, asset, 0, canvas.width,
                                "left", insets)
        if show_date:
            compose.draw_date_overlay(
                canvas, extra_x=insets["left"], extra_top=insets["top"])
    except Exception as error:  # noqa: BLE001
        # The bytes come from a remote server and image decoders can fail in
        # many ways (bad data, missing plugin, oversized image, ...). Any
        # failure just means "use the original file instead".
        logger.warning("Could not decode %s (%s); using the original file "
                       "instead", asset.get("originalFileName"), error)
        return None
    return canvas


def _save_jpeg(canvas, path: Path) -> Path:
    """Write `canvas` as a JPEG at `path`, readable by the owner only."""
    with settings.open_private(path, exclusive=True) as handle:
        canvas.save(handle, "JPEG", quality=JPEG_QUALITY)
    return path


def _save_original_file(
    asset: dict, data: bytes, content_type: str | None, stem: str,
) -> Path:
    """Save a photo's original bytes untouched, keeping its extension."""
    # The file name comes from the server, so only trust a plain short
    # alphanumeric extension; anything else falls back to the MIME type.
    extension = Path(asset.get("originalFileName") or "").suffix.lower()
    if not SAFE_EXTENSION.fullmatch(extension):
        extension = EXT_BY_MIME.get(
            asset.get("originalMimeType") or "",
            EXT_BY_MIME.get(content_type or "", ".jpg"))
    path = settings.IMAGES_DIR / f"{stem}{extension}"
    with settings.open_private(path, exclusive=True) as handle:
        handle.write(data)
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
    settings.ensure_private_dir(settings.IMAGES_DIR)
    # Timestamp plus random suffix: unique even across same-second calls.
    stem = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    show_info = bool(config.get("show_photo_info"))
    show_date = bool(config.get("show_date_overlay"))

    jpeg_path = settings.IMAGES_DIR / f"{stem}.jpg"
    if len(assets) == 2 and screen_size:
        kind, chosen = "pair", assets
        path = _save_jpeg(_compose_pair_wallpaper(
            config, assets, screen_size, show_info, show_date), jpeg_path)
    else:
        kind, chosen = "single", [assets[0]]
        # Downloaded once: the original bytes are kept for the fallback.
        data, content_type = download_asset_bytes(config, assets[0])
        canvas = None
        if screen_size or show_info or show_date:
            canvas = _compose_single(
                data, config, assets[0], screen_size, show_info, show_date)
        if canvas is None:
            path = _save_original_file(assets[0], data, content_type, stem)
        else:
            path = _save_jpeg(canvas, jpeg_path)

    return {
        "kind": kind,
        "path": str(path),
        "assets": [asset_meta(config, asset) for asset in chosen],
        "size_bytes": path.stat().st_size,
        "created_at": time.time(),
    }


# --------------------------------------------------------------------------
# Multi-monitor wallpapers
# --------------------------------------------------------------------------
def _caching_downloader(config: dict):
    """A download function that fetches each asset at most once.

    Screens showing the same photo (the "same" mode) share one download.
    """
    cache: dict[str, tuple[bytes, str | None]] = {}

    def download(asset: dict) -> tuple[bytes, str | None]:
        if asset["id"] not in cache:
            cache[asset["id"]] = download_asset_bytes(config, asset)
        return cache[asset["id"]]

    return download


def _with_aspects(assets: list[dict]) -> list[tuple[dict, float]]:
    """The assets whose shape is known, each with its aspect ratio."""
    pairs = []
    for asset in assets:
        aspect = asset_aspect(asset)
        if aspect:
            pairs.append((asset, aspect))
    return pairs


def _pick_for_screen(
    batch: list[dict], monitor: desktops.Monitor, max_photos: int,
) -> tuple[list[dict], list[layout.Placement] | None]:
    """Choose the photos for one screen from a shuffled batch.

    The first photo of the batch is the seed. Up to two photos use the
    original rule (two portraits side by side), kept only where that makes
    sense for this screen's shape, so a portrait monitor doesn't get two
    portraits squeezed side by side. Allowing more than two switches to the
    row layout, which fills the screen's width with as many portraits as
    fit and returns the placements to draw them at.
    """
    if max_photos <= 1:
        return [batch[0]], None
    size = (monitor.width, monitor.height)
    if max_photos == 2:
        assets = choose_from_batch(batch, allow_pair=True)
        if len(assets) == 2:
            pair = _with_aspects(assets)
            if len(pair) < 2 or len(layout.plan_row(
                    [aspect for _, aspect in pair], *size,
                    max_photos=2)) < 2:
                assets = assets[:1]
        return assets, None
    known = _with_aspects(batch)
    if not known or known[0][0] is not batch[0]:
        return [batch[0]], None    # seed's shape unknown: show it alone
    aspects = [aspect for _, aspect in known]
    placements = layout.plan_row(
        aspects, *size, max_photos=max_photos,
        companion_ok=lambda i: aspects[i] < 1 / ASPECT_RATIO_TOLERANCE)
    if len(placements) < 2:
        return [batch[0]], None
    remapped = [
        layout.Placement(index, p.x, p.y, p.width, p.height)
        for index, p in enumerate(placements)]
    return [known[p.index][0] for p in placements], remapped


def _render_screen(
    config: dict, assets: list[dict],
    placements: list[layout.Placement] | None, monitor: desktops.Monitor,
    download, show_info: bool, show_date: bool,
):
    """Draw one screen's photos at that monitor's own size.

    Returns the canvas, or None if a single photo can't be decoded (the
    caller then keeps its original file).
    """
    size = (monitor.width, monitor.height)
    if placements:
        canvas = compose.compose_row(
            [download(asset)[0] for asset in assets], placements, *size)
        for asset, place in zip(assets, placements):
            if show_info:
                _draw_photo_caption(canvas, config, asset, place.x,
                                    place.x + place.width, "left", _NO_INSETS)
        if show_date:
            compose.draw_date_overlay(canvas)
        return canvas
    if len(assets) == 2:
        return _compose_pair_wallpaper(
            config, assets, size, show_info, show_date, download=download,
            insets=_NO_INSETS)
    data, _ = download(assets[0])
    return _compose_single(
        data, config, assets[0], size, show_info, show_date,
        insets=_NO_INSETS)


def _render_span(
    config: dict, batch: list[dict], monitors: list[desktops.Monitor],
    download, show_info: bool, show_date: bool, max_photos: int,
) -> tuple[dict[str, Any], list[dict]]:
    """One mosaic across all `monitors`, sliced into one image per monitor.

    The monitors are laid side by side as a strip, filled with as many
    photos as fit without cropping any (at most `max_photos` in total, not
    per screen), then each monitor takes its own slice. Returns
    ({monitor name: image}, the photos used).
    """
    strip = screens.strip_layout(monitors)
    known = _with_aspects(batch)
    if not known:
        raise ValueError("no photos with known dimensions to span")
    placements = layout.plan_row(
        [aspect for _, aspect in known], strip.width, strip.height,
        max_photos=max_photos)
    chosen = [known[place.index][0] for place in placements]
    canvas = compose.compose_row(
        [download(asset)[0] for asset in chosen], placements,
        strip.width, strip.height)
    if show_info:
        for asset, place in zip(chosen, placements):
            _draw_photo_caption(canvas, config, asset, place.x,
                                place.x + place.width, "left", _NO_INSETS)
    if show_date:
        compose.draw_date_overlay(canvas, extra_x=strip.slots[0].x)
    slices = {
        slot.name: canvas.crop(
            (slot.x, slot.y, slot.x + slot.width, slot.y + slot.height))
        for slot in strip.slots}
    return slices, chosen


def _save_screen_image(
    canvas, assets: list[dict], download, stem: str, name: str,
) -> Path:
    """Write one screen's image and return its path.

    That is the drawn canvas or, when a single photo couldn't be decoded,
    that photo's original file.
    """
    file_stem = f"{stem}-{screens.safe_name(name)}"
    if canvas is None:
        data, content_type = download(assets[0])
        return _save_original_file(assets[0], data, content_type, file_stem)
    return _save_jpeg(canvas, settings.IMAGES_DIR / f"{file_stem}.jpg")


def build_multi_entry(
    config: dict, batch: list[dict], monitors: list[desktops.Monitor],
    mode: str, *, per_monitor: bool, max_photos: int,
) -> dict[str, Any]:
    """Build a history entry with one image per target monitor.

    Args:
        config: The user's config.
        batch: Shuffled candidate photos; the first is the seed.
        monitors: The monitors to change, left to right.
        mode: "same" (each screen shows the same seed photo, drawn at its
            own size), "different" (each screen its own photos, none
            shared) or "span" (one mosaic across all the monitors).
        per_monitor: Whether the desktop sets monitors individually. If
            not, the entry holds just its main image.
        max_photos: Most photos on one screen, or in total across the
            spanned picture in "span" mode.

    Returns:
        The entry. Its main `path` is the primary monitor's image, and
        `images` maps every monitor to its own file.
    """
    settings.ensure_private_dir(settings.IMAGES_DIR)
    stem = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    show_info = bool(config.get("show_photo_info"))
    show_date = bool(config.get("show_date_overlay"))
    download = _caching_downloader(config)
    primary = next((m for m in monitors if m.primary), monitors[0])
    several = len(monitors) > 1
    files: dict[str, Path] = {}
    used: list[dict] = []

    def remember(assets: list[dict]) -> None:
        for asset in assets:
            if all(asset["id"] != other["id"] for other in used):
                used.append(asset)

    if mode == "span" and several:
        slices, chosen = _render_span(
            config, batch, monitors, download, show_info, show_date,
            max_photos)
        remember(chosen)
        for name, canvas in slices.items():
            files[name] = _save_screen_image(
                canvas, chosen, download, stem, name)
    else:
        remaining = list(batch)
        for monitor in monitors:
            pool = batch
            if mode == "different" and several:
                pool = remaining or batch
            assets, placements = _pick_for_screen(pool, monitor, max_photos)
            canvas = _render_screen(
                config, assets, placements, monitor, download, show_info,
                show_date and monitor is primary)
            files[monitor.name] = _save_screen_image(
                canvas, assets, download, stem, monitor.name)
            remember(assets)
            remaining = [a for a in remaining
                         if all(a["id"] != b["id"] for b in assets)]

    if several or len(used) > 2:
        kind = "multi"
    else:
        kind = "pair" if len(used) == 2 else "single"
    entry: dict[str, Any] = {
        "kind": kind,
        "path": str(files[primary.name]),
        "assets": [asset_meta(config, asset) for asset in used],
        "size_bytes": sum(path.stat().st_size for path in files.values()),
        "created_at": time.time(),
    }
    if per_monitor:
        entry["images"] = {name: str(path) for name, path in files.items()}
    return entry


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
        for old_file in settings.entry_files(oldest):
            with contextlib.suppress(OSError):
                Path(old_file).unlink()

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
def _config_value(config: dict, key: str) -> Any:
    """A config value, falling back to the shared default when absent."""
    return config.get(key, settings.DEFAULT_CONFIG[key])


def _screen_options(config: dict) -> tuple[int, str]:
    """(most photos per screen, multi-monitor mode) from the config.

    Invalid values fall back to the defaults instead of failing a rotation.
    """
    try:
        max_photos = max(1, min(
            MAX_PHOTOS_PER_SCREEN_LIMIT,
            int(_config_value(config, "max_photos_per_screen"))))
    except (TypeError, ValueError):
        max_photos = settings.DEFAULT_CONFIG["max_photos_per_screen"]
    mode = _config_value(config, "multi_monitor_mode")
    return max_photos, mode if mode in MULTI_MONITOR_MODES else "same"


def _build_rotation_entry(
    config: dict, state: dict[str, Any],
) -> dict[str, Any] | None:
    """Fetch photos and build this rotation's history entry.

    With one monitor (or a desktop that can't set monitors individually)
    this is the ordinary single-image rotation. With several monitors it
    builds one image per targeted monitor according to the configured
    mode. Returns None, after recording the failure, if anything went
    wrong.
    """
    max_photos, mode = _screen_options(config)
    monitors = desktops.get_monitors()
    per_monitor = len(monitors) > 1 and desktops.supports_monitor_wallpapers()
    use_multi = per_monitor or (bool(monitors) and max_photos > 2)

    targets: list[desktops.Monitor] = []
    if use_multi:
        targets = (monitors if len(monitors) == 1 else
                   screens.select_monitors(
                       monitors, _config_value(config, "monitors")))
        if not targets:
            wanted = ", ".join(_config_value(config, "monitors"))
            _record_failure(
                state, "None of the selected monitors is connected "
                f"(looking for: {wanted}).")
            return None

    try:
        if use_multi:
            size = min(MAX_BATCH_SIZE, RANDOM_BATCH_SIZE
                       + EXTRA_BATCH_PER_MONITOR * (len(targets) - 1))
            batch = pick_image_batch(config, size=size)
            random.shuffle(batch)
            assets = batch
        else:
            screen_size = desktops.get_screen_size()
            assets = choose_assets_for_rotation(
                config, allow_pair=bool(screen_size) and max_photos >= 2)
    except urllib.error.HTTPError as error:
        # Fall back to the reason phrase when the server sent no body (a
        # proxy's bare 403, or one of our own refused redirects).
        body = (error.read().decode(errors="replace")[:200]
                or str(error.reason))
        _record_failure(
            state, f"Immich request failed: HTTP {error.code} {body}",
            details=_http_error_details(error))
        return None
    except urllib.error.URLError as error:
        _record_failure(
            state, f"Could not reach Immich server: {error.reason}")
        return None

    if not assets:
        _record_failure(
            state,
            "No matching image assets returned (check album/person filters).")
        return None

    try:
        if use_multi:
            return build_multi_entry(
                config, assets, targets, mode, per_monitor=per_monitor,
                max_photos=max_photos)
        return build_wallpaper_entry(config, assets, screen_size)
    except Exception as error:  # noqa: BLE001
        # Any download/decode/compose failure is recorded for the tray to
        # show rather than crashing the periodic run.
        details = (_http_error_details(error)
                   if isinstance(error, urllib.error.HTTPError) else None)
        _record_failure(
            state, f"Download/compose failed: {error}", details=details)
        return None


def _private_opener(path: str, flags: int) -> int:
    """os.open() that creates files readable by the owner only."""
    return os.open(path, flags, settings.PRIVATE_FILE_MODE)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """A rotating log file created readable by the owner only.

    The log holds request URLs and response headers, which are nobody
    else's business.
    """

    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding,
                    errors=getattr(self, "errors", None),
                    opener=_private_opener)


def _configure_logging() -> None:
    """Send log output to stdout, and failures also to the log file.

    The file handler is best-effort: if the log file can't be created,
    rotation carries on with stdout only.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        settings.ensure_private_dir(settings.CACHE_DIR)
        with contextlib.suppress(FileNotFoundError):
            settings.LOG_PATH.chmod(settings.PRIVATE_FILE_MODE)
        file_handler = _PrivateRotatingFileHandler(
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
    entry = _build_rotation_entry(config, state)
    if entry is None:
        return

    keep_count = max(
        MIN_KEEP_COUNT,
        int(config.get("keep_count", settings.DEFAULT_CONFIG["keep_count"])))
    was_live = append_history(state, entry, keep_count)
    image_name = Path(entry["path"]).name

    if was_live or force:
        entry["wallpaper_applied"] = apply_entry(entry)
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
