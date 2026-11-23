#!/usr/bin/env python3
"""Validate and package the OpenAdapt Chrome MV3 Co-Pilot extension for Chrome Web Store.

Usage:
    python3 scripts/package_chrome_extension.py
    python3 scripts/package_chrome_extension.py --out dist/openadapt-copilot-cws.zip
    python3 scripts/package_chrome_extension.py --check-only
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXT_DIR = REPO_ROOT / "packages" / "flow" / "chrome_extension"
DEFAULT_OUT_ZIP = REPO_ROOT / "dist" / "openadapt-copilot-cws.zip"

# Files/directories inside packages/flow/chrome_extension/ that must NOT be
# shipped in the Chrome Web Store upload zip.
EXCLUDED_TOP_LEVEL = {
    "STORE_LISTING.md",
    "README.md",
    "native_host",
    "__pycache__",
    ".DS_Store",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip", ".map", ".md"}

MAX_DESCRIPTION_LEN = 132
REQUIRED_ICON_SIZES = (16, 48, 128)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
INLINE_HANDLER_RE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
EVAL_RE = re.compile(r"\b(?:eval|new\s+Function)\s*\(")


def _read_png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a valid PNG file")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def validate_extension_dir(ext_dir: Path) -> list[str]:
    """Validate MV3 & Chrome Web Store readiness. Returns list of error strings."""
    errors: list[str] = []
    manifest_path = ext_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"Missing manifest.json in {ext_dir}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"Invalid manifest.json JSON: {exc}"]

    if manifest.get("manifest_version") != 3:
        errors.append("manifest_version must be 3")

    name = manifest.get("name", "")
    if not isinstance(name, str) or not name.strip():
        errors.append("manifest.name must be a non-empty string")
    elif len(name) > 75:
        errors.append(f"manifest.name exceeds 75 chars ({len(name)})")

    desc = manifest.get("description", "")
    if not isinstance(desc, str) or not desc.strip():
        errors.append("manifest.description must be a non-empty string")
    elif len(desc) > MAX_DESCRIPTION_LEN:
        errors.append(
            f"manifest.description exceeds {MAX_DESCRIPTION_LEN} chars ({len(desc)})"
        )

    version = manifest.get("version", "")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", version):
        errors.append(
            f"manifest.version must be 1-4 dot-separated integers: {version!r}"
        )

    # Verify background service worker exists
    bg = manifest.get("background") or {}
    sw_rel = bg.get("service_worker")
    if not isinstance(sw_rel, str) or not (ext_dir / sw_rel).is_file():
        errors.append(f"background.service_worker missing: {sw_rel!r}")

    # Verify side_panel.default_path exists
    sp = manifest.get("side_panel") or {}
    sp_rel = sp.get("default_path")
    if not isinstance(sp_rel, str) or not (ext_dir / sp_rel).is_file():
        errors.append(f"side_panel.default_path missing: {sp_rel!r}")

    # Verify icons exist and match exact declared PNG pixel dimensions
    icons = manifest.get("icons") or {}
    for size in REQUIRED_ICON_SIZES:
        rel = icons.get(str(size))
        if not isinstance(rel, str):
            errors.append(f"manifest.icons missing required size {size}")
            continue
        icon_path = ext_dir / rel
        if not icon_path.is_file():
            errors.append(f"Icon file missing for size {size}: {rel}")
            continue
        try:
            w, h = _read_png_dimensions(icon_path)
            if (w, h) != (size, size):
                errors.append(
                    f"Icon {rel} dimensions are {w}x{h}, expected {size}x{size}"
                )
        except ValueError as exc:
            errors.append(str(exc))

    # Verify content scripts exist
    for idx, cs in enumerate(manifest.get("content_scripts") or []):
        for js_rel in cs.get("js") or []:
            if not (ext_dir / js_rel).is_file():
                errors.append(f"content_scripts[{idx}] missing js file: {js_rel}")

    # CSP / MV3 static checks on packaged HTML & JS files
    for rel_path in collect_package_files(ext_dir):
        full_path = ext_dir / rel_path
        if rel_path.suffix.lower() == ".html":
            text = full_path.read_text(encoding="utf-8")
            if INLINE_SCRIPT_RE.search(text):
                errors.append(f"Inline <script> not allowed in MV3 HTML: {rel_path}")
            if INLINE_HANDLER_RE.search(text):
                errors.append(
                    f"Inline event handler not allowed in MV3 HTML: {rel_path}"
                )
        elif rel_path.suffix.lower() == ".js":
            text = full_path.read_text(encoding="utf-8")
            if EVAL_RE.search(text):
                errors.append(
                    f"eval()/new Function() not allowed in MV3 JS: {rel_path}"
                )

    return errors


def collect_package_files(ext_dir: Path) -> list[Path]:
    """Return sorted relative paths to include in the CWS zip bundle."""
    included: list[Path] = []
    for path in sorted(ext_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ext_dir)
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            continue
        if parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        if "__pycache__" in parts:
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        included.append(rel)
    return included


def build_cws_zip(ext_dir: Path, out_zip: Path) -> list[str]:
    """Validate and build a deterministic Chrome Web Store zip archive.

    Returns the sorted list of archive member names written to ``out_zip``.
    """
    errors = validate_extension_dir(ext_dir)
    if errors:
        raise ValueError(
            "Chrome extension validation failed:\n- " + "\n- ".join(errors)
        )

    rel_files = collect_package_files(ext_dir)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    members: list[str] = []
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in rel_files:
            arcname = rel.as_posix()
            info = zipfile.ZipInfo(filename=arcname, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (ext_dir / rel).read_bytes())
            members.append(arcname)
    return members


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and package the OpenAdapt Chrome MV3 Co-Pilot extension."
    )
    parser.add_argument(
        "--ext-dir",
        type=Path,
        default=DEFAULT_EXT_DIR,
        help="Path to the chrome_extension directory.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_ZIP,
        help="Output .zip path for Chrome Web Store upload.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run MV3 & CWS validation without writing a zip file.",
    )
    args = parser.parse_args(argv)

    errors = validate_extension_dir(args.ext_dir)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    if args.check_only:
        print(f"OK: {args.ext_dir} passed Chrome Web Store MV3 validation.")
        return 0

    members = build_cws_zip(args.ext_dir, args.out)
    print(f"Built {args.out} ({len(members)} files): {', '.join(members)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
