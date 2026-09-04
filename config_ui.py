#!/usr/bin/env python3
"""
Local configuration UI for the Immich wallpaper rotator.

Stdlib-only (no pip installs) so it runs unmodified on any desktop Linux
box with Python 3.8+ -- this machine (KDE Plasma) and a Manjaro XFCE box
alike. Binds to 127.0.0.1 only; nothing outside this machine can reach it.

Usage:
    python3 config_ui.py [--port 8877] [--no-browser]

Config is read/written at ~/.config/immich-wallpaper/config.json (mode 600).
"""
import json
import os
import stat
import sys
import argparse
import webbrowser
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "immich-wallpaper"
CONFIG_PATH = CONFIG_DIR / "config.json"
HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"

DEFAULT_CONFIG = {
    "immich_url": "",
    "api_key": "",
    "interval_minutes": 5,
    "keep_count": 2,
    "albums": [],
    "people": [],
    "person_match": "any",
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(CONFIG_PATH)


def immich_request(base_url, api_key, path, method="GET", body=None, headers_only=False):
    url = base_url.rstrip("/") + "/api" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if api_key:
        req.add_header("x-api-key", api_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        if headers_only:
            return resp.status, resp.getheader("Content-Type"), resp.read()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        if "application/json" in ctype:
            return json.loads(raw) if raw else None
        return raw


class Handler(BaseHTTPRequestHandler):
    server_version = "ImmichWallpaperConfigUI/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet; errors still raised to the client

    # ---- helpers -----------------------------------------------------
    def _send_json(self, obj, status=200):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def _path(self):
        from urllib.parse import urlparse
        return urlparse(self.path).path

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        path = self._path()
        if path == "/":
            html = INDEX_HTML.read_text()
            payload = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif path == "/api/config":
            self._send_json(load_config())
        elif path == "/api/person-thumb":
            q = self._query()
            person_id = q.get("person_id", [None])[0]
            immich_url = q.get("immich_url", [None])[0]
            api_key = q.get("api_key", [None])[0]
            if not (person_id and immich_url and api_key):
                self.send_response(400)
                self.end_headers()
                return
            try:
                data = immich_request(immich_url, api_key, f"/people/{person_id}/thumbnail")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(502)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
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

    # ---- handlers --------------------------------------------------------
    def _handle_test(self, body):
        url = (body.get("immich_url") or "").strip()
        key = (body.get("api_key") or "").strip()
        if not url or not key:
            self._send_json({"ok": False, "error": "Server URL and API key are both required."})
            return
        try:
            ping = immich_request(url, key, "/server/ping")
        except urllib.error.URLError as e:
            self._send_json({"ok": False, "error": f"Could not reach {url}: {e.reason}"})
            return
        except Exception as e:
            self._send_json({"ok": False, "error": f"Could not reach server: {e}"})
            return
        if not ping or "res" not in ping:
            self._send_json({"ok": False, "error": "Server responded but not with a valid Immich ping."})
            return
        try:
            albums = immich_request(url, key, "/albums")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._send_json({"ok": False, "error": "Server reachable, but the API key was rejected (401/403). Check the key and its permissions."})
            else:
                self._send_json({"ok": False, "error": f"Server reachable, but the auth check failed: HTTP {e.code}"})
            return
        except Exception as e:
            self._send_json({"ok": False, "error": f"Server reachable, but auth check failed: {e}"})
            return
        self._send_json({"ok": True, "album_count": len(albums) if isinstance(albums, list) else None})

    def _handle_albums(self, body):
        url = (body.get("immich_url") or "").strip()
        key = (body.get("api_key") or "").strip()
        try:
            albums = immich_request(url, key, "/albums")
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})
            return
        out = sorted(
            [{"id": a["id"], "name": a.get("albumName") or "(untitled album)", "count": a.get("assetCount", 0)} for a in albums],
            key=lambda a: a["name"].lower(),
        )
        self._send_json({"ok": True, "albums": out})

    def _handle_people(self, body):
        url = (body.get("immich_url") or "").strip()
        key = (body.get("api_key") or "").strip()
        people = []
        page = 1
        try:
            while True:
                resp = immich_request(url, key, f"/people?page={page}&size=250&withHidden=true")
                batch = resp.get("people", [])
                people.extend(batch)
                if not resp.get("hasNextPage") or not batch:
                    break
                page += 1
                if page > 40:  # safety valve, 10k people
                    break
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})
            return
        out = sorted(
            [{"id": p["id"], "name": p.get("name") or "(unnamed person)", "hidden": p.get("isHidden", False)} for p in people],
            key=lambda p: (p["name"] == "(unnamed person)", p["name"].lower()),
        )
        self._send_json({"ok": True, "people": out})

    def _handle_save(self, body):
        cfg = load_config()
        for key in ("immich_url", "api_key"):
            if key in body:
                cfg[key] = str(body[key]).strip()
        for key in ("interval_minutes", "keep_count"):
            if key in body:
                try:
                    cfg[key] = max(1, int(body[key]))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "error": f"{key} must be a whole number"}, 400)
                    return
        for key in ("albums", "people"):
            if key in body and isinstance(body[key], list):
                cfg[key] = body[key]
        if body.get("person_match") in ("any", "all", "both"):
            cfg["person_match"] = body["person_match"]
        save_config(cfg)
        self._send_json({"ok": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Immich wallpaper config UI running at {url}  (Ctrl+C to stop)")
    print(f"Config file: {CONFIG_PATH}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
