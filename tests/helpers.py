"""Shared test helpers: small local HTTP servers."""
from __future__ import annotations

import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


class Recorder:
    """Requests seen by a test server: (method, path, headers dict)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []


def serve(
    handler: Callable[[BaseHTTPRequestHandler], None],
    recorder: Recorder | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    """Start a server that calls `handler(request_handler)` per request.

    Returns (server, base URL). Call server.shutdown() when done.
    """
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _dispatch(self) -> None:
            if recorder is not None:
                recorder.requests.append(
                    (self.command, self.path, dict(self.headers.items())))
            length = int(self.headers.get("Content-Length") or 0)
            self.body = self.rfile.read(length) if length else b""
            # The client may give up mid-reply (e.g. it hit a size limit).
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                handler(self)

        do_GET = do_POST = _dispatch  # noqa: N815 (http.server's names)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def reply(
    request: BaseHTTPRequestHandler, status: int = 200, body: bytes = b"{}",
    content_type: str = "application/json", headers: dict | None = None,
) -> None:
    """Send a complete reply from inside a serve() handler."""
    request.send_response(status)
    request.send_header("Content-Type", content_type)
    request.send_header("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        request.send_header(name, value)
    request.end_headers()
    request.wfile.write(body)
