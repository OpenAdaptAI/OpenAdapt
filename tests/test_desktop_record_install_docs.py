"""A desktop `record` example must show the install that makes it work.

The base `pip install openadapt` brings the browser engine only. Desktop
recording needs the `capture` extra, and native replay needs the surface's
extra (for example `macos`). A reader who copies a desktop record/replay
example after the base install gets a missing-dependency error, so every
code block that records a native desktop surface has to carry its install.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = [
    "README.md",
    "docs/cli.md",
    "docs/getting-started/quickstart.md",
    "packages/flow/README.md",
]
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
DESKTOP_RECORD = re.compile(
    r"\brecord\b[^\n]*--backend\s+(windows|macos|linux|citrix)\b"
)
INSTALL = re.compile(r"pip install[^\n]*\[([^\]\n]*)\]")


@pytest.mark.parametrize("page", PAGES)
def test_desktop_record_blocks_show_capture_and_surface_install(page: str) -> None:
    text = (ROOT / page).read_text(encoding="utf-8")
    for block in FENCE.finditer(text):
        body = block.group(1)
        for match in DESKTOP_RECORD.finditer(body):
            surface = match.group(1)
            before = body[: match.start()]
            extras = {
                extra.strip()
                for install in INSTALL.finditer(before)
                for extra in install.group(1).split(",")
            }
            if surface == "citrix":
                # A local Citrix client records on a macOS or Windows host.
                assert "capture" in extras and extras & {"macos", "windows"}, (
                    page,
                    match.group(0),
                )
                continue
            assert {"capture", surface} <= extras, (
                page,
                match.group(0),
                sorted(extras),
            )
