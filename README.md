# immich-wallpaper

Rotate your desktop wallpaper using photos pulled live from your own
[Immich](https://immich.app) library — filtered by album or person, changed
on whatever interval you like, with a system tray icon for status and quick
controls.

Two portrait photos are automatically paired side-by-side into a single
composite when your screen resolution can be detected; landscape/square
photos are shown singly.

Only ever a handful of images sit on disk at once — the app asks Immich's
API for a random photo on each rotation and keeps a small rolling window
(your choice how many) rather than syncing your whole library locally.

## Features

- **Config UI** (`config_ui.py`) — a local web page (bind to `127.0.0.1`
  only) for entering your Immich server URL/API key, picking albums and/or
  people to pull from, and setting the rotation interval and how many
  images to keep on disk.
- **Rotator** (`rotate.py`) — does the actual work: picks a random photo
  (or two portraits, paired), downloads it, sets it as the desktop
  wallpaper, and keeps a bounded back/forward history.
- **Tray icon** (`tray_app.py`) — shows live status (active / paused /
  error), lets you step back/forward through recently-shown photos, open
  the current photo in Immich's web viewer, save a copy elsewhere, refresh
  on demand, pause/resume, and jump to settings.
- **Desktop support**: KDE Plasma (via D-Bus scripting — no QtWebEngine
  involved) and XFCE (via `xfconf-query`/`xrandr`). Desktop is
  auto-detected at runtime.
- No syncing, no local mirror of your library — everything is fetched
  on-demand from Immich's REST API.

## Installing

### Debian / Ubuntu

```
sudo apt install ./immich-wallpaper_<version>_all.deb
```

(or build it yourself — see `packaging/debian/build.sh`)

This pulls in `python3-pystray`, `python3-pil`, `python3-gi`, and
`gir1.2-ayatanaappindicator3-0.1` automatically, installs the app under
`/usr/share/immich-wallpaper/`, and registers `/etc/xdg/autostart` so the
tray icon starts on login for any user.

### Arch / Manjaro

```
cd packaging/arch
makepkg -si
```

`python-pystray` isn't in the official repos — this `PKGBUILD` pulls it
from the AUR as a dependency, so you'll need an AUR helper (`yay`, `paru`,
...) or to build `python-pystray` from the AUR yourself first.

### Manual / from source

Needs Python 3.8+, Pillow, pystray, PyGObject, and an AppIndicator
provider. On Debian/Ubuntu:

```
sudo apt install python3-pystray python3-pil python3-gi gir1.2-ayatanaappindicator3-0.1
python3 tray_app.py
```

The tray app drives its own periodic rotation internally (no cron/systemd
timer needed) — as long as it's running, it self-paces against the
interval you set in the config UI.

## Setup

1. Run the tray icon (or `python3 config_ui.py` directly) and open
   `http://127.0.0.1:8877`.
2. Enter your Immich server URL and an API key. Create the key under
   **Account Settings → API Keys** in Immich, and grant only:
   `asset.read`, `asset.download`, `album.read`, `person.read` — no
   write/delete/admin scopes needed. (If your Immich version doesn't offer
   granular scopes, an all-permissions key also works.)
3. Test the connection, then load and pick albums and/or people to filter
   by (leave both empty to pull from your whole library).
4. Set the rotation interval and how many images to keep on disk, save.
5. From the tray icon menu: **Refresh now** to pull the first photo, or
   just wait for the interval to elapse.

## How it decides portrait pairing

On each rotation, a batch of random candidates (filtered by your
album/person selection) is fetched with EXIF data. If the first pick is
portrait-oriented and the screen resolution can be detected, a second
portrait from the same batch is paired with it into one composite image
sized exactly to your screen (via KDE's `screenGeometry()` scripting API,
or `xrandr` on XFCE). Otherwise the photo is shown singly, letting the
desktop's own wallpaper fill mode handle scaling.

## License

MIT — see [LICENSE](LICENSE).

The Immich flower logo used in the tray icon is
[Immich](https://github.com/immich-app/immich)'s own trademark, used here
only to indicate integration with their project — this is an independent,
unofficial companion tool and isn't affiliated with or endorsed by the
Immich project.
