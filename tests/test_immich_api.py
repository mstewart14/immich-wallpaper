"""Tests for the Immich API client's safety checks."""
import json
import unittest
import urllib.error

import immich_api
from tests.helpers import Recorder, reply, serve

KEY = "SECRET-API-KEY-123"


class ValidateBaseUrlTests(unittest.TestCase):
    def test_accepts_plain_http_and_https(self):
        for url, want in [
            ("http://192.168.1.5:2283", "http://192.168.1.5:2283"),
            ("https://photos.example.com/", "https://photos.example.com"),
            ("  https://photos.example.com/immich/  ",
             "https://photos.example.com/immich"),
            ("HTTP://Host:8080", "http://Host:8080"),
            ("http://[::1]:2283", "http://[::1]:2283"),
        ]:
            self.assertEqual(immich_api.validate_base_url(url), want)

    def test_rejects_other_schemes_and_local_files(self):
        for url in ["file:///etc/passwd", "file:///etc/passwd#", "ftp://h/x",
                    "gopher://h", "javascript:alert(1)", "data:text/plain,x",
                    "//host/path", "192.168.1.5:2283", "photos.example.com",
                    ""]:
            with self.subTest(url=url), \
                    self.assertRaises(immich_api.UnsafeUrlError):
                immich_api.validate_base_url(url)

    def test_rejects_credentials_query_fragment_and_control_characters(self):
        for url in ["http://user:pass@host", "http://user@host",
                    "http://host/?a=b", "http://host/#frag",
                    "http://host/\r\nX-Injected: 1", "http://ho st",
                    "http://host:notaport", "http://host:99999999"]:
            with self.subTest(url=url), \
                    self.assertRaises(immich_api.UnsafeUrlError):
                immich_api.validate_base_url(url)


class QuoteSegmentTests(unittest.TestCase):
    def test_slashes_and_specials_are_encoded(self):
        self.assertEqual(immich_api.quote_segment("abc-123"), "abc-123")
        quoted = immich_api.quote_segment("../../assets/x/original?y=")
        self.assertNotIn("/", quoted)
        self.assertNotIn("?", quoted)
        self.assertNotIn("=", quoted)

    def test_dot_segments_are_refused(self):
        for value in (".", ".."):
            with self.assertRaises(ValueError):
                immich_api.quote_segment(value)


class RedirectPolicyTests(unittest.TestCase):
    def test_same_host_same_scheme_and_port(self):
        self.assertTrue(immich_api.redirect_allowed(
            "http://h:2283/api/a", "http://h:2283/api/b"))
        self.assertTrue(immich_api.redirect_allowed(
            "https://h/api/a", "https://H:443/other"))

    def test_other_host_or_port_refused(self):
        for new in ["http://other:2283/x", "http://h:9999/x",
                    "http://127.0.0.1:2283/x", "https://h.evil.com/x"]:
            with self.subTest(new=new):
                self.assertFalse(
                    immich_api.redirect_allowed("http://h:2283/api", new))

    def test_upgrade_to_https_allowed_but_never_downgrade(self):
        self.assertTrue(immich_api.redirect_allowed(
            "http://h:2283/api", "https://h/api"))
        self.assertFalse(immich_api.redirect_allowed(
            "https://h/api", "http://h/api"))

    def test_non_http_targets_refused(self):
        for new in ["file:///etc/passwd", "ftp://h/x", "javascript:x"]:
            self.assertFalse(immich_api.redirect_allowed("http://h/a", new))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def serve(self, handler, recorder=None):
        server, base = serve(handler, recorder)
        self.servers.append(server)
        return base

    def test_sends_key_and_parses_json(self):
        seen = Recorder()
        base = self.serve(lambda r: reply(r, body=b'{"ok": true}'), seen)
        self.assertEqual(immich_api.get_json(base, KEY, "/ping"),
                         {"ok": True})
        method, path, headers = seen.requests[0]
        self.assertEqual((method, path), ("GET", "/api/ping"))
        self.assertEqual(headers.get("X-Api-Key"), KEY)

    def test_post_json_sends_body(self):
        got = {}

        def handler(request):
            got["body"] = json.loads(request.body)
            reply(request, body=b"[1]")
        base = self.serve(handler)
        self.assertEqual(
            immich_api.post_json(base, KEY, "/search", {"size": 2}), [1])
        self.assertEqual(got["body"], {"size": 2})

    def test_get_bytes_returns_content_type(self):
        base = self.serve(lambda r: reply(
            r, body=b"\xff\xd8data", content_type="image/jpeg"))
        data, content_type = immich_api.get_bytes(base, KEY, "/img")
        self.assertEqual((data, content_type), (b"\xff\xd8data", "image/jpeg"))

    def test_unsafe_url_never_reaches_the_network(self):
        with self.assertRaises(immich_api.UnsafeUrlError):
            immich_api.get_bytes("file:///etc/hostname#", KEY, "/x")
        with self.assertRaises(immich_api.UnsafeUrlError):
            immich_api.get_json("ftp://example.com", KEY, "/x")

    def test_redirect_to_another_origin_is_refused_and_key_not_sent(self):
        stolen = Recorder()
        sink = self.serve(lambda r: reply(r), stolen)
        base = self.serve(lambda r: reply(
            r, status=302, body=b"", headers={"Location": sink + "/stolen"}))
        with self.assertRaises(urllib.error.HTTPError) as context:
            immich_api.get_json(base, KEY, "/albums")
        self.assertIn("refused", str(context.exception.reason))
        context.exception.close()
        self.assertEqual(stolen.requests, [], "nothing may reach the target")

    def test_redirect_within_the_same_origin_is_followed(self):
        def handler(request):
            if request.path == "/api/old":
                reply(request, status=302, body=b"",
                      headers={"Location": "/api/new"})
            else:
                reply(request, body=b'{"moved": true}')
        base = self.serve(handler)
        self.assertEqual(immich_api.get_json(base, KEY, "/old"),
                         {"moved": True})

    def test_oversized_replies_are_refused(self):
        base = self.serve(lambda r: reply(r, body=b"x" * 5000))
        original = immich_api.MAX_JSON_BYTES
        immich_api.MAX_JSON_BYTES = 1000
        try:
            with self.assertRaises(immich_api.ResponseTooLargeError):
                immich_api.get_json(base, KEY, "/big")
        finally:
            immich_api.MAX_JSON_BYTES = original

    def test_oversized_reply_without_content_length_is_refused(self):
        def handler(request):
            request.send_response(200)
            request.send_header("Connection", "close")
            request.end_headers()
            request.wfile.write(b"x" * 5000)
        base = self.serve(handler)
        original = immich_api.MAX_DOWNLOAD_BYTES
        immich_api.MAX_DOWNLOAD_BYTES = 1000
        try:
            with self.assertRaises(immich_api.ResponseTooLargeError):
                immich_api.get_bytes(base, KEY, "/big")
        finally:
            immich_api.MAX_DOWNLOAD_BYTES = original

    def test_path_injection_is_rejected(self):
        with self.assertRaises(ValueError):
            immich_api.get_json("http://h", KEY, "no-leading-slash")
        with self.assertRaises(ValueError):
            immich_api.get_json("http://h", KEY, "/x\r\nHost: evil")


if __name__ == "__main__":
    unittest.main()
