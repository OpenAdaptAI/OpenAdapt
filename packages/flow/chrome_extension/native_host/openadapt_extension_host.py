#!/usr/bin/env python3
"""Native messaging host stub for the optional OpenAdapt Chrome extension.

Chrome talks to this process over stdin/stdout (length-prefixed JSON). Each
EXTENSION_EVIDENCE message is appended to the recording directory named in
OPENADAPT_EXTENSION_RECORDING (or --recording). No remote calls.

With ``--active-file`` (or OPENADAPT_EXTENSION_ACTIVE_FILE), the host writes
only while that file is a regular file holding exactly ``active\n`` and the
recording directory already exists. OpenAdapt Desktop creates the file when a
recording starts and removes it (or overwrites it with ``revoked\n``) when the
recording stops, so a host Chrome keeps running after the stop writes nothing
more and never recreates a deleted recording folder.

With ``--active-lease SECONDS`` as well, the file must also have been touched
within that many seconds. Desktop touches it while the recording runs, so a
host keeps refusing even when Desktop's stop-time cleanup could not remove the
file: the lease simply runs out.

This is the unpack-first host for local development. A store listing and
installer come later (OpenAdapt#1872).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import struct
import sys
import time
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


ACTIVE_MARKER = b"active\n"


def recording_active(
    active_file: Path,
    recording: Path,
    lease: float | None = None,
    *,
    now: float | None = None,
) -> bool:
    """Whether Desktop still holds the recording open. Fails closed."""

    try:
        if not recording.is_dir():
            return False
        info = active_file.stat()
        if not stat.S_ISREG(info.st_mode):
            return False
        with active_file.open("rb") as handle:
            if handle.read(len(ACTIVE_MARKER) + 1) != ACTIVE_MARKER:
                return False
    except OSError:
        return False
    if lease is not None:
        current = time.time() if now is None else now
        if not abs(current - info.st_mtime) <= lease:
            return False
    return True


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
        help="Write only while this file holds 'active' (set by OpenAdapt Desktop)",
    )
    parser.add_argument(
        "--active-lease",
        default=os.environ.get("OPENADAPT_EXTENSION_ACTIVE_LEASE", ""),
        help="Also require the active file to be touched within this many seconds",
    )
    args, _unknown = parser.parse_known_args(argv)
    if not args.recording:
        # Fail closed: without a recording dir we acknowledge nothing useful.
        write_message({"ok": False, "error": "no_recording_dir"})
        return 2
    lease: float | None = None
    if args.active_lease:
        try:
            lease = float(args.active_lease)
        except ValueError:
            lease = math.nan
        if not args.active_file or not math.isfinite(lease) or lease <= 0:
            # Fail closed: a lease we can't enforce must not silently vanish.
            write_message({"ok": False, "error": "bad_active_lease"})
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
        if active_file is not None and not recording_active(
            active_file, recording, lease
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
