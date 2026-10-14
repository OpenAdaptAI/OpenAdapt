"""Capture exact, privacy-safe runtime facts for governed evidence campaigns."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not_installed"


def _browser_revision() -> str:
    distribution = importlib.metadata.distribution("playwright")
    browsers = distribution.locate_file("playwright/driver/package/browsers.json")
    document = json.loads(Path(browsers).read_text(encoding="utf-8"))
    for browser in document.get("browsers", []):
        if browser.get("name") == "chromium":
            revision = browser.get("revision")
            if isinstance(revision, str) and revision:
                return revision
    raise RuntimeError("Playwright does not declare a Chromium revision")


def capture_runtime(out_dir: Path, *, browser_required: bool) -> dict[str, Any]:
    """Write an exact installed-package snapshot and return its binding facts."""

    freeze = subprocess.run(
        [sys.executable, "-m", "pip", "freeze", "--all"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ).stdout
    freeze_path = out_dir / "dependency-freeze.txt"
    freeze_path.write_text(freeze, encoding="utf-8")
    facts: dict[str, Any] = {
        "python_version": platform.python_version(),
        "openadapt_types_version": _version("openadapt-types"),
        "dependency_snapshot_filename": freeze_path.name,
        "dependency_snapshot_sha256": _sha256(freeze_path),
    }
    if browser_required:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                facts["browser_version"] = browser.version
            finally:
                browser.close()
        facts["browser_revision"] = _browser_revision()
    return facts
