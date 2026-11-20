"""Local session mailbox and view-state contracts for the Chrome Side-Panel Co-Pilot.

Provides ``CopilotViewState``, ``CopilotFieldHighlight``, and ``CopilotSessionBridge``
for zero-cloud communication between the local Python ``form_tx`` runner and the
OpenAdapt Chrome extension over Chrome Native Messaging.

All state updates are written atomically via temp-file rename (``copilot_state.json``)
and all operator resolutions are appended with ``os.O_APPEND`` and deduplicated by
``idempotency_key`` (``copilot_decisions.jsonl``). Secret and password-like keys are
redacted to ``"<secret>"`` so sensitive credentials never appear in plaintext.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.form_tx.types import (
    FormHaltItem,
    FormTxOutcome,
    HumanResolutionInput,
)

COPILOT_STATE_SCHEMA = "openadapt.copilot-state/v1"
COPILOT_STATE_FILE = "copilot_state.json"
COPILOT_DECISIONS_FILE = "copilot_decisions.jsonl"
REDACTED_SECRET = "<secret>"

_SECRET_KEY_RE = re.compile(
    r"pass(word|wd|phrase)?\b"
    r"|passwd|pwd|secret|private[_-]?key"
    r"|(^|[_.\-\s])(pin|otp|ssn|cvv|api[_-]?key|access[_-]?token|auth[_-]?token"
    r"|refresh[_-]?token|session[_-]?token|bearer[_-]?token|client[_-]?secret"
    r"|credential)($|[_.\-\s])"
    r"|^token$",
    re.I,
)

_SECRET_SELECTOR_RE = re.compile(
    r'type\s*=\s*["\']?password'
    r"|data-openadapt-secret"
    r"|pass(word|wd|phrase)?\b"
    r"|passwd|pwd|secret|private[_-]?key"
    r"|(^|[#.\-_\[\s])(pin|otp|ssn)($|[#.\-_\]\s])",
    re.I,
)

_TOKEN_VALUE_RE = re.compile(
    r"^(Bearer\s+\S+"
    r"|gh[pousr]_[A-Za-z0-9_]{16,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)$",
    re.I,
)


def is_secret_key_name(name: Optional[str]) -> bool:
    """Return True when ``name`` looks like a password, secret, or credential key."""
    if not name:
        return False
    return bool(_SECRET_KEY_RE.search(str(name)))


def is_secret_selector(selector: Optional[str]) -> bool:
    """Return True when a DOM selector targets a password or secret input."""
    if not selector:
        return False
    return bool(_SECRET_SELECTOR_RE.search(str(selector)))


def _redact_value(value: Any, *, key_hint: Optional[str] = None) -> Any:
    """Recursively redact values under secret-like keys or raw bearer/API tokens."""
    if key_hint is not None and is_secret_key_name(key_hint):
        return None if value is None else REDACTED_SECRET
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str) and _TOKEN_VALUE_RE.match(value.strip()):
        return REDACTED_SECRET
    return value


def _redact_halt_item(halt: FormHaltItem) -> FormHaltItem:
    """Ensure ``FormHaltItem`` never exposes plaintext values for secret fields."""
    secret_target = (
        is_secret_key_name(halt.dossier_key)
        or is_secret_key_name(halt.control_id)
        or is_secret_key_name(halt.field_label)
        or is_secret_selector(halt.dom_selector)
    )
    if secret_target:
        portal_val = REDACTED_SECRET if halt.portal_value is not None else None
        dossier_val = REDACTED_SECRET if halt.dossier_value is not None else None
    else:
        portal_val = _redact_value(halt.portal_value)
        dossier_val = _redact_value(halt.dossier_value)
    if portal_val != halt.portal_value or dossier_val != halt.dossier_value:
        return halt.model_copy(
            update={
                "portal_value": portal_val,
                "dossier_value": dossier_val,
            }
        )
    return halt


class CopilotFieldHighlight(BaseModel):
    """Field-level visual highlight instruction rendered by ``content.js``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dom_selector: str = Field(min_length=1)
    badge_state: Literal["filled", "conflict", "halt"]
    caption: str = ""

    @field_validator("caption", mode="after")
    @classmethod
    def _scrub_caption(cls, value: str) -> str:
        if _TOKEN_VALUE_RE.match(value.strip()):
            return REDACTED_SECRET
        return value


