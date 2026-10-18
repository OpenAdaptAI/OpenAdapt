"""One admission command: compile, induce, propose checks, stop once, qualify.

``openadapt-flow admit <recordings...>`` (or ``--candidate ID`` for a
shadow-mined candidate) chains what used to be four manual steps:

1. compile each recording (:func:`~openadapt_flow.compiler.compile_recording`);
2. induce one program over them when there are two or more
   (:func:`~openadapt_flow.compiler.induction.induce_program`);
3. propose the qualification checks from the compiled demo
   (:func:`~openadapt_flow.qualification_proposal.propose_qualification`);
4. stop **once**, with a short summary, for a decision; on accept, apply the
   checks through :func:`~openadapt_flow.qualification_proposal.accept_proposal`,
   which re-runs the oracle gate, and reseal the bundle.

The decision can come from a person at a terminal, or from a delegated agent
approver over MCP (``openadapt-agent serve --delegate-approvals``) acting
under an approver grant a person signed (``openadapt-flow approver-grant
sign``). Either way the decision record names who decided, by kind, id and
route, and for an agent the grant's digest and the person who signed it.

Accepting admits the workflow to the ``problems_only`` approval mode: its runs
stop only for problems (a failed check, a changed screen, a step outside what
was qualified) instead of before every save. :func:`admitted_approval_mode`
is what ``openadapt-flow run`` reads. It applies only to the exact bundle that
was admitted, and for an agent's admission only while the grant still
verifies. ``problems_only`` keeps all of its own gates: until the switch is
earned and on, every save still waits for a person.

What it never does:

* weaken a gate. Induction that refuses, a proposal that HALTs, and an oracle
  gate that fails all stop the command with nothing to accept. Accepting runs
  the same ``accept_proposal`` as ``qualify accept``; certification still needs
  ``qualify campaign`` to pass.
* let the preparer decide as an agent. An agent can't accept an admission it
  prepared, an agent can't decide under the ``regulated`` policy pack, and a
  decision binds the exact summary (``pending_sha256``) and bundle digest it
  was shown.
* treat shadow-suggested checks as evidence. They are listed for the person
  or approver to read; only checks mined from real recordings are applied.
* sign an admission. No local-dev or production admission is minted here.
* let an agent admit without a person's signature. An agent decides only
  under a verified grant that names it, covers this workflow, and lets an
  agent approve consequential saves; anything less is refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

PENDING_FILE = "admission-pending.json"
DECISION_FILE = "admission-decision.json"
PROPOSAL_FILE = "qualification-proposal.draft.json"
BUNDLE_DIR = "bundle"
COMPILED_DIR = "compiled"
CANDIDATE_RECORDINGS_DIR = "recordings"
PENDING_SCHEMA: Literal["openadapt.admission-pending/v1"] = (
    "openadapt.admission-pending/v1"
)
DECISION_SCHEMA: Literal["openadapt.admission-decision/v1"] = (
    "openadapt.admission-decision/v1"
)
#: Opaque decider ids, the same shape Flow accepts for a delegated approver.
DECIDER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$"
#: Policy packs under which only a person may decide.
PERSON_ONLY_PACKS = frozenset({"regulated"})
#: The approval mode an accepted admission gives the workflow's runs.
ADMITTED_APPROVAL_MODE: Literal["problems_only"] = "problems_only"
_SHA256 = r"^sha256:[0-9a-f]{64}$"

PendingStatus = Literal["needs_decision", "needs_recording", "halted"]


class AdmissionError(ValueError):
    """The admission can't proceed; nothing was accepted."""


