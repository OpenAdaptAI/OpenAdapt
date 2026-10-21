"""Guarded save: Flow's transaction contract around one write by any agent.

A computer-use agent, a script, or a person does the typing and clicks Save.
The guard does three things around that one write and makes no model calls:

* :meth:`GuardedSave.begin` checks that the record the task names is the one
  open on the screen (identity gate), then reads the system of record so a new
  row can be told from an old one (the verify-run baseline).
* :meth:`Guard.commit` holds the write until a person approves it, when the
  approval mode says so, using the same ``item-approval`` files Desktop and
  the phone portal answer. It then records, once per task, that the write may
  now happen.
* :meth:`Guard.confirm` reads the system of record again, independently of the
  screen, through :func:`openadapt_flow.verify_run.verify_run`, and returns
  ``VERIFIED``, ``REFUTED`` or ``RECONCILIATION_REQUIRED`` with an
  ``openadapt.effect-receipt/v1`` receipt.

When the task says where its values came from (``provenance`` and the
``sources`` it cites), :meth:`GuardedSave.begin` reads the sources a second,
independent way. A value the second reading does not confirm sends the save
to a person, whatever the approval mode, and :meth:`Guard.confirm` compares
the saved record with the source document, not only with the task.

Once a task has been committed it is never committed again. ``VERIFIED`` means
it is done; any other verdict means a person must look at the record, and
:meth:`GuardedSave.begin` refuses the task until they do.

The task (which record, which values) comes from whoever assigned the work,
not from the agent: the agent can't choose what it is checked against.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from openadapt_flow.runtime.identity import normalize_structured
from openadapt_flow.verify_run import (
    VerifyConfig,
    capture_baseline,
    load_verify_config,
    verify_run,
    write_result,
)

GUARD_TASK_SCHEMA = "openadapt.guard-task/v1"
GUARD_RECORD_SCHEMA = "openadapt.guarded-save/v1"
APPROVAL_MODES = ("each_item", "not_gated")
ApprovalSetting = Literal["each_item", "not_gated"]

_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_TASK_KEYS = {"schema_version", "task_id", "expected_record", "intended_fields"}
_TASK_OPTIONAL = {"authorization_ref", "provenance", "sources"}


class GuardVerdict(str, Enum):
    """What the system of record shows after a committed write."""

    #: The intended record, exactly as declared, read from the system of record.
    VERIFIED = "VERIFIED"
    #: The store was read and shows a different write (a duplicate, a wrong
    #: value). Don't retry; a person fixes the record.
    REFUTED = "REFUTED"
    #: The store doesn't show the write, or couldn't be read. The write may
    #: still land, so a retry could save it twice. Don't retry; a person checks.
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class GuardRefused(Exception):
    """The guard refused a step. Nothing may be written. ``code`` is closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GuardTask:
    """One write, as the person or system that assigned it describes it.

    ``expected_record`` names the record the write belongs to (a patient id,
    a name, a date of birth). Every value must be visible on the open record
    before the write. ``intended_fields`` holds the values the write saves.
    Both are bound by name into the effect contract in ``verify.yaml``.
    """

    task_id: str
    expected_record: Mapping[str, str]
    intended_fields: Mapping[str, Any]
    authorization_ref: str = ""
    #: Where each value came from (``openadapt.param-provenance/v1``).
    provenance: Optional[Any] = None
    #: The source documents the provenance cites, by ``sha256:`` of their bytes.
    documents: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not _OPAQUE.match(self.task_id):
            raise GuardRefused(
                "task_invalid", "task_id must be an opaque id (8-128 of A-Za-z0-9._:-)"
            )
        if not self.expected_record:
            raise GuardRefused("task_invalid", "expected_record names no field")
        for name, value in self.expected_record.items():
            if not isinstance(value, str) or not normalize_structured(value):
                raise GuardRefused(
                    "task_invalid", f"expected_record.{name} must be non-empty text"
                )
        overlap = set(self.expected_record) & set(self.intended_fields)
        if overlap:
            raise GuardRefused(
                "task_invalid",
                "a field is in both expected_record and intended_fields: "
                + ", ".join(sorted(overlap)),
            )

    @property
    def params(self) -> dict[str, Any]:
        return {**self.expected_record, **self.intended_fields}

    @property
    def authorization(self) -> str:
        return self.authorization_ref or self.task_id


