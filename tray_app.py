#!/usr/bin/env python3
"""System tray icon for the Immich wallpaper rotator.

Shows live status (active / paused / error), info about the currently
loaded photo with a one-click link to view it in Immich's web viewer, a
"save a copy" action, and quick controls (refresh now, pause/resume,
open settings).

Needs: python3-pystray, python3-pil (Pillow), and a StatusNotifierItem
provider -- on Debian/Ubuntu that's gir1.2-ayatanaappindicator3-0.1. Both
KDE Plasma's system tray and XFCE's status tray plugin implement this
protocol natively.
"""
from __future__ import annotations

import http.client
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import rotate
import settings

try:
    import pystray
    from PIL import Image, ImageDraw, ImageOps
except ImportError as error:
    print(f"Missing dependency: {error}")
    print("On Debian/Ubuntu: sudo apt install python3-pystray python3-pil "
          "gir1.2-ayatanaappindicator3-0.1")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
FLOWER_ASSET = HERE / "assets" / "immich-flower.png"

ICON_SIZE = 128
ERROR_BADGE = (220, 53, 69, 255)
SCREEN_COLOR_ACTIVE = (245, 245, 245, 255)
SCREEN_COLOR_GREYED = (150, 150, 150, 255)

# How often the tray refreshes its status and asks rotate.py whether a
# rotation is due (rotate.py applies the configured interval itself).
POLL_INTERVAL_SECONDS = 20
ROTATE_TIMEOUT_SECONDS = 120
CONFIG_UI_URL = f"http://127.0.0.1:{settings.CONFIG_UI_PORT}/"
CONFIG_UI_PROBE_TIMEOUT_SECONDS = 1

SAVE_DIALOG_FILTER = "Images (*.jpg *.jpeg *.png *.heic *.webp)"
PICTURES_DIR = Path.home() / "Pictures"

stop_event = threading.Event()
rotate_lock = threading.Lock()


