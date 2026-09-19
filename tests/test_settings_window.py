"""Tests for the native GTK settings window.

They build the real widgets and drive them, but never show a window. They
are skipped when GTK or a display is unavailable (a headless machine).
Network and rotation calls are replaced, and background work is made
synchronous so the tests are deterministic.
"""
import io
import unittest
import warnings
from unittest import mock

import settings
import settings_service as service

# PyGObject itself uses an asyncio call that Python 3.14 deprecates.
warnings.filterwarnings("ignore", category=DeprecationWarning, module="gi")

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    HAVE_DISPLAY = bool(Gtk.init_check()[0])
except (ImportError, ValueError):
    HAVE_DISPLAY = False

if HAVE_DISPLAY:
    import settings_window
    from PIL import Image

CONFIG = {
    **settings.DEFAULT_CONFIG,
    "immich_url": "http://saved:2283", "api_key": "SAVED-KEY",
    "interval_minutes": 9, "keep_count": 4, "person_match": "both",
    "show_photo_info": True, "multi_monitor_mode": "span",
    "monitors": ["DVI-I-1", "DP-3"], "max_photos_per_screen": 3,
    "albums": [{"id": "a1", "name": "Trip"}],
    "people": [{"id": "p1", "name": "Ann"}],
}
THREE = {"ok": True, "per_monitor": True, "monitors": [
    {"name": "HDMI-A-1", "x": 0, "y": 0, "width": 2560, "height": 1440,
     "primary": True},
    {"name": "DVI-I-1", "x": 2560, "y": 0, "width": 1280, "height": 1024,
     "primary": False},
    {"name": "DP-1", "x": 3840, "y": 0, "width": 1920, "height": 1080,
     "primary": False}]}
ONE = {"ok": True, "per_monitor": True, "monitors": THREE["monitors"][:1]}


def run_now(work, done):
    """A synchronous stand-in for the background runner."""
    try:
        result = work()
    except Exception as error:  # noqa: BLE001
        result = error
    done(result)


