"""Observe Windows CRT modes and physical bytes before and after os.fdopen."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

PAYLOAD = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3 + b"\r\nLF\nCtrlZ\x1aEND\r\n"
TAIL = b"append\r\n\x1aafter-eof\n"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        assert written > 0
        view = view[written:]


def read_all(fd: int) -> bytes:
    parts = []
    while part := os.read(fd, 65536):
        parts.append(part)
    return b"".join(parts)


def physical_bytes(path: Path) -> bytes:
    """Read physical bytes through raw O_BINARY; no fdopen or text reader."""
    fd = os.open(path, os.O_RDONLY | os.O_BINARY)
    try:
        return read_all(fd)
    finally:
        os.close(fd)


def prepare_binary(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
    try:
        write_all(fd, data)
    finally:
        os.close(fd)
    assert physical_bytes(path) == data


def descriptor_mode(fd: int) -> int:
    """Observe the CRT mode, restoring it before any input or output."""
    import msvcrt

    previous = msvcrt.setmode(fd, os.O_BINARY)
    msvcrt.setmode(fd, previous)
    return previous


def record(report: dict, name: str, actual: bytes, expected: bytes, **details) -> None:
    row = {
        "case": name,
        "bytes": len(actual),
        "sha256": digest(actual),
        "expected_bytes": len(expected),
        "expected_sha256": digest(expected),
        "matches_expected": actual == expected,
        **details,
    }
    report["cases"].append(row)
    assert actual == expected, json.dumps(row)


def run(report: dict, root: Path) -> None:
    assert os.name == "nt", "This diagnostic must run on native Windows."
    expanded = PAYLOAD.replace(b"\n", b"\r\n")

    for label, flags in (("default", 0), ("explicit_text", os.O_TEXT)):
        path = root / f"raw-{label}.bin"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600)
        try:
            before = descriptor_mode(fd)
            write_all(fd, PAYLOAD)
        finally:
            os.close(fd)
        physical = physical_bytes(path)
        expected = expanded if before == os.O_TEXT else PAYLOAD
        record(
            report,
            f"raw_{label}_write_control",
            physical,
            expected,
            descriptor_mode=before,
            matches_original=physical == PAYLOAD,
            expected_text_expansion=before == os.O_TEXT,
        )
        if label == "explicit_text":
            assert before == os.O_TEXT and physical != PAYLOAD

    path = root / "raw-explicit-text-read.bin"
    prepare_binary(path, PAYLOAD)
    fd = os.open(path, os.O_RDONLY | os.O_TEXT)
    try:
        before = descriptor_mode(fd)
        translated = read_all(fd)
    finally:
        os.close(fd)
    record(
        report,
        "raw_explicit_text_read_control",
        translated,
        PAYLOAD.split(b"\x1a", 1)[0].replace(b"\r\n", b"\n"),
        descriptor_mode=before,
        matches_original=translated == PAYLOAD,
    )
    assert before == os.O_TEXT and translated != PAYLOAD
    assert physical_bytes(path) == PAYLOAD

    for label, flags in (("default", 0), ("explicit_text", os.O_TEXT)):
        path = root / f"fdopen-wb-{label}.bin"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600)
        before = descriptor_mode(fd)
        with os.fdopen(fd, "wb") as handle:
            after = descriptor_mode(handle.fileno())
            assert handle.write(PAYLOAD) == len(PAYLOAD)
        record(
            report,
            f"fdopen_wb_{label}",
            physical_bytes(path),
            PAYLOAD,
            mode_before=before,
            mode_after=after,
        )
        assert after == os.O_BINARY

        path = root / f"fdopen-rb-{label}.bin"
        prepare_binary(path, PAYLOAD)
        fd = os.open(path, os.O_RDONLY | flags)
        before = descriptor_mode(fd)
        with os.fdopen(fd, "rb") as handle:
            after = descriptor_mode(handle.fileno())
            observed = handle.read()
        record(
            report,
            f"fdopen_rb_{label}",
            observed,
            PAYLOAD,
            mode_before=before,
            mode_after=after,
            physical_unchanged=physical_bytes(path) == PAYLOAD,
        )
        assert after == os.O_BINARY and physical_bytes(path) == PAYLOAD

        path = root / f"fdopen-rplusb-{label}.bin"
        prepare_binary(path, PAYLOAD)
        fd = os.open(path, os.O_RDWR | flags)
        before = descriptor_mode(fd)
        with os.fdopen(fd, "r+b") as handle:
            after = descriptor_mode(handle.fileno())
            observed = handle.read()
            assert observed == PAYLOAD
            handle.seek(0, os.SEEK_END)
            assert handle.write(TAIL) == len(TAIL)
        record(
            report,
            f"fdopen_rplusb_{label}",
            physical_bytes(path),
            PAYLOAD + TAIL,
            mode_before=before,
            mode_after=after,
            read_sha256=digest(observed),
        )
        assert after == os.O_BINARY
        if label == "explicit_text":
            assert before == os.O_TEXT

    # The raw writer below bypasses FileIO.write entirely. If wrapping the
    # descriptor did not change the CRT mode, its physical LF bytes expand.
    path = root / "fdopen-then-raw-write.bin"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TEXT, 0o600)
    before = descriptor_mode(fd)
    with os.fdopen(fd, "wb", buffering=0) as handle:
        after = descriptor_mode(handle.fileno())
        write_all(handle.fileno(), PAYLOAD)
    record(
        report,
        "fdopen_unbuffered_then_raw_os_write",
        physical_bytes(path),
        PAYLOAD,
        mode_before=before,
        mode_after=after,
    )
    assert before == os.O_TEXT and after == os.O_BINARY


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "python": sys.version,
        "python_executable": sys.executable,
        "implementation": sys.implementation.name,
        "platform": platform.platform(),
        "os_name": os.name,
        "source_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "script_sha256": digest(Path(__file__).read_bytes()),
        "github_sha": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "payload_sha256": digest(PAYLOAD),
        "payload_hex": PAYLOAD.hex(),
        "tail_hex": TAIL.hex(),
        "cases": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="fdopen-byte-diagnostic-") as temporary:
            run(report, Path(temporary))
        report["status"] = "passed"
    except Exception:
        report["status"] = "failed"
        report["failure"] = traceback.format_exc()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
