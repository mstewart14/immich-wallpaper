#!/usr/bin/env python3
"""Local configuration UI for the Immich wallpaper rotator.

Stdlib-only (no pip installs) so it runs unmodified on any desktop Linux
box with Python 3.8+ -- this machine (KDE Plasma) and a Manjaro XFCE box
alike. Binds to 127.0.0.1 only; nothing outside this machine can reach it.

Usage:
    python3 config_ui.py [--port 8877] [--no-browser]

Config is read/written at ~/.config/immich-wallpaper/config.json (mode 600).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import immich_api
import settings

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"

# Static files the page may request: URL path -> (file, content type).
# A fixed whitelist, so a request can never name an arbitrary file.
STATIC_ASSETS = {
    "/assets/app-icon.png": (HERE / "assets" / "app-icon.png", "image/png"),
}
ASSET_CACHE_SECONDS = 86400

DEFAULT_PORT = settings.CONFIG_UI_PORT
IMMICH_TIMEOUT_SECONDS = 15
# Applying a saved config runs a full rotation, which downloads photos.
APPLY_TIMEOUT_SECONDS = 90

PEOPLE_PAGE_SIZE = 250
# Safety valve on paging through people: 40 pages of 250 is 10k people.
MAX_PEOPLE_PAGES = 40

PERSON_MATCH_MODES = ("any", "all", "both")
WHOLE_NUMBER_KEYS = ("interval_minutes", "keep_count")
LIST_KEYS = ("albums", "people")
FLAG_KEYS = ("show_photo_info", "show_date_overlay")


def _credentials(body: dict[str, Any]) -> tuple[str, str]:
    """Return the (server URL, API key) from a request body, stripped."""
    url = (body.get("immich_url") or "").strip()
    key = (body.get("api_key") or "").strip()
    return url, key


class Handler(BaseHTTPRequestHandler):
    """Serves the settings page and its JSON API."""

    server_version = "ImmichWallpaperConfigUI/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the terminal quiet; errors are still sent to the client."""

    # ---- helpers -------------------------------------------------------
    def _send_json(self, obj: Any, status: int = 200) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_static_asset(self, file_path: Path, content_type: str) -> None:
        try:
            payload = file_path.read_bytes()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", f"max-age={ASSET_CACHE_SECONDS}")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _path(self) -> str:
        return urlparse(self.path).path

    # ---- routes --------------------------------------------------------
    def do_GET(self) -> None:
        """Route GET requests: the page, its assets, config and thumbnails."""
        path = self._path()
        if path == "/":
            payload = INDEX_HTML.read_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif path in STATIC_ASSETS:
            self._send_static_asset(*STATIC_ASSETS[path])
        elif path == "/api/config":
            self._send_json(settings.load_config())
        elif path == "/api/person-thumb":
            self._handle_person_thumbnail()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        """Route POST requests to the matching handler."""
        path = self._path()
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "invalid JSON body"}, 400)
            return

        if path == "/api/test":
            self._handle_test(body)
        elif path == "/api/albums":
            self._handle_albums(body)
        elif path == "/api/people":
            self._handle_people(body)
        elif path == "/api/save":
            self._handle_save(body)
        else:
            self.send_response(404)
            self.end_headers()

    # ---- handlers ------------------------------------------------------
    def _handle_person_thumbnail(self) -> None:
        query = self._query()
        person_id = query.get("person_id", [None])[0]
        immich_url = query.get("immich_url", [None])[0]
        api_key = query.get("api_key", [None])[0]
        if not (person_id and immich_url and api_key):
            self.send_response(400)
            self.end_headers()
            return
        try:
            data, _ = immich_api.get_bytes(
                immich_url, api_key, f"/people/{person_id}/thumbnail",
                timeout=IMMICH_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            # Whatever went wrong talking to Immich, answer with a 502
            # rather than dropping the connection.
            self.send_response(502)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _handle_test(self, body: dict[str, Any]) -> None:
        """Check that the server is reachable and the API key is accepted."""
        url, key = _credentials(body)
        if not url or not key:
            self._send_json({
                "ok": False,
                "error": "Server URL and API key are both required.",
            })
            return
        try:
            ping = immich_api.get_json(
                url, key, "/server/ping", timeout=IMMICH_TIMEOUT_SECONDS)
        except urllib.error.URLError as error:
            self._send_json({
                "ok": False,
                "error": f"Could not reach {url}: {error.reason}",
            })
            return
        except Exception as error:  # noqa: BLE001
            self._send_json({
                "ok": False, "error": f"Could not reach server: {error}",
            })
            return
        if not ping or "res" not in ping:
            self._send_json({
                "ok": False,
                "error": "Server responded but not with a valid Immich ping.",
            })
            return
        try:
            albums = immich_api.get_json(
                url, key, "/albums", timeout=IMMICH_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                message = ("Server reachable, but the API key was rejected "
                           "(401/403). Check the key and its permissions.")
            else:
                message = ("Server reachable, but the auth check failed: "
                           f"HTTP {error.code}")
            self._send_json({"ok": False, "error": message})
            return
        except Exception as error:  # noqa: BLE001
            self._send_json({
                "ok": False,
                "error": f"Server reachable, but auth check failed: {error}",
            })
            return
        self._send_json({
            "ok": True,
            "album_count": len(albums) if isinstance(albums, list) else None,
        })

    def _handle_albums(self, body: dict[str, Any]) -> None:
        """List the server's albums, sorted by name."""
        url, key = _credentials(body)
        try:
            albums = immich_api.get_json(
                url, key, "/albums", timeout=IMMICH_TIMEOUT_SECONDS)
        except Exception as error:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(error)})
            return
        listing = sorted(
            [{"id": album["id"],
              "name": album.get("albumName") or "(untitled album)",
              "count": album.get("assetCount", 0)} for album in albums],
            key=lambda album: album["name"].lower(),
        )
        self._send_json({"ok": True, "albums": listing})

    def _handle_people(self, body: dict[str, Any]) -> None:
        """List the server's people (all pages), named ones first."""
        url, key = _credentials(body)
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
        except Exception as error:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(error)})
            return
        listing = sorted(
            [{"id": person["id"],
              "name": person.get("name") or "(unnamed person)",
              "hidden": person.get("isHidden", False)} for person in people],
            key=lambda person: (person["name"] == "(unnamed person)",
                                person["name"].lower()),
        )
        self._send_json({"ok": True, "people": listing})

    def _handle_save(self, body: dict[str, Any]) -> None:
        """Save the submitted settings, then run one rotation.

        The rotation makes the change show up straight away.
        """
        config = settings.load_config()
        for key in ("immich_url", "api_key"):
            if key in body:
                config[key] = str(body[key]).strip()
        for key in WHOLE_NUMBER_KEYS:
            if key in body:
                try:
                    config[key] = max(1, int(body[key]))
                except (TypeError, ValueError):
                    message = f"{key} must be a whole number"
                    self._send_json({"ok": False, "error": message}, 400)
                    return
        for key in LIST_KEYS:
            if key in body and isinstance(body[key], list):
                config[key] = body[key]
        if body.get("person_match") in PERSON_MATCH_MODES:
            config["person_match"] = body["person_match"]
        for key in FLAG_KEYS:
            if key in body:
                config[key] = bool(body[key])
        settings.save_config(config)

        applied = False
        try:
            result = subprocess.run(
                [sys.executable, str(HERE / "rotate.py"), "--once"],
                capture_output=True, text=True, timeout=APPLY_TIMEOUT_SECONDS,
            )
            applied = result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            pass
        self._send_json({"ok": True, "applied": applied})


def main() -> None:
    """Run the config UI server until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Immich wallpaper config UI running at {url}  (Ctrl+C to stop)")
    print(f"Config file: {settings.CONFIG_PATH}")
    if not args.no_browser:
        # Opening a browser is a convenience; the URL is printed above.
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
