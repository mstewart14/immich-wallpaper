"""Tests for building wallpaper images, and for the history that holds them."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import desktops
import layout
import rotate
import settings
from desktops import Monitor
from tests.fake_immich import asset, jpeg

RED, GREEN, BLUE = (200, 30, 30), (30, 160, 30), (30, 30, 200)
COLORS = [RED, GREEN, BLUE, (200, 200, 30), (30, 200, 200), (200, 30, 200),
          (120, 120, 120), (250, 130, 20)]
BLACK = (0, 0, 0)
CONFIG = {"immich_url": "http://x", "api_key": "k"}


def close(pixel, want, tolerance=16):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, want))


class Scenario:
    """Runs the builders with photo downloads mocked, in a temp cache."""

    def __init__(self, test: unittest.TestCase, photos: dict[str, bytes]):
        self.photos = photos
        self.downloads: list[str] = []
        self.tempdir = tempfile.TemporaryDirectory()
        test.addCleanup(self.tempdir.cleanup)
        for patch in (
                mock.patch.object(settings, "IMAGES_DIR",
                                  Path(self.tempdir.name)),
                mock.patch.object(rotate, "download_asset_bytes",
                                  self._download),
                mock.patch.object(rotate, "get_asset_details",
                                  lambda config, asset_id: None),
                mock.patch.object(desktops, "get_work_area",
                                  lambda: None)):
            patch.start()
            test.addCleanup(patch.stop)

    def _download(self, config, item):
        self.downloads.append(item["id"])
        return self.photos[item["id"]], "image/jpeg"

    @staticmethod
    def open(path):
        return Image.open(path).convert("RGB")


class LetterboxTests(unittest.TestCase):
    def build(self, width, height, screen, color, **config):
        photos = {"a": jpeg(width, height, color)}
        scenario = Scenario(self, photos)
        entry = rotate.build_wallpaper_entry(
            {**CONFIG, **config}, [asset("a", width, height)], screen)
        return entry, scenario

    def test_a_portrait_keeps_its_full_height_between_black_bars(self):
        entry, scenario = self.build(400, 800, (1920, 1080), RED)
        image = scenario.open(entry["path"])
        self.assertEqual(image.size, (1920, 1080))
        self.assertEqual(entry["kind"], "single")
        for point in ((960, 540), (960, 3), (960, 1076), (700, 540),
                      (1220, 540)):
            self.assertTrue(close(image.getpixel(point), RED), point)
        for point in ((660, 540), (1260, 540), (5, 5)):
            self.assertEqual(image.getpixel(point), BLACK, point)
        self.assertEqual(scenario.downloads, ["a"])

    def test_a_4_3_photo_gets_side_bars_on_16_9(self):
        entry, scenario = self.build(1600, 1200, (1920, 1080), BLUE)
        image = scenario.open(entry["path"])
        for point in ((960, 3), (250, 540), (1670, 540)):
            self.assertTrue(close(image.getpixel(point), BLUE), point)
        for point in ((200, 540), (1720, 540)):
            self.assertEqual(image.getpixel(point), BLACK, point)

    def test_a_photo_of_the_screens_shape_fills_it(self):
        entry, scenario = self.build(1920, 1080, (1920, 1080), RED)
        image = scenario.open(entry["path"])
        self.assertTrue(close(image.getpixel((2, 2)), RED))
        self.assertTrue(close(image.getpixel((1917, 1077)), RED))

    def test_unknown_screen_without_overlays_keeps_the_original_bytes(self):
        entry, scenario = self.build(400, 800, None, RED)
        self.assertEqual(Path(entry["path"]).read_bytes(),
                         scenario.photos["a"])

    def test_unknown_screen_with_an_overlay_stays_at_native_size(self):
        entry, scenario = self.build(400, 800, None, RED,
                                     show_date_overlay=True)
        self.assertEqual(scenario.open(entry["path"]).size, (400, 800))

    def test_overlays_with_a_known_screen_still_letterbox_once(self):
        entry, scenario = self.build(
            400, 800, (1920, 1080), RED, show_date_overlay=True,
            show_photo_info=True)
        self.assertEqual(scenario.open(entry["path"]).size, (1920, 1080))
        self.assertEqual(scenario.downloads, ["a"])

    def test_an_undecodable_original_is_kept_with_one_download(self):
        junk = b"\x00\x00\x00\x18ftypheic" + b"junk" * 40
        scenario = Scenario(self, {"j": junk})
        heic = asset("j", 3000, 2000, "IMG.HEIC", "image/heic")
        for config in ({}, {"show_date_overlay": True}):
            scenario.downloads.clear()
            with self.assertLogs(rotate.logger, "WARNING"):
                entry = rotate.build_wallpaper_entry(
                    {**CONFIG, **config}, [heic], (1920, 1080))
            self.assertEqual(Path(entry["path"]).read_bytes(), junk)
            self.assertEqual(Path(entry["path"]).suffix, ".heic")
            self.assertEqual(scenario.downloads, ["j"])

    def test_pairs_are_still_composed_at_the_screen_size(self):
        scenario = Scenario(self, {"a": jpeg(400, 800, RED),
                                   "b": jpeg(400, 800, BLUE)})
        entry = rotate.build_wallpaper_entry(
            CONFIG, [asset("a", 400, 800), asset("b", 400, 800)],
            (1920, 1080))
        self.assertEqual(entry["kind"], "pair")
        self.assertEqual(scenario.open(entry["path"]).size, (1920, 1080))


class AspectTests(unittest.TestCase):
    @staticmethod
    def shaped(width, height, orientation=None):
        return {"exifInfo": {"exifImageWidth": width,
                             "exifImageHeight": height,
                             "orientation": orientation}}

    def test_classify_orientation(self):
        cases = ((100, 200, None, "portrait"), (200, 100, None, "landscape"),
                 (100, 102, None, "square"), (None, 5, None, "unknown"),
                 (0, 5, None, "unknown"), (200, 100, 6, "portrait"),
                 (200, 100, 8, "portrait"), (200, 100, "junk", "landscape"))
        for width, height, orientation, want in cases:
            with self.subTest(width=width, orientation=orientation):
                self.assertEqual(rotate.classify_orientation(
                    self.shaped(width, height, orientation)), want)
        self.assertEqual(rotate.classify_orientation({}), "unknown")
        self.assertEqual(rotate.classify_orientation({"exifInfo": None}),
                         "unknown")

    def test_asset_aspect_honours_exif_rotation(self):
        self.assertEqual(rotate.asset_aspect(self.shaped(4000, 3000)),
                         4000 / 3000)
        self.assertEqual(rotate.asset_aspect(self.shaped(4000, 3000, 6)),
                         3000 / 4000)
        self.assertIsNone(rotate.asset_aspect(self.shaped(None, 3000)))
        self.assertIsNone(rotate.asset_aspect({}))

    def test_compose_row_fills_each_planned_rectangle(self):
        photos = [jpeg(600, 800, RED), jpeg(1600, 900, GREEN),
                  jpeg(600, 800, BLUE)]
        aspects = [0.75, 16 / 9, 0.75]
        plan = layout.plan_row(aspects, 5120, 1440, max_photos=3)
        canvas = rotate.compose_row(
            [photos[p.index] for p in plan], plan, 5120, 1440)
        self.assertEqual(canvas.size, (5120, 1440))
        for place, color in zip(plan, (RED, GREEN, BLUE)):
            centre = (place.x + place.width // 2, place.y + place.height // 2)
            self.assertTrue(close(canvas.getpixel(centre), color))
            self.assertTrue(close(canvas.getpixel(
                (place.x + 3, place.y + 3)), color))
        gap_x = plan[0].x + plan[0].width + 2
        self.assertEqual(canvas.getpixel((gap_x, 720)), BLACK)

    def test_a_single_photo_row_matches_the_letterbox(self):
        photo = jpeg(600, 800, RED)
        plan = layout.plan_row([0.75], 1920, 1080)
        row = rotate.compose_row([photo], plan, 1920, 1080)
        expected = rotate._letterbox_single(
            rotate._load_oriented(photo), 1920, 1080)
        self.assertEqual(row.size, expected.size)
        self.assertTrue(close(row.getpixel((960, 540)),
                              expected.getpixel((960, 540)), 10))
        self.assertEqual(row.getpixel((10, 10)), BLACK)


M1 = Monitor("HDMI-A-1", 0, 0, 2560, 1440, True)
M2 = Monitor("DVI-I-1", 2560, 0, 1280, 1024)


class MultiMonitorBuilderTests(unittest.TestCase):
    def photos(self, sizes):
        assets, images = [], {}
        for index, (width, height) in enumerate(sizes):
            assets.append(asset(f"a{index}", width, height))
            images[f"a{index}"] = jpeg(width // 10, height // 10,
                                       COLORS[index % len(COLORS)])
        return assets, images

    def build(self, sizes, monitors, mode, *, per_monitor=True, max_photos=2,
              **config):
        assets, images = self.photos(sizes)
        scenario = Scenario(self, images)
        entry = rotate.build_multi_entry(
            {**CONFIG, **config}, assets, monitors, mode,
            per_monitor=per_monitor, max_photos=max_photos)
        files = entry.get("images") or {"_main": entry["path"]}
        return entry, {n: scenario.open(p) for n, p in files.items()}, \
            scenario

    def test_same_gives_each_monitor_its_own_right_sized_image(self):
        entry, images, scenario = self.build(
            [(1600, 1200), (3000, 2000), (900, 1600)], [M1, M2], "same")
        self.assertEqual(entry["kind"], "multi")
        self.assertEqual(entry["path"], entry["images"]["HDMI-A-1"])
        self.assertEqual(images["HDMI-A-1"].size, (2560, 1440))
        self.assertEqual(images["DVI-I-1"].size, (1280, 1024))
        self.assertEqual([a["id"] for a in entry["assets"]], ["a0"])
        self.assertEqual(scenario.downloads, ["a0"])  # one shared download
        self.assertTrue(close(images["HDMI-A-1"].getpixel((1280, 720)), RED))
        self.assertTrue(close(images["DVI-I-1"].getpixel((640, 512)), RED))

    def test_a_4_3_photo_on_a_5_4_monitor_gets_thin_bars(self):
        _, images, _ = self.build([(1600, 1200)], [M2], "same")
        image = images["DVI-I-1"]
        self.assertEqual(image.getpixel((5, 5)), BLACK)
        self.assertTrue(close(image.getpixel((5, 40)), RED))
        self.assertEqual(image.getpixel((640, 1020)), BLACK)

    def test_same_pairs_on_a_wide_screen_and_stays_single_on_a_tall_one(self):
        tall = Monitor("DP-2", 0, 0, 1080, 1920)
        entry, images, _ = self.build(
            [(900, 1600), (900, 1600), (3000, 2000)], [M1, tall], "same")
        self.assertEqual([a["id"] for a in entry["assets"]], ["a0", "a1"])
        self.assertTrue(close(images["HDMI-A-1"].getpixel((700, 720)), RED))
        self.assertTrue(close(images["HDMI-A-1"].getpixel((1850, 720)),
                              GREEN))
        self.assertTrue(close(images["DP-2"].getpixel((540, 960)), RED))
        self.assertEqual(images["DP-2"].size, (1080, 1920))

    def test_different_gives_each_monitor_its_own_photo(self):
        entry, images, _ = self.build([(3200, 1800)] * 3, [M1, M2],
                                      "different")
        self.assertEqual([a["id"] for a in entry["assets"]], ["a0", "a1"])
        self.assertTrue(close(images["HDMI-A-1"].getpixel((1280, 720)), RED))
        self.assertTrue(close(images["DVI-I-1"].getpixel((640, 512)), GREEN))

    def test_different_reuses_photos_rather_than_failing(self):
        entry, _, _ = self.build([(3200, 1800)], [M1, M2], "different")
        self.assertEqual(set(entry["images"]), {"HDMI-A-1", "DVI-I-1"})
        self.assertEqual([a["id"] for a in entry["assets"]], ["a0"])

    def test_span_fills_the_strip_and_slices_per_monitor(self):
        entry, images, _ = self.build([(3200, 1800)] * 6, [M1, M2], "span")
        self.assertEqual(images["HDMI-A-1"].size, (2560, 1440))
        self.assertEqual(images["DVI-I-1"].size, (1280, 1024))
        self.assertGreaterEqual(len(entry["assets"]), 2)
        first = COLORS[int(entry["assets"][0]["id"][1:])]
        self.assertTrue(close(images["HDMI-A-1"].getpixel((100, 720)), first))
        # Nothing but photo at the outer edges of the strip.
        self.assertGreater(sum(images["HDMI-A-1"].getpixel((3, 720))), 60)
        self.assertGreater(sum(images["DVI-I-1"].getpixel((1276, 512))), 60)

    def test_one_selected_monitor_gets_only_its_own_image(self):
        entry, images, _ = self.build([(3200, 1800)] * 2, [M2], "same")
        self.assertEqual(set(entry["images"]), {"DVI-I-1"})
        self.assertEqual(entry["kind"], "single")
        self.assertEqual(entry["path"], entry["images"]["DVI-I-1"])
        self.assertEqual(images["DVI-I-1"].size, (1280, 1024))

    def test_no_per_monitor_support_gives_a_plain_entry(self):
        entry, images, _ = self.build([(3200, 1800)] * 2, [M1], "same",
                                      per_monitor=False)
        self.assertNotIn("images", entry)
        self.assertEqual(entry["kind"], "single")
        self.assertEqual(images["_main"].size, (2560, 1440))

    def test_several_portraits_fill_an_ultrawide_screen(self):
        ultra = Monitor("DP-1", 0, 0, 5120, 1440, True)
        entry, images, _ = self.build([(900, 1600)] * 8, [ultra], "same",
                                      per_monitor=False, max_photos=6)
        self.assertEqual(len(entry["assets"]), 6)
        self.assertEqual(entry["kind"], "multi")
        self.assertEqual(images["_main"].size, (5120, 1440))
        self.assertGreater(sum(images["_main"].getpixel((1000, 720))), 60)
        self.assertEqual(images["_main"].getpixel((5, 720)), BLACK)

    def test_max_photos_one_never_pairs_and_two_is_todays_pair(self):
        ultra = Monitor("DP-1", 0, 0, 5120, 1440, True)
        for limit, count, kind in ((1, 1, "single"), (2, 2, "pair")):
            entry, _, _ = self.build([(900, 1600)] * 8, [ultra], "same",
                                     per_monitor=False, max_photos=limit)
            self.assertEqual((len(entry["assets"]), entry["kind"]),
                             (count, kind))

    def test_photos_of_unknown_shape(self):
        photos = {"n": jpeg(80, 60, (9, 9, 9)), "a1": jpeg(320, 180, GREEN)}
        Scenario(self, photos)
        unknown = {"id": "n", "originalFileName": "n.jpg",
                   "originalMimeType": "image/jpeg"}
        known = asset("a1", 3200, 1800)
        entry = rotate.build_multi_entry(
            CONFIG, [unknown, known], [M1, M2], "span",
            per_monitor=True, max_photos=2)
        self.assertNotIn("n", [a["id"] for a in entry["assets"]])
        with self.assertRaises(ValueError):
            rotate.build_multi_entry(CONFIG, [unknown], [M1, M2], "span",
                                     per_monitor=True, max_photos=2)
        alone = rotate.build_multi_entry(
            CONFIG, [unknown], [M1], "same", per_monitor=False,
            max_photos=6)
        self.assertEqual([a["id"] for a in alone["assets"]], ["n"])

    def test_the_date_is_drawn_on_the_primary_monitor_only(self):
        with mock.patch.object(rotate, "draw_date_overlay") as date:
            self.build([(3200, 1800)] * 2, [M1, M2], "same",
                       show_date_overlay=True)
        self.assertEqual(date.call_count, 1)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        settings.ensure_private_dir(settings.IMAGES_DIR)
        self.made = []
        self.addCleanup(lambda: [p.unlink(missing_ok=True)
                                 for p in self.made])

    def touch(self, name):
        path = settings.IMAGES_DIR / name
        path.write_bytes(b"x")
        self.made.append(path)
        return str(path)

    def entry(self, name, *monitors):
        if not monitors:
            return {"path": self.touch(f"{name}.jpg")}
        images = {m: self.touch(f"{name}-{m}.jpg") for m in monitors}
        return {"path": images[monitors[0]], "images": images}

    def test_entry_files(self):
        self.assertEqual(settings.entry_files({"path": "/a.jpg"}),
                         ["/a.jpg"])
        self.assertEqual(
            settings.entry_files({"path": "/a.jpg", "images": {
                "M1": "/a.jpg", "M2": "/b.jpg"}}), ["/a.jpg", "/b.jpg"])
        self.assertEqual(
            settings.entry_files({"path": "/a.jpg", "images": None}),
            ["/a.jpg"])

    def test_trimming_removes_every_file_of_the_oldest_entry(self):
        e1, e2, e3, e4 = (self.entry("e1", "A", "B"), self.entry("e2"),
                          self.entry("e3", "A", "B"), self.entry("e4"))
        state = {"history": [e1, e2, e3], "position": 2}
        self.assertTrue(rotate.append_history(state, e4, 3))
        for path in settings.entry_files(e1):
            self.assertFalse(Path(path).exists())
        for kept in (e2, e3, e4):
            for path in settings.entry_files(kept):
                self.assertTrue(Path(path).exists())
        self.assertEqual((len(state["history"]), state["position"]), (3, 2))

    def test_the_entry_on_screen_keeps_all_its_files(self):
        e5, e6, e7, e8 = (self.entry("e5", "A", "B"), self.entry("e6"),
                          self.entry("e7"), self.entry("e8"))
        state = {"history": [e5, e6, e7], "position": 0}
        self.assertFalse(rotate.append_history(state, e8, 2))
        for path in settings.entry_files(e5):
            self.assertTrue(Path(path).exists())
        self.assertEqual(state["position"], 0)

    def test_viewing_the_middle_trims_the_oldest_and_refinds_the_spot(self):
        entries = [self.entry(f"m{i}") for i in range(5)]
        state = {"history": entries[:4], "position": 2}
        rotate.append_history(state, entries[4], 3)
        names = [Path(e["path"]).name for e in state["history"]]
        self.assertEqual(names, ["m2.jpg", "m3.jpg", "m4.jpg"])
        self.assertEqual(state["position"], 0)
        self.assertFalse(Path(entries[0]["path"]).exists())
        self.assertFalse(Path(entries[1]["path"]).exists())

    def test_apply_entry_routes_by_what_the_desktop_can_do(self):
        entry = self.entry("r", "M1", "M2")
        first, second = (Path(p) for p in entry["images"].values())
        with mock.patch.object(desktops, "supports_monitor_wallpapers",
                               return_value=True), \
                mock.patch.object(desktops, "set_monitor_wallpapers",
                                  return_value=True) as per_monitor, \
                mock.patch.object(desktops, "set_wallpaper") as single:
            self.assertTrue(rotate.apply_entry(entry))
        per_monitor.assert_called_once_with({"M1": first, "M2": second})
        single.assert_not_called()

    def test_without_per_monitor_support_the_main_image_is_used(self):
        entry = self.entry("r", "M1", "M2")
        with mock.patch.object(desktops, "supports_monitor_wallpapers",
                               return_value=False), \
                mock.patch.object(desktops, "set_wallpaper",
                                  return_value=True) as single:
            rotate.apply_entry(entry)
        single.assert_called_once_with(Path(entry["path"]))

    def test_older_entries_take_the_ordinary_route(self):
        entry = self.entry("old")
        with mock.patch.object(desktops, "supports_monitor_wallpapers",
                               return_value=True), \
                mock.patch.object(desktops, "set_monitor_wallpapers") as per, \
                mock.patch.object(desktops, "set_wallpaper",
                                  return_value=True) as single:
            rotate.apply_entry(entry)
        single.assert_called_once()
        per.assert_not_called()

    def test_vanished_files_are_skipped(self):
        entry = self.entry("v", "M1", "M2")
        Path(entry["images"]["M2"]).unlink()
        with mock.patch.object(desktops, "supports_monitor_wallpapers",
                               return_value=True), \
                mock.patch.object(desktops, "set_monitor_wallpapers",
                                  return_value=True) as per:
            rotate.apply_entry(entry)
        per.assert_called_once_with({"M1": Path(entry["images"]["M1"])})

    def test_navigate_needs_every_file_of_the_target(self):
        target = self.entry("n", "M1", "M2")
        current = self.entry("cur")
        settings.save_state({**settings.DEFAULT_STATE,
                             "history": [target, current], "position": 1})
        with mock.patch.object(desktops, "supports_monitor_wallpapers",
                               return_value=False), \
                mock.patch.object(desktops, "set_wallpaper",
                                  return_value=True):
            Path(target["images"]["M2"]).unlink()
            self.assertFalse(rotate.navigate(-1))
            self.made.append(Path(target["images"]["M2"]))
            Path(target["images"]["M2"]).write_bytes(b"x")
            self.assertTrue(rotate.navigate(-1))
        self.assertEqual(settings.load_state()["position"], 0)


class ScreenOptionTests(unittest.TestCase):
    def test_invalid_values_fall_back_to_defaults(self):
        cases = (({}, (2, "same")),
                 ({"max_photos_per_screen": "lots"}, (2, "same")),
                 ({"max_photos_per_screen": 99}, (6, "same")),
                 ({"max_photos_per_screen": -3}, (1, "same")),
                 ({"multi_monitor_mode": "bogus"}, (2, "same")),
                 ({"multi_monitor_mode": "span"}, (2, "span")))
        for config, want in cases:
            with self.subTest(config=config):
                self.assertEqual(rotate._screen_options(config), want)


class SandboxTests(unittest.TestCase):
    def test_the_suite_never_touches_the_real_home(self):
        # tests/__init__.py points HOME at a throwaway directory before any
        # project module loads; every path below must live inside it.
        sandbox = os.environ["HOME"]
        for path in (settings.CONFIG_PATH, settings.STATE_PATH,
                     settings.IMAGES_DIR, settings.LOG_PATH):
            self.assertTrue(str(path).startswith(sandbox), path)
        self.assertIn("immich-wallpaper-tests-", sandbox)


if __name__ == "__main__":
    unittest.main()
