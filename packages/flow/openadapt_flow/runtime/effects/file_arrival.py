"""File / SFTP arrival system-of-record :class:`EffectVerifier`.

Many consequential workflows end in a FILE LANDING somewhere -- an HL7/CSV
export dropped into an interface folder, a claim batch on an SFTP endpoint, a
generated report in an outbox. The screen saying "Export complete" proves
nothing; the truth is whether a file matching the contract actually ARRIVED:
right name pattern, non-trivial size, fresh mtime, and (optionally) the right
content.

Unlike :class:`~openadapt_flow.runtime.effects.document_hash.DocumentHashVerifier`
(exact content identity by SHA-256 -- use it when you know the bytes), this
verifier checks ARRIVAL: each candidate file flattens to a record carrying
``size_ok`` / ``fresh`` / ``content_match`` and their conjunction ``arrived``,
so the standard contract is::

    Effect(kind=RECORD_WRITTEN, match={"arrived": "True"},
           expected_count=1, count_new_only=True)

-- "exactly one NEW conforming file appeared because of this action" (the
``count_new_only`` duplicate-write guard catches a double export writing
``report (1).csv``; keep ``count_new_only=False`` and match on ``name``
instead when the workflow overwrites a fixed filename in place).

Local directories are first-class. A REMOTE endpoint (SFTP) is supported by
injecting a ``transport`` whose surface is duck-typed to paramiko's
``SFTPClient`` (``listdir_attr`` + ``open``) -- the kit adds NO paramiko
dependency, and the SFTP path is contract-proven against a fake transport in
CI, not live-proven against a real SFTP server.

Fail-safe: a missing root, a transport error, or an unreadable candidate file
reads as *unreadable* -> INDETERMINATE -> HALT (the interface folder being
gone is never "no file expected").
"""

from __future__ import annotations

import fnmatch
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)

#: How many bytes of a candidate file the optional content probe reads. An
#: arrival probe is a sanity check ("the batch header is present"), not a
#: full-content hash -- use DocumentHashVerifier for exact content identity.
CONTENT_PROBE_LIMIT = 1 << 20  # 1 MiB


@runtime_checkable
class ArrivalTransport(Protocol):
    """Duck-typed remote listing/reading surface (paramiko ``SFTPClient``).

    ``listdir_attr(path)`` returns entries with ``filename`` / ``st_size`` /
    ``st_mtime`` attributes; ``open(path, mode)`` returns a file-like whose
    ``read(n)`` yields bytes. A real ``paramiko.SFTPClient`` satisfies this
    as-is; tests use an in-memory fake.
    """

    def listdir_attr(self, path: str) -> list[Any]: ...

    def open(self, path: str, mode: str = "rb") -> Any: ...


