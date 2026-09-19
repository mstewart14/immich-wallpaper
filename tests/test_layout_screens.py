"""Tests for the row layout planner and the monitor selection helpers."""
import random
import unittest

import layout
import screens
from desktops import Monitor
from layout import plan_row

PORTRAIT, LANDSCAPE, WIDE = 0.75, 1.5, 16 / 9


def indices(placements):
    return [p.index for p in placements]


def portraits_only(aspects):
    return lambda i: aspects[i] < 1 / 1.05


class PlanRowTests(unittest.TestCase):
    def test_reproduces_todays_pairing_rule(self):
        aspects = [PORTRAIT, LANDSCAPE, PORTRAIT, LANDSCAPE]
        rows = plan_row(aspects, 2560, 1440, max_photos=2,
                        companion_ok=portraits_only(aspects))
        self.assertEqual(indices(rows), [0, 2])
        self.assertEqual([(p.width, p.height) for p in rows],
                         [(1080, 1440)] * 2)
        self.assertEqual(rows[0].x, (2560 - 2166) // 2)
        self.assertEqual(rows[1].x, rows[0].x + 1080 + 6)

    def test_landscape_seed_stays_single(self):
        aspects = [LANDSCAPE, PORTRAIT, PORTRAIT]
        rows = plan_row(aspects, 2560, 1440, max_photos=2,
                        companion_ok=portraits_only(aspects))
        self.assertEqual(indices(rows), [0])

    def test_single_photos_letterbox_and_centre(self):
        row, = plan_row([LANDSCAPE], 2560, 1440)
        self.assertEqual((row.width, row.height, row.x, row.y),
                         (2160, 1440, 200, 0))
        row, = plan_row([WIDE], 2560, 1440)
        self.assertEqual((row.width, row.height, row.x, row.y),
                         (2560, 1440, 0, 0))

    def test_a_very_wide_photo_uses_every_pixel_of_the_width(self):
        row, = plan_row([3.0], 2560, 1440)
        self.assertEqual((row.width, row.height, row.x, row.y),
                         (2560, 853, 0, (1440 - 853) // 2))

    def test_span_fills_wide_canvases_edge_to_edge(self):
        for monitors, canvas_width in ((2, 5120), (3, 7680)):
            rows = plan_row([WIDE] * 8, canvas_width, 1440, max_photos=6)
            self.assertEqual(len(rows), monitors)
            self.assertEqual(rows[0].x, 0)
            self.assertEqual(rows[-1].x + rows[-1].width, canvas_width)

    def test_portraits_across_a_wide_canvas(self):
        rows = plan_row([PORTRAIT] * 8, 5120, 1440, max_photos=6)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(
            p.height >= int(layout.MIN_ROW_SCALE * 1440) for p in rows))
        self.assertEqual(rows[-1].x + rows[-1].width, 5120)

    def test_extra_photos_must_earn_their_place(self):
        self.assertEqual(
            indices(plan_row([1.7, 1.7, 1.7], 2560, 1440, max_photos=3)), [0])
        self.assertEqual(len(plan_row([WIDE] * 8, 5120, 1440, max_photos=1)),
                         1)
        self.assertEqual(len(plan_row([WIDE] * 8, 5120, 1440, max_photos=2)),
                         2)

    def test_degenerate_input(self):
        self.assertEqual(plan_row([], 2560, 1440), [])
        self.assertEqual(plan_row([PORTRAIT], 0, 1440), [])
        self.assertEqual(plan_row([PORTRAIT], 2560, 0), [])
        self.assertLessEqual(
            len(plan_row([PORTRAIT] * 40, 2560, 1440, max_photos=3)), 3)

    def test_invariants_hold_for_random_inputs(self):
        rng = random.Random(1234)
        for _ in range(1500):
            n = rng.randint(1, 14)
            aspects = [rng.choice([rng.uniform(0.3, 0.9),
                                   rng.uniform(0.9, 2.2),
                                   rng.uniform(2.2, 4.0)])
                       for _ in range(n)]
            width = rng.choice([800, 1080, 1920, 2560, 3840, 5120, 7680])
            height = rng.choice([600, 1080, 1440, 2160])
            gap = rng.choice([0, 6, 20])
            limit = rng.randint(1, 7)
            ok = ((lambda i, shapes=aspects: shapes[i] < 1)
                  if rng.random() < 0.3 else None)
            rows = plan_row(aspects, width, height, gap=gap,
                            max_photos=limit, companion_ok=ok)
            with self.subTest(aspects=aspects, canvas=(width, height)):
                self.assertTrue(rows and rows[0].index == 0)  # seed used
                self.assertLessEqual(len(rows), limit)
                self.assertEqual(len({p.index for p in rows}), len(rows))
                if ok:
                    self.assertTrue(all(ok(p.index) for p in rows[1:]))
                for p in rows:
                    self.assertGreaterEqual(p.x, 0)
                    self.assertGreaterEqual(p.y, 0)
                    self.assertLessEqual(p.x + p.width, width)
                    self.assertLessEqual(p.y + p.height, height)
                    ratio_error = abs(p.width / p.height
                                      - aspects[p.index])
                    self.assertLessEqual(
                        ratio_error, (1 + aspects[p.index]) / p.height
                        + 1e-9)  # never cropped or stretched
                self.assertEqual(len({p.height for p in rows}), 1)
                for left, right in zip(rows, rows[1:]):
                    self.assertEqual(right.x - (left.x + left.width), gap)
                natural = height * sum(aspects[p.index] for p in rows) \
                    + gap * (len(rows) - 1)
                if natural > width:  # had to shrink: uses the whole width
                    self.assertEqual(rows[0].x, 0)
                    self.assertEqual(rows[-1].x + rows[-1].width, width)


M1 = Monitor("HDMI-A-1", 0, 0, 2560, 1440, True)
M2 = Monitor("DVI-I-1", 2560, 0, 1280, 1024)
M3 = Monitor("DP-1", 3840, 0, 1920, 1080)
ALL = [M1, M2, M3]


class ScreensTests(unittest.TestCase):
    def test_select_monitors(self):
        self.assertEqual(screens.select_monitors(ALL, []), ALL)
        self.assertEqual(screens.select_monitors(ALL, None), ALL)
        self.assertEqual(screens.select_monitors(ALL, ["DVI-I-1"]), [M2])
        # Their own order is kept, not the order they were asked for in.
        self.assertEqual(
            screens.select_monitors(ALL, ["DP-1", "HDMI-A-1"]), [M1, M3])
        self.assertEqual(
            screens.select_monitors(ALL, ["HDMI-A-1", "GONE-9"]), [M1])

    def test_nothing_connected_means_nothing_not_everything(self):
        self.assertEqual(screens.select_monitors(ALL, ["GONE-9"]), [])
        self.assertEqual(screens.select_monitors([], ["DP-1"]), [])

    def test_strip_layout(self):
        strip = screens.strip_layout(ALL)
        self.assertEqual((strip.width, strip.height), (5760, 1440))
        self.assertEqual(
            [(s.name, s.x, s.y, s.width, s.height) for s in strip.slots],
            [("HDMI-A-1", 0, 0, 2560, 1440),
             ("DVI-I-1", 2560, 208, 1280, 1024),
             ("DP-1", 3840, 180, 1920, 1080)])
        for left, right in zip(strip.slots, strip.slots[1:]):
            self.assertEqual(left.x + left.width, right.x)

    def test_strip_order_follows_position_not_input_order(self):
        names = [s.name for s in screens.strip_layout([M3, M1, M2]).slots]
        self.assertEqual(names, ["HDMI-A-1", "DVI-I-1", "DP-1"])
        stacked = screens.strip_layout([Monitor("B", 0, 1080, 1920, 1080),
                                        Monitor("A", 0, 0, 1920, 1080)])
        self.assertEqual([s.name for s in stacked.slots], ["A", "B"])
        left_of_primary = screens.strip_layout(
            [Monitor("R", 0, 0, 1920, 1080, True),
             Monitor("L", -1280, 0, 1280, 1024)])
        self.assertEqual([s.name for s in left_of_primary.slots], ["L", "R"])

    def test_a_skipped_middle_monitor_leaves_no_gap(self):
        chosen = screens.select_monitors(ALL, ["HDMI-A-1", "DP-1"])
        strip = screens.strip_layout(chosen)
        self.assertEqual([(s.name, s.x) for s in strip.slots],
                         [("HDMI-A-1", 0), ("DP-1", 2560)])
        self.assertEqual(strip.width, 2560 + 1920)

    def test_empty_and_single(self):
        self.assertEqual(screens.strip_layout([]), screens.Strip(0, 0, ()))
        self.assertEqual(screens.strip_layout([M1]).slots[0],
                         screens.Slot("HDMI-A-1", 0, 0, 2560, 1440))

    def test_safe_name(self):
        self.assertEqual(screens.safe_name("HDMI-A-1"), "HDMI-A-1")
        self.assertEqual(screens.safe_name("DP 1/left"), "DP_1_left")
        self.assertNotIn("/", screens.safe_name("../../etc"))


if __name__ == "__main__":
    unittest.main()
