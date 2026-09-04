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


def pick_image_batch(cfg, size=12):
    """One /search/random call, filtered by album/person selections, asking
    for exif so orientation can be judged without downloading anything."""
    body = {"size": size, "withExif": True}
    album_ids = [a["id"] for a in cfg.get("albums", [])]
    person_ids = [p["id"] for p in cfg.get("people", [])]
    if album_ids:
        body["albumIds"] = album_ids
    if person_ids:
        body["personIds"] = person_ids

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


def _cover_resize(img, target_w, target_h):
    from PIL import Image
    src_ratio = img.width / img.height
    dst_ratio = target_w / target_h
    if src_ratio > dst_ratio:
        new_h = target_h
        new_w = max(1, round(new_h * src_ratio))
    else:
        new_w = target_w
        new_h = max(1, round(new_w / src_ratio))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def compose_pair(data_a, data_b, target_w, target_h, gap=6, bg=(0, 0, 0)):
    from PIL import Image
    canvas = Image.new("RGB", (target_w, target_h), bg)
    half_w = (target_w - gap) // 2
    canvas.paste(_cover_resize(_load_oriented(data_a), half_w, target_h), (0, 0))
    canvas.paste(_cover_resize(_load_oriented(data_b), target_w - gap - half_w, target_h), (half_w + gap, 0))
    return canvas


def build_wallpaper_entry(cfg, assets, screen_size):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"  # unique even across same-second calls
    if len(assets) == 2 and screen_size:
        data_a, _ = download_asset_bytes(cfg, assets[0])
        data_b, _ = download_asset_bytes(cfg, assets[1])
        canvas = compose_pair(data_a, data_b, *screen_size)
        path = IMAGES_DIR / f"{stem}.jpg"
        canvas.save(path, "JPEG", quality=92)
        kind = "pair"
        chosen = assets
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
