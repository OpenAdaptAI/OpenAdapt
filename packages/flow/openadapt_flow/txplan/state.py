"""Durable state for plans: the idempotency ledger, approvals and the holdback queue.

Everything here lives in a state folder outside the agent's workspace
(``~/.openadapt/tx`` by default, ``OPENADAPT_TX_STATE`` to move it). A
checkpoint restore of the workspace, a ``git reset`` or a rewound agent
therefore can't roll the ledger back: that is what makes a replay visible.

**Idempotency key.** Each write's key is an HMAC, under a secret kept in the
state folder, of the task, the system, the collection, the operation and the
record *slot*: the record's id for an update or delete, and the ordinal of the
create (or send) within the task for a new record. None of those come from the
payload, so a model that regenerates the payload after a crash or a rewind
gets the same key. The ledger stores the payload digest and the plan digest
under the key, and a second write under the key with a different payload is
refused (``replay_mismatch``). The same payload is a replay: the stored result
is returned and nothing is sent. The plan digest is bound, not keyed: keying on
it would let a re-plan mint a fresh key.

**Approvals** bind to one plan digest and are single-use: apply consumes one
before its first send, so a consumed approval can't authorize a second apply.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

STATE_ENV = "OPENADAPT_TX_STATE"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def now() -> float:
    return time.time()


def iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def write_private(path: Path, payload: Any) -> None:
    """Write JSON owner-only, atomically."""
    _private_dir(path.parent)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(
            fd,
            (
                json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
            ).encode(),
        )
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class StateError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Entry:
    """What the ledger knows about one key."""

    key: str
    status: str  # begun | applied | uncertain | held | released | cancelled | undo_begun | undone
    plan: str
    payload: str
    events: list[dict[str, Any]]

    @property
    def result(self) -> dict[str, Any]:
        for event in reversed(self.events):
            if event.get("result"):
                return dict(event["result"])
        return {}


class State:
    """One state folder."""

    def __init__(self, root: Optional[Path] = None) -> None:
        if root is None:
            env = os.environ.get(STATE_ENV)
            root = Path(env) if env else Path.home() / ".openadapt" / "tx"
        self.root = _private_dir(Path(root).expanduser())
        for sub in ("ledger", "plans", "approvals", "runs", "holdback"):
            _private_dir(self.root / sub)

    # -- secret and keys -----------------------------------------------------

    def secret(self) -> bytes:
        path = self.root / "secret"
        if not path.exists():
            fd = None
            with contextlib.suppress(FileExistsError):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, secrets.token_hex(32).encode())
                os.fsync(fd)
            if fd is not None:
                os.close(fd)
        return bytes.fromhex(path.read_text().strip())

    def key_for(
        self, *, task: str, system: str, collection: str, op: str, slot: str
    ) -> str:
        body = canonical(
            {
                "task": task,
                "system": system,
                "collection": collection,
                "op": op,
                "slot": slot,
            }
        )
        return "ik-" + hmac.new(self.secret(), body, hashlib.sha256).hexdigest()[:40]

    def tag(self, text: str) -> str:
        """A keyed tag for a row id or a task: comparable, never the value."""
        return hmac.new(
            self.secret(), text.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:20]

    # -- the lock ------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """One applier per state folder at a time (an OS lock the kernel drops
        when the process dies)."""
        fd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":  # pragma: no cover - Windows
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing drops the lock

    # -- the ledger ----------------------------------------------------------

    def _ledger_path(self, key: str) -> Path:
        if not key.startswith("ik-") or not key[3:].isalnum():
            raise StateError("bad_key", key)
        return self.root / "ledger" / f"{key}.jsonl"

    def record(self, key: str, event: str, **fields: Any) -> None:
        """Append one event, durably, before the caller acts on it."""
        line = json.dumps(
            {"event": event, "at": now(), **fields}, sort_keys=True, default=str
        )
        fd = os.open(
            self._ledger_path(key), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def entry(self, key: str) -> Optional[Entry]:
        path = self._ledger_path(key)
        if not path.exists():
            return None
        events = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        if not events:
            return None
        begun = next((e for e in reversed(events) if e["event"] == "begun"), events[0])
        return Entry(
            key=key,
            status=events[-1]["event"],
            plan=str(begun.get("plan", "")),
            payload=str(begun.get("payload", "")),
            events=events,
        )

    def entries(self) -> list[Entry]:
        out = []
        for path in sorted((self.root / "ledger").glob("ik-*.jsonl")):
            found = self.entry(path.stem)
            if found is not None:
                out.append(found)
        return out

    # -- plans ---------------------------------------------------------------

    def plan_dir(self, digest: str) -> Path:
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise StateError("bad_digest", digest)
        return self.root / "plans" / digest

    def find_plan(self, prefix: str) -> str:
        prefix = prefix.removeprefix("pl-")
        hits = [
            p.name for p in (self.root / "plans").iterdir() if p.name.startswith(prefix)
        ]
        if len(hits) != 1:
            raise StateError("plan_not_found" if not hits else "plan_ambiguous", prefix)
        return hits[0]

    # -- approvals -----------------------------------------------------------

    def approve(self, digest: str, *, approver: str, decided_by: str) -> dict[str, Any]:
        path = self.root / "approvals" / f"{digest}.json"
        if (
            path.exists()
            or (self.root / "approvals" / f"{digest}.consumed.json").exists()
        ):
            raise StateError("already_approved", digest[:12])
        record = {
            "plan": digest,
            "approver_id": approver,
            "decided_by": decided_by,
            "approved_at": iso(now()),
            "nonce": secrets.token_hex(8),
        }
        write_private(path, record)
        return record

    def consume_approval(self, digest: str) -> dict[str, Any]:
        """Take the plan's approval, once. A second apply finds none."""
        path = self.root / "approvals" / f"{digest}.json"
        used = self.root / "approvals" / f"{digest}.consumed.json"
        if used.exists():
            raise StateError("approval_consumed", digest[:12])
        if not path.exists():
            raise StateError("not_approved", digest[:12])
        try:
            os.link(path, used)  # atomic: only one caller wins
        except FileExistsError as exc:
            raise StateError("approval_consumed", digest[:12]) from exc
        record = read_json(path)
        path.unlink()
        return dict(record)

    def approval_record(self, digest: str) -> Optional[dict[str, Any]]:
        for name in (f"{digest}.json", f"{digest}.consumed.json"):
            path = self.root / "approvals" / name
            if path.exists():
                return dict(read_json(path))
        return None

    # -- holdback ------------------------------------------------------------

    def hold(self, key: str, item: Mapping[str, Any]) -> None:
        write_private(self.root / "holdback" / f"{key}.json", dict(item))

    def held(self) -> list[dict[str, Any]]:
        return [
            read_json(p) for p in sorted((self.root / "holdback").glob("ik-*.json"))
        ]

    def unhold(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.root / "holdback" / f"{key}.json").unlink()