class CopilotViewState(BaseModel):
    """Read-only state snapshot projected into the Chrome Side-Panel Co-Pilot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.copilot-state/v1"] = COPILOT_STATE_SCHEMA
    run_id: str = Field(min_length=1)
    outcome: FormTxOutcome
    status_line: str
    fields_filled_auto: int = Field(ge=0)
    total_bound_fields: int = Field(ge=0)
    subroutines_completed: tuple[str, ...] = ()
    halts: tuple[FormHaltItem, ...] = ()
    field_highlights: tuple[CopilotFieldHighlight, ...] = ()
    outputs: dict[str, Any] = Field(default_factory=dict)
    receipt_verified: bool = False
    receipt_id: Optional[str] = None

    @field_validator("status_line", mode="after")
    @classmethod
    def _scrub_status_line(cls, value: str) -> str:
        return str(_redact_value(value))

    @field_validator("halts", mode="after")
    @classmethod
    def _scrub_halts(cls, value: tuple[FormHaltItem, ...]) -> tuple[FormHaltItem, ...]:
        return tuple(_redact_halt_item(item) for item in value)

    @field_validator("outputs", mode="after")
    @classmethod
    def _scrub_outputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        return {str(k): _redact_value(v, key_hint=str(k)) for k, v in value.items()}


class CopilotSessionBridge:
    """Atomic file-backed mailbox bridging the local runner and native host."""

    def __init__(self, recording_or_session_dir: Path | str) -> None:
        self._dir = Path(recording_or_session_dir)

    @property
    def session_dir(self) -> Path:
        return self._dir

    @property
    def state_path(self) -> Path:
        return self._dir / COPILOT_STATE_FILE

    @property
    def decisions_path(self) -> Path:
        return self._dir / COPILOT_DECISIONS_FILE

    def publish_state(self, state: CopilotViewState) -> None:
        """Atomically write ``copilot_state.json`` via temp-file rename."""
        validated = (
            state
            if isinstance(state, CopilotViewState)
            else CopilotViewState.model_validate(state)
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                validated.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")

        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(self._dir),
            prefix=".copilot_state.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            try:
                os.chmod(tmp_fd, 0o600)
            except OSError:
                pass
            view = memoryview(payload)
            written = 0
            while written < len(view):
                written += os.write(tmp_fd, view[written:])
            os.fsync(tmp_fd)
        except Exception:
            os.close(tmp_fd)
            tmp_path.unlink(missing_ok=True)
            raise
        os.close(tmp_fd)
        os.replace(tmp_path, self.state_path)

    def read_state(self) -> Optional[CopilotViewState]:
        """Read and validate ``copilot_state.json``, or return ``None`` if absent."""
        path = self.state_path
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        return CopilotViewState.model_validate_json(raw)

    def append_decision(
        self, decision: HumanResolutionInput | Mapping[str, Any]
    ) -> tuple[bool, int]:
        """Validate and append ``decision`` with ``os.O_APPEND`` and idempotency dedup.

        Returns ``(accepted_new, total_count)`` where ``accepted_new`` is ``False``
        when ``decision.idempotency_key`` was already recorded.
        """
        validated = (
            decision
            if isinstance(decision, HumanResolutionInput)
            else HumanResolutionInput.model_validate(decision)
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        fd = os.open(str(self.decisions_path), flags, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            existing_bytes = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                existing_bytes += chunk

            seen_keys: set[str] = set()
            for line in existing_bytes.decode("utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                prior = HumanResolutionInput.model_validate_json(stripped)
                seen_keys.add(prior.idempotency_key)

            if validated.idempotency_key in seen_keys:
                return False, len(seen_keys)

            encoded_line = (
                json.dumps(
                    validated.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            view = memoryview(encoded_line)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
            return True, len(seen_keys) + 1
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)

    def drain_new_decisions(
        self, already_seen_keys: set[str]
    ) -> list[HumanResolutionInput]:
        """Return validated decisions whose ``idempotency_key`` is not in ``already_seen_keys``.

        Updates ``already_seen_keys`` in place with every newly drained key.
        """
        path = self.decisions_path
        if not path.is_file():
            return []
        seen = (
            already_seen_keys
            if isinstance(already_seen_keys, set)
            else set(already_seen_keys)
        )
        fresh: list[HumanResolutionInput] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            item = HumanResolutionInput.model_validate_json(stripped)
            if item.idempotency_key in seen:
                continue
            seen.add(item.idempotency_key)
            fresh.append(item)
        return fresh


__all__ = [
    "COPILOT_DECISIONS_FILE",
    "COPILOT_STATE_FILE",
    "COPILOT_STATE_SCHEMA",
    "CopilotFieldHighlight",
    "CopilotSessionBridge",
    "CopilotViewState",
    "REDACTED_SECRET",
    "is_secret_key_name",
    "is_secret_selector",
]
