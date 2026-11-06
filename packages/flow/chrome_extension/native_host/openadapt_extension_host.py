#!/usr/bin/env python3
"""Native messaging host stub for the optional OpenAdapt Chrome extension.

Chrome talks to this process over stdin/stdout (length-prefixed JSON). Each
EXTENSION_EVIDENCE message is appended to the recording directory named in
OPENADAPT_EXTENSION_RECORDING (or --recording). No remote calls.

With ``--active-file`` (or OPENADAPT_EXTENSION_ACTIVE_FILE), the host writes
only while that file exists and the recording directory already exists.
OpenAdapt Desktop creates the file when a recording starts and removes it when
the recording stops, so a host Chrome keeps running after the stop writes
nothing more and never recreates a deleted recording folder.

This is the unpack-first host for local development. A store listing and
installer come later (OpenAdapt#1872).
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from pathlib import Path

# Allow running from a source checkout without install.
_FLOW_ROOT = Path(__file__).resolve().parents[2]
if str(_FLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(_FLOW_ROOT))

from openadapt_flow.extension_evidence import (  # noqa: E402
    ExtensionEvidenceBuffer,
    validate_native_message,
)


def read_message() -> dict | None:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len:
        return None
    (length,) = struct.unpack("<I", raw_len)
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        return None
    return json.loads(payload.decode("utf-8"))


def write_message(message: dict) -> None:
    data = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recording",
        default=os.environ.get("OPENADAPT_EXTENSION_RECORDING", ""),
        help="Recording directory that receives extension_evidence.jsonl",
    )
    parser.add_argument(
        "--active-file",
        default=os.environ.get("OPENADAPT_EXTENSION_ACTIVE_FILE", ""),
        help="Write only while this file exists (set by OpenAdapt Desktop)",
    )
    args, _unknown = parser.parse_known_args(argv)
    if not args.recording:
        # Fail closed: without a recording dir we acknowledge nothing useful.
        write_message({"ok": False, "error": "no_recording_dir"})
        return 2

    recording = Path(args.recording)
    active_file = Path(args.active_file) if args.active_file else None
    buf = ExtensionEvidenceBuffer()
    # Reload any existing lines so reconnects append.
    existing = recording / "extension_evidence.jsonl"
    if existing.is_file():
        from openadapt_flow.extension_evidence import load_extension_evidence

        for line in load_extension_evidence(recording):
            buf.append(line)

    while True:
        msg = read_message()
        if msg is None:
            break
        try:
            evidence = validate_native_message(msg)
        except ValueError as exc:
            write_message({"ok": False, "error": str(exc)})
            continue
        if active_file is not None and not (
            active_file.is_file() and recording.is_dir()
        ):
            # Fail closed: the recording that registered this host has ended.
            write_message({"ok": False, "error": "not_recording"})
            continue
        buf.append(evidence)
        buf.write(recording)
        write_message({"ok": True, "count": buf.count})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
