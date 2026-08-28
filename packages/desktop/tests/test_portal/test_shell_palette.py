"""The phone shell's palette must stay the canonical one.

The shell is served by the engine as plain files with no bundler, so it cannot
import the vendored token stylesheet the way the React cockpit does. Its
palette is therefore a literal copy, and a copy drifts unless something fails.

This is what fails. It reads the vendored canonical tokens
(src/styles/vendor/openadapt-web/tokens.json, itself hash-pinned by
scripts/designTokens.test.ts and scripts/vendor-design-tokens.mjs) and
checks the shell against them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "engine" / "portal" / "shell"
CANONICAL = ROOT / "src" / "styles" / "vendor" / "openadapt-web" / "tokens.json"

# Each shell token and the canonical token it copies.
SHELL_TO_CANONICAL = {
    "--bg": "--surface",
    "--panel": "--surface-raised",
    "--panel-2": "--surface-sunken",
    "--line": "--hairline",
    "--text": "--ink",
    "--muted": "--text-secondary",
    "--accent": "--accent-verified",
    "--warn": "--accent-halt",
    "--success": "--accent-verified",
    "--danger": "--accent-danger",
    "--teach": "--link-visited",
}


def _canonical_colors() -> dict[str, str]:
    return {
        name: value.upper()
        for name, value in json.loads(CANONICAL.read_text(encoding="utf-8"))["color"].items()
    }


def _shell_root() -> dict[str, str]:
    css = (SHELL / "styles.css").read_text(encoding="utf-8")
    block = css[css.index(":root {") : css.index("}", css.index(":root {"))]
    return {
        match.group(1): match.group(2).strip().upper()
        for match in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


@pytest.mark.parametrize(("shell_token", "canonical_token"), SHELL_TO_CANONICAL.items())
def test_shell_token_matches_canonical(shell_token: str, canonical_token: str) -> None:
    canonical = _canonical_colors()
    assert canonical_token in canonical, f"{canonical_token} is not a canonical token"
    assert _shell_root()[shell_token] == canonical[canonical_token], (
        f"{shell_token} in engine/portal/shell/styles.css drifted from "
        f"{canonical_token}. Copy the canonical value; do not pick a new one."
    )


def test_shell_theme_colour_matches_the_ground() -> None:
    ground = _canonical_colors()["--surface"]
    manifest = json.loads((SHELL / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["background_color"].upper() == ground
    assert manifest["theme_color"].upper() == ground
    head = (SHELL / "index.html").read_text(encoding="utf-8")
    assert re.search(rf'content="{ground}"', head, re.IGNORECASE), (
        "the shell's <meta name=theme-color> must be the canonical ground"
    )


def test_shell_carries_no_retired_warm_value() -> None:
    # The palette openadapt-cloud retired in its PR #325.
    retired = {
        "#f4f3ed", "#fffef9", "#eeede5", "#d6d8ce", "#252a22", "#687066",
        "#2f7154", "#a66a25", "#2e7c5a", "#a34f4c", "#356f9f",
        "#dcece5", "#dff1e8", "#f6ead7", "#f7e4e2", "#e0ebf5",
    }
    for name in ("styles.css", "index.html", "manifest.webmanifest", "app.js"):
        text = (SHELL / name).read_text(encoding="utf-8").lower()
        found = sorted(value for value in retired if value in text)
        assert not found, f"{name} still carries retired warm-palette values: {found}"