# --------------------------------------------------------------------------
# Icon drawing: a monitor glyph (left half) + the Immich flower (right half).
# Colored while actively rotating, greyed out while paused/idle, with a small
# red badge overlaid when the last rotation failed.
# --------------------------------------------------------------------------
def _screen_glyph(size: int, color: tuple[int, int, int, int]):
    """Draw a monitor silhouette on a transparent square of `size` pixels.

    The screen is black with a colored outline (and a solid-colored stand);
    the black fill lets it properly occlude whatever is layered behind it.
    """
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    line_width = max(2, size // 14)
    bezel_width, bezel_height = int(size * 0.86), int(size * 0.62)
    bezel_left, bezel_top = (size - bezel_width) // 2, int(size * 0.06)
    bezel_right = bezel_left + bezel_width
    bezel_bottom = bezel_top + bezel_height
    draw.rounded_rectangle(
        [bezel_left, bezel_top, bezel_right, bezel_bottom],
        radius=int(size * 0.08), fill=(0, 0, 0, 255), outline=color,
        width=line_width)
    neck_width, neck_height = int(size * 0.12), int(size * 0.10)
    neck_left = size // 2 - neck_width // 2
    draw.rectangle(
        [neck_left, bezel_bottom, neck_left + neck_width,
         bezel_bottom + neck_height], fill=color)
    base_width, base_height = int(size * 0.40), max(2, int(size * 0.07))
    base_left = size // 2 - base_width // 2
    base_top = bezel_bottom + neck_height
    draw.rounded_rectangle(
        [base_left, base_top, base_left + base_width, base_top + base_height],
        radius=base_height // 2, fill=color)
    return image


def _flower_glyph(size: int, greyscale: bool):
    """Load the Immich flower at `size` pixels, optionally greyed out."""
    flower = Image.open(FLOWER_ASSET).convert("RGBA")
    flower = flower.resize((size, size), Image.LANCZOS)
    if not greyscale:
        return flower
    alpha = flower.split()[-1]
    grey = ImageOps.grayscale(flower.convert("RGB")).convert("RGBA")
    grey.putalpha(alpha)
    return grey


def make_icon(status: str):
    """Build the tray icon for `status`.

    `status` is one of "active", "paused", "error" or "unknown". The
    flower fills the whole canvas as a backdrop, and a slightly shrunk
    monitor glyph sits in front of it, bottom-left -- so the flower peeks
    out from behind the screen along its top and right edges.
    """
    size = ICON_SIZE
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    greyed = status in ("paused", "unknown")

    flower = _flower_glyph(size, greyscale=greyed)
    canvas.paste(flower, (0, 0), flower)

    screen_side = int(size * 0.67)
    screen_color = SCREEN_COLOR_GREYED if greyed else SCREEN_COLOR_ACTIVE
    screen = _screen_glyph(screen_side, screen_color)
    canvas.paste(screen, (0, size - screen_side), screen)

    if status == "error":
        radius = int(size * 0.15)
        center_x, center_y = size - radius - 4, radius + 4
        ImageDraw.Draw(canvas).ellipse(
            [center_x - radius, center_y - radius,
             center_x + radius, center_y + radius],
            fill=ERROR_BADGE, outline=(20, 20, 20, 255), width=3)
    return canvas


def status_for_state(state: dict) -> str:
    """Map the rotation state to an icon status string."""
    if state.get("paused"):
        return "paused"
    if state.get("last_error"):
        return "error"
    if settings.current_entry(state):
        return "active"
    return "unknown"


# --------------------------------------------------------------------------
# Text helpers (menu labels; pystray calls these with the menu item)
# --------------------------------------------------------------------------
def human_age(timestamp: float | None) -> str:
    """Describe how long ago `timestamp` was, e.g. "5m ago"."""
    if not timestamp:
        return "unknown"
    seconds = max(0, time.time() - timestamp)
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def status_text(item=None) -> str:
    """Menu label describing the current rotation status."""
    state = settings.load_state()
    if state.get("paused"):
        return "⏸ Paused"
    if state.get("last_error"):
        return f"⚠ Error {human_age(state.get('last_error_at'))}"
    if state.get("last_success"):
        return f"● Active — updated {human_age(state.get('last_success'))}"
    return "○ Waiting for first run"


def image_text(item=None) -> str:
    """Menu label describing the current wallpaper and its history slot."""
    entry = settings.current_entry()
    if not entry:
        return "No image loaded yet"
    names = [asset.get("original_filename") or "?"
             for asset in entry.get("assets", [])]
    size_kb = (entry.get("size_bytes") or 0) // 1024
    if entry.get("kind") == "pair":
        label = " + ".join(names)
    else:
        label = names[0] if names else Path(entry["path"]).name
    state = settings.load_state()
    slot = state.get("position", -1) + 1
    total = len(state.get("history") or [])
    return f"\U0001f5bc [{slot}/{total}] {label} ({size_kb} KB)"


def has_current_image(item=None) -> bool:
    """Whether there is a wallpaper the image-specific actions apply to."""
    return settings.current_entry() is not None


def pause_toggle_text(item=None) -> str:
    """Menu label for the pause/resume action."""
    paused = settings.load_state().get("paused")
    return "Resume rotation" if paused else "Pause rotation"


def back_enabled(item=None) -> bool:
    """Whether the "Back" action currently has anywhere to go."""
    return settings.can_go_back()


def _open_url_action(url: str | None):
    """Build a menu action that opens `url` in the browser."""
    def action(icon, item):
        if url:
            webbrowser.open(url)
    return action


def view_links_items(_menu=None) -> list:
    """Dynamic submenu contents for "View in browser".

    One entry per photo in the current wallpaper (1 for a single image,
    2 for a side-by-side portrait pair).
    """
    nothing_loaded = pystray.MenuItem("(nothing loaded)", None, enabled=False)
    entry = settings.current_entry()
    if not entry:
        return [nothing_loaded]
    items = [
        pystray.MenuItem(
            asset.get("original_filename") or f"Photo {index + 1}",
            _open_url_action(asset.get("web_url")))
        for index, asset in enumerate(entry.get("assets", []))
    ]
    return items or [nothing_loaded]


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
def notify(icon, message: str, title: str = "Immich Wallpaper") -> None:
    """Show a desktop notification, via the tray icon or notify-send."""
    try:
        icon.notify(message, title)
        return
    except Exception:  # noqa: BLE001
        # Tray backends fail in backend-specific ways when they don't
        # support notifications; fall back to notify-send below.
        pass
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], capture_output=True)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def refresh_icon(icon) -> None:
    """Redraw the tray icon and menu from the current state."""
    icon.icon = make_icon(status_for_state(settings.load_state()))
    icon.update_menu()


def _run_dialog(command: list[str]) -> str | None:
    """Run a file-dialog `command`; return the chosen path.

    Returns None if the dialog was cancelled.
    """
    result = subprocess.run(command, capture_output=True, text=True)
    chosen = result.stdout.strip()
    return chosen if result.returncode == 0 and chosen else None


def pick_save_destination(default_name: str) -> str | None:
    """Ask where to save a copy of the wallpaper; None means cancelled."""
    default_path = str(PICTURES_DIR / default_name)
    if shutil.which("kdialog"):
        return _run_dialog(["kdialog", "--getsavefilename", default_path,
                            SAVE_DIALOG_FILTER])
    if shutil.which("zenity"):
        return _run_dialog(["zenity", "--file-selection", "--save",
                            "--confirm-overwrite",
                            f"--filename={default_path}"])
    # No dialog tool available -- fall back to a fixed, predictable folder.
    folder = PICTURES_DIR / "ImmichWallpaper"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / default_name)


