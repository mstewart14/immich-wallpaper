"""A small fake Immich server for tests.

Serves just enough of the API for rotation and the settings logic: ping,
albums, people, random search, asset details and original files. Requests
must carry the right API key, except /server/ping (public, as in Immich).
A key of "proxyblock" gets an empty-body 403 with a Server header, which
is what a reverse proxy refusing a client looks like.
"""
from __future__ import annotations

import io
import json

from PIL import Image

from tests.helpers import serve

API_KEY = "secret"


def jpeg(width: int, height: int, color=(120, 120, 120)) -> bytes:
    """A solid-colour JPEG."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(
        buffer, "JPEG", quality=95)
    return buffer.getvalue()


def asset(asset_id: str, width: int, height: int, name: str | None = None,
          mime: str = "image/jpeg") -> dict:
    """An asset record as /search/random returns it."""
    return {"id": asset_id, "originalFileName": name or f"{asset_id}.jpg",
            "originalMimeType": mime,
            "exifInfo": {"exifImageWidth": width, "exifImageHeight": height}}


class FakeImmich:
    """A running fake server; call stop() when done."""

    def __init__(self, assets: list[dict] | None = None,
                 images: dict[str, bytes] | None = None) -> None:
        """Start serving `assets` and their `images` (by asset id)."""
        self.assets = assets or []
        self.images = images or {}
        self.requests: list[tuple[str, str]] = []
        self._server, self.url = serve(self._handle)

    def stop(self) -> None:
        """Shut the server down."""
        self._server.shutdown()
        self._server.server_close()

    def _send(self, request, status: int, body: bytes,
              content_type: str = "application/json",
              headers: dict | None = None) -> None:
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            request.send_header(name, value)
        request.end_headers()
        request.wfile.write(body)

    def _handle(self, request) -> None:
        self.requests.append((request.command, request.path))
        path = request.path
        key = request.headers.get("x-api-key")
        if key == "proxyblock":
            self._send(request, 403, b"", headers={"Server": "traefik-test"})
        elif path == "/api/server/ping":
            self._send(request, 200, b'{"res": "pong"}')
        elif key != API_KEY:
            self._send(request, 401, b'{"message": "no"}')
        elif path == "/api/albums":
            self._send(request, 200, json.dumps(
                [{"id": "a1", "albumName": "Trip", "assetCount": 3}]
            ).encode())
        elif path == "/api/search/random":
            self._send(request, 200, json.dumps(self.assets).encode())
        elif path.endswith("/original"):
            self._send(request, 200, self.images[path.split("/")[3]],
                       "image/jpeg")
        elif path.startswith("/api/assets/"):
            self._send(request, 200, json.dumps({
                "people": [{"name": "Ann"}],
                "exifInfo": {"city": "Ottawa"}}).encode())
        else:
            self._send(request, 404, b"{}")
