"""Durable checkpoint + pending-escalation store (RFC §5, Tier 3).

The Workflow-Program IR RFC (``docs/design/WORKFLOW_PROGRAM_IR.md`` §5) makes
the runtime three-tier: a deterministic fast path, a bounded local model
recovery, and -- when neither can safely proceed -- a **durable pause /
approve / resume from the last verified checkpoint**. Today's replayer only
had the fast path and a bare ``halt``: on failure the run just dies, and a
re-run starts from step 0. That is unsafe in production, because re-running a
workflow that already performed consequential writes re-performs them.

This module is the persistence substrate for Tier 3. It is deliberately
import-light (pydantic + json + pathlib only -- no vision, no backend, no
model), and it lives in its OWN package so the state-machine interpreter
(Phase 2, which rewrites ``replayer.py`` heavily) can adopt it with a minimal,
localized set of replayer touch-points (see the module docstring of
``openadapt_flow.runtime.durable.controller``).

Two durable artifacts, both written under the run directory:

- :class:`RunCheckpoint` -- one per VERIFIED step (identity passed, effects
  CONFIRMED, postconditions passed), written to ``run_dir/checkpoints/``. It
  records enough to RESUME: the step index/id (and, for the Phase-2 state
  machine, an optional ``state_id``), the run's parameter bindings, and the
  index to continue from. Deterministic, cheap, ``$0``.
- :class:`PendingEscalation` -- written to ``run_dir/pending_escalation.json``
  when the run HALTS for an operator (a non-CONFIRMED effect that escalates,
  an unmet consequential guard, an unconfirmed placeholder effect, an
  unresolved disambiguation, ...). It captures WHY the run paused and the
  proposed operator options, and points at the last verified checkpoint so a
  human can approve and RESUME from there -- never from step 0, and never by
  handing the remaining workflow to a free-form agent.

A small :class:`RunManifest` (``run_dir/checkpoints/_manifest.json``) records
the bundle directory and parameter bindings so :func:`~.resume.resume` can
reconstruct the run from ``run_dir`` alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.durable.approval import ApprovalRecord
from openadapt_flow.runtime.durable.program_checkpoint import (
    GraphFrame,
    ProgramCheckpoint,
)

CHECKPOINTS_DIRNAME = "checkpoints"
MANIFEST_FILENAME = "_manifest.json"
PENDING_FILENAME = "pending_escalation.json"
APPROVAL_FILENAME = "approval.json"
#: Prefix of the per-verified-state Phase-2 interpreter checkpoints
#: (``pstate_0000.json``), written under ``run_dir/checkpoints/`` alongside the
#: linear ``step_*.json`` checkpoints.
PROGRAM_CHECKPOINT_PREFIX = "pstate_"
#: Default stale-pause window: a pause older than this is refused on resume
#: (the app state a stale checkpoint expects can no longer be trusted). 7 days.
DEFAULT_STALE_AFTER_S = 7 * 24 * 3600.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunManifest(BaseModel):
    """Enough to reconstruct a run from its ``run_dir`` alone.

    Written once at run start (durability enabled). :func:`~.resume.resume`
    reads it to find the bundle and the parameter bindings without the caller
    re-supplying them.
    """

    schema_version: int = 1
    #: Random run-instance identity, distinct from workflow/bundle identity.
    #: Attended capabilities bind to this so a capability copied between two
    #: runs of the same bundle is refused.
    run_id: str = ""
    workflow_name: str
    #: The workflow bundle directory (absolute), source of ``workflow.json``
    #: and the template crops.
    bundle_dir: str
    #: The run's fully-resolved parameter bindings (defaults + caller
    #: overrides), so a resume re-binds identically.
    params: dict[str, str] = Field(default_factory=dict)
    worklists: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    governed_authorization: Optional[GovernedRunAuthorization] = None
    #: Optional healed-bundle output path, mirrored from the original run.
    save_healed_to: Optional[str] = None
    created_at: str = Field(default_factory=_now)


class RunCheckpoint(BaseModel):
    """A durable marker that a step VERIFIED and the run may resume after it.

    Written after each step whose identity passed, whose declared effects were
    CONFIRMED (or a duplicate RECONCILED), and whose postconditions passed --
    i.e. every step that ``result.ok`` and did not halt. The last such
    checkpoint is the resume point: a run that pauses re-does only the paused
    step onward, so an already-confirmed consequential write is NEVER
    re-executed.
    """

    schema_version: int = 1
    workflow_name: str
    #: Index of the verified step in ``workflow.steps``.
    step_index: int
    step_id: str
    intent: str = ""
    #: Phase-2 state-machine state id, when the interpreter has one. None for
    #: the linear replayer -- the step index/id is the resume key. Present so
    #: the state-machine PR can populate it without a schema change.
    state_id: Optional[str] = None
    #: Where a resume continues: the NEXT step to execute (``step_index + 1``
    #: for the linear replayer). Stored explicitly so the state machine can
    #: point at an arbitrary successor state.
    next_step_index: int
    #: The run's parameter bindings at checkpoint time (resume re-binds these).
    params: dict[str, str] = Field(default_factory=dict)
    #: Verification evidence carried for the audit trail / operator.
    effect_verified: Optional[bool] = None
    effect_approved_unverified: bool = False
    effect_contract_hashes: list[str] = Field(default_factory=list)
    governed_authorization_id: Optional[str] = None
    governed_approval_source: Optional[str] = None
    postconditions_ok: Optional[bool] = None
    skipped: bool = False
    actuation: Optional[str] = None
    created_at: str = Field(default_factory=_now)


class PendingEscalation(BaseModel):
    """A durable pause: the run HALTED for an operator instead of dying.

    Captures the current state, WHY it paused, and the proposed operator
    options (derived from the halt reason / compensation escalation), plus the
    checkpoint to resume from. Persisted to ``run_dir/pending_escalation.json``;
    an operator (or a separate escalation path) reviews it, resolves the cause,
    and resumes -- deterministically, from :attr:`resume_from_index`, NOT from
    step 0 and NOT by delegating the tail of the workflow to a free-form agent
    (RFC §5 explicit non-goal).
    """

    schema_version: int = 1
    workflow_name: str
    #: The step that halted.
    step_index: int
    step_id: str
    intent: str = ""
    state_id: Optional[str] = None
    #: Coarse machine category (see :func:`classify_halt`): ``effect_refuted``,
    #: ``effect_indeterminate``, ``effect_escalated``, ``placeholder_effect``,
    #: ``effect_unverifiable``, ``unmet_guard``, ``disambiguation``,
    #: ``identity``, ``postcondition``, ``resolution``, ``human_required``, or
    #: ``halt``. ``human_required`` means CAPTCHA/MFA/re-authentication must be
    #: completed by the present operator; no automation acts on the challenge.
    category: str
    #: The verbatim halt reason (``result.error``) -- WHY it paused.
    reason: str = ""
    #: Supporting audit lines (e.g. ``result.effect_results`` verdicts).
    detail: list[str] = Field(default_factory=list)
    #: Operator-facing next actions proposed for this pause (from the halt
    #: reason / compensation). Approval and resume are always among them.
    proposed_options: list[str] = Field(default_factory=list)
    #: The step index a resume continues from = the last verified checkpoint's
    #: ``next_step_index`` (0 when nothing verified yet). The paused step and
    #: everything after it re-run; verified steps before it do NOT.
    resume_from_index: int = 0
    resume_from_step_id: Optional[str] = None
    #: The run's parameter bindings, so an approved resume re-binds identically.
    params: dict[str, str] = Field(default_factory=dict)
    status: Literal["pending", "approved"] = "pending"
    #: Stale-pause expiry (RFC §5, P0-5): a resume attempted more than this many
    #: seconds after ``created_at`` is REFUSED (:class:`~.approval.PauseExpired`)
    #: -- the app state a stale checkpoint expects can no longer be trusted.
    #: ``<= 0`` disables expiry.
    stale_after_s: float = DEFAULT_STALE_AFTER_S
    #: True when this pause is over a Phase-2 PROGRAM run (its resume point is a
    #: :class:`~.program_checkpoint.ProgramCheckpoint`, not a linear step index).
    program: bool = False
    #: Exact interpreter cursor at an ACTION-state halt. Empty for non-action
    #: program halts, which therefore cannot issue Continue/Skip authority.
    program_frames: list[GraphFrame] = Field(default_factory=list)
    #: Highest verified program-checkpoint sequence before this halted action.
    program_checkpoint_seq: int = Field(default=0, ge=0)
    #: Rolling visited-state digest captured at the halt, for audit continuity.
    program_history_hash: str = ""
    created_at: str = Field(default_factory=_now)


#: Suffix appended to a durable artifact's filename when it is encrypted at
#: rest, so a plaintext and an encrypted store never collide and a reader can
#: tell them apart on disk.
ENC_SUFFIX = ".enc"


class CheckpointStore:
    """Read/write the durable artifacts under a run directory.

    Layout (plaintext)::

        run_dir/
          checkpoints/
            _manifest.json            # RunManifest
            step_0000_<id>.json       # RunCheckpoint (one per verified step)
            step_0001_<id>.json
          pending_escalation.json     # PendingEscalation (present iff paused)

    Encryption-at-rest (opt-in, OFF by default): when a ``key`` passphrase is
    supplied (explicitly or via ``OPENADAPT_BUNDLE_KEY``), every artifact is
    sealed with AES-256-GCM (``openadapt_flow.crypto``) and written with a
    trailing ``.enc`` (``step_0000_<id>.json.enc`` etc.). A durable checkpoint
    carries the run's parameter bindings and verification evidence, so the same
    at-rest control the compiled bundle gets applies here. Reads transparently
    decrypt an ``.enc`` artifact (a wrong/missing key fails LOUDLY via
    ``crypto.DecryptionError`` / ``crypto.MissingKeyError``). With no key the
    behavior is byte-for-byte unchanged from before.
    """

    def __init__(self, run_dir: Path | str, *, key: Optional[str] = None) -> None:
        self.run_dir = Path(run_dir)
        self.checkpoints_dir = self.run_dir / CHECKPOINTS_DIRNAME
        # None => plaintext (unchanged default). A non-empty passphrase turns on
        # AEAD sealing for writes and decryption for reads.
        self.key = key

    # -- (de)serialization seam ---------------------------------------------

    def _write_model(self, path: Path, model: BaseModel) -> Path:
        """Serialize ``model`` to ``path`` (``.json``), or to ``path`` + ``.enc``
        sealed with AES-256-GCM when a key is configured. Returns the path
        actually written; removes the counterpart form if it lingers."""
        data = model.model_dump_json(indent=2).encode("utf-8")
        if self.key:
            from openadapt_flow import crypto as _crypto

            target = path.with_name(path.name + ENC_SUFFIX)
            target.write_bytes(
                _crypto.encrypt_bytes(data, self.key, aad=_crypto.CHECKPOINT_AAD)
            )
            if path.exists():
                path.unlink()
            return target
        path.write_bytes(data)
        enc = path.with_name(path.name + ENC_SUFFIX)
        if enc.exists():
            enc.unlink()
        return path

    def _read_json(self, path: Path) -> Optional[dict]:
        """Read a plaintext ``path`` or its ``.enc`` sibling (decrypting the
        latter), returning the parsed dict or None when neither exists."""
        enc = path.with_name(path.name + ENC_SUFFIX)
        if enc.is_file():
            from openadapt_flow import crypto as _crypto

            decrypted = _crypto.decrypt_bytes(
                enc.read_bytes(), self.key, aad=_crypto.CHECKPOINT_AAD
            )
            return json.loads(decrypted)  # type: ignore[no-any-return]
        if path.is_file():
            return json.loads(path.read_text())  # type: ignore[no-any-return]
        return None

    # -- manifest ------------------------------------------------------------

    def write_manifest(self, manifest: RunManifest) -> Path:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        return self._write_model(self.checkpoints_dir / MANIFEST_FILENAME, manifest)

    def read_manifest(self) -> Optional[RunManifest]:
        raw = self._read_json(self.checkpoints_dir / MANIFEST_FILENAME)
        return RunManifest.model_validate(raw) if raw is not None else None

    # -- checkpoints ---------------------------------------------------------

    @staticmethod
    def _safe(step_id: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in step_id)

    def _checkpoint_path(self, checkpoint: RunCheckpoint) -> Path:
        name = f"step_{checkpoint.step_index:04d}_{self._safe(checkpoint.step_id)}.json"
        return self.checkpoints_dir / name

    def write_checkpoint(self, checkpoint: RunCheckpoint) -> Path:
        """Persist a verified-step checkpoint.

        Idempotent per step index: re-writing the same index overwrites the
        file, never appends a duplicate -- so a resume that re-verifies a step
        cannot produce two checkpoints for it.
        """
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        return self._write_model(self._checkpoint_path(checkpoint), checkpoint)

    def checkpoints(self) -> list[RunCheckpoint]:
        """All checkpoints, ordered by step index (plaintext or encrypted)."""
        if not self.checkpoints_dir.is_dir():
            return []
        out: list[RunCheckpoint] = []
        seen: set[str] = set()
        for path in sorted(self.checkpoints_dir.glob("step_*.json*")):
            # A step's plaintext base name; the .enc sibling maps to the same
            # base so _read_json picks whichever exists (no double-counting).
            base = (
                path.name[: -len(ENC_SUFFIX)]
                if path.name.endswith(ENC_SUFFIX)
                else path.name
            )
            if base in seen:
                continue
            seen.add(base)
            raw = self._read_json(self.checkpoints_dir / base)
            if raw is not None:
                out.append(RunCheckpoint.model_validate(raw))
        out.sort(key=lambda c: c.step_index)
        return out

    def last_checkpoint(self) -> Optional[RunCheckpoint]:
        """The highest-index verified checkpoint, or None if nothing verified."""
        checkpoints = self.checkpoints()
        return checkpoints[-1] if checkpoints else None

    # -- pending escalation --------------------------------------------------

    def _pending_path(self) -> Path:
        return self.run_dir / PENDING_FILENAME

    def write_pending(self, pending: PendingEscalation) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return self._write_model(self._pending_path(), pending)

    def read_pending(self) -> Optional[PendingEscalation]:
        raw = self._read_json(self._pending_path())
        return PendingEscalation.model_validate(raw) if raw is not None else None

    def clear_pending(self) -> None:
        """Remove a resolved pending escalation (called when a resume starts)."""
        for path in (
            self._pending_path(),
            self._pending_path().with_name(PENDING_FILENAME + ENC_SUFFIX),
        ):
            if path.is_file():
                path.unlink()

    # -- program (Phase-2 state-machine) checkpoints -------------------------

    def _program_checkpoint_path(self, checkpoint: ProgramCheckpoint) -> Path:
        name = f"{PROGRAM_CHECKPOINT_PREFIX}{checkpoint.seq:04d}.json"
        return self.checkpoints_dir / name

    def write_program_checkpoint(self, checkpoint: ProgramCheckpoint) -> Path:
        """Persist a verified-state interpreter checkpoint (Phase-2 program run).

        Idempotent per ``seq``: re-writing the same sequence overwrites the file
        rather than appending a duplicate. Sealed at rest when a key is
        configured (the interpreter frame carries run params + effect contracts,
        so it gets the same AEAD control as the linear checkpoints)."""
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        return self._write_model(self._program_checkpoint_path(checkpoint), checkpoint)

    def program_checkpoints(self) -> list[ProgramCheckpoint]:
        """All Phase-2 interpreter checkpoints, ordered by ``seq`` (plaintext or
        encrypted)."""
        if not self.checkpoints_dir.is_dir():
            return []
        out: list[ProgramCheckpoint] = []
        seen: set[str] = set()
        for path in sorted(
            self.checkpoints_dir.glob(f"{PROGRAM_CHECKPOINT_PREFIX}*.json*")
        ):
            base = (
                path.name[: -len(ENC_SUFFIX)]
                if path.name.endswith(ENC_SUFFIX)
                else path.name
            )
            if base in seen:
                continue
            seen.add(base)
            raw = self._read_json(self.checkpoints_dir / base)
            if raw is not None:
                out.append(ProgramCheckpoint.model_validate(raw))
        out.sort(key=lambda c: c.seq)
        return out

    def last_program_checkpoint(self) -> Optional[ProgramCheckpoint]:
        """The highest-``seq`` interpreter checkpoint (the resume point), or None
        when the run is not a program run / nothing verified yet."""
        checkpoints = self.program_checkpoints()
        return checkpoints[-1] if checkpoints else None

    def completed_effect_keys(self) -> list[str]:
        """Every already-CONFIRMED effect's contract hash across the program run
        (the union of each checkpoint's ``new_effect_keys``) -- the idempotency
        ledger a resume consults so it never re-performs a confirmed write."""
        keys: list[str] = []
        for cp in self.program_checkpoints():
            keys.extend(cp.new_effect_keys)
        return keys

    def completed_effects(self) -> list[dict]:
        """Every already-CONFIRMED effect contract (resolved ``Effect`` dumps)
        across the program run -- so a resume can re-verify (read-only) that the
        already-confirmed writes still hold before restoring the interpreter."""
        effects: list[dict] = []
        for cp in self.program_checkpoints():
            effects.extend(cp.new_effects)
        return effects

    def completed_unverified_effect_keys(self) -> list[str]:
        """Already-performed, explicitly approved effect hashes.

        These prevent duplicate re-execution but are never treated as
        independently CONFIRMED and are never passed to record revalidation.
        """
        keys: list[str] = []
        for checkpoint in self.program_checkpoints():
            keys.extend(checkpoint.new_unverified_effect_keys)
        return keys

    # -- approval (authenticated resume authorization; P0-5) -----------------

    def _approval_path(self) -> Path:
        return self.run_dir / APPROVAL_FILENAME

    def write_approval(self, approval: ApprovalRecord) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return self._write_model(self._approval_path(), approval)

    def read_approval(self) -> Optional[ApprovalRecord]:
        raw = self._read_json(self._approval_path())
        return ApprovalRecord.model_validate(raw) if raw is not None else None

    def clear_approval(self) -> None:
        """Remove a consumed approval (called when a resume completes)."""
        for path in (
            self._approval_path(),
            self._approval_path().with_name(APPROVAL_FILENAME + ENC_SUFFIX),
        ):
            if path.is_file():
                path.unlink()
