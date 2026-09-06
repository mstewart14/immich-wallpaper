#!/usr/bin/env python3
"""
Pulls photos from Immich (filtered by the albums/people chosen in the
config UI) and sets the desktop wallpaper. Two portrait photos are paired
side-by-side into a single composite when the screen resolution can be
detected; landscape/square photos are shown singly. Keeps a bounded
history of recent wallpapers on disk (oldest deleted as new ones arrive)
so the tray app can step back/forward through what it's shown.

Meant to be triggered repeatedly (e.g. every 15-30s) by the tray app; it
self-paces against config.json's interval_minutes via a state file, so
callers can invoke it often without worrying about over-rotating.

Desktop support: auto-detects KDE Plasma (via D-Bus scripting, no
QtWebEngine involved) and XFCE (via xfconf-query / xrandr). Only Pillow is
a non-stdlib dependency, needed for the portrait-pairing composite.

Usage:
    python3 rotate.py             # normal run: no-ops if not due yet
    python3 rotate.py --once      # ignore the interval gate, rotate now
    python3 rotate.py --pause
    python3 rotate.py --resume
    python3 rotate.py --status
"""
import functools
import glob
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "immich-wallpaper" / "config.json"
CACHE_DIR = Path.home() / ".cache" / "immich-wallpaper"
IMAGES_DIR = CACHE_DIR / "images"
STATE_PATH = CACHE_DIR / "state.json"

EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/heic": ".heic", "image/heif": ".heif", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/tiff": ".tiff",
}


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------
def load_config():
    if not CONFIG_PATH.exists():
        log(f"No config at {CONFIG_PATH}. Run the config UI and save a configuration first.")
        sys.exit(1)
    cfg = json.loads(CONFIG_PATH.read_text())
    if not cfg.get("immich_url") or not cfg.get("api_key"):
        log("Config is missing immich_url or api_key. Run the config UI to finish setup.")
        sys.exit(1)
    return cfg


DEFAULT_STATE = {
    "last_run": 0,
    "last_success": None,
    "last_error": None,
    "last_error_at": None,
    "paused": False,
    "history": [],   # oldest -> newest; each entry: kind, path, assets[], size_bytes, created_at
    "position": -1,  # index into history currently applied to the desktop
}


def load_state():
    state = dict(DEFAULT_STATE)
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return state


def save_state(state):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


def set_paused(paused):
    state = load_state()
    state["paused"] = paused
    save_state(state)
    return state


def current_entry(state=None):
    state = state or load_state()
    history = state.get("history") or []
    position = state.get("position", -1)
    if 0 <= position < len(history):
        return history[position]
    return None


def can_go_back(state=None):
    state = state or load_state()
    return (state.get("position") or 0) > 0


def can_go_forward(state=None):
    state = state or load_state()
    history = state.get("history") or []
    return state.get("position", -1) < len(history) - 1


def navigate(direction):
    """direction: -1 for back, +1 for forward. Returns True if it moved."""
    state = load_state()
    history = state.get("history") or []
    if not history:
        return False
    position = state.get("position", len(history) - 1)
    new_pos = position + direction
    if new_pos < 0 or new_pos >= len(history):
        return False
    entry = history[new_pos]
    path = Path(entry["path"])
    if not path.exists():
        return False
    entry["wallpaper_applied"] = set_wallpaper(path)
    state["position"] = new_pos
    save_state(state)
    return True


# --------------------------------------------------------------------------
# Immich API
# --------------------------------------------------------------------------
def immich_post(base_url, api_key, path, body):
    url = base_url.rstrip("/") + "/api" + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def immich_get_bytes(base_url, api_key, path):
    url = base_url.rstrip("/") + "/api" + path
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(), resp.getheader("Content-Type")


def immich_get_json(base_url, api_key, path):
    url = base_url.rstrip("/") + "/api" + path
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def get_asset_details(cfg, asset_id):
    """Full asset record (exifInfo + people), used only for the on-image
    caption -- /search/random's per-asset payload isn't reliably this
    complete, so this is one extra small GET per chosen photo."""
    try:
        return immich_get_json(cfg["immich_url"], cfg["api_key"], f"/assets/{asset_id}")
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError):
        return None


