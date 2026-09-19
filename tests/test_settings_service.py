"""Tests for the settings logic shared by the GTK and web front-ends."""
import json
import unittest
from unittest import mock

import desktops
import immich_api
import settings
import settings_service as service
from desktops import Monitor
from tests.helpers import Recorder, reply, serve

KEY = "SAVED-KEY-123"


def save_baseline(**overrides):
    config = {**settings.DEFAULT_CONFIG, "immich_url": "http://saved:2283",
              "api_key": KEY, **overrides}
    settings.save_config(config)
    return config


class ServerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.config = save_baseline()

    def apply(self, **body):
        service.apply_settings(self.config, body)
        return self.config

    def test_valid_url_is_stored_normalised(self):
        self.assertEqual(
            self.apply(immich_url=" https://photos.example.com/ ",
                       api_key="k2")["immich_url"],
            "https://photos.example.com")

    def test_unsafe_urls_are_rejected_and_nothing_changes(self):
        for url in ["file:///etc/passwd", "javascript:alert(1)",
                    "http://user:pw@host", "ftp://h", "no scheme"]:
            with self.subTest(url=url), \
                    self.assertRaises(service.SettingsError):
                self.apply(immich_url=url, api_key="k")
        self.assertEqual(self.config["immich_url"], "http://saved:2283")

    def test_blank_key_keeps_the_saved_key_for_the_same_server(self):
        self.apply(immich_url="http://saved:2283/", api_key="")
        self.assertEqual(self.config["api_key"], KEY)

    def test_changing_the_server_requires_its_own_key(self):
        with self.assertRaises(service.SettingsError) as context:
            self.apply(immich_url="http://other:2283", api_key="")
        self.assertIn("API key", str(context.exception))
        self.assertEqual(self.config["api_key"], KEY)

    def test_a_new_server_with_a_new_key_is_accepted(self):
        self.apply(immich_url="http://other:2283", api_key="OTHER")
        self.assertEqual((self.config["immich_url"], self.config["api_key"]),
                         ("http://other:2283", "OTHER"))

    def test_a_blank_url_is_allowed_for_partial_first_time_setup(self):
        self.config.update(immich_url="", api_key="")
        self.apply(immich_url="", api_key="", keep_count=4)
        self.assertEqual(self.config["keep_count"], 4)


class OtherSettingsTests(unittest.TestCase):
    def setUp(self):
        self.config = save_baseline()

    def apply(self, **body):
        service.apply_settings(self.config, body)
        return self.config

    def test_whole_numbers(self):
        self.apply(interval_minutes="7", keep_count=0)
        self.assertEqual(self.config["interval_minutes"], 7)
        self.assertEqual(self.config["keep_count"], 1)   # at least 1
        for value in ("lots", None, [1]):
            with self.subTest(value=value), \
                    self.assertRaises(service.SettingsError):
                self.apply(keep_count=value)

    def test_photos_per_screen_is_clamped(self):
        for value, want in ((99, 6), (-3, 1), ("4", 4), (2, 2)):
            self.assertEqual(
                self.apply(max_photos_per_screen=value)[
                    "max_photos_per_screen"], want)
        with self.assertRaises(service.SettingsError):
            self.apply(max_photos_per_screen="lots")

    def test_selections_are_reduced_to_id_and_name(self):
        self.apply(albums=[{"id": "a1", "name": "Trip", "count": 3,
                            "evil": {"x": 1}}, {"id": ""}, "junk", 5,
                           {"name": "no id"}],
                   people=[{"id": "p1", "name": None, "hidden": True}])
        self.assertEqual(self.config["albums"],
                         [{"id": "a1", "name": "Trip"}])
        self.assertEqual(self.config["people"], [{"id": "p1", "name": ""}])

    def test_selection_sizes_are_bounded(self):
        self.apply(albums=[{"id": "x" * 5000, "name": "n" * 5000}])
        item = self.config["albums"][0]
        self.assertLessEqual(len(item["id"]), service.MAX_TEXT_LENGTH)
        self.assertLessEqual(len(item["name"]), service.MAX_TEXT_LENGTH)
        self.apply(albums=[{"id": str(i)} for i in range(30000)])
        self.assertEqual(len(self.config["albums"]),
                         service.MAX_SELECTED_ITEMS)

    def test_monitor_names_are_filtered(self):
        self.apply(monitors=[" HDMI-A-1 ", "DVI-I-1", "HDMI-A-1", "",
                             5, None, "bad;name", "x" * 100, "Screen 2"])
        self.assertEqual(self.config["monitors"],
                         ["HDMI-A-1", "DVI-I-1", "Screen 2"])
        self.apply(monitors=[])
        self.assertEqual(self.config["monitors"], [])

    def test_non_list_values_are_ignored_or_rejected(self):
        self.apply(monitors="HDMI-A-1", albums="x")
        self.assertEqual(self.config["monitors"], [])
        self.assertEqual(self.config["albums"], [])

    def test_enums_flags_and_unknown_keys(self):
        self.apply(person_match="bogus", multi_monitor_mode="span",
                   show_photo_info=1, show_date_overlay=0,
                   injected="x", api_key_extra="y")
        self.assertEqual(self.config["person_match"], "any")
        self.assertEqual(self.config["multi_monitor_mode"], "span")
        self.assertIs(self.config["show_photo_info"], True)
        self.assertIs(self.config["show_date_overlay"], False)
        self.assertNotIn("injected", self.config)
        self.assertNotIn("api_key_extra", self.config)