class Decider(BaseModel):
    """Who prepared or decided an admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["person", "agent"]
    id: str = Field(pattern=DECIDER_ID_PATTERN)
    route: Literal["cli", "mcp"]
    #: For an agent that decided: the digest of the person-signed approver
    #: grant it decided under. Left out of the record for a person.
    policy_ref: Optional[str] = Field(default=None, pattern=_SHA256)
    #: For an agent that decided: the registered person who signed that grant.
    on_behalf_of: Optional[str] = Field(default=None, pattern=DECIDER_ID_PATTERN)

    @model_serializer(mode="wrap")
    def _compact(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in ("policy_ref", "on_behalf_of"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def _grant_only_for_an_agent(self) -> "Decider":
        if self.kind == "person" and (
            self.policy_ref is not None or self.on_behalf_of is not None
        ):
            raise ValueError("a person decides for themselves, under no grant")
        if (self.policy_ref is None) != (self.on_behalf_of is None):
            raise ValueError("a grant names both its digest and its signer")
        return self

    def label(self) -> str:
        text = f"{self.kind}:{self.id} via {self.route}"
        if self.policy_ref is not None:
            short = self.policy_ref.removeprefix("sha256:")[:12]
            text += f" under grant {short} signed by {self.on_behalf_of}"
        return text


class PendingAdmission(BaseModel):
    """What the decider is shown, written before the single stop."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.admission-pending/v1"] = PENDING_SCHEMA
    admission_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: PendingStatus
    halt_reason: Optional[str] = None
    workflow_name: str
    policy_pack: str
    prepared_by: Decider
    created_at: str
    recordings: int = 0
    induced: bool = False
    candidate_motif_id: Optional[str] = None
    n_steps: int = 0
    n_writes: int = 0
    pins: list[dict[str, str]] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    failure_cases: int = 0
    shadow_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    record_next: list[dict[str, Any]] = Field(default_factory=list)
    bundle_content_digest: Optional[str] = None
    proposal_sha256: Optional[str] = None

    def sha256(self) -> str:
        return _sha(self.model_dump(mode="json"))


