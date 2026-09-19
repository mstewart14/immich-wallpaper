"""Tests for desktop detection, monitor detection and the setters."""
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import desktops
from desktops import Monitor, parse_xrandr_monitors


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class DetectionTests(unittest.TestCase):
    def detect(self, xdg, running=()):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": xdg}), \
                mock.patch.object(desktops, "_process_running",
                                  lambda name: name in running):
            return desktops.detect_desktop()

    def test_by_environment_variable(self):
        for xdg, want in (("KDE", "kde"), ("XFCE", "xfce"),
                          ("ubuntu:KDE", "kde"), ("GNOME", None),
                          ("", None), ("sway", None)):
            with self.subTest(xdg=xdg):
                self.assertEqual(self.detect(xdg), want)

    def test_process_fallback_for_a_bare_environment(self):
        self.assertEqual(self.detect("", ("plasmashell",)), "kde")
        self.assertEqual(self.detect("", ("xfce4-session",)), "xfce")
        self.assertEqual(
            self.detect("", ("plasmashell", "xfce4-session")), "kde")
        self.assertIsNone(self.detect("", ("bash",)))

    def test_backend_order_and_names(self):
        self.assertEqual([b.name for b in desktops.BACKENDS],
                         ["kde", "xfce"])
        self.assertTrue(all(
            n == n.lower() for b in desktops.BACKENDS for n in b.xdg_names))

    def test_unsupported_desktop_fails_cleanly(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}), \
                mock.patch.object(desktops, "_process_running",
                                  lambda name: False):
            self.assertFalse(desktops.set_wallpaper("/x.jpg"))
            self.assertIsNone(desktops.get_screen_size())
            self.assertEqual(desktops.get_monitors(), [])
            self.assertFalse(desktops.supports_monitor_wallpapers())
            self.assertFalse(desktops.set_monitor_wallpapers({"A": "/x"}))


class XrandrMonitorTests(unittest.TestCase):
    def test_single_monitor(self):
        listing = ("Monitors: 1\n"
                   " 0: +*HDMI-A-1 2560/597x1440/336+0+0  HDMI-A-1\n")
        self.assertEqual(parse_xrandr_monitors(listing),
                         [Monitor("HDMI-A-1", 0, 0, 2560, 1440, True)])

    def test_ordered_left_to_right_not_by_index(self):
        listing = (" 0: +*DP-1 2560/597x1440/336+1920+0  DP-1\n"
                   " 1: +HDMI-A-2 1920/508x1080/286+0+0  HDMI-A-2\n")
        monitors = parse_xrandr_monitors(listing)
        self.assertEqual([m.name for m in monitors], ["HDMI-A-2", "DP-1"])
        self.assertEqual([m.primary for m in monitors], [False, True])

    def test_rotated_and_negative_offsets(self):
        listing = (" 0: +*DP-1 2560/597x1440/336+1080+0  DP-1\n"
                   " 1: +HDMI-1 1080/286x1920/508-0+0  HDMI-1\n"
                   " 2: +eDP-1 1920/344x1080/194+1080-1080  eDP-1\n")
        monitors = parse_xrandr_monitors(listing)
        self.assertEqual(
            [(m.name, m.x, m.y, m.width, m.height) for m in monitors],
            [("HDMI-1", 0, 0, 1080, 1920), ("eDP-1", 1080, -1080, 1920, 1080),
             ("DP-1", 1080, 0, 2560, 1440)])
        left = parse_xrandr_monitors(" 0: +*A 1920/1x1080/1-1920+0  A\n")
        self.assertEqual(left, [Monitor("A", -1920, 0, 1920, 1080, True)])

    def test_user_defined_monitors_and_junk(self):
        listing = (" 0: *left 1920/508x1080/286+0+0  none\n"
                   " 1: right 2560/597x1440/336+1920+0  none\n")
        monitors = parse_xrandr_monitors(listing)
        self.assertEqual([(m.name, m.primary) for m in monitors],
                         [("left", True), ("right", False)])
        for junk in ("", "Monitors: 0\n", "garbage\nlines\n"):
            self.assertEqual(parse_xrandr_monitors(junk), [])

    def test_errors_give_no_monitors(self):
        cases = (
            mock.patch.object(desktops.subprocess, "run",
                              return_value=completed("", 1)),
            mock.patch.object(desktops.subprocess, "run",
                              side_effect=FileNotFoundError),
            mock.patch.object(desktops.subprocess, "run",
                              side_effect=subprocess.TimeoutExpired("x", 5)))
        for case in cases:
            with case:
                self.assertEqual(desktops.get_monitors_xrandr(), [])


