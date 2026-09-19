"""Minimal Immich REST API client (stdlib only).

Every function takes the server's base URL and an API key, and raises
urllib.error.URLError / HTTPError on network or HTTP failures.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 30


def _api_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/api" + path


def _open(
    base_url: str, api_key: str | None, path: str, timeout: int,
    method: str = "GET", body: dict | None = None,
) -> tuple[bytes, str | None]:
    """Perform one request; return (response bytes, Content-Type header)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        _api_url(base_url, path), data=data, method=method)
    if api_key:
        request.add_header("x-api-key", api_key)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return reply.read(), reply.getheader("Content-Type")


def _parse_json(raw: bytes) -> Any:
    return json.loads(raw) if raw else None


def get_json(
    base_url: str, api_key: str | None, path: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """GET `path`; return the parsed JSON (None if the reply is empty)."""
    raw, _ = _open(base_url, api_key, path, timeout)
    return _parse_json(raw)


def post_json(
    base_url: str, api_key: str | None, path: str, body: dict,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """POST `body` as JSON to `path`.

    Returns the parsed reply, or None if the reply is empty.
    """
    raw, _ = _open(
        base_url, api_key, path, timeout, method="POST", body=body)
    return _parse_json(raw)


def get_bytes(
    base_url: str, api_key: str | None, path: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, str | None]:
    """GET `path`; return (body bytes, Content-Type header)."""
    return _open(base_url, api_key, path, timeout)
