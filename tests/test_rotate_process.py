"""End-to-end tests: rotate.py run as a real process.

Each test gets its own HOME, a fake Immich server, and a stub desktop
backend that reports the monitors the test asks for and records what it is
asked to set, so nothing ever touches a real wallpaper.
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests.fake_immich import FakeImmich, asset, jpeg

REPO = Path(__file__).resolve().parent.parent
COLORS = [(200, 30, 30), (30, 160, 30), (30, 30, 200), (200, 200, 30),
          (30, 200, 200), (200, 30, 200), (120, 120, 120), (250, 130, 20)]
WRAPPER = f"""
import json, os, sys
sys.path.insert(0, {str(REPO)!r})
import desktops, rotate
from desktops import Monitor
monitors = [Monitor(**m) for m in json.loads(os.environ["FAKE_MONITORS"])]
record = []
def set_monitors(images):
    record.append({{"monitors": {{k: str(v) for k, v in images.items()}}}})
    return True
def set_one(path):
    record.append({{"single": str(path)}})
    return True
per_monitor = set_monitors if os.environ.get("PER_MONITOR") == "1" else None
size = (monitors[0].width, monitors[0].height) if monitors else None
desktops.BACKENDS[:] = [desktops.DesktopBackend(
    "stub", ("stub",), None, lambda: size, set_one, lambda: monitors,
    per_monitor)]
os.environ["XDG_CURRENT_DESKTOP"] = "stub"
try:
    rotate.main()
finally:
    open(os.environ["RECORD_FILE"], "w").write(json.dumps(record))
