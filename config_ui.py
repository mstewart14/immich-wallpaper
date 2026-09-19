#!/usr/bin/env python3
"""Web settings page for the Immich wallpaper rotator (non-Linux platforms).

On Linux the settings screen is the native window (settings_window.py) and
this server is neither installed nor started. It remains for platforms where
GTK is not practical to install, and it needs nothing beyond the standard
library.

Usage:
    python3 config_ui.py [--port 8877] [--no-browser]

Config is read/written at ~/.config/immich-wallpaper/config.json (mode 600).

Security model. The page can change what the rotator does and talks to your
Immich server with your API key, and any web page in your browser can try to
reach a server on localhost, so it does not rely on "only I can reach it":

* Every request must carry a random per-run access token. It is delivered
  once, in the link this program opens (and prints), and becomes an
  HttpOnly, SameSite=Strict cookie. Other web pages and other local users
  do not have it.
* The Host header must be this server's own address (blocking DNS
  rebinding), any Origin header must be this same origin, and POSTs must be
  application/json.
* The API key is never sent to the browser. Requests that need it use the
  saved key, and only for the saved server URL, so a typo can't send it
  somewhere else.
* Request bodies are size-limited and connections time out.
* Responses carry a strict Content-Security-Policy and other hardening
  headers, and all validation is the shared settings_service.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.cookies
import json
import re
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import settings
import settings_service as service

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"

# Static files the page may request: URL path -> (file, content type).
# A fixed whitelist, so a request can never name an arbitrary file.
STATIC_ASSETS = {
    "/assets/app-icon.png": (HERE / "assets" / "app-icon.png", "image/png"),
}
ASSET_CACHE_SECONDS = 86400

DEFAULT_PORT = settings.CONFIG_UI_PORT
REQUEST_TIMEOUT_SECONDS = 30
MAX_BODY_BYTES = 1024 * 1024
SESSION_COOKIE = "iw_session"
TOKEN_HEADER = "X-IW-Token"  # noqa: S105 (a header name, not a secret)
CONTENT_LENGTH = re.compile(r"[0-9]{1,10}")


def content_security_policy(html: str) -> str:
    """A policy that allows only the page's own inline script and style.

    They are allowed by content hash, so no `unsafe-inline` is needed, and
    nothing else may run or load: no other origins, frames or forms.
    """
    def hashes(tag: str) -> str:
        found = re.findall(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", html,
                           flags=re.DOTALL)
        digests = (base64.b64encode(
            hashlib.sha256(text.encode()).digest()).decode()
            for text in found)
        return " ".join(f"'sha256-{digest}'" for digest in digests) or "'none'"

    return "; ".join([
        "default-src 'none'",
        f"script-src {hashes('script')}",
        f"style-src {hashes('style')}",
        "img-src 'self' blob:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])


class Handler(BaseHTTPRequestHandler):
    """Serves the settings page and its JSON API."""

    server_version = "ImmichWallpaperConfigUI/1.0"
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the terminal quiet; errors are still sent to the client."""

    def end_headers(self) -> None:
        """Add the hardening headers every response carries."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        super().end_headers()

    # ---- access control -----------------------------------------------
    def _own_hosts(self) -> set[str]:
        address = cast("tuple[str, int]", self.server.server_address)
        port = address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _request_is_local(self) -> bool:
        """Whether the Host and Origin headers name this server itself.

        A wrong Host is the mark of DNS rebinding, and a foreign Origin the
        mark of a page on another site talking to us.
        """
        hosts = self._own_hosts()
        if self.headers.get("Host", "").lower() not in hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.lower() in {
            f"http://{host}" for host in hosts}

    def _supplied_token(self) -> str:
        cookies = http.cookies.SimpleCookie()
        with contextlib.suppress(http.cookies.CookieError):
            cookies.load(self.headers.get("Cookie", ""))
        cookie = cookies.get(SESSION_COOKIE)
        return ((cookie.value if cookie else "")
                or self.headers.get(TOKEN_HEADER, ""))

    def _token_matches(self, supplied: str) -> bool:
        token = getattr(self.server, "token", "")
        return bool(token and supplied) and secrets.compare_digest(
            supplied.encode(), token.encode())

    def _authorized(self) -> bool:
        return self._token_matches(self._supplied_token())

    # ---- responses ----------------------------------------------------
    def _send(
        self, status: int, body: bytes, content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json",
                   {"Cache-Control": "no-store"})

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode(), "text/plain; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _send_page(self) -> None:
        html = INDEX_HTML.read_text()
        self._send(200, html.encode(), "text/html; charset=utf-8", {
            "Cache-Control": "no-store",
            "Content-Security-Policy": content_security_policy(html),
        })

    def _send_static_asset(self, file_path: Path, content_type: str) -> None:
        try:
            payload = file_path.read_bytes()
        except OSError:
            self._send_text(404, "Not found")
            return
        self._send(200, payload, content_type,
                   {"Cache-Control": f"max-age={ASSET_CACHE_SECONDS}"})

    def _reject_not_authorized(self) -> None:
        self._send_text(
            403, "Forbidden. Open the settings from the tray icon, or use "
                 "the link printed by config_ui.py.")

    # ---- request parsing ----------------------------------------------
    def _path(self) -> str:
        return urlparse(self.path).path

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _read_json_body(self) -> dict[str, Any] | None:
        """The request's JSON object, or None after sending an error."""
        if self.headers.get_content_type() != "application/json":
            self._send_json({"ok": False, "error":
                             "Content-Type must be application/json"}, 415)
            return None
        declared = self.headers.get("Content-Length")
        if declared is None:
            return {}
        if not CONTENT_LENGTH.fullmatch(declared):
            self._send_json({"ok": False, "error": "bad Content-Length"}, 400)
            return None
        length = int(declared)
        if length > MAX_BODY_BYTES:
            self._send_json({"ok": False, "error": "request too large"}, 413)
            return None
        try:
            raw = self.rfile.read(length)
        except OSError:
            self._send_json({"ok": False, "error": "request timed out"}, 408)
            return None
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._send_json({"ok": False, "error": "invalid JSON body"}, 400)
            return None
        return body

    # ---- routes -------------------------------------------------------
    def do_GET(self) -> None:
        """Route GET requests: the page, its assets, config and monitors."""
        if not self._request_is_local():
            self._send_text(403, "Forbidden")
            return
        path = self._path()
        if path == "/ping":
            self._send_text(200, "ok")  # for the tray: nothing sensitive
            return
        if path == "/" and "token" in self._query():
            self._start_session(self._query()["token"][0])
            return
        if not self._authorized():
            self._reject_not_authorized()
            return
        if path == "/":
            self._send_page()
        elif path in STATIC_ASSETS:
            self._send_static_asset(*STATIC_ASSETS[path])
        elif path == "/api/config":
            self._send_json(self._config_for_page())
        elif path == "/api/monitors":
            self._send_json(service.describe_monitors())
        else:
            self._send_text(404, "Not found")

    def do_POST(self) -> None:
        """Route POST requests to the matching handler."""
        if not self._request_is_local():
            self._send_text(403, "Forbidden")
            return
        if not self._authorized():
            self._reject_not_authorized()
            return
        body = self._read_json_body()
        if body is None:
            return
        routes = {
            "/api/test": self._handle_test,
            "/api/albums": self._handle_albums,
            "/api/people": self._handle_people,
            "/api/person-thumb": self._handle_person_thumbnail,
            "/api/save": self._handle_save,
        }
        handler = routes.get(self._path())
        if handler is None:
            self._send_text(404, "Not found")
        else:
            handler(body)

    def _start_session(self, supplied: str) -> None:
        """Trade the link's token for a session cookie, then go to the page."""
        if not self._token_matches(supplied):
            self._reject_not_authorized()
            return
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={supplied}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _config_for_page(self) -> dict[str, Any]:
        """The saved config, without the API key (which stays server-side)."""
        config = settings.load_config()
        api_key_set = bool(config.get("api_key"))
        config["api_key"] = ""
        config["api_key_set"] = api_key_set
        return config

    # ---- handlers -----------------------------------------------------
    def _with_credentials(self, body: dict[str, Any], action) -> None:
        """Run action(url, key) and send its result.

        A credential problem is sent as a normal error reply instead.
        """
        try:
            url, key = service.resolve_credentials(body)
        except service.SettingsError as error:
            self._send_json({"ok": False, "error": str(error)})
            return
        self._send_json(action(url, key))

    def _handle_test(self, body: dict[str, Any]) -> None:
        self._with_credentials(body, service.check_connection)

    def _handle_albums(self, body: dict[str, Any]) -> None:
        self._with_credentials(body, service.list_albums)

    def _handle_people(self, body: dict[str, Any]) -> None:
        self._with_credentials(body, service.list_people)

    def _handle_person_thumbnail(self, body: dict[str, Any]) -> None:
        """A person's face thumbnail, fetched with the right credentials.

        This is a POST so the API key never appears in a URL.
        """
        try:
            url, key = service.resolve_credentials(body)
            data = service.fetch_person_thumbnail(
                url, key, str(body.get("person_id") or ""))
        except (service.SettingsError, ValueError):
            self._send_text(400, "Bad request")
            return
        except Exception as error:  # noqa: BLE001
            # Whatever went wrong talking to Immich, answer with a 502
            # rather than dropping the connection.
            with contextlib.suppress(Exception):
                error.close()  # type: ignore[attr-defined]
            self._send_text(502, "Bad gateway")
            return
        self._send(200, data, "image/jpeg", {"Cache-Control": "no-store"})

    def _handle_save(self, body: dict[str, Any]) -> None:
        result = service.save_settings(body)
        self._send_json(result, 200 if result["ok"] else 400)


def publish_token(token: str) -> None:
    """Make the run's access token available to the tray, owner-only."""
    settings.ensure_private_dir(settings.CACHE_DIR)
    with settings.open_private(settings.UI_TOKEN_PATH) as handle:
        handle.write(token.encode())


def main() -> None:
    """Run the web settings page until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    token = secrets.token_urlsafe(32)
    server.token = token  # type: ignore[attr-defined]
    publish_token(token)
    url = f"http://127.0.0.1:{args.port}/?token={token}"
    print(f"Immich wallpaper settings running at {url}  (Ctrl+C to stop)")
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
    finally:
        settings.UI_TOKEN_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