def load_task(path: str | Path) -> GuardTask:
    """Read an ``openadapt.guard-task/v1`` JSON file."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardRefused("task_unreadable", f"cannot read task {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != GUARD_TASK_SCHEMA:
        raise GuardRefused("task_invalid", f"task must be an {GUARD_TASK_SCHEMA} file")
    if not _TASK_KEYS <= set(raw) <= _TASK_KEYS | _TASK_OPTIONAL:
        raise GuardRefused(
            "task_invalid", "task must hold exactly " + ", ".join(sorted(_TASK_KEYS))
        )
    if not isinstance(raw["expected_record"], dict) or not isinstance(
        raw["intended_fields"], dict
    ):
        raise GuardRefused(
            "task_invalid", "expected_record and intended_fields are objects"
        )
    provenance = None
    documents: dict[str, Any] = {}
    if "provenance" in raw:
        from openadapt_flow.provenance import ProvenanceFile, read_sources

        try:
            provenance = ProvenanceFile.model_validate(raw["provenance"])
            sources = raw.get("sources") or []
            if not isinstance(sources, list):
                raise ValueError("sources must be a list of paths")
            base = Path(path).resolve().parent
            documents = read_sources([base / str(p) for p in sources])
        except (OSError, ValueError) as exc:
            raise GuardRefused(
                "task_invalid", f"provenance or a source can't be read: {exc}"
            ) from exc
    elif "sources" in raw:
        raise GuardRefused("task_invalid", "sources needs provenance")
    task = GuardTask(
        task_id=raw["task_id"],
        expected_record=dict(raw["expected_record"]),
        intended_fields=dict(raw["intended_fields"]),
        authorization_ref=str(raw.get("authorization_ref") or ""),
        provenance=provenance,
        documents=documents,
    )
    if provenance is not None and provenance.matches(task.params):
        raise GuardRefused(
            "task_invalid",
            "the task's values differ from the values its provenance records: "
            + ", ".join(provenance.matches(task.params)),
        )
    return task


def check_identity(
    expected_record: Mapping[str, str], screen_text: Optional[str]
) -> tuple[Literal["verified", "mismatch", "unreadable"], list[str]]:
    """Whether every expected value is on the open record's screen text.

    ``screen_text`` is the structured text of what is open (an accessibility
    tree, the DOM's text, or OCR), read by the agent's harness, not described
    by the model. Each value must appear as a whole token run after
    whitespace and case are normalized (:func:`normalize_structured`, no OCR
    folding), so ``p1`` does not match ``p12`` and ``Jane Sample`` does not
    match ``Jane Sampleton``. Returns the status and the names of the fields
    that were not found. Values are never returned.
    """

    text = normalize_structured(screen_text or "")
    if not text:
        return "unreadable", sorted(expected_record)
    missing = []
    for name, value in expected_record.items():
        needle = re.escape(normalize_structured(value))
        if not re.search(rf"(?<!\w){needle}(?!\w)", text):
            missing.append(name)
    return ("mismatch" if missing else "verified"), sorted(missing)


def _sha256_hex(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json(
    path: Path, payload: Mapping[str, Any], *, exclusive: bool = False
) -> None:
    """Write 0600 JSON. ``exclusive`` raises FileExistsError if it exists."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


@dataclass
class CommitResult:
    """``go``: the write may happen now. ``waiting_for_approval``: not yet."""

    status: Literal["go", "waiting_for_approval"]
    guard_id: str
    #: Set while waiting: what a person answers with
    #: ``openadapt-flow item-approval approve RUN_DIR --request-id ...``.
    approval_request_id: Optional[str] = None
    approval_request_sha256: Optional[str] = None
    approval_run_dir: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class GuardResult:
    """The verdict on one committed write. Never says to retry."""

    verdict: GuardVerdict
    guard_id: str
    task_id: str
    #: The receipt's ``outcome`` (``verified``, ``reconciliation_required``),
    #: or None when the store could not be read at all.
    outcome: Optional[str]
    receipt_path: Optional[str]
    receipt: Optional[dict[str, Any]] = None
    reason: Optional[str] = None
    retry_allowed: bool = False

    @property
    def what_to_do(self) -> str:
        if self.verdict is GuardVerdict.VERIFIED:
            return "Saved, and the system of record shows it. Nothing else to do."
        if self.verdict is GuardVerdict.REFUTED:
            return (
                "The system of record shows a different write than intended. "
                "Don't retry. A person must correct the record."
            )
        return (
            "The system of record doesn't show this save, or couldn't be read. "
            "It may still land. Don't retry. A person must check the record."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "guard_id": self.guard_id,
            "task_id": self.task_id,
            "outcome": self.outcome,
            "receipt_path": self.receipt_path,
            "reason": self.reason,
            "retry_allowed": self.retry_allowed,
            "what_to_do": self.what_to_do,
        }


@dataclass
class GuardedSave:
    """Wraps one write at a time in the identity, approval and read-back checks.

    ``verify`` is the ``verify.yaml`` verify-run reads: the system-of-record
    verifier and the effect contract the write must produce. ``state_dir``
    holds each guard's files (its private baseline, approval request and
    receipt) and the per-task ledger that stops a second commit. When it is
    the runs folder the console watches, or a folder directly inside it, the
    phone portal shows its approval requests.
    """

    verify: VerifyConfig
    state_dir: Path
    approval: ApprovalSetting = "each_item"

    def __post_init__(self) -> None:
        if self.approval not in APPROVAL_MODES:
            raise ValueError("approval must be each_item or not_gated")
        self.state_dir = Path(self.state_dir)

    @classmethod
    def from_file(
        cls, verify_yaml: str | Path, state_dir: str | Path, **kwargs: Any
    ) -> "GuardedSave":
        return cls(load_verify_config(verify_yaml), Path(state_dir), **kwargs)

    def _ledger(self, task_id: str) -> Path:
        return self.state_dir / "tasks" / f"{task_id}.json"

    def task_state(self, task_id: str) -> Optional[dict[str, Any]]:
        """The ledger entry for a committed task, or None if never committed."""

        try:
            return json.loads(self._ledger(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # Unreadable: treat as committed with an unknown result.
            return {"state": "unknown"}

    def begin(
        self,
        task: GuardTask,
        *,
        screen_text: Optional[str],
        agent_id: str = "unidentified-agent",
    ) -> "Guard":
        """Check the open record and read the baseline. Raises :class:`GuardRefused`."""

        prior = self.task_state(task.task_id)
        if prior is not None:
            if prior.get("verdict") == GuardVerdict.VERIFIED.value:
                raise GuardRefused(
                    "already_saved", "this task was already saved and verified"
                )
            raise GuardRefused(
                "needs_reconciliation",
                "this task was already committed and its result is not verified; "
                "a person must check the record before anything else is written",
            )
        guard_id = f"guard-{secrets.token_hex(16)}"
        guard = Guard(self, task, guard_id, agent_id=agent_id)
        if task.provenance is not None and task.provenance.has_documents():
            # A second, independent reading of the sources. A value it does
            # not confirm goes to a person before anything is written.
            from openadapt_flow.provenance import cross_check, default_extractor

            check = cross_check(
                task.provenance, task.documents, default_extractor(task.provenance)
            )
            guard.source_check = check.status
            guard.source_held_fields = check.disagreements
        status, missing = check_identity(task.expected_record, screen_text)
        guard.identity = status
        if status != "verified":
            guard._save("refused_identity", reason=f"identity_{status}")
            raise GuardRefused(
                f"identity_{status}",
                "the open record does not match the task"
                + (f" (not found: {', '.join(missing)})" if missing else "")
                + "; nothing may be written",
            )
        try:
            baseline = capture_baseline(
                self.verify, task.params, guard.dir / "baseline.json"
            )
            reachable = bool(baseline["state"].get("reachable"))
            problem = "" if reachable else "unreachable"
        except Exception as exc:  # a bad config, or a store that can't be reached
            problem = type(exc).__name__
        if problem:
            # Without a baseline, a new row can't be told from an old one, so
            # the write could never be verified. Refuse before it happens.
            guard._save("refused_baseline", reason="baseline_unreadable")
            raise GuardRefused(
                "baseline_unreadable",
                f"the system of record could not be read before the write ({problem})",
            )
        guard._save("begun")
        return guard


@dataclass
class Guard:
    """One guarded write: ``begun`` -> ``committed`` -> ``confirmed``."""

    owner: GuardedSave
    task: GuardTask
    guard_id: str
    agent_id: str = "unidentified-agent"
    state: str = "new"
    identity: str = "unchecked"
    approval: Optional[dict[str, str]] = None
    #: ``agree`` | ``hold`` | ``instruction``, or None without provenance.
    source_check: Optional[str] = None
    source_held_fields: list[str] = field(default_factory=list)
    result: Optional[GuardResult] = None
    _request: Any = field(default=None, repr=False)
    _approver: Optional[dict[str, str]] = field(default=None, repr=False)
    _policy_ref: str = field(default="", repr=False)

    @property
    def dir(self) -> Path:
        return self.owner.state_dir / self.guard_id

    # -- records ---------------------------------------------------------------
    def _record(self, reason: Optional[str] = None) -> dict[str, Any]:
        # PHI-free: ids, states, codes and digests only.
        return {
            "schema_version": GUARD_RECORD_SCHEMA,
            "guard_id": self.guard_id,
            "task_id": self.task.task_id,
            "state": self.state,
            "identity": self.identity,
            "approval_mode": self.owner.approval,
            "approval": self.approval,
            "source_check": self.source_check,
            "source_held_fields": list(self.source_held_fields),
            "params_sha256": _sha256_hex(self.task.params),
            "verdict": self.result.verdict.value if self.result else None,
            "outcome": self.result.outcome if self.result else None,
            "reason": reason,
            "model_calls": 0,
        }

    def _save(self, state: str, reason: Optional[str] = None) -> None:
        self.state = state
        _write_json(self.dir / "guard.json", self._record(reason))

    # -- commit ----------------------------------------------------------------
    def commit(
        self,
        *,
        screenshot_png: Optional[bytes] = None,
        screen_text: Optional[str] = None,
        wait_s: float = 0.0,
        reason: Optional[str] = None,
    ) -> CommitResult:
        """Allow the write, once, after approval when the mode asks for it.

        ``screen_text``, when given, re-checks the open record right before
        the write. ``screenshot_png`` is what a person approving sees; it is
        required under ``each_item``. ``wait_s`` is how long to wait for the
        answer; call again with the same guard to keep waiting. ``reason``
        is the agent's own account of why it is saving; the person sees it,
        labelled as the agent's words, with the screen text it was given and
        any of that text that reads like an instruction to an agent.
        """

        if self.state not in ("begun", "waiting_for_approval"):
            raise GuardRefused(
                "wrong_state", f"commit needs a begun guard; this one is {self.state}"
            )
        if screen_text is not None:
            status, missing = check_identity(self.task.expected_record, screen_text)
            if status != "verified":
                self.identity = status
                self._save("refused_identity", reason=f"identity_{status}")
                raise GuardRefused(
                    f"identity_{status}",
                    "the open record changed and no longer matches the task"
                    + (f" (not found: {', '.join(missing)})" if missing else "")
                    + "; nothing may be written",
                )
        if self.owner.approval == "each_item" or self.source_check == "hold":
            waiting = self._await_approval(
                screenshot_png, wait_s, reason=reason, screen_text=screen_text
            )
            if waiting is not None:
                return waiting
        try:
            _write_json(
                self.owner._ledger(self.task.task_id),
                {"guard_id": self.guard_id, "state": "committed", "verdict": None},
                exclusive=True,
            )
        except FileExistsError:
            self._save("refused_duplicate", reason="already_committed")
            raise GuardRefused(
                "already_committed",
                "another guard already committed this task; it is never written twice",
            ) from None
        self._save("committed")
        return CommitResult(status="go", guard_id=self.guard_id)

    def _await_approval(
        self,
        screenshot_png: Optional[bytes],
        wait_s: float,
        *,
        reason: Optional[str] = None,
        screen_text: Optional[str] = None,
    ) -> Optional[CommitResult]:
        from openadapt_flow.approval_mode.approver import DEFAULT_APPROVER_POLICY
        from openadapt_flow.approval_mode.item_approval import (
            ItemApprovalRefused,
            ItemApprovalStore,
            wait_for_answer,
        )
        from openadapt_flow.approval_mode.model import ApprovalMode
        from openadapt_flow.approval_mode.switch import canonical_json

        store = ItemApprovalStore(self.dir)
        if self._request is None:
            if not screenshot_png:
                raise GuardRefused(
                    "screenshot_required",
                    "a person approves this save; commit needs the screenshot "
                    "they will be shown",
                )
            try:
                # Person-only, pinned before anything is asked; the agent that
                # started this guard is recorded so it can never answer it.
                store.write_policy(DEFAULT_APPROVER_POLICY)
                store.write_initiator(f"agent:{self.agent_id}")
                self._request = store.open_request(
                    run_id=self.guard_id,
                    workflow_name="guarded-save",
                    step_id="save",
                    step_index=0,
                    step_intent=(
                        "Save "
                        + ", ".join(sorted(self.task.intended_fields))
                        + (
                            ". A second reading of the source did not confirm: "
                            + ", ".join(self.source_held_fields)
                            + ". Compare them with the source document"
                            if self.source_check == "hold"
                            else ""
                        )
                    ),
                    mode=ApprovalMode.EACH_ITEM,
                    bundle_content_digest=None,
                    runtime_inputs_digest=_sha256_hex(self.task.params),
                    frame_png=screenshot_png,
                    form_state=("dom", "sha256:" + _sha256_hex(self.task.params)),
                    provenance=self._provenance(reason, screen_text),
                )
            except ItemApprovalRefused as exc:
                self._save("refused_approval", reason=exc.code)
                raise GuardRefused(exc.code, str(exc)) from exc
            self._policy_ref = (
                "sha256:"
                + hashlib.sha256(
                    canonical_json(DEFAULT_APPROVER_POLICY.model_dump(mode="json"))
                ).hexdigest()
            )
        request = self._request
        answer, code = wait_for_answer(store, request, timeout_s=max(0.0, wait_s))
        if code == "approval_wait_timed_out":
            self._save("waiting_for_approval")
            return CommitResult(
                status="waiting_for_approval",
                guard_id=self.guard_id,
                approval_request_id=request.request_id,
                approval_request_sha256=request.digest(),
                approval_run_dir=str(self.dir),
            )
        if answer is None or answer.decision != "approve":
            reason = code or "declined"
            self._save("declined", reason=reason)
            raise GuardRefused(
                reason, "the save was not approved; nothing may be written"
            )
        if not store.consume(request.request_id):
            self._save("refused_approval", reason="approval_already_used")
            raise GuardRefused(
                "approval_already_used", "this approval was already used"
            )
        self.approval = {
            "request_sha256": request.digest(),
            "answer_sha256": answer.digest(),
            "decided_by": answer.decided_by,
            "route": answer.route,
        }
        self._approver = {
            "kind": "person",
            "id": answer.approver_id or "unauthenticated-person",
            "policy_ref": self._policy_ref,
        }
        return None

    def _provenance(
        self, reason: Optional[str], screen_text: Optional[str]
    ) -> dict[str, Any]:
        # Local only, like the screenshot; inside the request digest.
        from openadapt_flow.teach_scope import provenance_for

        prov = provenance_for(
            actor=f"agent:{self.agent_id}",
            reason=reason,
            seen_text=screen_text,
        )
        prov["task_scope"] = (
            f"record {', '.join(sorted(self.task.expected_record))}; "
            f"fields {', '.join(sorted(self.task.intended_fields)) or 'none'}"
        )[:512]
        return prov

    # -- confirm ---------------------------------------------------------------
    def confirm(self) -> GuardResult:
        """Read the system of record and rule on the write. Never retries it."""

        if self.result is not None:
            return self.result
        if self.state != "committed":
            raise GuardRefused(
                "wrong_state",
                f"confirm needs a committed guard; this one is {self.state}",
            )
        receipt_doc: Optional[dict[str, Any]] = None
        receipt_path: Optional[str] = None
        reason: Optional[str] = None
        try:
            receipt_doc = verify_run(
                self.owner.verify,
                params=self.task.params,
                run_record={"executor_kind": "agent", "executor_id": self.agent_id},
                authorization_ref=self.task.authorization,
                baseline_path=self.dir / "baseline.json",
                private_dir=self.dir,
                approver=self._approver,
                correlation_id=self.guard_id,
                write_attempted=True,
                provenance=self.task.provenance,
                documents=self.task.documents,
            )
            receipt_path = str(write_result(receipt_doc, self.dir))
        except Exception as exc:  # unreadable store, timeout, bad config
            reason = "readback_failed:" + type(exc).__name__
        verdict = _verdict(receipt_doc)
        self.result = GuardResult(
            verdict=verdict,
            guard_id=self.guard_id,
            task_id=self.task.task_id,
            outcome=receipt_doc["receipt"]["outcome"] if receipt_doc else None,
            receipt_path=receipt_path,
            receipt=receipt_doc,
            reason=reason,
        )
        _write_json(
            self.owner._ledger(self.task.task_id),
            {"guard_id": self.guard_id, "state": "confirmed", "verdict": verdict.value},
        )
        self._save("confirmed", reason)
        return self.result


def _verdict(doc: Optional[Mapping[str, Any]]) -> GuardVerdict:
    if doc is None:
        return GuardVerdict.RECONCILIATION_REQUIRED
    receipt = doc["receipt"]
    if receipt["outcome"] == "verified":
        return GuardVerdict.VERIFIED
    rows = doc["evidence"]["readback"]["effects"]
    # REFUTED only when the store shows a write; absent or unknown is uncertain.
    if receipt["effect_verdict"] == "REFUTED" and any(
        row["observed_effect"] in ("present", "conflicting") for row in rows
    ):
        return GuardVerdict.REFUTED
    return GuardVerdict.RECONCILIATION_REQUIRED


__all__ = [
    "GUARD_TASK_SCHEMA",
    "CommitResult",
    "Guard",
    "GuardRefused",
    "GuardResult",
    "GuardTask",
    "GuardVerdict",
    "GuardedSave",
    "check_identity",
    "load_task",
]
