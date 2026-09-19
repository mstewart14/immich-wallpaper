"""Tests for private storage and for handling untrusted server data."""
import contextlib
import io
import json
import logging
import os
import re
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import desktops
import immich_api
import rotate
import settings


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@contextlib.contextmanager
def loose_umask():
    """A umask that would make new files world-readable if we let it."""
    old = os.umask(0)
    try:
        yield
    finally:
        os.umask(old)


class PrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(os.environ["HOME"])
        self.addCleanup(self._clean)

    def _clean(self):
        for path in (settings.CONFIG_PATH, settings.STATE_PATH,
                     settings.LOG_PATH):
            path.unlink(missing_ok=True)

    def test_config_is_private_from_the_moment_it_exists(self):
        with loose_umask():
            settings.save_config({"api_key": "SECRET"})
        self.assertEqual(mode(settings.CONFIG_PATH), 0o600)
        self.assertEqual(mode(settings.CONFIG_DIR), 0o700)

    def test_state_and_cache_are_private(self):
        with loose_umask():
            settings.save_state(dict(settings.DEFAULT_STATE))
        self.assertEqual(mode(settings.STATE_PATH), 0o600)
        self.assertEqual(mode(settings.CACHE_DIR), 0o700)

    def test_existing_loose_directory_is_tightened(self):
        settings.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        settings.CACHE_DIR.chmod(0o775)
        settings.ensure_private_dir(settings.CACHE_DIR)
        self.assertEqual(mode(settings.CACHE_DIR), 0o700)

    def test_atomic_write_leaves_no_temp_file(self):
        settings.save_config({"a": 1})
        settings.save_config({"a": 2})
        leftovers = [p.name for p in settings.CONFIG_DIR.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertEqual(settings.read_stored_config(), {"a": 2})

    def test_exclusive_open_refuses_to_overwrite(self):
        settings.ensure_private_dir(settings.IMAGES_DIR)
        path = settings.IMAGES_DIR / "already-here.jpg"
        with settings.open_private(path, exclusive=True) as handle:
            handle.write(b"x")
        with self.assertRaises(FileExistsError):
            settings.open_private(path, exclusive=True)
        path.unlink()

    def test_saved_images_are_private(self):
        from PIL import Image
        settings.ensure_private_dir(settings.IMAGES_DIR)
        canvas = Image.new("RGB", (4, 4))
        with loose_umask():
            path = rotate._save_jpeg(canvas, settings.IMAGES_DIR / "t.jpg")
        self.assertEqual(mode(path), 0o600)
        path.unlink()

    def test_failure_log_is_private(self):
        settings.LOG_PATH.unlink(missing_ok=True)
        with loose_umask():
            handler = rotate._PrivateRotatingFileHandler(
                settings.LOG_PATH, delay=True)
            record = logging.LogRecord("t", logging.ERROR, "", 0, "boom",
                                       None, None)
            handler.emit(record)
            handler.close()
        self.assertEqual(mode(settings.LOG_PATH), 0o600)

    def test_existing_loose_log_is_tightened_on_startup(self):
        settings.ensure_private_dir(settings.CACHE_DIR)
        settings.LOG_PATH.write_text("old")
        settings.LOG_PATH.chmod(0o664)
        with mock.patch.object(rotate.logging, "basicConfig"):
            rotate._configure_logging()
        self.assertEqual(mode(settings.LOG_PATH), 0o600)


class UntrustedNamesAndIdsTests(unittest.TestCase):
    def setUp(self):
        settings.ensure_private_dir(settings.IMAGES_DIR)
        self.saved = []

    def tearDown(self):
        for path in self.saved:
            path.unlink(missing_ok=True)

    def save(self, name, mime="image/jpeg", content_type="image/jpeg"):
        path = rotate._save_original_file(
            {"originalFileName": name, "originalMimeType": mime},
            b"data", content_type, f"t{len(self.saved)}")
        self.saved.append(path)
        return path

    def test_safe_extensions_are_kept(self):
        self.assertEqual(self.save("holiday.JPG").suffix, ".jpg")
        self.assertEqual(self.save("x.heic", "image/heic").suffix, ".heic")

    def test_hostile_or_odd_names_fall_back_to_the_mime_type(self):
        for name in ["evil.jp\x00g", "a.b/../../c", "x.", "noext", "",
                     "x.averyveryverylongextension", "x.j pg", "x.<sh>",
                     "..", ".hidden"]:
            with self.subTest(name=name):
                path = self.save(name)
                self.assertEqual(path.suffix, ".jpg")
                self.assertEqual(path.parent, settings.IMAGES_DIR)
                self.assertRegex(path.name, r"^[A-Za-z0-9._-]+$")

    def test_missing_file_name_is_tolerated(self):
        path = rotate._save_original_file(
            {"originalFileName": None, "originalMimeType": "image/png"},
            b"d", None, "tnone")
        self.saved.append(path)
        self.assertEqual(path.suffix, ".png")

    def test_saved_original_is_private(self):
        with loose_umask():
            path = self.save("x.jpg")
        self.assertEqual(mode(path), 0o600)

    def test_asset_ids_cannot_change_the_request_path(self):
        seen = []

        def capture(_url, _key, path, **_kwargs):
            seen.append(path)
            return b"", None

        config = {"immich_url": "http://h", "api_key": "k"}
        with mock.patch.object(immich_api, "get_bytes", capture):
            rotate.download_asset_bytes(config, {"id": "a/../../b?x=1"})
        self.assertEqual(len(seen), 1)
        self.assertNotIn("../", seen[0])
        # Exactly /assets/<one segment>/original: three slashes.
        self.assertEqual(seen[0].count("/"), 3, seen[0])
        with self.assertRaises(ValueError):
            rotate.download_asset_bytes(config, {"id": ".."})

    def test_a_bad_id_just_means_no_caption(self):
        config = {"immich_url": "http://h", "api_key": "k"}
        self.assertIsNone(rotate.get_asset_details(config, ".."))

    def test_web_url_is_built_from_a_validated_url_and_quoted_id(self):
        meta = rotate.asset_meta(
            {"immich_url": "http://h:2283/"},
            {"id": "a b/c", "originalFileName": "x.jpg"})
        self.assertEqual(meta["web_url"], "http://h:2283/photos/a%20b%2Fc")
        with self.assertRaises(immich_api.UnsafeUrlError):
            rotate.asset_meta({"immich_url": "javascript:alert(1)"},
                              {"id": "x"})


class ConfigUrlTests(unittest.TestCase):
    def test_unsafe_server_url_in_the_config_is_refused(self):
        for url in ["file:///etc/passwd", "http://user:pw@host",
                    "ftp://host", "not a url"]:
            with self.subTest(url=url):
                settings.save_config({"immich_url": url, "api_key": "k"})
                with self.assertRaises(SystemExit):
                    rotate.load_required_config()

    def test_a_good_url_is_normalised(self):
        settings.save_config({"immich_url": "http://h:2283/", "api_key": "k"})
        self.assertEqual(rotate.load_required_config()["immich_url"],
                         "http://h:2283")


class DecodingUntrustedImagesTests(unittest.TestCase):
    def bomb(self):
        """A tiny PNG that claims to be ~144 megapixels."""
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("1", (12000, 12000)).save(buffer, "PNG")
        self.assertLess(len(buffer.getvalue()), 2_000_000, "must be small")
        return buffer.getvalue()

    def test_a_decompression_bomb_is_refused(self):
        with self.assertRaises(Exception):  # noqa: B017 (warning/error)
            rotate._load_oriented(self.bomb())

    def test_a_bomb_falls_back_to_the_original_file(self):
        self.assertIsNone(rotate._compose_single(
            self.bomb(), {}, {"originalFileName": "b.png"}, (1920, 1080),
            False, False))

    def test_any_decoder_failure_falls_back(self):
        for error in (ValueError("v"), OSError("o"), ZeroDivisionError(),
                      RecursionError(), MemoryError()):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(rotate, "_load_oriented",
                                      side_effect=error):
                self.assertIsNone(rotate._compose_single(
                    b"x", {}, {"originalFileName": "f.jpg"}, (100, 100),
                    False, False))

    def test_ordinary_photos_still_decode(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (640, 480), (10, 20, 30)).save(buffer, "JPEG")
        canvas = rotate._compose_single(
            buffer.getvalue(), {}, {"originalFileName": "ok.jpg"},
            (1920, 1080), False, False)
        self.assertEqual(canvas.size, (1920, 1080))


class KdeScriptInjectionTests(unittest.TestCase):
    def run_setter(self, path):
        scripts = []

        def capture(script):
            scripts.append(script)
            return subprocess.CompletedProcess([], 0, 'string "Error: 0"', "")
        with mock.patch.object(desktops, "_run_plasma_script", capture):
            desktops.set_wallpaper_kde(path)
        return scripts[0]

    def test_a_path_cannot_break_out_of_the_script_string(self):
        hostile = '/home/u/x"); evil(); ("\\\n '
        script = self.run_setter(hostile)
        match = re.search(r'writeConfig\("Image", (.*)\);', script)
        self.assertIsNotNone(match)
        # The argument is one valid JSON string that decodes to the path.
        self.assertEqual(json.loads(match.group(1)), "file://" + hostile)
        self.assertNotIn(" ", script)
        self.assertEqual(script.count("evil();"), 1)  # only inside the string

    def test_per_monitor_setter_escapes_names_and_paths_too(self):
        scripts = []
        with mock.patch.object(
                desktops, "_run_plasma_script",
                lambda s: scripts.append(s) or subprocess.CompletedProcess(
                    [], 0, 'string "applied|end"', "")):
            desktops.set_monitor_wallpapers_kde({'A"); evil(); ("': '/x"y'})
        mapping = json.loads(re.search(
            r"var images = (\{.*?\});", scripts[0]).group(1))
        self.assertEqual(mapping, {'A"); evil(); ("': 'file:///x"y'})


if __name__ == "__main__":
    unittest.main()
