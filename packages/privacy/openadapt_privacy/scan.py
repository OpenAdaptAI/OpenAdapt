#!/usr/bin/env python3
"""Fail closed on PHI shapes, RDP hostnames, and forbidden paths.

This module is the git/CI gate. It is stdlib-only: importing
``openadapt_privacy.scan`` must not load Presidio, spaCy, or Pillow.

It must refuse a match, and it must refuse a silent miss: ``self_test()``
plants fixtures in a temp dir and returns non-zero if any rule fails to fire.

Do not put a matching example in this file. Fixtures are built at runtime.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# Python regexes. Keep in sync with gitleaks.toml and phi-patterns.txt.
RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "ohip-dashed",
        re.compile(r"\b\d{4}-\d{3}-\d{3}(?:-[A-Za-z]{1,2})?\b"),
    ),
    ("ohip-spaced", re.compile(r"\b\d{4} \d{3} \d{3}\b")),
    ("ohip-dotted", re.compile(r"\b\d{4}\.\d{3}\.\d{3}\b")),
    ("ohip-labeled-digits", re.compile(r"(?i)\bohip\b.{0,24}\d{10}\b")),
    (
        "chart-id-assigned",
        re.compile(r"(?i)\bchart[_ -]?(?:id|no|num|number)\s*[:=#]\s*[A-Za-z0-9]"),
    ),
    ("chart-hash", re.compile(r"(?i)\bchart\s*#\s*[A-Za-z0-9]")),
    ("mrn-assigned", re.compile(r"(?i)\bmrn\s*[:=#]\s*[A-Za-z0-9]")),
    ("unc-path", re.compile(r"\\\\[A-Za-z0-9._-]+\\[A-Za-z0-9]")),
    (
        "rdp-full-address",
        re.compile(
            r"(?i)(?:full address|alternate full address|gatewayhostname)\s*:\s*s\s*:"
        ),
    ),
    (
        "rdp-env-host",
        re.compile(r"(?i)\b(?:RDP_HOST|RDP_HOSTNAME|RDP_SERVER|MSTSC_HOST)\s*="),
    ),
    ("mstsc-host", re.compile(r"(?i)mstsc(?:\.exe)?\s+/v:")),
]

FORBIDDEN_SUFFIXES = {
    ".rdp",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".mdb",
    ".accdb",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".wav",
    ".mp3",
    ".m4a",
}

FORBIDDEN_DIR_PARTS = {
    "recordings",
    "captures",
    "screenshots",
    "retinology",
    ".private",
}

SKIP_DIR_NAMES = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}


def _git_files(root: Path) -> list[Path] | None:
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    names = [n for n in out.stdout.split(b"\0") if n]
    return [root / n.decode("utf-8", "surrogateescape") for n in names]


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for name in filenames:
            files.append(Path(dirpath) / name)
    return files


def iter_scan_files(root: Path) -> list[Path]:
    tracked = _git_files(root)
    if tracked is not None:
        return [p for p in tracked if p.is_file()]
    return [p for p in _walk_files(root) if p.is_file()]


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return True
    return b"\0" in chunk


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def scan_forbidden_paths(root: Path, files: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in files:
        rel = _rel(root, path)
        parts = Path(rel).parts
        lower_parts = {p.lower() for p in parts}
        suffix = Path(rel).suffix.lower()
        if suffix in FORBIDDEN_SUFFIXES:
            hits.append(f"forbidden-suffix\t{rel}")
        if lower_parts & FORBIDDEN_DIR_PARTS:
            hits.append(f"forbidden-dir\t{rel}")
    return hits


def scan_contents(root: Path, files: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in files:
        if _is_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            hits.append(f"unreadable\t{_rel(root, path)}\t{exc}")
            continue
        for rule_id, pattern in RULES:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                hits.append(f"{rule_id}\t{_rel(root, path)}:{line_no}")
                break
    return hits


def scan_tree(root: Path) -> list[str]:
    """Return the same hit lines the clinic git scanner printed."""
    files = iter_scan_files(root)
    return scan_forbidden_paths(root, files) + scan_contents(root, files)


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IRUSR)


def plant_fixtures(tmp: Path) -> dict[str, Path]:
    """Build matching fixtures without storing those strings in this file."""
    four = "9" * 4
    three = "9" * 3
    ten = "9" * 10
    ohip_dash = f"{four}-{three}-{three}"
    ohip_space = f"{four} {three} {three}"
    ohip_dot = f"{four}.{three}.{three}"
    planted = {
        "ohip-dashed": tmp / "ohip-dashed.txt",
        "ohip-spaced": tmp / "ohip-spaced.txt",
        "ohip-dotted": tmp / "ohip-dotted.txt",
        "ohip-labeled-digits": tmp / "ohip-labeled.txt",
        "chart-id-assigned": tmp / "chart-id.txt",
        "chart-hash": tmp / "chart-hash.txt",
        "mrn-assigned": tmp / "mrn.txt",
        "unc-path": tmp / "unc.txt",
        "rdp-full-address": tmp / "rdp-address.txt",
        "rdp-env-host": tmp / "rdp-env.txt",
        "mstsc-host": tmp / "mstsc.txt",
        "rdp-file": tmp / "session.rdp",
    }
    # Concatenate so this source file itself does not match the rules.
    _write(planted["ohip-dashed"], ohip_dash + "\n")
    _write(planted["ohip-spaced"], ohip_space + "\n")
    _write(planted["ohip-dotted"], ohip_dot + "\n")
    _write(planted["ohip-labeled-digits"], "OHIP " + ten + "\n")
    _write(planted["chart-id-assigned"], "chart" + "_id: Z9\n")
    _write(planted["chart-hash"], "chart " + "#Z9\n")
    _write(planted["mrn-assigned"], "mr" + "n: Z9\n")
    host = "testhost"
    share = "share"
    _write(planted["unc-path"], "\\\\" + host + "\\" + share + "\n")
    _write(planted["rdp-full-address"], "full address" + ":s:" + host + "\n")
    _write(planted["rdp-env-host"], "RDP_" + "HOST=" + host + "\n")
    _write(planted["mstsc-host"], "mstsc " + "/v:" + host + "\n")
    _write(planted["rdp-file"], "full address" + ":s:" + host + "\n")
    return planted


def self_test() -> int:
    """Plant fixtures in a temp dir. Return 1 if any rule stays silent."""
    with tempfile.TemporaryDirectory(prefix="openadapt-privacy-scan-selftest-") as raw:
        tmp = Path(raw)
        planted = plant_fixtures(tmp)
        files = [p for p in planted.values() if p.is_file()]
        content_hits = scan_contents(tmp, files)
        path_hits = scan_forbidden_paths(tmp, files)
        fired = {h.split("\t", 1)[0] for h in content_hits}
        if any(h.startswith("forbidden-suffix\t") and h.endswith(".rdp") for h in path_hits):
            fired.add("rdp-file")
        required = set(planted)
        missing = sorted(required - fired)
        if missing:
            print("self-test miss (scanner is blind):", ", ".join(missing), file=sys.stderr)
            print("hits:", *content_hits, *path_hits, sep="\n", file=sys.stderr)
            return 1
        print("self-test ok:", ", ".join(sorted(required)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="plant fixtures in a temp dir and require every rule to fire",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="tree to scan (default: current working directory)",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    root = (args.root if args.root is not None else Path.cwd()).resolve()
    hits = scan_tree(root)
    if hits:
        print("PHI / clinic-path scan failed:", file=sys.stderr)
        for hit in hits:
            print(hit, file=sys.stderr)
        return 1
    print(f"PHI scan clean ({len(iter_scan_files(root))} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
