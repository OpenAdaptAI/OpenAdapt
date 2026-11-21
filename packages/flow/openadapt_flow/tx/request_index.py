"""Bind opaque request identity to the existing durable ledger, not encryption keys."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path
from typing import Any

from openadapt_flow.tx.types import salted_sha256
from openadapt_flow.write_ledger import FileLedger, LedgerError

_SCHEMA = "openadapt.tx-request-index/v1"
_REQUEST_SCOPE = "openadapt.tx-authorized-request/v1"
_DIRECTORY_SCOPE = "tx-task-directory/v2"
_INIT_LOCK = hashlib.sha256(b"openadapt.tx-request-index/init/v1").hexdigest()
_MARKER = "tx-request-index.json"


class RequestIndexHeld(ValueError):
    """Request identity cannot be safely recovered from this workdir."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise RequestIndexHeld("duplicate_request_index_binding")
    return result


def has_request_data(workdir: Path) -> bool:
    """Inspect only known request-state metadata; never infer a fresh namespace."""
    try:
        if workdir.is_symlink():
            raise RequestIndexHeld("request_index_state_is_symlink")
        for directory in (
            workdir / "ledger" / "entries",
            workdir / "ledger" / "attempts",
        ):
            if directory.is_symlink():
                raise RequestIndexHeld("request_index_state_is_symlink")
            if directory.exists() and (
                not directory.is_dir() or any(directory.iterdir())
            ):
                return True
        if (workdir / "log" / "leaves").exists():
            return True
        return any(
            any(workdir.glob("*/" + name))
            for name in (
                "tx-receipt.cose",
                "undo_meta.json",
                "undo_r0.json",
                "undo_postcommit.json",
                "undo_proposal.json",
            )
        )
    except OSError as exc:
        raise RequestIndexHeld("request_index_state_unavailable") from exc


def default_request_workdir(state_home: Path) -> Path:
    """Default scope is caller-owned state, never a model-supplied lens label."""
    history = state_home / "tx"
    current = history / "requests-v1"
    if history.exists():
        for child in history.iterdir():
            if child != current and has_request_data(child):
                raise RequestIndexHeld("legacy_default_request_state_requires_review")
    return current


class RequestIndex:
    """One versioned namespace bound to FileLedger's existing private HMAC key.

    A populated unbound/legacy workdir is held for explicit reconciliation.
    Initialization uses the ledger's own process/thread transaction lock. An
    interrupted initialization before any request can reuse the same ledger key;
    it never invents a replacement for lost request state.
    """

    def __init__(self, workdir: Path | str, backend: object | None = None) -> None:
        self.workdir = Path(workdir).resolve()
        candidate = (
            backend if backend is not None else FileLedger(self.workdir / "ledger")
        )
        if not isinstance(candidate, FileLedger) or type(candidate) is not FileLedger:
            raise RequestIndexHeld("request_index_requires_matching_persistent_ledger")
        self.backend = candidate
        if self.backend.root.resolve() != self.workdir / "ledger":
            raise RequestIndexHeld("request_index_requires_matching_persistent_ledger")
        self.marker = self.backend.root / _MARKER
        try:
            with self.backend.transaction(_INIT_LOCK):
                self._secret = self._load_or_initialize()
        except LedgerError as exc:
            raise RequestIndexHeld("request_index_ledger_unavailable") from exc

    def _has_prior_data(self) -> bool:
        return has_request_data(self.workdir)

    @staticmethod
    def _private_regular(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise RequestIndexHeld("request_index_state_unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or (
            os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid())
        ):
            raise RequestIndexHeld("request_index_state_not_private_regular")

    def _checked_secret(self, *, existing: bool) -> bytes:
        if not self.backend.integrity_protected:
            path = self.backend.root / "ledger.key"
            if existing or path.exists():
                self._private_regular(path)
                try:
                    wire = path.read_bytes()
                    if len(wire) != 64 or any(
                        byte not in b"0123456789abcdef" for byte in wire
                    ):
                        raise RequestIndexHeld("request_index_key_invalid")
                except OSError as exc:
                    raise RequestIndexHeld("request_index_key_unavailable") from exc
        secret = self.backend.secret()
        if len(secret) != 32:
            raise RequestIndexHeld("request_index_key_invalid")
        return secret

    def _read_binding(self) -> dict[str, Any]:
        self._private_regular(self.marker)
        try:
            value = json.loads(
                self.marker.read_bytes(), object_pairs_hook=_unique_object
            )
        except (OSError, ValueError) as exc:
            raise RequestIndexHeld("request_index_binding_invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value)
            != {"schema", "ledger_key_sha256", "request_scope", "directory_scope"}
            or value.get("schema") != _SCHEMA
            or value.get("request_scope") != _REQUEST_SCOPE
            or value.get("directory_scope") != _DIRECTORY_SCOPE
            or not isinstance(value.get("ledger_key_sha256"), str)
        ):
            raise RequestIndexHeld("request_index_binding_version_or_shape")
        return value

    def _load_or_initialize(self) -> bytes:
        if self.marker.exists() or self.marker.is_symlink():
            binding = self._read_binding()
            secret = self._checked_secret(existing=True)
            if not hmac.compare_digest(
                binding["ledger_key_sha256"], hashlib.sha256(secret).hexdigest()
            ):
                raise RequestIndexHeld("request_index_binding_mismatch")
            return secret
        if self._has_prior_data():
            raise RequestIndexHeld("request_index_binding_missing_for_existing_data")
        secret = self._checked_secret(existing=False)
        binding = {
            "schema": _SCHEMA,
            "ledger_key_sha256": hashlib.sha256(secret).hexdigest(),
            "request_scope": _REQUEST_SCOPE,
            "directory_scope": _DIRECTORY_SCOPE,
        }
        # Reuse the shared atomic private-file primitive without an import cycle.
        from openadapt_flow.tx.receipt import write_private_bytes

        write_private_bytes(
            self.marker,
            (json.dumps(binding, sort_keys=True) + "\n").encode(),
            overwrite=False,
        )
        observed = self._read_binding()
        if observed != binding:
            raise RequestIndexHeld("request_index_binding_mismatch")
        return secret

    def require_current(self) -> None:
        try:
            with self.backend.transaction(_INIT_LOCK):
                if not self.marker.exists():
                    raise RequestIndexHeld("request_index_binding_missing")
                current = self._load_or_initialize()
                if not hmac.compare_digest(current, self._secret):
                    raise RequestIndexHeld("request_index_key_changed")
        except LedgerError as exc:
            raise RequestIndexHeld("request_index_ledger_unavailable") from exc

    def task_key(self, task_id: str) -> str:
        self.require_current()
        return "task-" + salted_sha256(
            self._secret, {"domain": _DIRECTORY_SCOPE, "task": task_id}
        )

    def ledger_key(self, task_id: str) -> str:
        """One authorized write per task ID/workdir, independent of candidate metadata."""
        self.require_current()
        return salted_sha256(self._secret, {"domain": _REQUEST_SCOPE, "task": task_id})