class AdmissionDecision(BaseModel):
    """The decision record. Names who decided, and binds what they saw."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.admission-decision/v1"] = DECISION_SCHEMA
    admission_id: str
    decision: Literal["accept", "reject"]
    reason: Optional[str] = None
    decided_by: Decider
    prepared_by: Decider
    decided_at: str
    pending_sha256: str
    bundle_content_digest: Optional[str]
    proposal_sha256: Optional[str]
    accepted_bundle_content_digest: Optional[str] = None
    oracle_gate_passed: Optional[bool] = None
    next: list[str] = Field(default_factory=list)
    #: The workflow the admission names, for the grant's workflow scope.
    workflow_name: Optional[str] = None
    #: Set on accept: the approval mode the admitted workflow's runs use.
    approval_mode: Optional[Literal["problems_only"]] = None
    #: The person-signed grant an agent decided under, kept whole so a run
    #: re-verifies it. ``None`` for a person's decision.
    approver_grant: Optional[dict[str, Any]] = None


def _sha(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bundle_digest(bundle: Path) -> str:
    from openadapt_flow.bundle_validation import (
        compute_content_digest,
        compute_file_hashes,
    )
    from openadapt_flow.ir import Workflow

    workflow = Workflow.load(bundle)
    return compute_content_digest(workflow, compute_file_hashes(workflow, bundle))


def _is_bundle(path: Path) -> bool:
    return (path / "workflow.json").is_file() or (path / "workflow.json.enc").is_file()


def resolve_candidate(candidate: str, root: Path | str) -> Path:
    """A candidate id (``motif_id``) under ``root``, or a candidate folder."""

    from openadapt_flow.compiler.motif_induction import CANDIDATE_FILENAME

    direct = Path(candidate)
    if (direct / CANDIDATE_FILENAME).is_file():
        return direct
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", candidate):
        raise AdmissionError("a candidate id is one folder name under the root")
    folder = Path(root) / candidate
    if not (folder / CANDIDATE_FILENAME).is_file():
        raise AdmissionError(f"no shadow candidate {candidate!r} under {root}")
    return folder


def _load_candidate(folder: Path) -> dict[str, Any]:
    from openadapt_flow.compiler.motif_induction import CANDIDATE_FILENAME

    data = json.loads((folder / CANDIDATE_FILENAME).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("admitted") is not False:
        raise AdmissionError("candidate.json is not an unadmitted shadow candidate")
    return data


def _checks_from_proposal(proposal: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in proposal.pin("effect").get("payload", {}).get("effects") or []:
        out.append(
            {
                "step_id": raw.get("step_id"),
                "kind": raw.get("kind"),
                "tier": raw.get("tier"),
            }
        )
    for raw in proposal.pin("identity").get("payload", {}).get("policies") or []:
        out.append(
            {
                "step_id": raw.get("step_id"),
                "kind": f"identity:{raw.get('enforcement')}",
            }
        )
    return out


def prepare_admission(
    inputs: Sequence[Path | str],
    *,
    out: Path | str,
    name: str,
    prepared_by: Decider,
    candidate: Optional[Path | str] = None,
    policy_pack: str = "community",
    admission_id: Optional[str] = None,
) -> PendingAdmission:
    """Run compile, induce and the check proposal; write the single stop.

    ``inputs`` are recording directories, or already-compiled bundles. With
    ``candidate`` and no inputs, the recordings are read from the candidate's
    ``recordings/`` folder; with none there, the result is
    ``needs_recording`` and carries the candidate's record-next list.
    """

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.compiler.induction import induce_program
    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualification_proposal import (
        propose_qualification,
        write_proposal,
    )
    from openadapt_flow.traversal import iter_workflow_steps

    target = Path(out)
    if (target / PENDING_FILE).exists() or (target / DECISION_FILE).exists():
        raise AdmissionError(f"{target} already holds an admission; use a new --out")
    target.mkdir(parents=True, exist_ok=True)

    paths = [Path(item) for item in inputs]
    motif_id: Optional[str] = None
    suggestions: list[dict[str, Any]] = []
    candidate_next: list[dict[str, Any]] = []
    if candidate is not None:
        folder = Path(candidate)
        data = _load_candidate(folder)
        motif_id = str(data.get("motif_id") or folder.name)
        suggestions = [
            {
                "kind": item.get("kind"),
                "summary": item.get("summary"),
                "step_id": item.get("step_id"),
                "note": "suggested by shadow episodes; not evidence",
            }
            for item in data.get("proposed_checks") or []
        ]
        candidate_next = list(data.get("record_next") or [])
        if not paths:
            rec_root = folder / CANDIDATE_RECORDINGS_DIR
            if rec_root.is_dir():
                paths = sorted(p for p in rec_root.iterdir() if p.is_dir())

    base: dict[str, Any] = {
        "admission_id": admission_id or uuid.uuid4().hex,
        "workflow_name": name,
        "policy_pack": policy_pack,
        "prepared_by": prepared_by,
        "created_at": _now(),
        "candidate_motif_id": motif_id,
        "shadow_suggestions": suggestions,
    }

    def stop(status: PendingStatus, reason: str, **extra: Any) -> PendingAdmission:
        pending = PendingAdmission(status=status, halt_reason=reason, **base, **extra)
        _write_json(target / PENDING_FILE, pending.model_dump(mode="json"))
        return pending

    if not paths:
        return stop(
            "needs_recording",
            "no recording yet: record this task once with OpenAdapt Capture",
            record_next=candidate_next
            or [{"code": "record", "instruction": "Record the task once."}],
        )

    compiled: list[Path] = []
    recording_meta: Optional[Path] = None
    for index, path in enumerate(paths):
        if not path.is_dir():
            raise AdmissionError(f"{path} is not a directory")
        if recording_meta is None and (path / "meta.json").is_file():
            recording_meta = path
        if _is_bundle(path):
            compiled.append(path)
            continue
        bundle = target / COMPILED_DIR / f"{index:02d}"
        compile_recording(path, bundle, name=name)
        compiled.append(bundle)

    bundle_dir = target / BUNDLE_DIR
    induced = False
    if len(compiled) == 1:
        workflow = Workflow.load(compiled[0])
    else:
        result = induce_program([Workflow.load(p) for p in compiled])
        if not result.certified or result.workflow is None:
            return stop(
                "halted",
                "induction refused: the recordings don't pin down one program",
                recordings=len(compiled),
                record_next=[
                    item.model_dump(mode="json") for item in result.record_next
                ],
            )
        workflow = result.workflow
        induced = True
    workflow = workflow.model_copy(update={"name": name})
    _copy_templates(compiled, bundle_dir)
    workflow.save(bundle_dir)
    workflow = Workflow.load(bundle_dir)

    proposal = propose_qualification(
        workflow, recording_dir=recording_meta, policy_pack=policy_pack
    )
    write_proposal(proposal, target / PROPOSAL_FILE)
    steps = list(iter_workflow_steps(workflow))
    detail: dict[str, Any] = {
        "recordings": len(compiled),
        "induced": induced,
        "n_steps": len(steps),
        "n_writes": sum(1 for step in steps if step.risk == "irreversible"),
        "pins": [
            {"kind": str(pin.get("kind")), "status": str(pin.get("status"))}
            for pin in proposal.pins
        ],
        "checks": _checks_from_proposal(proposal),
        "failure_cases": len(proposal.failure_matrix),
        "bundle_content_digest": _bundle_digest(bundle_dir),
        "proposal_sha256": proposal.sha256(),
    }
    if proposal.status == "halted":
        return stop(
            "halted",
            proposal.halt_reason or "qualification proposal HALTed",
            **detail,
        )
    pending = PendingAdmission(status="needs_decision", **base, **detail)
    _write_json(target / PENDING_FILE, pending.model_dump(mode="json"))
    return pending


def _copy_templates(compiled: Sequence[Path], bundle_dir: Path) -> None:
    """Carry each compiled bundle's crops; the first bundle wins a name clash."""

    import shutil

    for source in compiled:
        templates = source / "templates"
        if not templates.is_dir():
            continue
        for item in sorted(templates.rglob("*")):
            if not item.is_file():
                continue
            dest = bundle_dir / "templates" / item.relative_to(templates)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)