class KdeMonitorTests(unittest.TestCase):
    TWO = parse_xrandr_monitors(
        " 0: +*DP-1 2560/597x1440/336+1920+0  DP-1\n"
        " 1: +HDMI-A-2 1920/508x1080/286+0+0  HDMI-A-2\n")

    def kde(self, plasma_output, named=None, returncode=0):
        with mock.patch.object(
                desktops, "get_monitors_xrandr",
                return_value=self.TWO if named is None else named), \
                mock.patch.object(desktops, "_run_plasma_script",
                                  return_value=completed(
                                      plasma_output, returncode)):
            return desktops.get_monitors_kde()

    def test_names_resolved_through_plasma_and_sorted(self):
        reply = ('string "monitor|DP-1|1920|0|2560|1440'
                 'monitor|HDMI-A-2|0|0|1920|1080"')
        monitors = self.kde(reply)
        self.assertEqual(
            [(m.name, m.x, m.width, m.primary) for m in monitors],
            [("HDMI-A-2", 0, 1920, False), ("DP-1", 1920, 2560, True)])

    def test_plasma_geometry_wins_over_xrandrs_for_scaled_sessions(self):
        scaled = parse_xrandr_monitors(
            " 0: +*DP-1 3840/597x2160/336+0+0  DP-1\n")
        monitors = self.kde('string "monitor|DP-1|0|0|1920|1080"', scaled)
        self.assertEqual(monitors, [Monitor("DP-1", 0, 0, 1920, 1080, True)])

    def test_unknown_connector_is_dropped(self):
        monitors = self.kde('string "monitor|DP-1|1920|0|2560|1440"')
        self.assertEqual([m.name for m in monitors], ["DP-1"])

    def test_no_xrandr_falls_back_to_plasmas_screens(self):
        reply = ('string "monitor|Screen 1|0|0|2560|1440'
                 'monitor|Screen 2|2560|0|1920|1080"')
        monitors = self.kde(reply, named=[])
        self.assertEqual([(m.name, m.primary) for m in monitors],
                         [("Screen 1", True), ("Screen 2", False)])

    def test_script_failure_gives_nothing(self):
        self.assertEqual(self.kde("", returncode=1), [])