def _save_copy_worker(icon) -> None:
    entry = settings.current_entry()
    if not entry:
        notify(icon, "No image loaded yet.")
        return
    source = Path(entry["path"])
    if not source.exists():
        notify(icon, "Current image file is no longer on disk.")
        return
    destination = pick_save_destination(source.name)
    if not destination:
        return  # user cancelled the dialog
    try:
        shutil.copy2(source, destination)
        notify(icon, f"Saved to {destination}")
    except OSError as error:
        notify(icon, f"Save failed: {error}")


def action_save_copy(icon, item) -> None:
    """Menu action: save a copy of the current wallpaper."""
    threading.Thread(
        target=_save_copy_worker, args=(icon,), daemon=True).start()


def _refresh_worker(icon) -> None:
    subprocess.run(
        [sys.executable, str(HERE / "rotate.py"), "--once"],
        capture_output=True)
    refresh_icon(icon)
    state = settings.load_state()
    if state.get("last_error"):
        notify(icon, state["last_error"])


def action_refresh(icon, item) -> None:
    """Menu action: fetch a new wallpaper now."""
    threading.Thread(target=_refresh_worker, args=(icon,), daemon=True).start()


def action_toggle_pause(icon, item) -> None:
    """Menu action: pause or resume rotation."""
    paused = settings.load_state().get("paused")
    settings.set_paused(not paused)
    refresh_icon(icon)


def action_back(icon, item) -> None:
    """Menu action: step back to the previous wallpaper."""
    if rotate.navigate(-1):
        refresh_icon(icon)


def action_forward(icon, item) -> None:
    """Menu action: step forward, or fetch a new photo at the newest."""
    if settings.can_go_forward():
        rotate.navigate(1)
        refresh_icon(icon)
    else:
        # Already at the newest -- "Forward" past the edge just fetches a
        # fresh photo instead of doing nothing.
        threading.Thread(
            target=_refresh_worker, args=(icon,), daemon=True).start()


def _settings_worker() -> None:
    """Open the config UI, starting it first if it isn't already running."""
    try:
        urllib.request.urlopen(
            CONFIG_UI_URL, timeout=CONFIG_UI_PROBE_TIMEOUT_SECONDS)
        webbrowser.open(CONFIG_UI_URL)
        return
    except (OSError, http.client.HTTPException):
        pass  # nothing listening yet: start our own below
    subprocess.Popen([sys.executable, str(HERE / "config_ui.py")])


def action_settings(icon, item) -> None:
    """Menu action: open the settings page."""
    threading.Thread(target=_settings_worker, daemon=True).start()


def action_quit(icon, item) -> None:
    """Menu action: stop the polling loop and remove the tray icon."""
    stop_event.set()
    icon.stop()


# --------------------------------------------------------------------------
# Menu / icon setup
# --------------------------------------------------------------------------
def build_menu():
    """Assemble the tray icon's context menu."""
    return pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(image_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("◀ Back", action_back, enabled=back_enabled),
        pystray.MenuItem("Forward ▶", action_forward),
        pystray.MenuItem("View in browser", pystray.Menu(view_links_items),
                         enabled=has_current_image),
        pystray.MenuItem("Save a copy...", action_save_copy,
                         enabled=has_current_image),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh now", action_refresh),
        pystray.MenuItem(pause_toggle_text, action_toggle_pause),
        pystray.MenuItem("Settings...", action_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", action_quit),
    )


def _maybe_rotate() -> None:
    """Kick off a background rotation check.

    Self-gated by rotate.py's own interval logic, so calling this often is
    cheap -- it's a no-op except when actually due. This is what replaces
    a systemd timer: as long as the tray is running, this is the sole
    driver of periodic rotation.
    """
    if not rotate_lock.acquire(blocking=False):
        return
    try:
        subprocess.run(
            [sys.executable, str(HERE / "rotate.py")],
            capture_output=True, timeout=ROTATE_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        pass  # try again on the next poll
    finally:
        rotate_lock.release()


def poll_loop(icon) -> None:
    """Refresh the icon and trigger rotation checks until told to stop."""
    last_status = None
    while not stop_event.is_set():
        threading.Thread(target=_maybe_rotate, daemon=True).start()
        status = status_for_state(settings.load_state())
        if status != last_status:
            icon.icon = make_icon(status)
            last_status = status
        icon.update_menu()
        stop_event.wait(POLL_INTERVAL_SECONDS)


def main() -> None:
    """Create the tray icon and run it until quit."""
    icon = pystray.Icon(
        "immich-wallpaper",
        make_icon(status_for_state(settings.load_state())),
        "Immich Wallpaper",
        menu=build_menu(),
    )
    threading.Thread(target=poll_loop, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
