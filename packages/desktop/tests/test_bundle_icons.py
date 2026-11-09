"""Guard the macOS app icon and branded DMG window against the placeholder."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "src-tauri" / "icons"
DMG_BG = ROOT / "src-tauri" / "dmg" / "background.png"
TAURI_CONF = ROOT / "src-tauri" / "tauri.conf.json"

# SHA-256 prefix of the green rounded-square-with-bar placeholder Finder
# reads as a red minus. The v0.15.0 DMG shipped it.
FORBIDDEN_PNG_SHA256 = "bfe0288bd6f672e2883e771ae509769f25e704bb"


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    assert data[12:16] == b"IHDR", f"{path} PNG has no IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_icon_png_is_not_the_minus_bar_placeholder() -> None:
    icon = ICONS / "icon.png"
    assert icon.is_file()
    digest = hashlib.sha256(icon.read_bytes()).hexdigest()
    assert not digest.startswith(FORBIDDEN_PNG_SHA256), (
        f"icon.png still matches the minus-bar placeholder ({digest})"
    )


def test_icon_ico_contains_windows_sizes() -> None:
    data = (ICONS / "icon.ico").read_bytes()
    reserved, itype, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0
    assert itype == 1
    assert count >= 6


def test_icon_icns_exists_and_is_listed_in_tauri_conf() -> None:
    icns = ICONS / "icon.icns"
    assert icns.is_file()
    assert icns.read_bytes()[:4] == b"icns"
    config = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    icons = config["bundle"]["icon"]
    assert "icons/icon.icns" in icons
    assert "icons/icon.ico" in icons
    assert "icons/icon.png" in icons


def test_dmg_background_is_configured_and_660x400() -> None:
    config = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    dmg = config["bundle"]["macOS"]["dmg"]
    background = dmg["background"]
    assert background
    path = ROOT / "src-tauri" / background
    assert path.is_file(), f"missing DMG background {path}"
    assert path == DMG_BG
    assert _png_dimensions(path) == (660, 400)
    assert dmg["windowSize"] == {"width": 660, "height": 400}
    assert dmg["appPosition"] == {"x": 180, "y": 170}
    assert dmg["applicationFolderPosition"] == {"x": 480, "y": 170}


def test_gitignore_tracks_icon_icns() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in gitignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    ignored = "src-tauri/icons/*.icns" in lines
    tracked = "!src-tauri/icons/icon.icns" in lines
    assert not ignored or tracked