def pick_image_batch(cfg, size=12):
    """One /search/random call, filtered by album/person selections, asking
    for exif so orientation can be judged without downloading anything.

    Immich's personIds filter is an AND (asset must contain every listed
    person). person_match controls how multiple selected people combine:
      "any"  -- each call filters to one randomly-chosen person, so over
                many rotations you get an OR across the whole group.
      "all"  -- every call requires all of them together (the raw AND).
      "both" -- each call is a coin flip between the two, so you get a
                genuine blend of solo and together photos over time."""
    body = {"size": size, "withExif": True}
    album_ids = [a["id"] for a in cfg.get("albums", [])]
    people = cfg.get("people", [])
    if album_ids:
        body["albumIds"] = album_ids
    if people:
        mode = cfg.get("person_match", "any")
        want_all = mode == "all" or (mode == "both" and random.random() < 0.5)
        if want_all or len(people) == 1:
            body["personIds"] = [p["id"] for p in people]
        else:
            body["personIds"] = [random.choice(people)["id"]]

    assets = immich_post(cfg["immich_url"], cfg["api_key"], "/search/random", body)
    if not assets:
        return []
    return [a for a in assets if (a.get("originalMimeType") or "").startswith("image/")]


def classify_orientation(asset):
    exif = asset.get("exifInfo") or {}
    w, h = exif.get("exifImageWidth"), exif.get("exifImageHeight")
    if not w or not h:
        return "unknown"
    try:
        if int(exif.get("orientation") or 1) in (5, 6, 7, 8):
            w, h = h, w
    except (TypeError, ValueError):
        pass
    if h > w * 1.05:
        return "portrait"
    if w > h * 1.05:
        return "landscape"
    return "square"


def choose_assets_for_rotation(cfg, allow_pair):
    batch = pick_image_batch(cfg)
    if not batch:
        return []
    random.shuffle(batch)
    first = batch[0]
    if allow_pair and classify_orientation(first) == "portrait":
        for candidate in batch[1:]:
            if candidate["id"] != first["id"] and classify_orientation(candidate) == "portrait":
                return [first, candidate]
    return [first]


def download_asset_bytes(cfg, asset):
    return immich_get_bytes(cfg["immich_url"], cfg["api_key"], f"/assets/{asset['id']}/original")


def asset_meta(cfg, asset):
    return {
        "id": asset["id"],
        "original_filename": asset.get("originalFileName"),
        "web_url": cfg["immich_url"].rstrip("/") + f"/photos/{asset['id']}",
    }


# --------------------------------------------------------------------------
# Image composition (only used for the 2-portrait side-by-side layout)
# --------------------------------------------------------------------------
def _load_oriented(data):
    from PIL import Image, ImageOps
    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def _contain_resize(img, target_w, target_h):
    """Scales to fit entirely within target_w x target_h, no cropping --
    the caller's canvas shows through as letterbox bars around it."""
    from PIL import Image
    scale = min(target_w / img.width, target_h / img.height)
    new_w, new_h = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    return img.resize((new_w, new_h), Image.LANCZOS)