def load_pending(out: Path | str) -> PendingAdmission:
    path = Path(out) / PENDING_FILE
    if not path.is_file():
        raise AdmissionError(f"no pending admission in {out}")
    return PendingAdmission.model_validate_json(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")


def _agent_identity(decided_by: Decider) -> Any:
    from openadapt_flow.approval_mode.approver import (
        ApproverAuth,
        ApproverIdentityV1,
        ApproverKind,
    )

    return ApproverIdentityV1(
        kind=ApproverKind.AGENT,
        auth=(
            ApproverAuth.MCP_SESSION
            if decided_by.route == "mcp"
            else ApproverAuth.LOCAL_CLI
        ),
        id=decided_by.id,
    )


def _grant_refusal(
    signed: Any,
    agent: Decider,
    workflow_name: str,
    *,
    key_dir: Optional[Path | str],
    now: Optional[datetime] = None,
) -> tuple[Optional[Any], Optional[str]]:
    """``(verified grant, None)`` or ``(None, closed reason)``."""

    from openadapt_flow.approval_mode.approver import StepClass
    from openadapt_flow.approval_mode.signed_policy import (
        GrantRefused,
        SignedApproverGrant,
        verify_grant,
    )

    if signed is None:
        return None, "approver_grant_missing"
    try:
        grant = (
            signed
            if isinstance(signed, SignedApproverGrant)
            else SignedApproverGrant.model_validate(signed)
        )
    except ValueError:
        return None, "approver_grant_unreadable"
    if key_dir is None:
        from openadapt_flow.jit.lifecycle import default_key_dir

        key_dir = default_key_dir()
    try:
        verified = verify_grant(grant, key_dir, now=now)
    except GrantRefused as exc:
        return None, exc.code
    if not verified.names(agent.id):
        return None, "delegate_not_in_grant"
    if not verified.covers(workflow_name):
        return None, "workflow_not_in_grant"
    # Admitting to problems_only lets consequential saves go ahead without a
    # person. An agent may grant that only where the person's grant already
    # lets an agent approve a consequential save itself.
    if verified.policy.refusal(StepClass.CONSEQUENTIAL, _agent_identity(agent)):
        return None, "grant_does_not_allow_agent_saves"
    if agent.policy_ref is not None and agent.policy_ref != verified.policy_ref:
        return None, "policy_ref_mismatch"
    return verified, None


def check_decider(
    pending: PendingAdmission,
    decided_by: Decider,
    *,
    approver_grant: Any = None,
    key_dir: Optional[Path | str] = None,
) -> Decider:
    """Refuse self-approval, and any agent decision a person didn't sign for.

    Returns the decider as recorded: an agent gains the digest of the grant
    it decided under and the person who signed it.
    """

    if decided_by.kind != "agent":
        return decided_by
    if pending.policy_pack in PERSON_ONLY_PACKS:
        raise AdmissionError(
            f"the {pending.policy_pack!r} policy pack needs a person to decide"
        )
    prepared = pending.prepared_by
    if prepared.kind == "agent" and prepared.id == decided_by.id:
        raise AdmissionError(
            "self-approval refused: the agent that prepared this admission "
            "can't decide it"
        )
    verified, refusal = _grant_refusal(
        approver_grant, decided_by, pending.workflow_name, key_dir=key_dir
    )
    if verified is None:
        raise AdmissionError(
            f"an agent admits a workflow only under an approver grant a person "
            f"signed that names it, covers this workflow and lets an agent "
            f"approve saves ({refusal}); see `openadapt-flow approver-grant sign`"
        )
    return decided_by.model_copy(
        update={"policy_ref": verified.policy_ref, "on_behalf_of": verified.signer}
    )


def decide_admission(
    out: Path | str,
    *,
    pending_sha256: str,
    decision: Literal["accept", "reject"],
    decided_by: Decider,
    reason: Optional[str] = None,
    approver_grant: Any = None,
    key_dir: Optional[Path | str] = None,
) -> AdmissionDecision:
    """Record one decision on the pending admission, and apply it on accept.

    An agent decides only with ``approver_grant``: a person-signed
    :class:`~openadapt_flow.approval_mode.signed_policy.SignedApproverGrant`
    that verifies against the signer registry in ``key_dir`` (default
    ``~/.openadapt/jit``). Accepting sets the workflow's approval mode to
    ``problems_only``.
    """

    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualification_proposal import (
        QualificationProposalError,
        accept_proposal,
        load_proposal,
        save_accepted_bundle,
    )

    target = Path(out)
    pending = load_pending(target)
    if (target / DECISION_FILE).exists():
        raise AdmissionError("this admission was already decided")
    if pending.sha256() != pending_sha256:
        raise AdmissionError(
            "the pending admission changed since it was shown; show it again"
        )
    if pending.status != "needs_decision":
        raise AdmissionError(
            f"nothing to decide: the admission is {pending.status}"
            + (f" ({pending.halt_reason})" if pending.halt_reason else "")
        )
    decided_by = check_decider(
        pending, decided_by, approver_grant=approver_grant, key_dir=key_dir
    )
    if decision not in ("accept", "reject"):
        raise AdmissionError("decision must be accept or reject")
    if decision == "reject" and not reason:
        raise AdmissionError("a rejection needs a reason")
    bundle = target / BUNDLE_DIR
    from openadapt_flow.bundle_validation import BundleIntegrityError

    try:
        current = _bundle_digest(bundle)
    except (OSError, ValueError, BundleIntegrityError) as exc:
        raise AdmissionError(
            f"the bundle changed since the summary was written: {exc}"
        ) from exc
    if current != pending.bundle_content_digest:
        raise AdmissionError("the bundle changed since the summary was written")

    record: dict[str, Any] = {
        "admission_id": pending.admission_id,
        "decision": decision,
        "reason": reason,
        "decided_by": decided_by,
        "prepared_by": pending.prepared_by,
        "decided_at": _now(),
        "pending_sha256": pending_sha256,
        "bundle_content_digest": pending.bundle_content_digest,
        "proposal_sha256": pending.proposal_sha256,
        "workflow_name": pending.workflow_name,
    }
    if decided_by.kind == "agent":
        from openadapt_flow.approval_mode.signed_policy import SignedApproverGrant

        signed = (
            approver_grant
            if isinstance(approver_grant, SignedApproverGrant)
            else SignedApproverGrant.model_validate(approver_grant)
        )
        record["approver_grant"] = signed.model_dump(mode="json", by_alias=True)
    if decision == "accept":
        proposal = load_proposal(target / PROPOSAL_FILE)
        if proposal.sha256() != pending.proposal_sha256:
            raise AdmissionError("the check proposal changed since it was shown")
        workflow = Workflow.load(bundle)
        try:
            accepted = accept_proposal(workflow, proposal)
        except QualificationProposalError as exc:
            raise AdmissionError(str(exc)) from exc
        save_accepted_bundle(workflow, bundle, proposal=accepted)
        gate = accepted.oracle_gate or {}
        record.update(
            accepted_bundle_content_digest=_bundle_digest(bundle),
            oracle_gate_passed=bool(gate.get("passed")),
            approval_mode=ADMITTED_APPROVAL_MODE,
            next=[
                "qualify campaign: run the qualification cases before this "
                "program can be certified",
                "runs use the problems_only approval mode: they stop only for "
                "problems once the switch is earned; until then each save "
                "still waits for a person",
            ],
        )
    result = AdmissionDecision(**record)
    payload = result.model_dump(mode="json")
    _write_json(target / DECISION_FILE, payload)
    if decision == "accept":
        _write_json(bundle / DECISION_FILE, payload)
    return result


class AdmittedMode(BaseModel):
    """What a run reads from the bundle's admission decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``problems_only`` when the admission applies; ``None`` otherwise.
    approval_mode: Optional[Literal["problems_only"]] = None
    admitted_by: Optional[Decider] = None
    #: Why a recorded admission doesn't apply, as a closed code.
    refusal: Optional[str] = None


def admitted_approval_mode(
    bundle: Path | str,
    *,
    key_dir: Optional[Path | str] = None,
    now: Optional[datetime] = None,
) -> AdmittedMode:
    """The approval mode an accepted admission gives this exact bundle.

    No admission, or a rejected one: no mode (the run's default applies). A
    recorded admission for a bundle that has since changed, or an agent's
    admission whose grant no longer verifies, names the reason and gives no
    mode. Never raises.
    """

    path = Path(bundle) / DECISION_FILE
    if not path.is_file() or path.is_symlink():
        return AdmittedMode()
    try:
        record = AdmissionDecision.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValueError):
        return AdmittedMode(refusal="admission_unreadable")
    if record.decision != "accept" or record.approval_mode is None:
        return AdmittedMode()
    try:
        current = _bundle_digest(Path(bundle))
    except Exception:  # noqa: BLE001 - an unreadable bundle never widens a run
        return AdmittedMode(refusal="admission_bundle_unreadable")
    if current != record.accepted_bundle_content_digest:
        return AdmittedMode(refusal="admission_bundle_changed")
    decider = record.decided_by
    if decider.kind == "agent":
        if record.workflow_name is None or decider.policy_ref is None:
            return AdmittedMode(refusal="approver_grant_missing")
        verified, refusal = _grant_refusal(
            record.approver_grant,
            decider,
            record.workflow_name,
            key_dir=key_dir,
            now=now,
        )
        if verified is None:
            return AdmittedMode(refusal=refusal)
    return AdmittedMode(approval_mode=record.approval_mode, admitted_by=decider)


def render_summary(pending: PendingAdmission) -> str:
    """The short summary shown at the single stop."""

    lines = [f"Admission {pending.admission_id[:12]}: {pending.workflow_name}"]
    if pending.candidate_motif_id:
        lines.append(f"  from shadow candidate {pending.candidate_motif_id}")
    lines.append(
        f"  {pending.recordings} recording(s)"
        + (", induced into one program" if pending.induced else "")
        + f"; {pending.n_steps} step(s), {pending.n_writes} write(s)"
    )
    if pending.status != "needs_decision":
        lines.append(f"  STOPPED ({pending.status}): {pending.halt_reason}")
        for item in pending.record_next:
            lines.append(f"  - record next: {item.get('instruction')}")
        return "\n".join(lines) + "\n"
    lines.append("  Proposed checks (applied on accept):")
    for check in pending.checks:
        tier = f", tier {check['tier']}" if check.get("tier") is not None else ""
        lines.append(f"  - {check.get('step_id')}: {check.get('kind')}{tier}")
    lines.append(f"  {pending.failure_cases} fault case(s) to qualify against")
    if pending.shadow_suggestions:
        lines.append("  Suggested by shadow episodes (not evidence, not applied):")
        for item in pending.shadow_suggestions:
            lines.append(f"  - {item.get('kind')}: {item.get('summary')}")
    lines.append(f"  Prepared by {pending.prepared_by.label()}")
    return "\n".join(lines) + "\n"


__all__ = [
    "ADMITTED_APPROVAL_MODE",
    "AdmissionDecision",
    "AdmittedMode",
    "admitted_approval_mode",
    "AdmissionError",
    "Decider",
    "PendingAdmission",
    "check_decider",
    "decide_admission",
    "load_pending",
    "prepare_admission",
    "render_summary",
    "resolve_candidate",
]