class FileArrivalVerifier:
    """Verify that a conforming file ARRIVED in a directory or SFTP endpoint.

    Args:
        root: The watched directory (local path, or the remote directory when
            ``transport`` is given).
        pattern: Filename glob selecting candidate records (local mode matches
            relative paths recursively via ``Path.glob``; transport mode
            matches entry filenames via ``fnmatch``, non-recursive).
        min_size: Minimum byte size for ``size_ok`` (default 1 -- a
            zero-byte "arrival" is a failed/truncated transfer, not a record).
        mtime_window_s: When set, ``fresh`` is True only for files modified
            within this many seconds before the read; ``None`` marks every
            file fresh (rely on ``count_new_only`` for newness instead).
        content_probe: Optional regular expression; ``content_match`` is True
            when it matches within the first :data:`CONTENT_PROBE_LIMIT`
            bytes (decoded UTF-8, errors replaced). ``None`` -> always True.
        transport: Optional :class:`ArrivalTransport` (e.g. a paramiko
            ``SFTPClient``) for a remote endpoint; ``None`` -> local
            filesystem.
    """

    substrate = "file"

    def __init__(
        self,
        root: Path | str,
        *,
        pattern: str = "*",
        min_size: int = 1,
        mtime_window_s: Optional[float] = None,
        content_probe: Optional[str] = None,
        transport: Optional[ArrivalTransport] = None,
    ) -> None:
        self.root = str(root)
        self.pattern = pattern
        self.min_size = int(min_size)
        self.mtime_window_s = mtime_window_s
        self.content_probe = (
            re.compile(content_probe) if content_probe is not None else None
        )
        self.transport = transport

    # -- listing ------------------------------------------------------------

    def _probe_content(self, read_bytes: bytes) -> bool:
        assert self.content_probe is not None
        text = read_bytes.decode("utf-8", errors="replace")
        return self.content_probe.search(text) is not None

    def _scan_local(self) -> Optional[list[dict[str, Any]]]:
        root = Path(self.root)
        if not root.is_dir():
            return None
        try:
            paths = sorted(p for p in root.glob(self.pattern) if p.is_file())
        except OSError:
            return None
        now = time.time()
        records: list[dict[str, Any]] = []
        for p in paths:
            try:
                stat = p.stat()
                content_match = True
                if self.content_probe is not None:
                    with p.open("rb") as fh:
                        content_match = self._probe_content(
                            fh.read(CONTENT_PROBE_LIMIT)
                        )
            except OSError:
                return None  # a candidate we cannot read -> unreadable SoR
            records.append(
                self._record(
                    rel=str(p.relative_to(root)),
                    name=p.name,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    now=now,
                    content_match=content_match,
                )
            )
        return records

    def _scan_transport(self) -> Optional[list[dict[str, Any]]]:
        assert self.transport is not None
        try:
            entries = self.transport.listdir_attr(self.root)
        except Exception:  # noqa: BLE001 - remote listing failure is unreadable
            return None
        now = time.time()
        records: list[dict[str, Any]] = []
        for entry in sorted(entries, key=lambda e: str(getattr(e, "filename", ""))):
            name = str(getattr(entry, "filename", ""))
            if not name or not fnmatch.fnmatch(name, self.pattern):
                continue
            try:
                size = int(getattr(entry, "st_size"))
                mtime = float(getattr(entry, "st_mtime"))
            except (AttributeError, TypeError, ValueError):
                return None  # a listing without usable stat data is unreadable
            content_match = True
            if self.content_probe is not None:
                remote_path = f"{self.root.rstrip('/')}/{name}"
                try:
                    fh = self.transport.open(remote_path, "rb")
                    try:
                        content_match = self._probe_content(
                            fh.read(CONTENT_PROBE_LIMIT)
                        )
                    finally:
                        fh.close()
                except Exception:  # noqa: BLE001 - unreadable candidate
                    return None
            records.append(
                self._record(
                    rel=name,
                    name=name,
                    size=size,
                    mtime=mtime,
                    now=now,
                    content_match=content_match,
                )
            )
        return records

    def _record(
        self,
        *,
        rel: str,
        name: str,
        size: int,
        mtime: float,
        now: float,
        content_match: bool,
    ) -> dict[str, Any]:
        size_ok = size >= self.min_size
        fresh = (
            True
            if self.mtime_window_s is None
            else (now - mtime) <= self.mtime_window_s
        )
        return {
            # Identity includes size+mtime so an in-place rewrite counts as a
            # NEW record under count_new_only (the arrival is the event). A
            # flat string, so it stays hashable across JSON round-trips.
            "id": f"{rel}@{int(size)}@{float(mtime):.3f}",
            "name": name,
            "size": size,
            "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "size_ok": size_ok,
            "fresh": fresh,
            "content_match": content_match,
            "arrived": size_ok and fresh and content_match,
        }

    def _scan(self) -> Optional[list[dict[str, Any]]]:
        if self.transport is not None:
            return self._scan_transport()
        return self._scan_local()

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._scan()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={
                "root": self.root,
                "pattern": self.pattern,
                "remote": self.transport is not None,
            },
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        while True:
            current = self._scan()
            last = judge_records(expected, before, current, substrate=self.substrate)
            if last.confirmed or time.monotonic() >= deadline:
                return last
            time.sleep(0.2)
