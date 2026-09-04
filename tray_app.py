#!/usr/bin/env python3
"""
System tray icon for the Immich wallpaper rotator.

Shows live status (active / paused / error), info about the currently
loaded photo with a one-click link to view it in Immich's web viewer, a
"save a copy" action, and quick controls (refresh now, pause/resume,
open settings).

Needs: python3-pystray, python3-pil (Pillow), and a StatusNotifierItem
provider -- on Debian/Ubuntu that's gir1.2-ayatanaappindicator3-0.1. Both
KDE Plasma's system tray and XFCE's status tray plugin implement this
protocol natively.
"""
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rotate  # noqa: E402  (local module, see sys.path.insert above)

try:
    import pystray
    from PIL import Image, ImageDraw, ImageOps
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("On Debian/Ubuntu: sudo apt install python3-pystray python3-pil gir1.2-ayatanaappindicator3-0.1")
    sys.exit(1)

FLOWER_ASSET = HERE / "assets" / "immich-flower.png"
ERROR_BADGE = (220, 53, 69, 255)

stop_event = threading.Event()
rotate_lock = threading.Lock()


# --------------------------------------------------------------------------
# Icon drawing: a monitor glyph (left half) + the Immich flower (right half).
# Colored while actively rotating, greyed out while paused/idle, with a small
# red badge overlaid when the last rotation failed.
# --------------------------------------------------------------------------
def _screen_glyph(size, color):
    """A monitor silhouette: black screen with a colored outline (and a
    solid-colored stand), black fill so it still properly occludes
    whatever's layered behind it."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(2, size // 14)
    bezel_w, bezel_h = int(size * 0.86), int(size * 0.62)
    bx0, by0 = (size - bezel_w) // 2, int(size * 0.06)
    bx1, by1 = bx0 + bezel_w, by0 + bezel_h
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(size * 0.08),
                         fill=(0, 0, 0, 255), outline=color, width=lw)
    neck_w, neck_h = int(size * 0.12), int(size * 0.10)
    nx0 = size // 2 - neck_w // 2
    d.rectangle([nx0, by1, nx0 + neck_w, by1 + neck_h], fill=color)
    base_w, base_h = int(size * 0.40), max(2, int(size * 0.07))
    bx = size // 2 - base_w // 2
    by = by1 + neck_h
    d.rounded_rectangle([bx, by, bx + base_w, by + base_h], radius=base_h // 2, fill=color)
    return img


def _flower_glyph(size, greyscale):
    base = Image.open(FLOWER_ASSET).convert("RGBA").resize((size, size), Image.LANCZOS)
    if not greyscale:
        return base
    alpha = base.split()[-1]
    grey = ImageOps.grayscale(base.convert("RGB")).convert("RGBA")
    grey.putalpha(alpha)
    return grey


def make_icon(status):
    """status: 'active' | 'paused' | 'error' | 'unknown'

    The flower fills the whole canvas as a backdrop, and a slightly
    shrunk monitor glyph sits in front of it, bottom-left -- so the flower
    peeks out from behind the screen along its top and right edges."""
    size = 128
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    greyed = status in ("paused", "unknown")

    flower = _flower_glyph(size, greyscale=greyed)
    canvas.paste(flower, (0, 0), flower)

    screen_size = int(size * 0.67)
    screen_color = (150, 150, 150, 255) if greyed else (245, 245, 245, 255)
    screen = _screen_glyph(screen_size, screen_color)
    canvas.paste(screen, (0, size - screen_size), screen)

    if status == "error":
        r = int(size * 0.15)
        cx, cy = size - r - 4, r + 4
        ImageDraw.Draw(canvas).ellipse([cx - r, cy - r, cx + r, cy + r], fill=ERROR_BADGE,
                                        outline=(20, 20, 20, 255), width=3)
    return canvas


def status_for_state(state):
    if state.get("paused"):
        return "paused"
    if state.get("last_error"):
        return "error"
    if rotate.current_entry(state):
        return "active"
    return "unknown"


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def human_age(ts):
    if not ts:
        return "unknown"
    secs = max(0, time.time() - ts)
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def status_text(item=None):
    state = rotate.load_state()
    if state.get("paused"):
        return "⏸ Paused"
    if state.get("last_error"):
        return f"⚠ Error {human_age(state.get('last_error_at'))}"
    if state.get("last_success"):
        return f"● Active — updated {human_age(state.get('last_success'))}"
    return "○ Waiting for first run"


def image_text(item=None):
    entry = rotate.current_entry()
    if not entry:
        return "No image loaded yet"
    names = [a.get("original_filename") or "?" for a in entry.get("assets", [])]
    kb = (entry.get("size_bytes") or 0) // 1024
    label = " + ".join(names) if entry.get("kind") == "pair" else (names[0] if names else Path(entry["path"]).name)
    pos_state = rotate.load_state()
    idx = pos_state.get("position", -1) + 1
    total = len(pos_state.get("history") or [])
    return f"\U0001f5bc [{idx}/{total}] {label} ({kb} KB)"


def has_current_image(item=None):
    return rotate.current_entry() is not None


def pause_toggle_text(item=None):
    return "Resume rotation" if rotate.load_state().get("paused") else "Pause rotation"


def back_enabled(item=None):
    return rotate.can_go_back()


def _open_url_action(url):
    def action(icon, item):
        if url:
            webbrowser.open(url)
    return action


def view_links_items(_menu=None):
    """Dynamic submenu contents: one entry per photo in the current
    wallpaper (1 for a single image, 2 for a side-by-side portrait pair)."""
    entry = rotate.current_entry()
    if not entry:
        return [pystray.MenuItem("(nothing loaded)", None, enabled=False)]
    assets = entry.get("assets", [])
    return [
        pystray.MenuItem(a.get("original_filename") or f"Photo {i + 1}", _open_url_action(a.get("web_url")))
        for i, a in enumerate(assets)
    ] or [pystray.MenuItem("(nothing loaded)", None, enabled=False)]


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
def notify(icon, message, title="Immich Wallpaper"):
    try:
        icon.notify(message, title)
        return
    except Exception:
        pass
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], capture_output=True)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def refresh_icon(icon):
    state = rotate.load_state()
    icon.icon = make_icon(status_for_state(state))
    icon.update_menu()


def pick_save_destination(default_name):
    default_path = str(Path.home() / "Pictures" / default_name)
    if shutil.which("kdialog"):
        r = subprocess.run(
            ["kdialog", "--getsavefilename", default_path,
             "Images (*.jpg *.jpeg *.png *.heic *.webp)"],
            capture_output=True, text=True,
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    if shutil.which("zenity"):
        r = subprocess.run(
            ["zenity", "--file-selection", "--save", "--confirm-overwrite",
             f"--filename={default_path}"],
            capture_output=True, text=True,
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    # No dialog tool available -- fall back to a fixed, predictable folder.
    folder = Path.home() / "Pictures" / "ImmichWallpaper"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / default_name)


def _save_copy_worker(icon):
    entry = rotate.current_entry()
    if not entry:
        notify(icon, "No image loaded yet.")
        return
    src = Path(entry["path"])
    if not src.exists():
        notify(icon, "Current image file is no longer on disk.")
        return
    dest = pick_save_destination(src.name)
    if not dest:
        return  # user cancelled the dialog
    try:
        shutil.copy2(src, dest)
        notify(icon, f"Saved to {dest}")
    except OSError as e:
        notify(icon, f"Save failed: {e}")


def action_save_copy(icon, item):
    threading.Thread(target=_save_copy_worker, args=(icon,), daemon=True).start()


def _refresh_worker(icon):
    subprocess.run([sys.executable, str(HERE / "rotate.py"), "--once"], capture_output=True)
    refresh_icon(icon)
    state = rotate.load_state()
    if state.get("last_error"):
        notify(icon, state["last_error"])


def action_refresh(icon, item):
    threading.Thread(target=_refresh_worker, args=(icon,), daemon=True).start()


def action_toggle_pause(icon, item):
    state = rotate.load_state()
    rotate.set_paused(not state.get("paused"))
    refresh_icon(icon)


def action_back(icon, item):
    if rotate.navigate(-1):
        refresh_icon(icon)


def action_forward(icon, item):
    if rotate.can_go_forward():
        rotate.navigate(1)
        refresh_icon(icon)
    else:
        # Already at the newest -- "Forward" past the edge just fetches a
        # fresh photo instead of doing nothing.
        threading.Thread(target=_refresh_worker, args=(icon,), daemon=True).start()


def _settings_worker():
    url = "http://127.0.0.1:8877/"
    try:
        urllib.request.urlopen(url, timeout=1)
        webbrowser.open(url)
        return
    except Exception:
        pass
    subprocess.Popen([sys.executable, str(HERE / "config_ui.py")])


def action_settings(icon, item):
    threading.Thread(target=_settings_worker, daemon=True).start()


def action_quit(icon, item):
    stop_event.set()
    icon.stop()


# --------------------------------------------------------------------------
# Menu / icon setup
# --------------------------------------------------------------------------
def build_menu():
    return pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(image_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("◀ Back", action_back, enabled=back_enabled),
        pystray.MenuItem("Forward ▶", action_forward),
        pystray.MenuItem("View in browser", pystray.Menu(view_links_items), enabled=has_current_image),
        pystray.MenuItem("Save a copy...", action_save_copy, enabled=has_current_image),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh now", action_refresh),
        pystray.MenuItem(pause_toggle_text, action_toggle_pause),
        pystray.MenuItem("Settings...", action_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", action_quit),
    )


def _maybe_rotate():
    """Kicks off a background rotation check (self-gated by rotate.py's own
    interval logic, so calling this often is cheap -- it's a no-op except
    when actually due). This is what replaces a systemd timer: as long as
    the tray is running, this is the sole driver of periodic rotation."""
    if not rotate_lock.acquire(blocking=False):
        return
    try:
        subprocess.run([sys.executable, str(HERE / "rotate.py")], capture_output=True, timeout=120)
    except Exception:
        pass
    finally:
        rotate_lock.release()


def poll_loop(icon):
    last_status = None
    while not stop_event.is_set():
        threading.Thread(target=_maybe_rotate, daemon=True).start()
        state = rotate.load_state()
        status = status_for_state(state)
        if status != last_status:
            icon.icon = make_icon(status)
            last_status = status
        icon.update_menu()
        stop_event.wait(20)


def main():
    state = rotate.load_state()
    icon = pystray.Icon(
        "immich-wallpaper",
        make_icon(status_for_state(state)),
        "Immich Wallpaper",
        menu=build_menu(),
    )
    threading.Thread(target=poll_loop, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
