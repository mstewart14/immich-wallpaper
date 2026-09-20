"""Tests for tray behaviour that doesn't need a running tray."""
import sys
import unittest
from unittest import mock

import settings

try:
    import tray_app
except SystemExit:  # pystray / Pillow not installed
    tray_app = None  # type: ignore[assignment]


@unittest.skipIf(tray_app is None, "pystray or Pillow not installed")
class SettingsLauncherTests(unittest.TestCase):
    def test_linux_opens_the_native_window_without_any_web_request(self):
        with mock.patch.object(tray_app, "NATIVE_SETTINGS", True), \
                mock.patch.object(tray_app.subprocess, "Popen") as popen, \
                mock.patch.object(tray_app.urllib.request, "urlopen") as web, \
                mock.patch.object(tray_app.webbrowser, "open") as browser:
            tray_app._settings_worker()
        command = popen.call_args.args[0]
        self.assertTrue(command[-1].endswith("settings_window.py"))
        web.assert_not_called()
        browser.assert_not_called()

    def test_other_platforms_open_the_running_page_with_its_token(self):
        settings.ensure_private_dir(settings.CACHE_DIR)
        settings.UI_TOKEN_PATH.write_text("tok-123\n")
        self.addCleanup(settings.UI_TOKEN_PATH.unlink, missing_ok=True)
        with mock.patch.object(tray_app, "NATIVE_SETTINGS", False), \
                mock.patch.object(tray_app.subprocess, "Popen") as popen, \
                mock.patch.object(tray_app.urllib.request,
                                  "urlopen") as probe, \
                mock.patch.object(tray_app.webbrowser, "open") as browser:
            tray_app._settings_worker()      # the page is already running
        self.assertTrue(probe.call_args.args[0].endswith("/ping"))
        browser.assert_called_once_with(tray_app.CONFIG_UI_URL
                                        + "?token=tok-123")
        popen.assert_not_called()

    def test_without_a_readable_token_the_bare_page_is_opened(self):
        settings.UI_TOKEN_PATH.unlink(missing_ok=True)
        with mock.patch.object(tray_app, "NATIVE_SETTINGS", False), \
                mock.patch.object(tray_app.urllib.request, "urlopen"), \
                mock.patch.object(tray_app.webbrowser, "open") as browser:
            tray_app._settings_worker()
        browser.assert_called_once_with(tray_app.CONFIG_UI_URL)

    def test_other_platforms_start_the_web_page_when_it_is_not_running(self):
        with mock.patch.object(tray_app, "NATIVE_SETTINGS", False), \
                mock.patch.object(tray_app.subprocess, "Popen") as popen, \
                mock.patch.object(tray_app.urllib.request, "urlopen",
                                  side_effect=OSError("refused")):
            tray_app._settings_worker()
        self.assertTrue(popen.call_args.args[0][-1].endswith("config_ui.py"))

    def test_native_settings_follow_the_platform(self):
        self.assertEqual(tray_app.NATIVE_SETTINGS,
                         sys.platform.startswith("linux"))


if __name__ == "__main__":
    unittest.main()
