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
SHELL = ROOT / "openadapt_desktop" / "portal" / "shell"
CANONICAL = ROOT / "src" / "styles" / "vendor" / "openadapt-web" / "tokens.json"

# The shell is dark first. Each shell token and the canonical token it copies,
# per theme: the dark theme from the inset set, the light theme from the page
# set. A dark token with no canonical twin is mixed in CSS from canonical
# values and is not listed here; test_every_literal_colour_is_canonical covers
# what it may be mixed from.
DARK_TO_CANONICAL = {
    "--bg": "--inset-bg",
    "--panel": "--inset-raised",
    "--line": "--inset-border",
    "--text": "--inset-text",
    "--accent": "--inset-ok",
    "--warn": "--inset-warn",
    "--success": "--inset-ok",
}

LIGHT_TO_CANONICAL = {
    "--bg": "--surface",
    "--panel": "--surface-raised",
    "--panel-2": "--surface-sunken",
    "--line": "--hairline",
    "--text": "--ink",
    "--muted": "--text-secondary",
    "--faint": "--text-tertiary",
    "--accent": "--accent-verified",
    "--warn": "--accent-halt",
    "--success": "--accent-verified",
    "--danger": "--accent-danger",
    "--teach": "--link-visited",
    "--focus": "--focus-ring",
}

CSS = (SHELL / "styles.css").read_text(encoding="utf-8")


def _canonical_colors() -> dict[str, str]:
    return {
        name: value.upper()
        for name, value in json.loads(CANONICAL.read_text(encoding="utf-8"))["color"].items()
    }


def _tokens(block: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2).strip().upper()
        for match in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


def _dark_root() -> dict[str, str]:
    start = CSS.index(":root {")
    return _tokens(CSS[start : CSS.index("}", start)])


def _light_root() -> dict[str, str]:
    media = CSS.index("@media (prefers-color-scheme: light)")
    start = CSS.index(":root {", media)
    return _tokens(CSS[start : CSS.index("}", start)])


@pytest.mark.parametrize(("shell_token", "canonical_token"), DARK_TO_CANONICAL.items())
def test_dark_token_matches_canonical(shell_token: str, canonical_token: str) -> None:
    canonical = _canonical_colors()
    assert canonical_token in canonical, f"{canonical_token} is not a canonical token"
    assert _dark_root()[shell_token] == canonical[canonical_token], (
        f"dark {shell_token} in openadapt_desktop/portal/shell/styles.css drifted from "
        f"{canonical_token}. Copy the canonical value; do not pick a new one."
    )


@pytest.mark.parametrize(("shell_token", "canonical_token"), LIGHT_TO_CANONICAL.items())
def test_light_token_matches_canonical(shell_token: str, canonical_token: str) -> None:
    canonical = _canonical_colors()
    assert canonical_token in canonical, f"{canonical_token} is not a canonical token"
    assert _light_root()[shell_token] == canonical[canonical_token], (
        f"light {shell_token} in openadapt_desktop/portal/shell/styles.css drifted from "
        f"{canonical_token}. Copy the canonical value; do not pick a new one."
    )


def test_every_literal_colour_is_canonical() -> None:
    """A dark value without a canonical twin is mixed, never picked."""
    canonical = set(_canonical_colors().values())
    for value in re.findall(r"#[0-9A-Fa-f]{6}\b", CSS):
        assert value.upper() in canonical, f"{value} in styles.css is not a canonical colour"


def test_shell_theme_colour_matches_the_ground() -> None:
    colors = _canonical_colors()
    dark, light = colors["--inset-bg"], colors["--surface"]
    manifest = json.loads((SHELL / "manifest.webmanifest").read_text(encoding="utf-8"))
    # The installed app opens dark first, matching the shell's default.
    assert manifest["background_color"].upper() == dark
    assert manifest["theme_color"].upper() == dark
    head = (SHELL / "index.html").read_text(encoding="utf-8")
    for scheme, ground in (("dark", dark), ("light", light)):
        assert re.search(
            rf'name="theme-color" media="\(prefers-color-scheme: {scheme}\)" content="{ground}"',
            head,
            re.IGNORECASE,
        ), f"the shell's {scheme} <meta name=theme-color> must be the canonical ground"


def test_shell_carries_no_retired_warm_value() -> None:
    # The palette openadapt-cloud retired in its PR #325.
    retired = {
        "#f4f3ed",
        "#fffef9",
        "#eeede5",
        "#d6d8ce",
        "#252a22",
        "#687066",
        "#2f7154",
        "#a66a25",
        "#2e7c5a",
        "#a34f4c",
        "#356f9f",
        "#dcece5",
        "#dff1e8",
        "#f6ead7",
        "#f7e4e2",
        "#e0ebf5",
    }
    for name in ("styles.css", "index.html", "manifest.webmanifest", "app.js"):
        text = (SHELL / name).read_text(encoding="utf-8").lower()
        found = sorted(value for value in retired if value in text)
        assert not found, f"{name} still carries retired warm-palette values: {found}"