class ResolveCredentialsTests(unittest.TestCase):
    def setUp(self):
        save_baseline()

    def test_blank_means_the_saved_credentials(self):
        self.assertEqual(service.resolve_credentials({}),
                         ("http://saved:2283", KEY))

    def test_saved_key_is_used_only_for_the_saved_url(self):
        self.assertEqual(
            service.resolve_credentials({"immich_url": "http://saved:2283/"}),
            ("http://saved:2283", KEY))
        with self.assertRaises(service.SettingsError):
            service.resolve_credentials({"immich_url": "http://other:2283"})

    def test_entered_values_win(self):
        self.assertEqual(
            service.resolve_credentials(
                {"immich_url": "http://other:2283", "api_key": "MINE"}),
            ("http://other:2283", "MINE"))

    def test_unsafe_url_is_refused(self):
        with self.assertRaises(service.SettingsError):
            service.resolve_credentials({"immich_url": "file:///etc/passwd",
                                         "api_key": "k"})

    def test_nothing_saved_and_nothing_entered(self):
        settings.save_config({**settings.DEFAULT_CONFIG})
        with self.assertRaises(service.SettingsError):
            service.resolve_credentials({})


class ImmichCallTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def immich(self, routes, seen=None):
        def handler(request):
            body, status = routes.get(request.path, (b"{}", 404))
            reply(request, status=status,
                  body=body if isinstance(body, bytes)
                  else json.dumps(body).encode())
        server, base = serve(handler, seen)
        self.servers.append(server)
        return base

    def test_connection_ok(self):
        base = self.immich({"/api/server/ping": ({"res": "pong"}, 200),
                            "/api/albums": ([{"id": "a"}, {"id": "b"}], 200)})
        self.assertEqual(service.check_connection(base, KEY),
                         {"ok": True, "album_count": 2})

    def test_connection_failure_messages(self):
        ping = ({"res": "pong"}, 200)
        for status, words in ((401, "rejected"), (403, "rejected"),
                              (500, "HTTP 500")):
            with self.subTest(status=status):
                base = self.immich({"/api/server/ping": ping,
                                    "/api/albums": ({}, status)})
                result = service.check_connection(base, KEY)
                self.assertFalse(result["ok"])
                self.assertIn(words, result["error"])
        base = self.immich({"/api/server/ping": ({"nope": 1}, 200)})
        self.assertIn("valid Immich ping",
                      service.check_connection(base, KEY)["error"])
        result = service.check_connection("http://127.0.0.1:1", KEY)
        self.assertFalse(result["ok"])
        self.assertIn("Could not reach", result["error"])

    def test_albums_sorted_case_insensitively(self):
        base = self.immich({"/api/albums": ([
            {"id": "1", "albumName": "zebra", "assetCount": 1},
            {"id": "2", "albumName": "Apple"}, {"id": "3"}], 200)})
        names = [a["name"] for a in service.list_albums(base, KEY)["albums"]]
        self.assertEqual(names, ["(untitled album)", "Apple", "zebra"])

    def test_people_are_paged_and_sorted_with_unnamed_last(self):
        pages = {
            "/api/people?page=1&size=250&withHidden=true": ({"people": [
                {"id": "1", "name": "bob"}, {"id": "2", "name": ""}],
                "hasNextPage": True}, 200),
            "/api/people?page=2&size=250&withHidden=true": ({"people": [
                {"id": "3", "name": "Alice", "isHidden": True}],
                "hasNextPage": False}, 200)}
        result = service.list_people(self.immich(pages), KEY)
        self.assertEqual([p["name"] for p in result["people"]],
                         ["Alice", "bob", "(unnamed person)"])
        self.assertTrue(result["people"][0]["hidden"])

    def test_list_errors_are_reported_not_raised(self):
        base = self.immich({"/api/albums": ({}, 500)})
        self.assertFalse(service.list_albums(base, KEY)["ok"])
        self.assertFalse(service.list_people(base, KEY)["ok"])

    def test_thumbnail_id_is_one_path_segment(self):
        seen = Recorder()
        base = self.immich({}, seen)
        with self.assertRaises(Exception):  # noqa: B017 (404 from fake)
            service.fetch_person_thumbnail(base, KEY, "../../assets/x")
        self.assertEqual(len(seen.requests), 1)
        path = seen.requests[0][1]
        self.assertTrue(path.startswith("/api/people/"))
        self.assertTrue(path.endswith("/thumbnail"))
        self.assertEqual(path.count("/"), 4, path)
        with self.assertRaises(ValueError):
            service.fetch_person_thumbnail(base, KEY, "..")

    def test_thumbnail_size_limit(self):
        big = self.immich({"/api/people/p/thumbnail":
                           (b"x" * (service.THUMBNAIL_MAX_BYTES + 10), 200)})
        with self.assertRaises(immich_api.ResponseTooLargeError):
            service.fetch_person_thumbnail(big, KEY, "p")

    def test_the_key_never_reaches_a_different_server_on_redirect(self):
        stolen = Recorder()
        sink, sink_base = serve(lambda r: reply(r), stolen)
        self.servers.append(sink)

        def redirect(request):
            reply(request, status=302, body=b"",
                  headers={"Location": sink_base + "/x"})
        bouncer, base = serve(redirect)
        self.servers.append(bouncer)
        self.assertFalse(service.check_connection(base, KEY)["ok"])
        self.assertEqual(stolen.requests, [])


