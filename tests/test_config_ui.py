"""Security and behaviour tests for the web settings page.

Each test talks to a real server over HTTP, including the attacks that
worked against the earlier version: cross-site requests, DNS rebinding,
reading the API key, and abusing the thumbnail proxy.
"""
import base64
import hashlib
import http.client
import json
import re
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import config_ui
import desktops
import settings
import settings_service as service
from desktops import Monitor
from tests.helpers import Recorder, reply, serve

TOKEN = "test-token-abc123"
KEY = "SECRET-API-KEY-123"
IMMICH = "http://immich.internal:2283"


class WebTestCase(unittest.TestCase):
    def setUp(self):
        settings.save_config({**settings.DEFAULT_CONFIG,
                              "immich_url": IMMICH, "api_key": KEY})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), config_ui.Handler)
        self.server.token = TOKEN
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.others = []
        patch = mock.patch.object(service, "run_rotation_now",
                                  return_value=True)
        self.rotation = patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for server in self.others:
            server.shutdown()
            server.server_close()

    def fake_immich(self, handler, seen=None):
        server, base = serve(handler, seen)
        self.others.append(server)
        return base

    def request(self, method, path, body=None, headers=None, host=None,
                authorised=True, raw_body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=5)
        sent = {"Host": f"127.0.0.1:{self.port}" if host is None else host}
        if authorised:
            sent["Cookie"] = f"{config_ui.SESSION_COOKIE}={TOKEN}"
        if body is not None:
            raw_body = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        connection.request(method, path, body=raw_body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body=body if body is not None
                            else {}, **kwargs)


class AccessControlTests(WebTestCase):
    def test_a_wrong_host_header_is_refused_dns_rebinding(self):
        for host in ("attacker.example:8877", "attacker.example", "",
                     f"127.0.0.1.evil.com:{self.port}", "127.0.0.1"):
            with self.subTest(host=host):
                status, _, data = self.get("/api/config", host=host)
                self.assertEqual(status, 403)
                self.assertNotIn(KEY.encode(), data)

    def test_both_loopback_names_are_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}",
                     f"LocalHost:{self.port}"):
            self.assertEqual(self.get("/api/config", host=host)[0], 200)

    def test_a_foreign_origin_is_refused_cross_site(self):
        for origin in ("http://evil.example", "null", "https://127.0.0.1",
                       f"http://127.0.0.1:{self.port + 1}"):
            with self.subTest(origin=origin):
                status, _, _ = self.post(
                    "/api/save", {"keep_count": 9},
                    headers={"Origin": origin})
                self.assertEqual(status, 403)
        self.assertNotEqual(settings.read_stored_config().get("keep_count"), 9)

    def test_the_pages_own_origin_is_accepted(self):
        origin = f"http://127.0.0.1:{self.port}"
        status, _, _ = self.post("/api/albums", headers={"Origin": origin})
        self.assertEqual(status, 200)

    def test_no_token_no_access_but_ping_is_open(self):
        for path in ("/", "/api/config", "/api/monitors",
                     "/assets/app-icon.png"):
            self.assertEqual(self.get(path, authorised=False)[0], 403, path)
        self.assertEqual(
            self.post("/api/save", {"keep_count": 9}, authorised=False)[0],
            403)
        status, _, data = self.get("/ping", authorised=False)
        self.assertEqual((status, data), (200, b"ok"))

    def test_wrong_tokens_are_refused(self):
        for cookie in ("iw_session=wrong", "iw_session=", "other=" + TOKEN,
                       "iw_session=" + TOKEN + "x", "garbage;;;"):
            with self.subTest(cookie=cookie):
                status, _, _ = self.get("/api/config", authorised=False,
                                        headers={"Cookie": cookie})
                self.assertEqual(status, 403)

    def test_the_token_header_also_works(self):
        status, _, _ = self.get("/api/config", authorised=False,
                                headers={config_ui.TOKEN_HEADER: TOKEN})
        self.assertEqual(status, 200)

    def test_no_server_token_means_nobody_gets_in(self):
        self.server.token = ""
        self.assertEqual(
            self.get("/api/config", authorised=False,
                     headers={"Cookie": "iw_session="})[0], 403)

    def test_the_link_token_becomes_a_strict_cookie(self):
        status, headers, _ = self.get(f"/?token={TOKEN}", authorised=False)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        self.assertIn(f"{config_ui.SESSION_COOKIE}={TOKEN}", cookie)
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, cookie)

    def test_a_bad_link_token_gets_no_cookie(self):
        status, headers, _ = self.get("/?token=nope", authorised=False)
        self.assertEqual(status, 403)
        self.assertNotIn("Set-Cookie", headers)

    def test_unknown_routes_404_after_authentication(self):
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.post("/api/nope")[0], 404)
        self.assertEqual(self.get("/api/person-thumb?person_id=x")[0], 404)


