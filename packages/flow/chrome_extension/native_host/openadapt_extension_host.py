#!/usr/bin/env python3
"""Native messaging host stub for the optional OpenAdapt Chrome extension.

Chrome talks to this process over stdin/stdout (length-prefixed JSON). Each
EXTENSION_EVIDENCE message is appended to the recording directory named in
OPENADAPT_EXTENSION_RECORDING (or --recording). No remote calls.

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
    args, _unknown = parser.parse_known_args(argv)
    if not args.recording:
        # Fail closed: without a recording dir we acknowledge nothing useful.
        write_message({"ok": False, "error": "no_recording_dir"})
        return 2

    recording = Path(args.recording)
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
        buf.append(evidence)
        buf.write(recording)
        write_message({"ok": True, "count": buf.count})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