class MonitorAndSaveTests(unittest.TestCase):
    def test_describe_monitors(self):
        monitors = [Monitor("HDMI-A-1", 0, 0, 2560, 1440, True)]
        with mock.patch.object(desktops, "get_monitors",
                               return_value=monitors), \
                mock.patch.object(desktops, "supports_monitor_wallpapers",
                                  return_value=True):
            info = service.describe_monitors()
        self.assertEqual(info, {"ok": True, "per_monitor": True, "monitors": [
            {"name": "HDMI-A-1", "x": 0, "y": 0, "width": 2560,
             "height": 1440, "primary": True}]})

    def test_save_validates_persists_and_applies(self):
        save_baseline()
        with mock.patch.object(service, "run_rotation_now",
                               return_value=True) as run:
            result = service.save_settings(
                {"keep_count": 5, "multi_monitor_mode": "span"})
        self.assertEqual(result, {"ok": True, "applied": True})
        run.assert_called_once()
        stored = settings.read_stored_config()
        self.assertEqual((stored["keep_count"], stored["multi_monitor_mode"]),
                         (5, "span"))

    def test_a_rejected_save_changes_nothing_and_does_not_rotate(self):
        save_baseline(keep_count=2)
        with mock.patch.object(service, "run_rotation_now") as run:
            result = service.save_settings(
                {"keep_count": 9, "interval_minutes": "lots"})
        self.assertFalse(result["ok"])
        self.assertIn("whole number", result["error"])
        run.assert_not_called()
        self.assertEqual(settings.read_stored_config()["keep_count"], 2)

    def test_rotation_failure_is_reported_not_raised(self):
        save_baseline()
        with mock.patch.object(service.subprocess, "run",
                               side_effect=OSError("no python")):
            self.assertFalse(service.run_rotation_now())


if __name__ == "__main__":
    unittest.main()
