"""Run-bound local operator decisions; accepted messages are not yet applied effects."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.form_tx.types import HumanResolutionInput


class CopilotRunBinding(BaseModel):
    """Capability for one published halt generation in a locally owned run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=160)
    session_nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation: int = Field(ge=1)
    pack_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    dossier_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    operator_id: str = Field(min_length=1, max_length=160)
    expires_at_unix: int = Field(gt=0)


class CopilotBoundDecision(BaseModel):
    """Decision carrying the exact state the person reviewed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: CopilotRunBinding
    decision: HumanResolutionInput


def private_session_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
        raise ValueError("copilot session must be a private directory")
    if os.name == "posix":
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(
                "copilot session directory must be owned by you with mode 0700"
            )


class BoundDecisionStore:
    """Cross-process SQLite serialization and crash-safe decision claiming.

    A decision is claimed before applying it. A crash can require human
    reconciliation, but cannot silently reapply an already claimed decision.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "copilot_bound_decisions.sqlite3"

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        private_session_directory(self.directory)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("copilot decision store must be a regular file")
            if os.name == "posix" and (
                info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValueError("copilot decision store requires owner-only access")
        finally:
            os.close(fd)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS decisions ("
                "id INTEGER PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, "
                "payload TEXT NOT NULL, claimed INTEGER NOT NULL DEFAULT 0)"
            )
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append(
        self,
        item: CopilotBoundDecision,
        validate_current: Callable[[CopilotBoundDecision], None],
    ) -> tuple[bool, int]:
        payload = item.model_dump_json()
        if len(payload.encode("utf-8")) > 32768:
            raise ValueError("copilot decision exceeds size limit")
        with self.transaction() as connection:
            validate_current(item)
            previous = connection.execute(
                "SELECT payload FROM decisions WHERE idempotency_key = ?",
                (item.decision.idempotency_key,),
            ).fetchone()
            if previous is not None:
                if previous[0] != payload:
                    raise ValueError("idempotency key reused for a different decision")
                accepted = False
            else:
                connection.execute(
                    "INSERT INTO decisions(idempotency_key,payload) VALUES (?,?)",
                    (item.decision.idempotency_key, payload),
                )
                accepted = True
            count = connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            return accepted, int(count)

    def claim(
        self, binding: CopilotRunBinding, validate_binding: Callable[[], None]
    ) -> Optional[CopilotBoundDecision]:
        with self.transaction() as connection:
            validate_binding()
            for row_id, payload in connection.execute(
                "SELECT id,payload FROM decisions WHERE claimed=0 ORDER BY id"
            ).fetchall():
                item = CopilotBoundDecision.model_validate_json(payload)
                connection.execute(
                    "UPDATE decisions SET claimed=1 WHERE id=?", (row_id,)
                )
                if item.binding == binding:
                    return item
            return None


__all__ = ["CopilotRunBinding", "CopilotBoundDecision", "BoundDecisionStore"]