class RequestParsingTests(WebTestCase):
    def test_post_must_be_json(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded",
                             "multipart/form-data", ""):
            with self.subTest(content_type=content_type):
                headers = {"Content-Type": content_type} if content_type \
                    else {}
                status, _, _ = self.request(
                    "POST", "/api/save", raw_body=b'{"keep_count": 9}',
                    headers=headers)
                self.assertEqual(status, 415)
        self.assertNotEqual(settings.read_stored_config().get("keep_count"), 9)

    def test_json_with_a_charset_is_fine(self):
        status, _, _ = self.request(
            "POST", "/api/albums", raw_body=b"{}",
            headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)

    def test_malformed_and_oversized_bodies_are_rejected_cleanly(self):
        for length, want in (("abc", 400), ("-5", 400), ("1e3", 400),
                             ("99999999999999", 400),
                             (str(config_ui.MAX_BODY_BYTES + 1), 413)):
            with self.subTest(length=length):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", self.port, timeout=5)
                connection.putrequest("POST", "/api/test")
                connection.putheader("Host", f"127.0.0.1:{self.port}")
                connection.putheader("Cookie", f"iw_session={TOKEN}")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", length)
                connection.endheaders()
                self.assertEqual(connection.getresponse().status, want)
                connection.close()

    def test_bad_json_is_a_400_not_a_crash(self):
        for raw in (b"{not json", b"[1, 2]", b'"text"', b"null", b"12"):
            with self.subTest(raw=raw):
                status, _, _ = self.request(
                    "POST", "/api/save", raw_body=raw,
                    headers={"Content-Type": "application/json"})
                self.assertEqual(status, 400)

    def test_the_server_survives_the_abuse(self):
        self.request("POST", "/api/save", raw_body=b"{",
                     headers={"Content-Type": "application/json"})
        self.assertEqual(self.get("/api/config")[0], 200)


class SecretsTests(WebTestCase):
    def test_the_api_key_is_never_sent_to_the_page(self):
        status, _, data = self.get("/api/config")
        config = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual(config["api_key"], "")
        self.assertIs(config["api_key_set"], True)
        self.assertNotIn(KEY.encode(), data)

    def test_no_response_anywhere_contains_the_key(self):
        for method, path in (("GET", "/"), ("GET", "/api/config"),
                             ("GET", "/api/monitors")):
            self.assertNotIn(KEY.encode(), self.request(method, path)[2])
        for path in ("/api/test", "/api/albums", "/api/people",
                     "/api/save"):
            self.assertNotIn(KEY.encode(), self.post(path)[2], path)

    def test_key_not_set_is_reported(self):
        settings.save_config({**settings.DEFAULT_CONFIG})
        self.assertIs(json.loads(self.get("/api/config")[2])["api_key_set"],
                      False)

    def test_saved_key_is_used_only_for_the_saved_server(self):
        seen = Recorder()
        base = self.fake_immich(lambda r: reply(r, body=b"[]"), seen)
        # Same URL as saved: the saved key is used.
        settings.save_config({**settings.load_config(), "immich_url": base})
        self.post("/api/albums", {})
        self.assertEqual(seen.requests[-1][2].get("X-Api-Key"), KEY)
        # A different URL with no key entered: refused, nothing is sent.
        count = len(seen.requests)
        other = self.fake_immich(lambda r: reply(r, body=b"[]"), seen)
        _, _, data = self.post("/api/albums", {"immich_url": other})
        self.assertFalse(json.loads(data)["ok"])
        self.assertIn("API key", json.loads(data)["error"])
        self.assertEqual(len(seen.requests), count)
        # A different URL with its own key: that key, not the saved one.
        self.post("/api/albums", {"immich_url": other, "api_key": "MINE"})
        self.assertEqual(seen.requests[-1][2].get("X-Api-Key"), "MINE")

    def test_the_saved_key_survives_a_save_that_leaves_it_blank(self):
        self.post("/api/save", {"keep_count": 3, "api_key": ""})
        self.assertEqual(settings.read_stored_config()["api_key"], KEY)


