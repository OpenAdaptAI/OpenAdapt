"""The in-flight marker of a task's fallback run, and who owns it.

While a fallback run may save, ``<fallback root>/in-flight.json`` exists and
no other fallback of the task starts. The marker names its owner: the
fallback request id, a nonce, the process id and the host. The owner removes
it only when it can prove what the run wrote, and only while it is still its
own. Otherwise the owner marks it ``ended`` and leaves it for a person.

A person releases a marker (``wrap-dispatch release``) only when its owner
has ended: marked ``ended``, or a process on this host that no longer
exists. A live owner, or one on another host that this host can't check,
keeps its marker.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Optional

ACTIVE = "in-flight.json"


def _write(path: Path, data: dict[str, Any], *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def read(root: Path) -> Optional[dict[str, Any]]:
    """The marker, ``{}`` when it is unreadable, None when there is none."""

    path = root / ACTIVE
    if not (path.exists() or path.is_symlink()):
        return None
    try:
        if path.is_symlink():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Marker:
    """One fallback run's ownership of the task's in-flight marker."""

    def __init__(self, root: Path, request_id: str) -> None:
        self.root = root
        self.request_id = request_id
        self.nonce = uuid.uuid4().hex

    @property
    def path(self) -> Path:
        return self.root / ACTIVE

    def claim(self) -> None:
        """Create the marker. Raises FileExistsError when one exists."""

        _write(
            self.path,
            {
                "request_id": self.request_id,
                "nonce": self.nonce,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "at": time.time(),
                "state": "running",
            },
            exclusive=True,
        )

    def owned(self) -> bool:
        data = read(self.root)
        return bool(
            data
            and data.get("nonce") == self.nonce
            and data.get("request_id") == self.request_id
        )

    def clear(self) -> bool:
        """Remove the marker if it is still this run's. False otherwise."""

        if not self.owned():
            return False
        self.path.unlink(missing_ok=True)
        return True

    def end(self) -> bool:
        """Mark the marker's owner ended; the marker stays for a person."""

        data = read(self.root)
        if not (data and data.get("nonce") == self.nonce):
            return False
        data = dict(data, state="ended", ended_at=time.time())
        temporary = self.root / f".{ACTIVE}.{self.nonce}"
        _write(temporary, data, exclusive=False)
        os.replace(temporary, self.path)
        return True


def owner_state(data: Optional[dict[str, Any]]) -> str:
    """``none``, ``ended``, ``running`` or ``unknown`` for a marker.

    ``running`` and ``unknown`` mean a save may still happen.
    """

    from openadapt_flow.agent.runs import pid_alive

    if data is None:
        return "none"
    if not data:
        return "unknown"
    if data.get("state") == "ended":
        return "ended"
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return "unknown"
    if data.get("host") != socket.gethostname():
        return "unknown"
    return "running" if pid_alive(pid) else "ended"


def release(root: Path) -> dict[str, Any]:
    """A person drops a marker whose owner ended. The evidence stays.

    Returns ``{"cleared": bool, "owner": state, "request_id": ...}``.
    """

    data = read(root)
    state = owner_state(data)
    out = {
        "cleared": False,
        "owner": state,
        "request_id": (data or {}).get("request_id"),
    }
    if state != "ended":
        return out
    path = root / ACTIVE
    kept = path.with_name(f"reconciled-{time.time_ns()}.json")
    try:
        # An ended owner never writes again, and no other run claims the
        # marker while it exists, so the file renamed is the one read.
        path.rename(kept)
    except FileNotFoundError:
        return dict(out, owner="none")
    return dict(out, cleared=True, kept_at=str(kept))
