"""Minimal Immich REST API client (stdlib only).

Every function takes the server's base URL and an API key. Failures raise
urllib.error.URLError / HTTPError, and UnsafeUrlError (a ValueError) for a
server URL that is not a plain http(s) address.

The client is deliberately strict, because the URL comes from a settings
file and the API key travels with every request:

* only http and https URLs are accepted, with no embedded credentials;
* a redirect is followed only within the same host, so a server (or proxy)
  cannot bounce a request, and the API key with it, to another address;
* replies are size-limited, so a misbehaving server cannot exhaust memory;
* identifiers put into a URL path are quoted as a single path segment.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 30
ALLOWED_SCHEMES = ("http", "https")
# Generous ceilings that no legitimate reply comes near.
MAX_JSON_BYTES = 50 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024


class UnsafeUrlError(ValueError):
    """The server URL is not something we are willing to send requests to."""


class ResponseTooLargeError(urllib.error.URLError):
    """The server sent more data than we are willing to read."""


def validate_base_url(url: str) -> str:
    """Return `url` tidied for use as an API base, or raise UnsafeUrlError.

    It must be an http:// or https:// address with a host, and no username,
    password, query or fragment. A trailing slash is removed.
    """
    text = (url or "").strip()
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in text):
        raise UnsafeUrlError("The server URL must not contain spaces or "
                             "control characters.")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            "The server URL must start with http:// or https://")
    if not parts.hostname:
        raise UnsafeUrlError("The server URL has no host name.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError(
            "The server URL must not contain a username or password.")
    if parts.query or parts.fragment:
        raise UnsafeUrlError(
            "The server URL must not contain a query or fragment.")
    try:
        parts.port  # noqa: B018 -- raises ValueError for a bad port number
    except ValueError as error:
        raise UnsafeUrlError("The server URL has an invalid port.") from error
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc, parts.path.rstrip("/"), "", ""))


def quote_segment(value: object) -> str:
    """Make `value` safe to use as one segment of a URL path.

    Slashes and other special characters are percent-encoded, so an
    identifier can't reach a different endpoint. "." and ".." are refused
    because a client or server may resolve them as path steps.
    """
    text = str(value)
    if text in (".", ".."):
        raise ValueError(f"not a valid identifier: {text!r}")
    return urllib.parse.quote(text, safe="")


def _api_url(base_url: str, path: str) -> str:
    if not path.startswith("/") or any(c in path for c in "\r\n"):
        raise ValueError(f"invalid API path: {path!r}")
    return validate_base_url(base_url) + "/api" + path


def _effective_port(parts: urllib.parse.SplitResult) -> int | None:
    if parts.port is not None:
        return parts.port
    return {"http": 80, "https": 443}.get(parts.scheme.lower())


def redirect_allowed(old_url: str, new_url: str) -> bool:
    """Whether a redirect may be followed without leaking the API key.

    Only within the same host name. The scheme must stay the same, except
    that an upgrade from http to https is fine (a proxy forcing TLS); a
    downgrade never is.
    """
    old = urllib.parse.urlsplit(old_url)
    new = urllib.parse.urlsplit(new_url)
    if new.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    if (old.hostname or "").lower() != (new.hostname or "").lower():
        return False
    if old.scheme.lower() == new.scheme.lower():
        return _effective_port(old) == _effective_port(new)
    return old.scheme.lower() == "http" and new.scheme.lower() == "https"


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects only where redirect_allowed() says it is safe."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not redirect_allowed(req.full_url, newurl):
            raise urllib.error.HTTPError(
                req.full_url, code,
                "redirect to a different address refused (the API key must "
                "not be sent there)", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener that speaks only http and https, with safe redirects.

    The default opener also handles file: and ftp: URLs, which have no
    business here; leaving them out means such a URL fails even if one
    somehow got past validation. Proxy settings from the environment are
    still honoured.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(), urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(), _SameHostRedirectHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


_OPENER = _build_opener()


def _read_limited(reply, limit: int) -> bytes:
    """Read a whole reply, refusing anything larger than `limit` bytes."""
    declared = reply.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ResponseTooLargeError(f"reply larger than {limit} bytes")
    data = reply.read(limit + 1)
    if len(data) > limit:
        raise ResponseTooLargeError(f"reply larger than {limit} bytes")
    return data


def _open(
    base_url: str, api_key: str | None, path: str, timeout: int,
    max_bytes: int, method: str = "GET", body: dict | None = None,
) -> tuple[bytes, str | None]:
    """Perform one request; return (response bytes, Content-Type header)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        _api_url(base_url, path), data=data, method=method)
    if api_key:
        request.add_header("x-api-key", api_key)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with _OPENER.open(request, timeout=timeout) as reply:
        return (_read_limited(reply, max_bytes),
                reply.headers.get("Content-Type"))


def _parse_json(raw: bytes) -> Any:
    return json.loads(raw) if raw else None


def get_json(
    base_url: str, api_key: str | None, path: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """GET `path`; return the parsed JSON (None if the reply is empty)."""
    raw, _ = _open(base_url, api_key, path, timeout, MAX_JSON_BYTES)
    return _parse_json(raw)


def post_json(
    base_url: str, api_key: str | None, path: str, body: dict,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """POST `body` as JSON to `path`.

    Returns the parsed reply, or None if the reply is empty.
    """
    raw, _ = _open(base_url, api_key, path, timeout, MAX_JSON_BYTES,
                   method="POST", body=body)
    return _parse_json(raw)


def get_bytes(
    base_url: str, api_key: str | None, path: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int | None = None,
) -> tuple[bytes, str | None]:
    """GET `path`; return (body bytes, Content-Type header).

    `max_bytes` lowers the size ceiling for callers that expect small
    replies (a thumbnail); the default is MAX_DOWNLOAD_BYTES.
    """
    limit = MAX_DOWNLOAD_BYTES if max_bytes is None else max_bytes
    return _open(base_url, api_key, path, timeout, limit)