class SavingTests(WebTestCase):
    def test_a_cross_site_style_change_of_server_is_refused(self):
        status, _, data = self.post(
            "/api/save", {"immich_url": "http://attacker.example",
                          "api_key": ""})
        self.assertEqual(status, 400)
        self.assertIn("API key", json.loads(data)["error"])
        self.assertEqual(settings.read_stored_config()["immich_url"], IMMICH)

    def test_unsafe_server_urls_are_refused(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)",
                    "http://user:pw@host", "ftp://host"):
            with self.subTest(url=url):
                status, _, _ = self.post(
                    "/api/save", {"immich_url": url, "api_key": "k"})
                self.assertEqual(status, 400)
        self.assertEqual(settings.read_stored_config()["immich_url"], IMMICH)

    def test_valid_saves_are_stored_and_applied(self):
        status, _, data = self.post("/api/save", {
            "keep_count": "6", "multi_monitor_mode": "span",
            "monitors": ["DVI-I-1"], "max_photos_per_screen": 99})
        self.assertEqual((status, json.loads(data)),
                         (200, {"ok": True, "applied": True}))
        stored = settings.read_stored_config()
        self.assertEqual((stored["keep_count"], stored["multi_monitor_mode"],
                          stored["monitors"], stored["max_photos_per_screen"]),
                         (6, "span", ["DVI-I-1"], 6))
        self.rotation.assert_called_once()

    def test_invalid_numbers_are_a_400_and_change_nothing(self):
        status, _, data = self.post("/api/save", {"keep_count": "lots"})
        self.assertEqual(status, 400)
        self.assertIn("whole number", json.loads(data)["error"])
        self.rotation.assert_not_called()

    def test_monitors_endpoint(self):
        monitors = [Monitor("HDMI-A-1", 0, 0, 2560, 1440, True)]
        with mock.patch.object(desktops, "get_monitors",
                               return_value=monitors), \
                mock.patch.object(desktops, "supports_monitor_wallpapers",
                                  return_value=True):
            info = json.loads(self.get("/api/monitors")[2])
        self.assertEqual(info["monitors"][0]["name"], "HDMI-A-1")
        self.assertTrue(info["per_monitor"])


