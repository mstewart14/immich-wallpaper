"""Test suite for immich-wallpaper.

Run from the repository root with:

    python3 -m unittest discover -s tests -t . -v

Every test runs against a throwaway HOME, set here before any project
module is imported (they compute their paths at import time), so a test
can never read or change the real configuration, state or wallpaper.
"""
import os
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="immich-wallpaper-tests-")
os.environ.pop("XDG_RUNTIME_DIR", None)