@unittest.skipUnless(HAVE_DISPLAY, "GTK or a display is not available")
class SettingsPanelTests(unittest.TestCase):
    def make(self, config=CONFIG, monitors=THREE):
        settings.save_config(config)
        patches = [
            mock.patch.object(settings_window, "_background", run_now),
            mock.patch.object(service, "describe_monitors",
                              return_value=monitors)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return settings_window.SettingsPanel()

    # ---- populate / collect -----------------------------------------------
    def test_saved_settings_are_shown_and_round_trip(self):
        panel = self.make()
        body = panel.collect()
        self.assertEqual(body["immich_url"], "http://saved:2283")
        self.assertEqual(body["api_key"], "SAVED-KEY")
        self.assertEqual(
            (body["interval_minutes"], body["keep_count"]), (9, 4))
        self.assertEqual(body["person_match"], "both")
        self.assertEqual(body["multi_monitor_mode"], "span")
        self.assertEqual(body["max_photos_per_screen"], 3)
        self.assertTrue(body["show_photo_info"])
        self.assertFalse(body["show_date_overlay"])
        self.assertEqual(body["albums"], CONFIG["albums"])
        self.assertEqual(body["people"], CONFIG["people"])
        # The whole body is acceptable to the shared validator.
        config = dict(CONFIG)
        service.apply_settings(config, body)
        self.assertEqual(config["monitors"], ["DVI-I-1", "DP-3"])

    def test_defaults_when_nothing_is_saved(self):
        panel = self.make({**settings.DEFAULT_CONFIG})
        body = panel.collect()
        self.assertEqual((body["multi_monitor_mode"], body["person_match"],
                          body["max_photos_per_screen"]), ("same", "any", 2))
        self.assertEqual(body["monitors"], [])
        self.assertEqual(body["albums"], [])

    def test_the_api_key_field_is_masked(self):
        panel = self.make()
        self.assertFalse(panel.key_entry.get_visibility())
        self.assertEqual(panel.key_entry.get_input_purpose(),
                         Gtk.InputPurpose.PASSWORD)

    # ---- lists --------------------------------------------------------------
    def test_names_from_the_server_are_shown_literally(self):
        panel = self.make()
        nasty = "<b>bold</b> &amp; <span foreground='red'>x</span>"
        panel.albums.set_items([{"id": "n", "name": nasty, "count": 2}],
                               "count")
        self.assertEqual(panel.albums.store[0][settings_window.NAME], nasty)
        label = settings_window._label(nasty)
        self.assertEqual(label.get_text(), nasty)
        self.assertFalse(label.get_use_markup())
        heading = settings_window._label("<i>x</i>", bold=True)
        self.assertEqual(heading.get_text(), "<i>x</i>")

    def test_ticking_and_filtering(self):
        panel = self.make()
        items = [{"id": "1", "name": "Summer trip", "count": 1},
                 {"id": "2", "name": "Winter", "count": 2}]
        panel.albums.set_items(items, "count")
        panel.albums.toggle("2")
        self.assertEqual(list(panel.albums.selected), ["a1", "2"])
        self.assertTrue(panel.albums.store[1][settings_window.SELECTED])
        panel.albums.toggle("2")
        self.assertNotIn("2", panel.albums.selected)
        panel.albums.filter_entry.set_text("wint")
        panel.albums._on_filter_changed(panel.albums.filter_entry)
        self.assertEqual(len(panel.albums._model), 1)
        panel.albums.filter_entry.set_text("")
        panel.albums._on_filter_changed(panel.albums.filter_entry)
        self.assertEqual(len(panel.albums._model), 2)
        # Ticking through the filtered view changes the right item.
        panel.albums.filter_entry.set_text("wint")
        panel.albums._on_filter_changed(panel.albums.filter_entry)
        panel.albums._on_toggled(None, "0")
        self.assertIn("2", panel.albums.selected)

    def test_load_albums_and_people(self):
        panel = self.make()
        albums = {"ok": True, "albums": [
            {"id": "a1", "name": "Trip", "count": 3}]}
        people = {"ok": True, "people": [
            {"id": "p1", "name": "Ann", "hidden": False}]}
        with mock.patch.object(service, "list_albums", return_value=albums), \
                mock.patch.object(service, "list_people",
                                  return_value=people), \
                mock.patch.object(panel, "_load_thumbnails") as thumbs:
            panel._on_load_albums(None)
            panel._on_load_people(None)
        self.assertEqual(panel.albums.item_ids(), ["a1"])
        self.assertTrue(panel.albums.store[0][settings_window.SELECTED])
        self.assertEqual(panel.albums.store[0][settings_window.DETAIL],
                         "3 photos")
        thumbs.assert_called_once_with(("http://saved:2283", "SAVED-KEY"),
                                       ["p1"])
        self.assertTrue(panel.load_albums_button.get_sensitive())

    def test_list_errors_are_shown(self):
        panel = self.make()
        with mock.patch.object(service, "list_albums",
                               return_value={"ok": False, "error": "nope"}):
            panel._on_load_albums(None)
        self.assertEqual(panel.list_status.get_text(), "nope")

    def test_thumbnails_are_shown_and_bad_data_is_ignored(self):
        panel = self.make()
        panel.people.set_items([{"id": "p1", "name": "Ann"}])
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 30, 30)).save(buffer, "JPEG")
        panel._show_thumbnail("p1", buffer.getvalue())
        pixbuf = panel.people.store[0][settings_window.THUMB]
        self.assertEqual(pixbuf.get_width(), settings_window.THUMBNAIL_SIZE)
        panel._show_thumbnail("p1", b"not an image")     # must not raise
        panel._show_thumbnail("gone", buffer.getvalue())  # unknown row

    # ---- monitors ---------------------------------------------------------
    def test_three_monitors_with_a_stale_choice(self):
        panel = self.make()
        checks = panel.monitor_checks
        self.assertEqual(list(checks), ["HDMI-A-1", "DVI-I-1", "DP-1",
                                        "DP-3"])
        self.assertEqual([n for n, c in checks.items() if c.get_active()],
                         ["DVI-I-1", "DP-3"])
        self.assertIn("not connected", checks["DP-3"].get_label())
        self.assertIn("2560×1440 (primary)",
                      checks["HDMI-A-1"].get_label())
        self.assertTrue(all(b.get_sensitive()
                            for b in panel.multi_mode.values()))
        self.assertEqual(panel.screens_note.get_text(), "3 screens detected.")
        checks["DP-3"].set_active(False)
        self.assertEqual(panel.collect()["monitors"], ["DVI-I-1"])

    def test_one_monitor_disables_the_multi_screen_choices(self):
        panel = self.make({**CONFIG, "monitors": []}, monitors=ONE)
        self.assertFalse(any(b.get_sensitive()
                             for b in panel.multi_mode.values()))
        self.assertFalse(panel.monitor_checks["HDMI-A-1"].get_sensitive())
        self.assertIn("One screen detected", panel.screens_note.get_text())
        self.assertTrue(panel.photos_spin.get_sensitive())

    def test_desktop_without_per_monitor_support(self):
        info = {**THREE, "per_monitor": False}
        panel = self.make(monitors=info)
        self.assertFalse(any(b.get_sensitive()
                             for b in panel.multi_mode.values()))
        self.assertIn("cannot set screens one by one",
                      panel.screens_note.get_text())

    def test_detection_failure_is_tolerated(self):
        panel = self.make()
        panel.show_monitors(RuntimeError("no display"))
        self.assertIn("Could not detect", panel.screens_note.get_text())
        # Stale selections are still listed so they can be unticked.
        self.assertEqual(list(panel.monitor_checks), ["DVI-I-1", "DP-3"])
        self.assertTrue(panel.monitor_checks["DP-3"].get_sensitive())

    # ---- actions ------------------------------------------------------------
    def test_test_connection_reports_the_result(self):
        panel = self.make()
        with mock.patch.object(service, "check_connection", return_value={
                "ok": True, "album_count": 4}) as check:
            panel._on_test(None)
        check.assert_called_once_with("http://saved:2283", "SAVED-KEY")
        self.assertEqual(panel.server_status.get_text(),
                         "Connected — 4 albums visible.")
        with mock.patch.object(service, "check_connection", return_value={
                "ok": False, "error": "key rejected"}):
            panel._on_test(None)
        self.assertEqual(panel.server_status.get_text(), "key rejected")
        self.assertTrue(panel.test_button.get_sensitive())

    def test_a_bad_url_is_reported_without_any_request(self):
        panel = self.make()
        panel.url_entry.set_text("file:///etc/passwd")
        with mock.patch.object(service, "check_connection") as check:
            panel._on_test(None)
        check.assert_not_called()
        self.assertIn("http://", panel.server_status.get_text())

    def test_typed_url_does_not_borrow_the_saved_key(self):
        panel = self.make()
        panel.url_entry.set_text("http://elsewhere:2283")
        panel.key_entry.set_text("")
        with mock.patch.object(service, "check_connection") as check:
            panel._on_test(None)
        check.assert_not_called()
        self.assertIn("API key", panel.server_status.get_text())

    def test_save_sends_the_form_and_reports_the_outcome(self):
        panel = self.make()
        panel.keep_spin.set_value(7)
        for result, text in (
                ({"ok": True, "applied": True}, "Saved and applied."),
                ({"ok": True, "applied": False}, "wallpaper refresh failed"),
                ({"ok": False, "error": "keep_count must be a whole number"},
                 "whole number")):
            with mock.patch.object(service, "save_settings",
                                   return_value=result) as save:
                panel._on_save(None)
            self.assertEqual(save.call_args.args[0]["keep_count"], 7)
            self.assertIn(text, panel.save_status.get_text())
            self.assertTrue(panel.save_button.get_sensitive())

    def test_save_error_from_a_crash_is_shown(self):
        panel = self.make()
        with mock.patch.object(service, "save_settings",
                               side_effect=RuntimeError("boom")):
            panel._on_save(None)
        self.assertEqual(panel.save_status.get_text(), "boom")

    # ---- rendering ----------------------------------------------------------
    def test_every_page_renders_offscreen(self):
        panel = self.make()
        window = Gtk.OffscreenWindow()
        window.set_default_size(680, 640)
        window.add(panel)
        window.show_all()
        for index in range(panel.notebook.get_n_pages()):
            panel.notebook.set_current_page(index)
            while Gtk.events_pending():
                Gtk.main_iteration()
            pixbuf = window.get_pixbuf()
            self.assertIsNotNone(pixbuf)
            self.assertGreater(pixbuf.get_width(), 100)
        window.remove(panel)
        window.destroy()


@unittest.skipUnless(HAVE_DISPLAY, "GTK or a display is not available")
class WindowTests(unittest.TestCase):
    def test_window_wraps_the_panel_and_stops_background_work(self):
        settings.save_config(dict(CONFIG))
        with mock.patch.object(service, "describe_monitors",
                               return_value=ONE), \
                mock.patch.object(settings_window, "_background", run_now):
            window = settings_window.SettingsWindow()
        self.assertIsInstance(window.panel, settings_window.SettingsPanel)
        self.assertEqual(window.get_title(), "Immich Wallpaper Settings")
        window.destroy()
        self.assertTrue(window.panel._closed)


if __name__ == "__main__":
    unittest.main()