class ThumbnailProxyTests(WebTestCase):
    def use_server(self, handler, seen=None):
        base = self.fake_immich(handler, seen)
        settings.save_config({**settings.load_config(), "immich_url": base})
        return base

    def test_returns_the_image_bytes(self):
        self.use_server(lambda r: reply(r, body=b"\xff\xd8JPEG",
                                        content_type="image/jpeg"))
        status, headers, data = self.post("/api/person-thumb",
                                          {"person_id": "p1"})
        self.assertEqual((status, data), (200, b"\xff\xd8JPEG"))
        self.assertEqual(headers["Content-Type"], "image/jpeg")

    def test_person_id_cannot_walk_out_of_the_people_path(self):
        seen = Recorder()
        self.use_server(lambda r: reply(r, body=b"x"), seen)
        self.post("/api/person-thumb",
                  {"person_id": "../../assets/SOME-ID/original?x="})
        self.assertEqual(len(seen.requests), 1)
        path = seen.requests[0][1]
        self.assertTrue(path.startswith("/api/people/"), path)
        self.assertEqual(path.count("/"), 4, path)
        self.assertNotIn("assets", path.split("/")[2:3])

    def test_dot_segments_and_missing_ids_are_a_400(self):
        seen = Recorder()
        self.use_server(lambda r: reply(r, body=b"x"), seen)
        for person_id in ("..", ".", ""):
            status, _, _ = self.post("/api/person-thumb",
                                     {"person_id": person_id})
            self.assertEqual(status, 400, person_id)
        self.assertEqual(seen.requests, [])

    def test_file_urls_cannot_be_read_through_it(self):
        for url in ("file:///etc/hostname#", "file:///etc/hostname"):
            status, _, data = self.post("/api/person-thumb", {
                "person_id": "x", "immich_url": url, "api_key": "k"})
            self.assertEqual(status, 400)
            self.assertNotIn(b"\x7fELF", data)

    def test_upstream_failures_are_a_502(self):
        self.use_server(lambda r: reply(r, status=500, body=b"boom"))
        self.assertEqual(self.post("/api/person-thumb",
                                   {"person_id": "p"})[0], 502)

    def test_the_key_is_not_in_any_url_it_requests(self):
        seen = Recorder()
        self.use_server(lambda r: reply(r, body=b"x"), seen)
        self.post("/api/person-thumb", {"person_id": "p"})
        self.assertNotIn(KEY, seen.requests[0][1])

    def test_get_is_not_an_option(self):
        self.assertEqual(self.get(
            "/api/person-thumb?person_id=x&api_key=k")[0], 404)


class ResponseHeaderTests(WebTestCase):
    def test_hardening_headers_on_every_kind_of_response(self):
        for path, kwargs in (
                ("/", {}), ("/api/config", {}), ("/assets/app-icon.png", {}),
                ("/api/config", {"authorised": False}), ("/nope", {})):
            _, headers, _ = self.get(path, **kwargs)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(headers["Cross-Origin-Resource-Policy"],
                             "same-origin")

    def test_pages_and_api_replies_are_not_cached(self):
        for path in ("/", "/api/config", "/api/monitors"):
            self.assertEqual(self.get(path)[1]["Cache-Control"], "no-store")

    def test_the_csp_allows_exactly_the_pages_own_script_and_style(self):
        _, headers, body = self.get("/")
        policy = headers["Content-Security-Policy"]
        html = body.decode()

        def digest(text):
            return "'sha256-" + base64.b64encode(
                hashlib.sha256(text.encode()).digest()).decode() + "'"
        script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        self.assertIn(f"script-src {digest(script)};", policy)
        self.assertIn(f"style-src {digest(style)};", policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("unsafe-eval", policy)
        for directive in ("default-src 'none'", "frame-ancestors 'none'",
                          "form-action 'none'", "base-uri 'none'",
                          "connect-src 'self'"):
            self.assertIn(directive, policy)

    def test_the_page_uses_no_inline_styles_or_handlers(self):
        html = self.get("/")[2].decode()
        self.assertNotRegex(html, r'\sstyle="')
        self.assertNotRegex(html, r'\son[a-z]+="')
        self.assertNotIn("innerHTML =", html.replace("innerHTML = ''", ""))

    def test_only_the_whitelisted_asset_is_served(self):
        self.assertEqual(self.get("/assets/app-icon.png")[0], 200)
        for path in ("/assets/immich-flower.png", "/assets/../config_ui.py",
                     "/assets/%2e%2e/config_ui.py", "/config_ui.py",
                     "/index.html"):
            self.assertEqual(self.get(path)[0], 404, path)


class TokenFileTests(unittest.TestCase):
    def test_the_token_file_is_private(self):
        import os
        import stat
        old = os.umask(0)
        try:
            config_ui.publish_token("abc")
        finally:
            os.umask(old)
        self.assertEqual(settings.UI_TOKEN_PATH.read_text(), "abc")
        self.assertEqual(stat.S_IMODE(settings.UI_TOKEN_PATH.stat().st_mode),
                         0o600)
        settings.UI_TOKEN_PATH.unlink()


if __name__ == "__main__":
    unittest.main()