def _letterbox_single(img, target_w, target_h, bg=(0, 0, 0)):
    from PIL import Image
    canvas = Image.new("RGB", (target_w, target_h), bg)
    fitted = _contain_resize(img, target_w, target_h)
    canvas.paste(fitted, ((target_w - fitted.width) // 2, (target_h - fitted.height) // 2))
    return canvas


def compose_pair(data_a, data_b, target_w, target_h, gap=6, bg=(0, 0, 0)):
    from PIL import Image
    canvas = Image.new("RGB", (target_w, target_h), bg)
    half_w = (target_w - gap) // 2
    right_w = target_w - gap - half_w

    left_img = _contain_resize(_load_oriented(data_a), half_w, target_h)
    canvas.paste(left_img, ((half_w - left_img.width) // 2, (target_h - left_img.height) // 2))

    right_img = _contain_resize(_load_oriented(data_b), right_w, target_h)
    right_x = half_w + gap + (right_w - right_img.width) // 2
    canvas.paste(right_img, (right_x, (target_h - right_img.height) // 2))
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


def _load_font(size):
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


def _draw_outlined_text(draw, xy, text, font, fill=(190, 190, 190), outline=(0, 0, 0)):
    x, y = xy
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        draw.text((x + dx, y + dy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def _format_taken_date(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return None


def photo_caption_lines(details):
    """[people names, location, date taken] from a full asset record, top
    to bottom, skipping whichever parts are missing. Empty list if there's
    nothing to show."""
    if not details:
        return []
    exif = details.get("exifInfo") or {}
    lines = []
    people = [p.get("name") for p in (details.get("people") or []) if p.get("name") and not p.get("isHidden")]
    if people:
        lines.append(", ".join(people))
    location = ", ".join(x for x in (exif.get("city"), exif.get("state") or exif.get("country")) if x)
    if location:
        lines.append(location)
    date = _format_taken_date(exif.get("dateTimeOriginal"))
    if date:
        lines.append(date)
    return lines


EDGE_MARGIN_INCHES = 0.5


@functools.lru_cache(maxsize=1)
def get_screen_dpi():
    """Best-effort physical DPI via xrandr's per-monitor mm dimensions
    (works via XWayland on a Wayland KDE session too). Falls back to the
    common 96 DPI default if detection fails for any reason. Cached --
    doesn't change within a single rotation, and each rotate.py invocation
    is a fresh short-lived process anyway."""
    try:
        result = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 96.0
    if result.returncode != 0:
        return 96.0
    m = re.search(r"connected primary (\d+)x(\d+)\+\d+\+\d+.*?(\d+)mm x (\d+)mm", result.stdout) or \
        re.search(r"connected (\d+)x(\d+)\+\d+\+\d+.*?(\d+)mm x (\d+)mm", result.stdout)
    if not m:
        return 96.0
    px_w, px_h, mm_w, mm_h = (int(g) for g in m.groups())
    if mm_w <= 0 or mm_h <= 0:
        return 96.0
    return ((px_w / (mm_w / 25.4)) + (px_h / (mm_h / 25.4))) / 2


def edge_margin_px():
    return round(get_screen_dpi() * EDGE_MARGIN_INCHES)


def draw_caption(canvas, lines, region_x0, region_x1, region_bottom, corner="left", extra_x=0, extra_bottom=0):
    """Draws caption `lines` bottom-anchored inside [region_x0, region_x1]
    of `canvas` (a region rather than the whole canvas so pair captions
    stay against their own half), right-aligned if corner == "right".
    extra_x/extra_bottom pad past a detected taskbar/panel on top of the
    base EDGE_MARGIN_INCHES -- see screen_insets()."""
    if not lines:
        return
    from PIL import ImageDraw
    draw = ImageDraw.Draw(canvas)
    font_size = max(13, round(canvas.height * 0.022))
    font = _load_font(font_size)
    margin = edge_margin_px()
    line_gap = max(2, font_size // 6)
    measured = [draw.textbbox((0, 0), line, font=font) for line in lines]
    total_h = sum(b[3] - b[1] for b in measured) + line_gap * (len(lines) - 1)
    y = region_bottom - (margin + extra_bottom) - total_h
    for line, bbox in zip(lines, measured):
        w = bbox[2] - bbox[0]
        x = (region_x1 - (margin + extra_x) - w) if corner == "right" else (region_x0 + margin + extra_x)
        _draw_outlined_text(draw, (x, y), line, font)
        y += (bbox[3] - bbox[1]) + line_gap


def draw_date_overlay(canvas, extra_x=0, extra_top=0):
    """Today's date, top-left. Baked in at rotation time -- see module note
    above on why this isn't a live clock. extra_x/extra_top pad past a
    detected taskbar/panel on top of the base EDGE_MARGIN_INCHES -- see
    screen_insets()."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(canvas)
    font_size = max(15, round(canvas.height * 0.026))
    font = _load_font(font_size)
    margin = edge_margin_px()
    _draw_outlined_text(draw, (margin + extra_x, margin + extra_top), time.strftime("%A, %B %-d"), font)


def get_work_area():
    """Best-effort usable-desktop-area query via the EWMH _NET_WORKAREA root
    window property -- works via XWayland even on a Wayland KDE session,
    and natively under XFCE's X11. Returns (x, y, width, height) or None
    if xprop is missing, fails, or the output doesn't parse."""
    try:
        result = subprocess.run(["xprop", "-root", "_NET_WORKAREA"],
                                 capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    m = re.search(r"=\s*(-?\d+),\s*(-?\d+),\s*(\d+),\s*(\d+)", result.stdout)
    return tuple(int(g) for g in m.groups()) if m else None


def screen_insets(screen_size):
    """How many pixels on each screen edge are covered by a taskbar/panel,
    derived from comparing the usable work area to the full screen size.
    All-zero (today's flat-margin behaviour) if detection fails or looks
    nonsensical -- e.g. a multi-monitor workarea union wider than this one
    screen, which we'd rather ignore than risk a broken layout from."""
    zero = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    if not screen_size:
        return zero
    work = get_work_area()
    if not work:
        return zero
    wx, wy, ww, wh = work
    sw, sh = screen_size
    left, top = max(0, wx), max(0, wy)
    right, bottom = max(0, sw - (wx + ww)), max(0, sh - (wy + wh))
    if left > sw * 0.4 or right > sw * 0.4 or top > sh * 0.4 or bottom > sh * 0.4:
        return zero
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def build_wallpaper_entry(cfg, assets, screen_size):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"  # unique even across same-second calls
    show_info = bool(cfg.get("show_photo_info"))
    show_date = bool(cfg.get("show_date_overlay"))

    if len(assets) == 2 and screen_size:
        insets = screen_insets(screen_size)
        data_a, _ = download_asset_bytes(cfg, assets[0])
        data_b, _ = download_asset_bytes(cfg, assets[1])
        canvas = compose_pair(data_a, data_b, *screen_size)
        if show_info:
            gap, half_w = 6, (screen_size[0] - 6) // 2
            draw_caption(canvas, photo_caption_lines(get_asset_details(cfg, assets[0]["id"])),
                         0, half_w, screen_size[1], corner="left",
                         extra_x=insets["left"], extra_bottom=insets["bottom"])
            draw_caption(canvas, photo_caption_lines(get_asset_details(cfg, assets[1]["id"])),
                         half_w + gap, screen_size[0], screen_size[1], corner="right",
                         extra_x=insets["right"], extra_bottom=insets["bottom"])
        if show_date:
            draw_date_overlay(canvas, extra_x=insets["left"], extra_top=insets["top"])
        path = IMAGES_DIR / f"{stem}.jpg"
        canvas.save(path, "JPEG", quality=92)
        kind = "pair"
        chosen = assets
    elif show_info or show_date:
        asset = assets[0]
        data, _ = download_asset_bytes(cfg, asset)
        canvas = _load_oriented(data)
        # Pre-letterbox onto the real screen size (when known) so the
        # taskbar insets below -- measured in real screen pixels -- land in
        # the same coordinate space as what's drawn here. Without this, a
        # single image left at its own native resolution has no reliable
        # correspondence to on-screen pixel positions.
        if screen_size:
            canvas = _letterbox_single(canvas, *screen_size)
        insets = screen_insets(screen_size)
        if show_info:
            draw_caption(canvas, photo_caption_lines(get_asset_details(cfg, asset["id"])),
                         0, canvas.width, canvas.height, corner="left",
                         extra_x=insets["left"], extra_bottom=insets["bottom"])
        if show_date:
            draw_date_overlay(canvas, extra_x=insets["left"], extra_top=insets["top"])
        path = IMAGES_DIR / f"{stem}.jpg"
        canvas.save(path, "JPEG", quality=92)
        kind = "single"
        chosen = [asset]
    else:
        asset = assets[0]
        data, ctype = download_asset_bytes(cfg, asset)
        ext = Path(asset.get("originalFileName", "")).suffix.lower()
        if not ext or len(ext) > 6:
            ext = EXT_BY_MIME.get(asset.get("originalMimeType"), EXT_BY_MIME.get(ctype, ".jpg"))
        path = IMAGES_DIR / f"{stem}{ext}"
        path.write_bytes(data)
        kind = "single"
        chosen = [asset]

    return {
        "kind": kind,
        "path": str(path),
        "assets": [asset_meta(cfg, a) for a in chosen],
        "size_bytes": path.stat().st_size,
        "created_at": time.time(),
    }


def append_history(state, entry, keep_count):
    """Adds entry, trims from the oldest end past keep_count, and keeps
    `position` pointing at the same logical spot (or the new live edge if
    it was already there). Returns True if the caller was at the live edge
    (i.e. this rotation should actually be applied to the desktop).

    Whatever's currently applied to the desktop is never deleted, even if
    it's outside the keep_count window -- e.g. the user has navigated back
    to an older photo and a background rotation happens while they're
    looking at it. keep_count is a soft bound in that case (briefly +1)
    rather than risk pointing the desktop at a file we just unlinked."""
    history = state.get("history") or []
    position = state.get("position", -1)
    was_live = position == -1 or position == len(history) - 1
    displayed_path = history[position]["path"] if 0 <= position < len(history) else None

    history.append(entry)
    while len(history) > keep_count and history[0]["path"] != displayed_path:
        old = history.pop(0)
        try:
            Path(old["path"]).unlink()
        except OSError:
            pass

    if was_live:
        position = len(history) - 1
    else:
        position = next((i for i, e in enumerate(history) if e["path"] == displayed_path), len(history) - 1)

    state["history"] = history
    state["position"] = position
    return was_live


# --------------------------------------------------------------------------
# Desktop environment adapters
# --------------------------------------------------------------------------
def ensure_dbus_env():
    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        bus_path = f"{runtime_dir}/bus"
        if os.path.exists(bus_path):
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"


def ensure_display_env():
    if "DISPLAY" not in os.environ:
        sockets = sorted(glob.glob("/tmp/.X11-unix/X*"))
        if sockets:
            os.environ["DISPLAY"] = ":" + os.path.basename(sockets[0])[1:]


def detect_desktop():
    xdg = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "kde" in xdg:
        return "kde"
    if "xfce" in xdg:
        return "xfce"
    for proc, de in (("plasmashell", "kde"), ("xfce4-session", "xfce")):
        if subprocess.run(["pgrep", "-x", proc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return de
    return None


def _kde_eval(script):
    ensure_dbus_env()
    return subprocess.run(
        ["dbus-send", "--session", "--print-reply",
         "--dest=org.kde.plasmashell", "/PlasmaShell",
         "org.kde.PlasmaShell.evaluateScript", f"string:{script}"],
        capture_output=True, text=True,
    )


def get_screen_size_kde():
    result = _kde_eval("print(screenGeometry(0).width + 'x' + screenGeometry(0).height);")
    if result.returncode != 0:
        return None
    m = re.search(r'string "(\d+)x(\d+)"', result.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def get_screen_size_xfce():
    result = subprocess.run(["xrandr", "--query"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    # Prefer the monitor xrandr marks "primary"; fall back to the first
    # connected+active one otherwise (e.g. "eDP-1 connected 1920x1080+0+0").
    m = re.search(r"connected primary (\d+)x(\d+)\+", result.stdout) or \
        re.search(r"connected (\d+)x(\d+)\+", result.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def get_screen_size():
    de = detect_desktop()
    if de == "kde":
        return get_screen_size_kde()
    if de == "xfce":
        return get_screen_size_xfce()
    return None


def set_wallpaper_kde(image_path):
    # Toggling the plugin away and back (even when it's already org.kde.image)
    # forces Plasma to tear down and recreate the wallpaper QML item. Without
    # this, writeConfig() alone updates the stored config correctly but the
    # on-screen render can silently stop refreshing after the first call,
    # because assigning wallpaperPlugin to its current value is a no-op that
    # Qt's property system skips -- no change signal, no re-render.
    script = f'''
var allDesktops = desktops();
for (i = 0; i < allDesktops.length; i++) {{
    d = allDesktops[i];
    d.wallpaperPlugin = "org.kde.color";
    d.wallpaperPlugin = "org.kde.image";
    d.currentConfigGroup = Array("Wallpaper", "org.kde.image", "General");
    d.writeConfig("Image", "file://{image_path}");
    d.writeConfig("FillMode", 1);
}}
'''
    result = _kde_eval(script)
    if result.returncode != 0:
        log(f"KDE wallpaper set failed: {result.stderr.strip()}")
        return False
    if "error" in result.stdout.lower() and "Error: 0" not in result.stdout:
        log(f"KDE wallpaper set returned an error: {result.stdout.strip()}")
        return False
    return True


def set_wallpaper_xfce(image_path):
    ensure_dbus_env()
    ensure_display_env()
    list_props = subprocess.run(["xfconf-query", "-c", "xfce4-desktop", "-l"], capture_output=True, text=True)
    if list_props.returncode != 0:
        log(f"xfconf-query -l failed: {list_props.stderr.strip()}")
        return False
    props = [p for p in list_props.stdout.splitlines() if p.endswith("last-image")]
    if not props:
        log("No xfce4-desktop 'last-image' properties found (no monitors configured yet?).")
        return False
    ok = True
    for prop in props:
        r = subprocess.run(
            ["xfconf-query", "-c", "xfce4-desktop", "-p", prop, "-s", str(image_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            log(f"Failed to set {prop}: {r.stderr.strip()}")
            ok = False
        # image-style 4 = "Scaled": fit the whole image, letterboxed, no crop
        # (5 = "Zoomed" crops to fill, which is what was clipping portraits)
        style_prop = prop[: -len("last-image")] + "image-style"
        subprocess.run(
            ["xfconf-query", "-c", "xfce4-desktop", "-p", style_prop, "-s", "4"],
            capture_output=True, text=True,
        )
    subprocess.run(["xfdesktop", "--reload"], capture_output=True, text=True)
    return ok


def set_wallpaper(image_path):
    de = detect_desktop()
    if de == "kde":
        return set_wallpaper_kde(image_path)
    if de == "xfce":
        return set_wallpaper_xfce(image_path)
    log("Could not detect a supported desktop environment (looked for KDE Plasma / XFCE).")
    return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def _fail(state, message):
    log(message)
    state["last_error"] = message
    state["last_error_at"] = time.time()
    save_state(state)


def main():
    args = sys.argv[1:]
    if "--pause" in args:
        set_paused(True); log("Paused."); return
    if "--resume" in args:
        set_paused(False); log("Resumed."); return
    if "--status" in args:
        print(json.dumps(load_state(), indent=2)); return
    if "--back" in args:
        print("moved" if navigate(-1) else "at oldest"); return
    if "--forward" in args:
        print("moved" if navigate(1) else "at newest"); return

    force = "--once" in args
    cfg = load_config()
    state = load_state()

    if state.get("paused") and not force:
        return  # quiet no-op while paused

    interval_s = max(60, int(cfg.get("interval_minutes", 5)) * 60)
    elapsed = time.time() - state.get("last_run", 0)
    if not force and elapsed < interval_s:
        return  # not due yet -- quiet no-op, caller polls often

    state["last_run"] = time.time()
    screen_size = get_screen_size()

    try:
        assets = choose_assets_for_rotation(cfg, allow_pair=bool(screen_size))
    except urllib.error.HTTPError as e:
        _fail(state, f"Immich request failed: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
        return
    except urllib.error.URLError as e:
        _fail(state, f"Could not reach Immich server: {e.reason}")
        return

    if not assets:
        _fail(state, "No matching image assets returned (check album/person filters).")
        return

    try:
        entry = build_wallpaper_entry(cfg, assets, screen_size)
    except Exception as e:
        _fail(state, f"Download/compose failed: {e}")
        return

    keep_count = max(2, int(cfg.get("keep_count", 3)))
    was_live = append_history(state, entry, keep_count)

    if was_live or force:
        entry["wallpaper_applied"] = set_wallpaper(Path(entry["path"]))
        if was_live is False:
            state["position"] = len(state["history"]) - 1  # --once always jumps to the new live edge
        log(f"Stored {Path(entry['path']).name} ({entry['kind']}, {entry['size_bytes']//1024} KB); "
            f"wallpaper {'applied' if entry['wallpaper_applied'] else 'NOT applied'}")
    else:
        log(f"Stored {Path(entry['path']).name} ({entry['kind']}) in the background "
            f"(you've navigated back in history, so it wasn't applied to the desktop)")

    state["last_error"] = None
    state["last_error_at"] = None
    state["last_success"] = time.time()
    save_state(state)


if __name__ == "__main__":
    main()
