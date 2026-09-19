"""Desktop-environment integration: wallpaper setting and screen geometry.

Each supported desktop is a DesktopBackend in BACKENDS. To support another
desktop, write its screen-size and set-wallpaper functions and register a
DesktopBackend for it; nothing else in the project needs to change.
"""
from __future__ import annotations

import functools
import glob
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)

FALLBACK_DPI = 96.0
XRANDR_TIMEOUT_SECONDS = 5
# A detected panel wider than this fraction of the screen is assumed to be
# a bad measurement (e.g. a multi-monitor work area) and ignored.
MAX_INSET_FRACTION = 0.4


# --------------------------------------------------------------------------
# Screen geometry (X11 tools; work via XWayland on Wayland sessions)
# --------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def get_screen_dpi() -> float:
    """Best-effort physical DPI via xrandr's per-monitor mm dimensions.

    Works via XWayland on a Wayland KDE session too. Falls back to the
    common 96 DPI default if detection fails for any reason. Cached --
    doesn't change within a single rotation, and each rotate.py invocation
    is a fresh short-lived process anyway.
    """
    try:
        result = subprocess.run(
            ["xrandr", "--query"], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return FALLBACK_DPI
    if result.returncode != 0:
        return FALLBACK_DPI
    match = (
        re.search(r"connected primary (\d+)x(\d+)\+\d+\+\d+.*?"
                  r"(\d+)mm x (\d+)mm", result.stdout)
        or re.search(r"connected (\d+)x(\d+)\+\d+\+\d+.*?"
                     r"(\d+)mm x (\d+)mm", result.stdout)
    )
    if not match:
        return FALLBACK_DPI
    width_px, height_px, width_mm, height_mm = map(int, match.groups())
    if width_mm <= 0 or height_mm <= 0:
        return FALLBACK_DPI
    horizontal_dpi = width_px / (width_mm / 25.4)
    vertical_dpi = height_px / (height_mm / 25.4)
    return (horizontal_dpi + vertical_dpi) / 2


@dataclass(frozen=True)
class Monitor:
    """One physical display and where it sits on the shared desktop.

    Attributes:
        name: Connector name, e.g. "HDMI-A-1". Stable across sessions, so it
            is what settings use to pick monitors.
        x: Left edge, in the desktop's shared coordinate space.
        y: Top edge, likewise (may be negative).
        width: Width in pixels, as currently oriented (a monitor turned on
            its side reports its rotated size).
        height: Height in pixels.
        primary: Whether the desktop treats it as the primary display.
    """

    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False


# One line of `xrandr --listmonitors`, e.g.
#   " 0: +*HDMI-A-1 2560/597x1440/336+0+0  HDMI-A-1"
# after the index come optional flags ('+' automatic, '*' primary), the
# monitor name, then width/mm x height/mm and signed x and y offsets.
_XRANDR_MONITOR_LINE = re.compile(
    r"^\s*\d+:\s+([+*]*)(\S+)\s+(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)")


def parse_xrandr_monitors(listing: str) -> list[Monitor]:
    """Parse `xrandr --listmonitors` output into Monitors.

    Lines that don't look like a monitor (the "Monitors: N" header, blanks)
    are ignored. The result is ordered left to right, then top to bottom.
    """
    monitors = []
    for line in listing.splitlines():
        match = _XRANDR_MONITOR_LINE.match(line)
        if match:
            flags, name, width, height, x, y = match.groups()
            monitors.append(Monitor(
                name=name, x=int(x), y=int(y), width=int(width),
                height=int(height), primary="*" in flags))
    return sorted(monitors, key=lambda monitor: (monitor.x, monitor.y))


def get_monitors_xrandr() -> list[Monitor]:
    """All monitors via xrandr; empty if xrandr is unavailable or fails.

    Works on X11 sessions and, through XWayland, on Wayland ones.
    """
    ensure_display_env()
    try:
        result = subprocess.run(
            ["xrandr", "--listmonitors"], capture_output=True, text=True,
            timeout=XRANDR_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return parse_xrandr_monitors(result.stdout)


def get_work_area() -> tuple[int, int, int, int] | None:
    """Best-effort usable-desktop-area query, as (x, y, width, height).

    Uses the EWMH _NET_WORKAREA root window property -- works via XWayland
    even on a Wayland KDE session, and natively under XFCE's X11. Returns
    None if xprop is missing, fails, or the output doesn't parse.
    """
    try:
        result = subprocess.run(
            ["xprop", "-root", "_NET_WORKAREA"],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"=\s*(-?\d+),\s*(-?\d+),\s*(\d+),\s*(\d+)",
                      result.stdout)
    return tuple(int(group) for group in match.groups()) if match else None


def screen_insets(screen_size: tuple[int, int] | None) -> dict[str, int]:
    """How many pixels on each screen edge are covered by a taskbar/panel.

    Derived from comparing the usable work area to the full screen size.
    All-zero (today's flat-margin behaviour) if detection fails or looks
    nonsensical -- e.g. a multi-monitor work area union wider than this one
    screen, which we'd rather ignore than risk a broken layout from.
    """
    no_insets = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    if not screen_size:
        return no_insets
    work_area = get_work_area()
    if not work_area:
        return no_insets
    work_x, work_y, work_width, work_height = work_area
    screen_width, screen_height = screen_size
    insets = {
        "left": max(0, work_x),
        "top": max(0, work_y),
        "right": max(0, screen_width - (work_x + work_width)),
        "bottom": max(0, screen_height - (work_y + work_height)),
    }
    if (insets["left"] > screen_width * MAX_INSET_FRACTION
            or insets["right"] > screen_width * MAX_INSET_FRACTION
            or insets["top"] > screen_height * MAX_INSET_FRACTION
            or insets["bottom"] > screen_height * MAX_INSET_FRACTION):
        return no_insets
    return insets


# --------------------------------------------------------------------------
# Session environment
# --------------------------------------------------------------------------
def ensure_dbus_env() -> None:
    """Point DBUS_SESSION_BUS_ADDRESS at the user's session bus if unset.

    Needed when launched from e.g. a systemd unit with a bare environment.
    """
    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        runtime_dir = os.environ.get(
            "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        bus_path = f"{runtime_dir}/bus"
        if os.path.exists(bus_path):
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"


def ensure_display_env() -> None:
    """Point DISPLAY at the first X11 socket if unset (see ensure_dbus_env)."""
    if "DISPLAY" not in os.environ:
        sockets = sorted(glob.glob("/tmp/.X11-unix/X*"))
        if sockets:
            display_number = os.path.basename(sockets[0])[1:]
            os.environ["DISPLAY"] = ":" + display_number


# --------------------------------------------------------------------------
# KDE Plasma
# --------------------------------------------------------------------------
def _run_plasma_script(script: str) -> subprocess.CompletedProcess:
    """Run a Plasma desktop-scripting `script` in the running plasmashell."""
    ensure_dbus_env()
    return subprocess.run(
        ["dbus-send", "--session", "--print-reply",
         "--dest=org.kde.plasmashell", "/PlasmaShell",
         "org.kde.PlasmaShell.evaluateScript", f"string:{script}"],
        capture_output=True, text=True,
    )


def get_screen_size_kde() -> tuple[int, int] | None:
    """Primary screen size as (width, height) via Plasma, or None."""
    result = _run_plasma_script(
        "print(screenGeometry(0).width + 'x' + screenGeometry(0).height);")
    if result.returncode != 0:
        return None
    match = re.search(r'string "(\d+)x(\d+)"', result.stdout)
    return (int(match.group(1)), int(match.group(2))) if match else None


def set_monitor_wallpapers_kde(images: dict[str, Path | str]) -> bool:
    """Set a wallpaper per monitor, leaving other monitors untouched.

    `images` maps a monitor's connector name to its image. Each name is
    resolved to Plasma's own screen, and only that screen's desktop is
    changed. Returns True only if every named monitor was found and set.
    """
    uris = {name: f"file://{path}" for name, path in images.items()}
    # Same plugin toggle as set_wallpaper_kde, which explains why it's needed.
    script = f'''
var images = {json.dumps(uris)};
var allDesktops = desktops();
var done = [];
for (var name in images) {{
    var screen = screenForConnector(name);
    if (screen < 0) continue;
    for (var i = 0; i < allDesktops.length; i++) {{
        var d = allDesktops[i];
        if (d.screen != screen) continue;
        d.wallpaperPlugin = "org.kde.color";
        d.wallpaperPlugin = "org.kde.image";
        d.currentConfigGroup = Array("Wallpaper", "org.kde.image", "General");
        d.writeConfig("Image", images[name]);
        d.writeConfig("FillMode", 1);
        done.push(name);
    }}
}}
print("applied|" + done.join("|") + "|end");
'''
    result = _run_plasma_script(script)
    if result.returncode != 0:
        logger.error("KDE wallpaper set failed: %s", result.stderr.strip())
        return False
    match = re.search(r"applied\|(.*?)\|?end", result.stdout)
    applied = set(filter(None, match.group(1).split("|"))) if match else set()
    missing = [name for name in images if name not in applied]
    if missing:
        logger.error("KDE has no screen for monitor(s): %s",
                     ", ".join(missing))
    return not missing


def get_monitors_kde() -> list[Monitor]:
    """All monitors on KDE Plasma.

    Connector names and the primary flag come from xrandr; each name is
    then resolved to Plasma's own screen and its geometry is read from
    Plasma, which is what wallpapers are laid out against. If xrandr gives
    nothing, Plasma's screens are listed as "Screen 1", "Screen 2", ....
    """
    named = get_monitors_xrandr()
    names = [monitor.name for monitor in named]
    script = f'''
function emit(label, g) {{
    print("monitor|" + label + "|" + g.left + "|" + g.top + "|"
          + g.width + "|" + g.height);
}}
var names = {json.dumps(names)};
for (var i = 0; i < names.length; i++) {{
    var screen = screenForConnector(names[i]);
    if (screen >= 0) emit(names[i], screenGeometry(screen));
}}
if (names.length == 0) {{
    for (var s = 0; s < screenCount; s++)
        emit("Screen " + (s + 1), screenGeometry(s));
}}
'''
    result = _run_plasma_script(script)
    if result.returncode != 0:
        return []
    primary = {monitor.name for monitor in named if monitor.primary}
    monitors = [
        Monitor(name=name, x=int(x), y=int(y), width=int(width),
                height=int(height), primary=name in primary)
        for name, x, y, width, height in re.findall(
            r"monitor\|([^|]+)\|(-?\d+)\|(-?\d+)\|(\d+)\|(\d+)",
            result.stdout)
    ]
    if monitors and not any(monitor.primary for monitor in monitors):
        monitors[0] = replace(monitors[0], primary=True)
    return sorted(monitors, key=lambda monitor: (monitor.x, monitor.y))


def set_wallpaper_kde(image_path: Path | str) -> bool:
    """Set `image_path` as the wallpaper on every Plasma desktop.

    Returns True on success; logs and returns False on failure.
    """
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
    result = _run_plasma_script(script)
    if result.returncode != 0:
        logger.error("KDE wallpaper set failed: %s", result.stderr.strip())
        return False
    if "error" in result.stdout.lower() and "Error: 0" not in result.stdout:
        logger.error("KDE wallpaper set returned an error: %s",
                     result.stdout.strip())
        return False
    return True


# --------------------------------------------------------------------------
# XFCE
# --------------------------------------------------------------------------
def get_screen_size_xfce() -> tuple[int, int] | None:
    """Primary screen size as (width, height) via xrandr, or None."""
    result = subprocess.run(
        ["xrandr", "--query"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    # Prefer the monitor xrandr marks "primary"; fall back to the first
    # connected+active one otherwise (e.g. "eDP-1 connected 1920x1080+0+0").
    match = (re.search(r"connected primary (\d+)x(\d+)\+", result.stdout)
             or re.search(r"connected (\d+)x(\d+)\+", result.stdout))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _xfce_image_properties() -> list[str] | None:
    """List every xfce4-desktop `last-image` property.

    There is one per monitor and workspace. Returns None (after logging) if
    they can't be listed.
    """
    ensure_dbus_env()
    ensure_display_env()
    list_result = subprocess.run(
        ["xfconf-query", "-c", "xfce4-desktop", "-l"],
        capture_output=True, text=True)
    if list_result.returncode != 0:
        logger.error("xfconf-query -l failed: %s",
                     list_result.stderr.strip())
        return None
    image_properties = [
        name for name in list_result.stdout.splitlines()
        if name.endswith("last-image")
    ]
    if not image_properties:
        logger.error("No xfce4-desktop 'last-image' properties found "
                     "(no monitors configured yet?).")
        return None
    return image_properties


def _xfce_apply(assignments: dict[str, Path | str]) -> bool:
    """Set each `last-image` property to its image, scaled, then reload.

    Returns True if every property was set; failures are logged.
    """
    all_set = True
    for image_property, image_path in assignments.items():
        result = subprocess.run(
            ["xfconf-query", "-c", "xfce4-desktop", "-p", image_property,
             "-s", str(image_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error("Failed to set %s: %s",
                         image_property, result.stderr.strip())
            all_set = False
        # image-style 4 = "Scaled": fit the whole image, letterboxed, no crop
        # (5 = "Zoomed" crops to fill, which is what was clipping portraits)
        style_property = (
            image_property[:-len("last-image")] + "image-style")
        subprocess.run(
            ["xfconf-query", "-c", "xfce4-desktop", "-p", style_property,
             "-s", "4"],
            capture_output=True, text=True,
        )
    subprocess.run(["xfdesktop", "--reload"], capture_output=True, text=True)
    return all_set


def set_wallpaper_xfce(image_path: Path | str) -> bool:
    """Set `image_path` as the wallpaper on every XFCE monitor/workspace.

    Returns True if every property was set; logs and returns False on
    failure.
    """
    image_properties = _xfce_image_properties()
    if image_properties is None:
        return False
    return _xfce_apply({name: image_path for name in image_properties})


def set_monitor_wallpapers_xfce(images: dict[str, Path | str]) -> bool:
    """Set a wallpaper per monitor, leaving other monitors untouched.

    `images` maps a monitor's connector name to its image. XFCE keeps one
    setting per monitor, named `monitor<connector>`. Returns True only if
    every named monitor was found and set.
    """
    image_properties = _xfce_image_properties()
    if image_properties is None:
        return False
    assignments: dict[str, Path | str] = {}
    missing = []
    for name, image_path in images.items():
        matches = [prop for prop in image_properties
                   if f"/monitor{name}/" in prop]
        if not matches:
            missing.append(name)
        for prop in matches:
            assignments[prop] = image_path
    if missing:
        logger.error("No xfce4-desktop settings for monitor(s): %s",
                     ", ".join(missing))
    if not assignments:
        return False
    return _xfce_apply(assignments) and not missing


# --------------------------------------------------------------------------
# Backend registry and dispatch
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DesktopBackend:
    """One supported desktop.

    To add a desktop: write its screen_size / set_wallpaper functions above
    and append a DesktopBackend to BACKENDS.

    Attributes:
        name: Short identifier, e.g. "kde".
        xdg_names: Lowercase substrings matched against
            $XDG_CURRENT_DESKTOP.
        process: Process name to pgrep for when the env var doesn't match
            (e.g. when launched from a systemd unit with a bare env).
        screen_size: Called with no arguments; returns (width, height), or
            None if it can't be determined.
        set_wallpaper: Called with the image path; returns True on success,
            or False (after logging) on failure.
        monitors: Called with no arguments; returns the connected Monitors
            ordered left to right, or an empty list if they can't be
            determined.
        set_monitor_wallpapers: Optional. Called with a dict of monitor
            connector name -> image path; sets each named monitor and
            leaves the others alone. Returns True only if all were set.
            None if the desktop can't set monitors individually.

    """

    name: str
    xdg_names: tuple[str, ...]
    process: str | None
    screen_size: Callable[[], tuple[int, int] | None]
    set_wallpaper: Callable[[Path], bool]
    monitors: Callable[[], list[Monitor]] = get_monitors_xrandr
    set_monitor_wallpapers: Callable[[dict[str, Path]], bool] | None = None


# Order matters: earlier entries win when several would match.
BACKENDS = [
    DesktopBackend(
        "kde", ("kde",), "plasmashell",
        get_screen_size_kde, set_wallpaper_kde, get_monitors_kde,
        set_monitor_wallpapers_kde),
    DesktopBackend(
        "xfce", ("xfce",), "xfce4-session",
        get_screen_size_xfce, set_wallpaper_xfce, get_monitors_xrandr,
        set_monitor_wallpapers_xfce),
]


def _process_running(name: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-x", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def current_backend() -> DesktopBackend | None:
    """Return the DesktopBackend for the running session, or None.

    Checks $XDG_CURRENT_DESKTOP across all backends first, and only then
    falls back to looking for each backend's session process.
    """
    current_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    for backend in BACKENDS:
        if any(name in current_desktop for name in backend.xdg_names):
            return backend
    for backend in BACKENDS:
        if backend.process and _process_running(backend.process):
            return backend
    return None


def detect_desktop() -> str | None:
    """Name of the detected desktop (e.g. "kde"), or None if unsupported."""
    backend = current_backend()
    return backend.name if backend else None


def get_screen_size() -> tuple[int, int] | None:
    """Screen size as (width, height) on the detected desktop, or None."""
    backend = current_backend()
    return backend.screen_size() if backend else None


def get_monitors() -> list[Monitor]:
    """All monitors on the detected desktop, left to right.

    Empty if the desktop is unsupported or the monitors can't be determined.
    """
    backend = current_backend()
    return backend.monitors() if backend else []


def supports_monitor_wallpapers() -> bool:
    """Whether the detected desktop can set monitors individually."""
    backend = current_backend()
    return bool(backend and backend.set_monitor_wallpapers)


def set_monitor_wallpapers(images: dict[str, Path]) -> bool:
    """Set a wallpaper per monitor, leaving other monitors untouched.

    `images` maps connector names to image paths. Returns True only if
    every named monitor was set; logs and returns False if the desktop
    can't set monitors individually.
    """
    backend = current_backend()
    if not backend or not backend.set_monitor_wallpapers:
        logger.error("This desktop can't set wallpapers per monitor.")
        return False
    return backend.set_monitor_wallpapers(images)


def set_wallpaper(image_path: Path | str) -> bool:
    """Set `image_path` as the wallpaper on the detected desktop.

    Returns True on success; logs and returns False if the desktop is
    unsupported or the backend fails.
    """
    backend = current_backend()
    if not backend:
        supported = " / ".join(b.name for b in BACKENDS)
        logger.error("Could not detect a supported desktop environment "
                     "(looked for: %s).", supported)
        return False
    return backend.set_wallpaper(image_path)
