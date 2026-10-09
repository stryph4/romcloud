"""Helpers for tests that require a runnable Chromium-family browser."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest


@lru_cache(maxsize=1)
def _usable_chromium() -> str | None:
    """Return a browser that can complete a minimal headless DOM load."""

    candidates = (
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    )
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        with tempfile.TemporaryDirectory(prefix="romcloud-browser-probe-") as profile:
            try:
                probe = subprocess.run(
                    [
                        str(candidate),
                        "--headless=new",
                        "--disable-gpu",
                        "--no-sandbox",
                        f"--user-data-dir={profile}",
                        "--dump-dom",
                        "data:text/html,<title>romcloud-browser-probe</title>",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0 and "romcloud-browser-probe" in probe.stdout:
                return str(candidate)
    return None


def usable_chromium() -> str:
    """Return a runnable browser, ignoring package-manager launcher stubs."""

    browser = _usable_chromium()
    if browser is not None:
        return browser
    pytest.skip("A runnable Chromium-family browser is not installed on this host")
