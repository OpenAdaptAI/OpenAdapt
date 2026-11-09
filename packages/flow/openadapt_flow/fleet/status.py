"""Signed fleet status: the revocation list and kill switches.

Both are signed by a ``status`` signer certificate (see
:mod:`openadapt_flow.fleet.signing`) and travel in the patch feed.

* A :class:`SignedRevocationList` names revoked intermediate and signer keys
  and signer certificate serials. Each list says when it was issued and when
  the next one is due (``next_update``, at most 24 hours later).
* A :class:`SignedKillSwitch` can turn off auto-promotion for every tenant
  and can name patch ids every tenant must roll back. The entry with the
  highest sequence number decides whether auto-promotion is off; rollback ids
  accumulate across all entries.

:func:`read_fleet_status` reads both and never raises. It fails closed: an
unreachable, missing, unverifiable, or stale revocation list, or a kill-switch
entry that does not verify, each produce a reason, and any reason stops
auto-promotion (:mod:`openadapt_flow.fleet.policy`). A person can still
approve the patch through the ordinary repair lifecycle.

Replay protection: a :class:`StatusHighWater` file on the tenant records the
highest revocation-list and kill-switch sequence numbers the tenant has
accepted. A revocation list with a lower sequence, or a kill-switch history
whose newest entry is lower than the recorded one, is refused, so an old
list cannot be replayed to undo a revocation. If the file exists but cannot
be read or parsed, or cannot be written, status is not fresh. A missing file
means the tenant has accepted nothing yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.fleet._lock import exclusive_lock
from openadapt_flow.fleet.signing import (
    CLOCK_SKEW,
    CertificateChain,
    FleetTrust,
    KeyCustody,
    PatchSignatureError,
    chain_revocation_reason,
    sign_document,
    verify_chain,
    verify_document,
)
from openadapt_flow.qualification_admission_v2 import canonical_json

REVOCATION_DOMAIN: Final[bytes] = b"openadapt.fleet.revocation-list/v1\x00"
KILL_SWITCH_DOMAIN: Final[bytes] = b"openadapt.fleet.kill-switch/v1\x00"
#: Longest gap between ``issued_at`` and ``next_update``.
MAX_REVOCATION_LIST_VALIDITY: Final[timedelta] = timedelta(hours=24)
#: A tenant refuses a list older than this, whatever its ``next_update``.
DEFAULT_MAX_STATUS_AGE: Final[timedelta] = timedelta(hours=24)
_SHA256 = r"^[a-f0-9]{64}$"
_KEY_ID = r"^[a-f0-9]{32}$"


#: File name of the tenant's sequence high-water mark, in its fleet directory.
STATUS_STATE_FILE: Final[str] = "status-state.json"


class FleetStatusUnavailable(RuntimeError):
    """The feed could not serve a status document."""


class FleetStatusStateError(RuntimeError):
    """The persisted sequence high-water mark cannot be read or written."""


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time has no timezone")
    return parsed.astimezone(timezone.utc)


def _sorted_unique(value: tuple[str, ...], what: str) -> tuple[str, ...]:
    if list(value) != sorted(set(value)):
        raise ValueError(f"{what} must be sorted and unique")
    return value


class RevocationListPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.revocation-list/v1"] = (
        "openadapt.fleet.revocation-list/v1"
    )
    sequence: int = Field(ge=1)
    issued_at: str = Field(min_length=20, max_length=64)
    next_update: str = Field(min_length=20, max_length=64)
    revoked_key_ids: tuple[str, ...] = Field(default=(), max_length=10_000)
    revoked_serials: tuple[str, ...] = Field(default=(), max_length=10_000)

    @field_validator("revoked_key_ids", "revoked_serials")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if any(not re.match(_KEY_ID, v) for v in value):
            raise ValueError("revoked ids are 32 hex characters")
        return _sorted_unique(value, "revoked ids")

    @model_validator(mode="after")
    def _window(self) -> "RevocationListPayload":
        start, end = _utc(self.issued_at), _utc(self.next_update)
        if not start < end <= start + MAX_REVOCATION_LIST_VALIDITY:
            raise ValueError("next_update must fall within 24 hours of issued_at")
        return self


class SignedRevocationList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: RevocationListPayload
    signature: str = Field(min_length=1, max_length=128)
    chain: CertificateChain


KillReason = Literal[
    "contributor_revoked",
    "key_compromise",
    "bad_patch",
    "incident",
    "resume",
]


class KillSwitchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.kill-switch/v1"] = (
        "openadapt.fleet.kill-switch/v1"
    )
    sequence: int = Field(ge=1)
    issued_at: str = Field(min_length=20, max_length=64)
    #: True turns auto-promotion off on every tenant until a later entry
    #: (higher sequence) turns it back on.
    disable_auto_promotion: bool
    #: Patch ids every tenant rolls back (canary or active) or rejects.
    rollback_patch_ids: tuple[str, ...] = Field(default=(), max_length=1000)
    reason: KillReason

    @field_validator("rollback_patch_ids")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        if any(not re.match(_SHA256, v) for v in value):
            raise ValueError("patch ids are SHA-256 hex")
        return _sorted_unique(value, "patch ids")


class SignedKillSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: KillSwitchPayload
    signature: str = Field(min_length=1, max_length=128)
    chain: CertificateChain

    @property
    def entry_id(self) -> str:
        return hashlib.sha256(
            KILL_SWITCH_DOMAIN + canonical_json(self.payload)
        ).hexdigest()


def sign_revocation_list(
    payload: RevocationListPayload, signer: KeyCustody, chain: CertificateChain
) -> SignedRevocationList:
    _require_status_signer(signer, chain)
    return SignedRevocationList(
        payload=payload,
        signature=sign_document(signer, REVOCATION_DOMAIN, payload),
        chain=chain,
    )


def sign_kill_switch(
    payload: KillSwitchPayload, signer: KeyCustody, chain: CertificateChain
) -> SignedKillSwitch:
    _require_status_signer(signer, chain)
    return SignedKillSwitch(
        payload=payload,
        signature=sign_document(signer, KILL_SWITCH_DOMAIN, payload),
        chain=chain,
    )


def _require_status_signer(signer: KeyCustody, chain: CertificateChain) -> None:
    if signer.public_key_b64() != chain.signer.payload.signer_public_key:
        raise PatchSignatureError("the signing key does not match its certificate")
    if chain.signer.payload.purpose != "status":
        raise PatchSignatureError("status documents need a status certificate")


class FleetStatusSource(Protocol):
    def revocation_list(self) -> SignedRevocationList:
        """The newest list. Raise :class:`FleetStatusUnavailable` if none."""
        ...

    def kill_switch_documents(self) -> list[str]:
        """Every kill-switch entry as raw JSON, oldest first.

        Raw, so one malformed entry cannot hide the others: the reader parses
        each entry on its own.
        """
        ...


@dataclass
class FleetStatus:
    """What a tenant learned from the feed, and why it may not trust it."""

    checked_at: datetime
    #: Reasons auto-promotion must not proceed. Empty means fresh and verified.
    problems: list[str] = field(default_factory=list)
    revocation_sequence: Optional[int] = None
    kill_switch_sequence: Optional[int] = None
    revoked_key_ids: frozenset[str] = frozenset()
    revoked_serials: frozenset[str] = frozenset()
    auto_promotion_disabled: bool = False
    rollback_patch_ids: frozenset[str] = frozenset()

    @property
    def fresh(self) -> bool:
        return not self.problems

    def revocation_reason(self, chain: CertificateChain) -> Optional[str]:
        return chain_revocation_reason(
            chain, self.revoked_key_ids, self.revoked_serials
        )


class StatusHighWaterState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.status-state/v1"] = (
        "openadapt.fleet.status-state/v1"
    )
    revocation_sequence: int = Field(default=0, ge=0)
    kill_switch_sequence: int = Field(default=0, ge=0)
    updated_at: Optional[str] = None


class StatusHighWater:
    """The highest status sequence numbers this tenant has accepted.

    Stored as JSON in ``path`` and replaced atomically. :meth:`load` raises
    :class:`FleetStatusStateError` for a file that exists but is unreadable
    or invalid; :func:`read_fleet_status` turns that into a problem, so
    auto-promotion stops until an admin repairs or removes the file.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def in_dir(cls, fleet_dir: Path) -> "StatusHighWater":
        return cls(Path(fleet_dir) / STATUS_STATE_FILE)

    def load(self) -> StatusHighWaterState:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return StatusHighWaterState()
        except OSError as exc:
            raise FleetStatusStateError(
                f"cannot read {self.path.name} ({type(exc).__name__})"
            ) from exc
        try:
            return StatusHighWaterState.model_validate(json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise FleetStatusStateError(f"{self.path.name} is not valid") from exc

    def advance(
        self,
        *,
        revocation_sequence: Optional[int],
        kill_switch_sequence: Optional[int],
        now: datetime,
    ) -> StatusHighWaterState:
        """Raise the recorded sequences; never lower them.

        The read, compare and write run under an exclusive lock on
        :attr:`lock_path`, so two processes cannot interleave and lower a
        number the other just recorded.
        """
        try:
            with exclusive_lock(self.lock_path):
                return self._advance_locked(
                    revocation_sequence=revocation_sequence,
                    kill_switch_sequence=kill_switch_sequence,
                    now=now,
                )
        except OSError as exc:
            raise FleetStatusStateError(
                f"cannot lock {self.path.name} ({type(exc).__name__})"
            ) from exc

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _advance_locked(
        self,
        *,
        revocation_sequence: Optional[int],
        kill_switch_sequence: Optional[int],
        now: datetime,
    ) -> StatusHighWaterState:
        current = self.load()
        state = StatusHighWaterState(
            revocation_sequence=max(
                current.revocation_sequence, revocation_sequence or 0
            ),
            kill_switch_sequence=max(
                current.kill_switch_sequence, kill_switch_sequence or 0
            ),
            updated_at=now.isoformat(),
        )
        if (
            state.revocation_sequence == current.revocation_sequence
            and (state.kill_switch_sequence == current.kill_switch_sequence)
            and self.path.exists()
        ):
            return current
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(state.model_dump_json(indent=2))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise FleetStatusStateError(
                f"cannot write {self.path.name} ({type(exc).__name__})"
            ) from exc
        return state


def _verify_status_chain(
    chain: CertificateChain, trust: FleetTrust, now: datetime
) -> None:
    verify_chain(chain, trust, purpose="status", now=now)


def read_fleet_status(
    source: FleetStatusSource,
    trust: FleetTrust,
    *,
    now: Optional[datetime] = None,
    max_age: timedelta = DEFAULT_MAX_STATUS_AGE,
    high_water: Optional[StatusHighWater] = None,
) -> FleetStatus:
    """Read and verify the revocation list and kill switches. Never raises.

    With ``high_water``, refuse status older than what this tenant already
    accepted, and record what it accepts now.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    status = FleetStatus(checked_at=current)
    floor: Optional[StatusHighWaterState] = None
    if high_water is not None:
        try:
            floor = high_water.load()
        except FleetStatusStateError as exc:
            status.problems.append(
                f"persisted fleet-status state is unreadable: {exc}; failing closed"
            )
    accepted_revocation: Optional[int] = None

    try:
        rl = source.revocation_list()
    except Exception as exc:  # noqa: BLE001 - any failure is "unreachable"
        status.problems.append(
            f"revocation list unreachable ({type(exc).__name__}); failing closed"
        )
        rl = None
    if rl is not None:
        try:
            _verify_status_chain(rl.chain, trust, current)
            verify_document(
                rl.chain.signer.payload.signer_public_key,
                REVOCATION_DOMAIN,
                rl.payload,
                rl.signature,
                "revocation list",
            )
        except PatchSignatureError as exc:
            status.problems.append(f"revocation list does not verify: {exc}")
        else:
            before = len(status.problems)
            issued, due = _utc(rl.payload.issued_at), _utc(rl.payload.next_update)
            if issued > current + CLOCK_SKEW:
                status.problems.append("revocation list is dated in the future")
            elif current >= due or current - issued > max_age:
                status.problems.append(
                    "revocation list is stale (issued "
                    f"{rl.payload.issued_at}, next update was due "
                    f"{rl.payload.next_update})"
                )
            status.revocation_sequence = rl.payload.sequence
            status.revoked_key_ids = frozenset(rl.payload.revoked_key_ids)
            status.revoked_serials = frozenset(rl.payload.revoked_serials)
            own = status.revocation_reason(rl.chain)
            if own is not None:
                status.problems.append(f"revocation list signer: {own}")
            if floor is not None and rl.payload.sequence < floor.revocation_sequence:
                status.problems.append(
                    f"revocation list sequence {rl.payload.sequence} is lower "
                    f"than {floor.revocation_sequence}, already accepted; "
                    "refusing a replayed list"
                )
            if len(status.problems) == before:
                accepted_revocation = rl.payload.sequence

    try:
        documents = source.kill_switch_documents()
    except Exception as exc:  # noqa: BLE001
        status.problems.append(
            f"kill switches unreachable ({type(exc).__name__}); failing closed"
        )
        documents = []
    verified: list[SignedKillSwitch] = []
    for document in documents:
        try:
            entry = SignedKillSwitch.model_validate_json(document)
        except ValueError:
            # Its rollback ids are unknown, so nothing may auto-promote, but
            # the verified entries around it still apply.
            status.problems.append("a kill-switch entry is malformed")
            continue
        try:
            _verify_status_chain(entry.chain, trust, current)
            verify_document(
                entry.chain.signer.payload.signer_public_key,
                KILL_SWITCH_DOMAIN,
                entry.payload,
                entry.signature,
                "kill switch",
            )
        except PatchSignatureError as exc:
            status.problems.append(f"a kill-switch entry does not verify: {exc}")
            continue
        verified.append(entry)
    if verified:
        latest = max(verified, key=lambda e: e.payload.sequence)
        status.auto_promotion_disabled = latest.payload.disable_auto_promotion
        ids: set[str] = set()
        for entry in verified:
            ids.update(entry.payload.rollback_patch_ids)
        status.rollback_patch_ids = frozenset(ids)
        status.kill_switch_sequence = latest.payload.sequence
    accepted_kill: Optional[int] = None
    if floor is not None:
        newest = status.kill_switch_sequence or 0
        if newest < floor.kill_switch_sequence:
            status.problems.append(
                f"newest kill-switch sequence {newest} is lower than "
                f"{floor.kill_switch_sequence}, already accepted; refusing "
                "replayed kill-switch history"
            )
        else:
            accepted_kill = status.kill_switch_sequence
    if high_water is not None and floor is not None:
        try:
            high_water.advance(
                revocation_sequence=accepted_revocation,
                kill_switch_sequence=accepted_kill,
                now=current,
            )
        except FleetStatusStateError as exc:
            status.problems.append(
                f"cannot record fleet-status state: {exc}; failing closed"
            )
    return status


__all__ = [
    "DEFAULT_MAX_STATUS_AGE",
    "FleetStatus",
    "FleetStatusSource",
    "FleetStatusStateError",
    "FleetStatusUnavailable",
    "KillSwitchPayload",
    "MAX_REVOCATION_LIST_VALIDITY",
    "RevocationListPayload",
    "SignedKillSwitch",
    "STATUS_STATE_FILE",
    "SignedRevocationList",
    "StatusHighWater",
    "StatusHighWaterState",
    "read_fleet_status",
    "sign_kill_switch",
    "sign_revocation_list",
]