"""
ONE = [dict(name="HDMI-A-1", x=0, y=0, width=1920, height=1080,
            primary=True)]
TWO = [dict(name="HDMI-A-1", x=0, y=0, width=2560, height=1440,
            primary=True),
       dict(name="DVI-I-1", x=2560, y=0, width=1280, height=1024,
            primary=False)]
THREE = TWO + [dict(name="DP-1", x=3840, y=0, width=1920, height=1080,
                    primary=False)]


class RotateProcessCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="iw-proc-"))
        self.addCleanup(self._cleanup)
        self.immich = None
        self.landscape(8)

    def _cleanup(self):
        if self.immich:
            self.immich.stop()
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def serve(self, assets, images):
        if self.immich:
            self.immich.stop()
        self.immich = FakeImmich(assets, images)

    def landscape(self, count):
        self.serve(
            [asset(f"m{i}", 3200, 1800) for i in range(count)],
            {f"m{i}": jpeg(320, 180, COLORS[i % 8]) for i in range(count)})

    def portraits(self, count):
        self.serve(
            [asset(f"p{i}", 900, 1600) for i in range(count)],
            {f"p{i}": jpeg(90, 160, COLORS[i % 8]) for i in range(count)})

    def write_config(self, **config):
        base = {"immich_url": self.immich.url, "api_key": "secret",
                "keep_count": 3, "interval_minutes": 5}
        path = self.home / ".config" / "immich-wallpaper" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**base, **config}))

    def run_rotate(self, *args, monitors=ONE, per_monitor=False):
        record = self.home / "record.json"
        record.unlink(missing_ok=True)
        env = {**os.environ, "HOME": str(self.home),
               "FAKE_MONITORS": json.dumps(monitors),
               "RECORD_FILE": str(record),
               "PER_MONITOR": "1" if per_monitor else "0"}
        env.pop("XDG_RUNTIME_DIR", None)
        result = subprocess.run(
            [sys.executable, "-B", "-c", WRAPPER, *args], env=env,
            capture_output=True, text=True, timeout=60)
        recorded = json.loads(record.read_text()) if record.exists() else None
        return result, recorded

    @property
    def cache(self):
        return self.home / ".cache" / "immich-wallpaper"

    def state(self):
        return json.loads((self.cache / "state.json").read_text())

    def last_entry(self):
        state = self.state()
        return state["history"][state["position"]], state


class SingleScreenFlowTests(RotateProcessCase):
    def test_a_rotation_stores_applies_and_logs_in_the_documented_format(self):
        self.portraits(4)
        self.write_config()
        result, recorded = self.run_rotate("--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(
            result.stdout.strip(),
            r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] Stored \S+\.jpg "
            r"\(pair, \d+ KB\); wallpaper applied$")
        entry, state = self.last_entry()
        self.assertEqual(entry["kind"], "pair")
        self.assertIsNone(state["last_error"])
        self.assertEqual(Image.open(entry["path"]).size, (1920, 1080))
        self.assertEqual(recorded, [{"single": entry["path"]}])

    def test_control_commands(self):
        self.landscape(3)
        self.write_config()
        self.run_rotate("--once")
        result, _ = self.run_rotate("--pause")
        self.assertIn("Paused.", result.stdout)
        self.assertTrue(self.state()["paused"])
        result, recorded = self.run_rotate("--once")   # forced beats paused
        self.assertIn("Stored", result.stdout)
        self.run_rotate("--pause")
        result, _ = self.run_rotate()                   # paused: quiet no-op
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        result, _ = self.run_rotate("--resume")
        self.assertIn("Resumed.", result.stdout)
        result, _ = self.run_rotate("--status")
        self.assertFalse(json.loads(result.stdout)["paused"])
        result, _ = self.run_rotate("--back")
        self.assertEqual(result.stdout.strip(), "moved")
        result, _ = self.run_rotate("--forward")
        self.assertEqual(result.stdout.strip(), "moved")

    def test_not_due_yet_is_a_quiet_no_op(self):
        self.landscape(3)
        self.write_config()
        self.run_rotate("--once")
        result, recorded = self.run_rotate()
        self.assertEqual((result.returncode, result.stdout, recorded),
                         (0, "", []))

    def test_missing_or_unsafe_config_exits_with_a_message(self):
        result, _ = self.run_rotate("--once")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No config at", result.stdout)
        for url in ("file:///etc/passwd", "http://user:pw@host"):
            self.write_config(immich_url=url)
            result, _ = self.run_rotate("--once")
            self.assertEqual(result.returncode, 1, url)
            self.assertIn("unusable immich_url", result.stdout)

    def test_a_wrong_key_is_recorded_and_the_desktop_is_left_alone(self):
        self.write_config(api_key="wrong")
        result, recorded = self.run_rotate("--once")
        self.assertIn("Immich request failed: HTTP 401", result.stdout)
        self.assertIn("HTTP 401", self.state()["last_error"])
        self.assertEqual(recorded, [])

    def test_the_failure_log_holds_proxy_headers_but_no_secrets(self):
        self.write_config(api_key="proxyblock")
        self.assertFalse((self.cache / "rotate.log").exists())
        result, _ = self.run_rotate("--once")
        self.assertIn("HTTP 403", result.stdout)
        log = (self.cache / "rotate.log").read_text()
        self.assertIn("HTTP 403 from", log)
        self.assertIn("/api/search/random", log)
        self.assertIn("Server: traefik-test", log)
        self.assertNotIn("proxyblock", log)      # the API key
        self.assertEqual(
            stat.S_IMODE((self.cache / "rotate.log").stat().st_mode), 0o600)

    def test_successful_runs_create_no_log_and_leave_private_files(self):
        self.write_config()
        self.run_rotate("--once")
        self.assertFalse((self.cache / "rotate.log").exists())

        def mode(path):
            return stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(mode(self.cache), 0o700)
        self.assertEqual(mode(self.cache / "images"), 0o700)
        self.assertEqual(mode(self.cache / "state.json"), 0o600)
        entry, _ = self.last_entry()
        self.assertEqual(mode(Path(entry["path"])), 0o600)

    def test_a_server_that_redirects_elsewhere_never_receives_the_key(self):
        from tests.helpers import Recorder, reply, serve, stop
        stolen = Recorder()
        sink, sink_url = serve(lambda r: reply(r), stolen)
        bouncer, bounce_url = serve(lambda r: reply(
            r, status=302, body=b"", headers={"Location": sink_url + "/x"}))
        self.addCleanup(stop, sink)
        self.addCleanup(stop, bouncer)
        self.write_config(immich_url=bounce_url)
        result, _ = self.run_rotate("--once")
        self.assertIn("redirect to a different address refused",
                      result.stdout)
        self.assertEqual(stolen.requests, [])


class MultiScreenFlowTests(RotateProcessCase):
    def run_multi(self, connected, **config):
        """Rotate with the `connected` monitors and the given config."""
        self.write_config(**config)
        return self.run_rotate("--once", monitors=connected,
                               per_monitor=True)

    def test_same(self):
        result, recorded = self.run_multi(TWO, multi_monitor_mode="same")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry, state = self.last_entry()
        self.assertEqual(entry["kind"], "multi")
        self.assertEqual(Image.open(entry["images"]["HDMI-A-1"]).size,
                         (2560, 1440))
        self.assertEqual(Image.open(entry["images"]["DVI-I-1"]).size,
                         (1280, 1024))
        self.assertEqual(recorded, [{"monitors": entry["images"]}])
        self.assertIn("wallpaper applied", result.stdout)

    def test_different_gives_two_distinct_photos(self):
        self.run_multi(TWO, multi_monitor_mode="different")
        entry, _ = self.last_entry()
        self.assertEqual(len(entry["assets"]), 2)

    def test_span_slices_a_three_monitor_strip(self):
        self.run_multi(THREE, multi_monitor_mode="span")
        entry, _ = self.last_entry()
        self.assertEqual(set(entry["images"]),
                         {"HDMI-A-1", "DVI-I-1", "DP-1"})
        self.assertEqual(Image.open(entry["images"]["DP-1"]).size,
                         (1920, 1080))
        self.assertGreaterEqual(len(entry["assets"]), 2)

    def test_only_the_chosen_monitors_are_set(self):
        _, recorded = self.run_multi(THREE, monitors=["DVI-I-1", "DP-1"])
        entry, _ = self.last_entry()
        self.assertEqual(set(entry["images"]), {"DVI-I-1", "DP-1"})
        self.assertEqual(recorded, [{"monitors": entry["images"]}])
        _, recorded = self.run_multi(THREE, monitors=["DVI-I-1"])
        entry, _ = self.last_entry()
        self.assertEqual(entry["kind"], "single")
        self.assertEqual(list(recorded[0]["monitors"]), ["DVI-I-1"])

    def test_no_selected_monitor_connected_changes_nothing(self):
        self.run_multi(THREE)
        before = self.state()["position"]
        result, recorded = self.run_multi(THREE, monitors=["GONE-9"])
        state = self.state()
        self.assertIn("None of the selected monitors is connected",
                      result.stdout)
        self.assertIn("GONE-9", state["last_error"])
        self.assertEqual((recorded, state["position"]), ([], before))

    def test_a_desktop_without_per_monitor_support_gets_one_image(self):
        self.write_config(multi_monitor_mode="different",
                          monitors=["DVI-I-1"])
        _, recorded = self.run_rotate("--once", monitors=TWO,
                                      per_monitor=False)
        entry, _ = self.last_entry()
        self.assertNotIn("images", entry)
        self.assertEqual(recorded, [{"single": entry["path"]}])
        self.assertEqual(Image.open(entry["path"]).size, (2560, 1440))

    def test_one_monitor_is_always_the_plain_path(self):
        self.write_config(multi_monitor_mode="span")
        _, recorded = self.run_rotate("--once", monitors=ONE,
                                      per_monitor=True)
        entry, _ = self.last_entry()
        self.assertNotIn("images", entry)
        self.assertEqual(recorded, [{"single": entry["path"]}])

    def test_several_portraits_on_an_ultrawide(self):
        self.portraits(8)
        ultra = [dict(name="DP-1", x=0, y=0, width=5120, height=1440,
                      primary=True)]
        self.write_config(max_photos_per_screen=6)
        self.run_rotate("--once", monitors=ultra)
        entry, _ = self.last_entry()
        self.assertEqual(Image.open(entry["path"]).size, (5120, 1440))
        self.assertGreaterEqual(len(entry["assets"]), 4)
        self.write_config(max_photos_per_screen=1)
        self.run_rotate("--once", monitors=ultra)
        entry, _ = self.last_entry()
        self.assertEqual(len(entry["assets"]), 1)

    def test_invalid_screen_settings_fall_back_to_defaults(self):
        result, _ = self.run_multi(TWO, multi_monitor_mode="bogus",
                                   max_photos_per_screen="lots")
        entry, state = self.last_entry()
        self.assertEqual((result.returncode, state["last_error"]), (0, None))
        self.assertEqual(set(entry["images"]), {"HDMI-A-1", "DVI-I-1"})

    def test_history_keeps_exactly_the_files_of_its_entries(self):
        for _ in range(4):
            self.run_multi(THREE, multi_monitor_mode="different",
                           keep_count=2)
        state = self.state()
        on_disk = sorted(p.name for p in (self.cache / "images").iterdir())
        expected = sorted({Path(f).name for entry in state["history"]
                           for f in [entry["path"],
                                     *entry.get("images", {}).values()]})
        self.assertEqual(len(state["history"]), 2)
        self.assertEqual(len(on_disk), 6)
        self.assertEqual(on_disk, expected)
        self.assertTrue(all(re.match(r"^\d+-[0-9a-f]{8}-", n)
                            for n in on_disk))


if __name__ == "__main__":
    unittest.main()