class XfceSetterTests(unittest.TestCase):
    LISTING = "\n".join([
        "/backdrop/screen0/monitorHDMI-1/workspace0/last-image",
        "/backdrop/screen0/monitorHDMI-1/workspace0/image-style",
        "/backdrop/screen0/monitorHDMI-1/workspace1/last-image",
        "/backdrop/screen0/monitorDVI-I-1/workspace0/last-image"])

    def run_setter(self, function, argument, fail=()):
        calls = []

        def fake(command, **kwargs):
            calls.append(command)
            if command[:4] == ["xfconf-query", "-c", "xfce4-desktop", "-l"]:
                return completed(self.LISTING)
            failed = any(f in command for f in fail)
            return completed("", 1 if failed else 0, "boom" if failed else "")

        with mock.patch.object(desktops.subprocess, "run", fake):
            return function(argument), calls

    @staticmethod
    def image_sets(calls):
        return {c[4]: c[6] for c in calls
                if c[:4] == ["xfconf-query", "-c", "xfce4-desktop", "-p"]
                and c[5] == "-s" and c[4].endswith("last-image")}

    def test_every_monitor_and_workspace_gets_the_image(self):
        ok, calls = self.run_setter(desktops.set_wallpaper_xfce, "/i/x.jpg")
        self.assertTrue(ok)
        self.assertEqual(set(self.image_sets(calls).values()), {"/i/x.jpg"})
        self.assertEqual(len(self.image_sets(calls)), 3)
        styles = [c for c in calls if c[-2:] == ["-s", "4"]]
        self.assertEqual(len(styles), 3)   # scaled, so nothing is cropped
        self.assertEqual(calls[-1], ["xfdesktop", "--reload"])

    def test_a_failed_property_makes_the_whole_call_fail(self):
        prop = "/backdrop/screen0/monitorHDMI-1/workspace1/last-image"
        ok, _ = self.run_setter(desktops.set_wallpaper_xfce, "/i/x.jpg",
                                fail=(prop,))
        self.assertFalse(ok)

    def test_listing_failures(self):
        for returncode, output in ((1, ""), (0, "/backdrop/other\n")):
            with mock.patch.object(
                    desktops.subprocess, "run",
                    return_value=completed(output, returncode, "err")):
                self.assertFalse(desktops.set_wallpaper_xfce("/x"))

    def test_only_the_named_monitor_is_changed(self):
        ok, calls = self.run_setter(desktops.set_monitor_wallpapers_xfce,
                                    {"DVI-I-1": "/i/a.jpg"})
        self.assertTrue(ok)
        self.assertEqual(
            self.image_sets(calls),
            {"/backdrop/screen0/monitorDVI-I-1/workspace0/last-image":
             "/i/a.jpg"})

    def test_different_images_per_monitor(self):
        ok, calls = self.run_setter(
            desktops.set_monitor_wallpapers_xfce,
            {"HDMI-1": "/i/1.jpg", "DVI-I-1": "/i/2.jpg"})
        self.assertTrue(ok)
        sets = self.image_sets(calls)
        self.assertEqual(len(sets), 3)
        self.assertEqual(
            sets["/backdrop/screen0/monitorDVI-I-1/workspace0/last-image"],
            "/i/2.jpg")

    def test_an_unknown_monitor_is_reported_but_others_still_apply(self):
        ok, calls = self.run_setter(
            desktops.set_monitor_wallpapers_xfce,
            {"DVI-I-1": "/i/2.jpg", "GONE-1": "/i/3.jpg"})
        self.assertFalse(ok)
        self.assertEqual(len(self.image_sets(calls)), 1)
        ok, _ = self.run_setter(desktops.set_monitor_wallpapers_xfce,
                                {"GONE-1": "/x"})
        self.assertFalse(ok)


class KdeSetterTests(unittest.TestCase):
    def test_result_parsing(self):
        cases = (('string "applied|HDMI-A-1|end"', {"HDMI-A-1": "/1"}, True),
                 ('string "applied|HDMI-A-1|end"',
                  {"HDMI-A-1": "/1", "GONE-9": "/2"}, False),
                 ('string "applied||end"', {"X": "/1"}, False),
                 ("garbage", {"X": "/1"}, False))
        for output, images, want in cases:
            with self.subTest(output=output), \
                    mock.patch.object(desktops, "_run_plasma_script",
                                      return_value=completed(output)):
                self.assertEqual(
                    desktops.set_monitor_wallpapers_kde(images), want)

    def test_script_failure(self):
        with mock.patch.object(desktops, "_run_plasma_script",
                               return_value=completed("", 1, "dbus down")):
            self.assertFalse(
                desktops.set_monitor_wallpapers_kde({"X": "/1"}))

    def test_legacy_setter_reports_plasma_errors(self):
        for output, want in (('string "Error: 0"', True),
                             ('string "Error: something"', False)):
            with mock.patch.object(desktops, "_run_plasma_script",
                                   return_value=completed(output)):
                self.assertEqual(desktops.set_wallpaper_kde("/x.jpg"), want)


class DispatchTests(unittest.TestCase):
    def test_registry_hooks(self):
        by_name = {b.name: b for b in desktops.BACKENDS}
        self.assertIs(by_name["kde"].set_monitor_wallpapers,
                      desktops.set_monitor_wallpapers_kde)
        self.assertIs(by_name["xfce"].set_monitor_wallpapers,
                      desktops.set_monitor_wallpapers_xfce)
        self.assertIs(by_name["kde"].monitors, desktops.get_monitors_kde)

    def test_backend_without_per_monitor_support(self):
        backend = desktops.replace(desktops.BACKENDS[0],
                                   set_monitor_wallpapers=None)
        with mock.patch.object(desktops, "current_backend",
                               return_value=backend):
            self.assertFalse(desktops.supports_monitor_wallpapers())
            self.assertFalse(desktops.set_monitor_wallpapers({"A": "/x"}))

    def test_session_environment_helpers(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "exists", return_value=False):
            desktops.ensure_dbus_env()
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", os.environ)


if __name__ == "__main__":
    unittest.main()
