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

With ``--active-lock`` as well (what Desktop's launcher passes), the marker
must also be exclusively locked by another process (Desktop holds the lock for
the whole recording). Releasing a lock cannot fail and also happens when
Desktop exits, so a running host refuses the moment the recording stops, even
when every file cleanup step fails and before the lease runs out. Each write
holds a shared lock on the ``<marker>.gate`` file from the check until the
evidence is persisted, and Desktop takes that gate exclusively before its stop
returns, so no write that was authorized before the stop lands after it.
Persistence never creates the recording folder.

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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX; --active-lock then refuses
    fcntl = None  # type: ignore[assignment]

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
GATE_SUFFIX = ".gate"
# How long one write waits for the gate while Desktop is draining it on stop.
GATE_WAIT_SECONDS = 2.0


def gate_path(active_file: Path) -> Path:
    return active_file.with_name(active_file.name + GATE_SUFFIX)


def locked_elsewhere(fd: int) -> bool:
    """Whether another open file holds an exclusive lock on ``fd``'s file.

    True only on a definite "would block". No lock support, or any other
    error, counts as not locked, so the caller refuses.
    """

    if fcntl is None:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


@contextmanager
def write_gate(active_file: Path, wait: float = GATE_WAIT_SECONDS) -> Iterator[None]:
    """Hold the recording's write gate (shared) for one check-and-persist.

    Raises OSError when the gate is missing, locking is unsupported, or the
    gate stays exclusively held (Desktop is stopping) past ``wait``.
    """

    if fcntl is None:
        raise OSError("file locking is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(gate_path(active_file), flags | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("write gate is not a regular file")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def recording_active(
    active_file: Path,
    recording: Path,
    lease: float | None = None,
    *,
    now: float | None = None,
    require_lock: bool = False,
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
            if require_lock:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    return False
                if not locked_elsewhere(handle.fileno()):
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
    parser.add_argument(
        "--active-lock",
        action="store_true",
        default=os.environ.get("OPENADAPT_EXTENSION_ACTIVE_LOCK", "") == "1",
        help="Also require Desktop's exclusive lock on the active file, and a write gate",
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
    if args.active_lock and (not args.active_file or fcntl is None):
        # Fail closed: a lock we can't check must not silently vanish.
        write_message({"ok": False, "error": "bad_active_lock"})
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
        if active_file is None:
            buf.append(evidence)
            buf.write(recording)
            write_message({"ok": True, "count": buf.count})
            continue
        if persist_while_active(
            buf, evidence, recording, active_file, lease, require_lock=args.active_lock
        ):
            write_message({"ok": True, "count": buf.count})
        else:
            # Fail closed: the recording that registered this host has ended.
            write_message({"ok": False, "error": "not_recording"})
    return 0


def _check_and_persist(
    buf: ExtensionEvidenceBuffer,
    evidence: Mapping[str, Any],
    recording: Path,
    active_file: Path,
    lease: float | None,
    require_lock: bool,
) -> bool:
    if not recording_active(active_file, recording, lease, require_lock=require_lock):
        return False
    kept = buf.count
    buf.append(evidence)
    try:
        # Never create the folder: a recording deleted after the check stays gone.
        buf.write(recording, create_dir=False)
    except OSError:
        buf.truncate(kept)
        return False
    return True


def persist_while_active(
    buf: ExtensionEvidenceBuffer,
    evidence: Mapping[str, Any],
    recording: Path,
    active_file: Path,
    lease: float | None,
    *,
    require_lock: bool = False,
) -> bool:
    """Persist one evidence line only while the recording is active.

    With ``require_lock`` the check and the write happen under the shared write
    gate, so Desktop's stop (which takes the gate exclusively) cannot return
    while an authorized write is still in flight.
    """

    if not require_lock:
        return _check_and_persist(buf, evidence, recording, active_file, lease, False)
    try:
        with write_gate(active_file):
            return _check_and_persist(
                buf, evidence, recording, active_file, lease, True
            )
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
