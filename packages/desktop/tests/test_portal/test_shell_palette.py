"""The plain-file phone shell is generated from the pinned shared theme.

The same guard compares every phone role in dark and light mode, rejects
unowned literal colors, and checks the installed browser chrome colors.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "openadapt_desktop" / "portal" / "shell"
CANONICAL = ROOT / "src" / "styles" / "vendor" / "semantic-theme" / "semantic-theme.json"

# The shell is dark first. Each shell token and the canonical token it copies,
# per theme: the dark theme from the inset set, the light theme from the page
# set. A dark token with no canonical twin is mixed in CSS from canonical
# values and is not listed here; test_every_literal_colour_is_canonical covers
# what it may be mixed from.
DARK_TO_CANONICAL = {
    "--bg": "--oa-canvas",
    "--panel": "--oa-surface",
    "--panel-2": "--oa-surface-sunken",
    "--line": "--oa-border",
    "--text": "--oa-text",
    "--muted": "--oa-text-muted",
    "--faint": "--oa-text-muted",
    "--accent": "--oa-link",
    "--warn": "--oa-stopped",
    "--success": "--oa-verified",
    "--danger": "--oa-reconcile",
    "--teach": "--oa-link",
    "--focus": "--oa-focus",
    "--action": "--oa-text",
    "--on-action": "--oa-canvas",
}
LIGHT_TO_CANONICAL = DARK_TO_CANONICAL

CSS = (SHELL / "styles.css").read_text(encoding="utf-8")


def _canonical_colors(theme: str = "dark") -> dict[str, str]:
    return {
        name: value.upper()
        for name, value in json.loads(CANONICAL.read_text(encoding="utf-8"))["themes"][
            theme
        ].items()
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
    canonical = _canonical_colors("light")
    assert canonical_token in canonical, f"{canonical_token} is not a canonical token"
    assert _light_root()[shell_token] == canonical[canonical_token], (
        f"light {shell_token} in openadapt_desktop/portal/shell/styles.css drifted from "
        f"{canonical_token}. Copy the canonical value; do not pick a new one."
    )


def test_every_literal_colour_is_canonical() -> None:
    """A dark value without a canonical twin is mixed, never picked."""
    canonical = set(_canonical_colors().values()) | set(_canonical_colors("light").values())
    for value in re.findall(r"#[0-9A-Fa-f]{6}\b", CSS):
        assert value.upper() in canonical, f"{value} in styles.css is not a canonical colour"


def test_shell_theme_colour_matches_the_ground() -> None:
    colors = _canonical_colors()
    dark = colors["--oa-canvas"]
    light = _canonical_colors("light")["--oa-canvas"]
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


def test_the_self_hosted_interface_font_and_its_license_are_the_pinned_bytes() -> None:
    import hashlib

    vendor = CANONICAL.parent
    assert 'url("/fonts/PublicSans-Variable.woff2")' in CSS
    assert (
        hashlib.sha256((SHELL / "fonts/PublicSans-Variable.woff2").read_bytes()).digest()
        == hashlib.sha256((vendor / "fonts/PublicSans-Variable.woff2").read_bytes()).digest()
    )
    assert (vendor / "fonts/OFL-PublicSans.txt").read_bytes() == (
        SHELL / "fonts/OFL-PublicSans.txt"
    ).read_bytes()
    assert "/fonts/OFL-PublicSans.txt" in CSS
    assert ".ans.yes.primary { background: var(--action);" in CSS
