"""Propose, confirm, or HALT a qualification contract from a compiled demo.

A person who can demonstrate the task gets a production-shaped contract
without a consulting ritual. The gates stay: identity, effect, and admission
are still required. This module fills the pins from the recording and the
compiled bundle, then waits for one operator confirmation. Refusing a pin
HALTs. Guessing is not a path.

The accepted project is the existing
:class:`~openadapt_flow.qualification.QualificationProject`. Local MockMed
quickstart can then sign a local-dev admission that production trust maps
refuse.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.compiler.induction import Proposer
from openadapt_flow.compiler.qualification_pins import (
    MinedQualificationPins,
    load_recording_meta,
    mine_qualification_pins,
)
from openadapt_flow.compiler.qualification_proposer import collect_suggestions
from openadapt_flow.ir import Workflow
from openadapt_flow.policy_packs import PolicyPackName, load_policy_pack
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    IdentityEnforcement,
    IdentityEvidenceSource,
    IdentityPolicy,
    IdentitySignalKey,
    IdentitySignalPolicy,
    QualificationCase,
    QualificationCaseKind,
    QualificationError,
    QualificationOutcome,
    add_case,
    init_project,
    save_qualified_workflow,
    set_action_classification,
    set_effect_policy,
    set_identity_policy,
    set_minimum_effect_tier,
    workflow_contract_sha256,
)
from openadapt_flow.qualification_dev_signer import (
    LocalDevAdmission,
    sign_local_dev_admission,
)
from openadapt_flow.qualification_oracle_gate import evaluate_oracle_gate
from openadapt_flow.traversal import iter_workflow_steps
from openadapt_flow.verification import VerificationTier

PROPOSAL_SCHEMA: Literal["openadapt.qualification-proposal/v1"] = (
    "openadapt.qualification-proposal/v1"
)
PinKind = Literal["application", "environment", "identity", "effect"]
ProposalStatus = Literal["draft", "halted", "accepted"]


class QualificationProposalError(QualificationError):
    """The proposal is incomplete, refused, or cannot be applied."""


class QualificationProposal(BaseModel):
    """Draft contract the operator confirms in one command."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.qualification-proposal/v1"] = PROPOSAL_SCHEMA
    status: ProposalStatus
    halt_reason: Optional[str] = None
    bundle_name: str
    policy_pack: PolicyPackName
    recording_present: bool
    has_parameters: bool
    pins: list[dict[str, Any]]
    failure_matrix: list[dict[str, Any]]
    created_at: str
    oracle_gate: Optional[dict[str, Any]] = None
    suggestions: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("pins")
    @classmethod
    def _four_pins(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kinds = [item.get("kind") for item in value]
        expected = ["application", "environment", "identity", "effect"]
        if kinds != expected:
            raise ValueError(
                "proposal must contain application, environment, identity, effect"
            )
        return value

    def pin(self, kind: PinKind) -> dict[str, Any]:
        for item in self.pins:
            if item.get("kind") == kind:
                return item
        raise QualificationProposalError(f"proposal has no {kind} pin")

    def sha256(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def proposer_from_name(name: str | None) -> Optional[Proposer]:
    """Resolve the optional CLI proposer. ``off`` / None -> absent."""

    if not name or name == "off":
        return None
    if name == "llm":
        from openadapt_flow.compiler.qualification_proposer import (
            LazyLLMQualificationProposer,
        )

        return LazyLLMQualificationProposer()
    raise QualificationProposalError(
        f"unknown qualification proposer {name!r}; known: off, llm"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("openadapt-flow")
    except PackageNotFoundError:
        from openadapt_flow import __version__

        return __version__


def _halted_from_pins(
    mined: MinedQualificationPins,
    *,
    bundle_name: str,
    policy_pack: PolicyPackName,
) -> Optional[str]:
    missing = [pin for pin in mined.pins if pin.status == "missing"]
    if not missing:
        return None
    reasons = [pin.halt_reason or pin.summary for pin in missing]
    return "; ".join(reasons)


def propose_qualification(
    workflow: Workflow,
    *,
    recording_dir: Path | str | None = None,
    policy_pack: str = "community",
    runtime_version: Optional[str] = None,
    proposer: Optional[Proposer] = None,
) -> QualificationProposal:
    """Emit a draft contract from the compiled demo. Missing pins HALT.

    The optional ``proposer`` may sketch an identity field or effect contract
    from sanitized recording metadata. Suggestions are flagged, never trusted,
    and still face the oracle gates. Tests pass with the proposer absent.
    """

    pack = load_policy_pack(policy_pack)
    mined = mine_qualification_pins(
        workflow,
        recording_dir=recording_dir,
        runtime_version=runtime_version or _package_version(),
    )
    suggestions = [
        item.model_dump(mode="json")
        for item in collect_suggestions(
            proposer,
            workflow,
            meta=load_recording_meta(recording_dir),
        )
    ]
    halt_reason = _halted_from_pins(
        mined, bundle_name=workflow.name, policy_pack=pack.name
    )
    if halt_reason is None and pack.require_system_of_record_effect:
        effect = next(pin for pin in mined.pins if pin.kind == "effect")
        if effect.status == "proposed" and not effect.payload.get("effects"):
            writes = [
                step
                for step in iter_workflow_steps(workflow)
                if step.risk == "irreversible"
            ]
            if writes:
                halt_reason = (
                    "effect pin is missing: this policy pack requires a "
                    "system-of-record oracle on each irreversible write"
                )
    gate = evaluate_oracle_gate(workflow)
    if gate.halt_reason:
        if halt_reason is None:
            halt_reason = gate.halt_reason
        elif gate.halt_reason not in halt_reason:
            halt_reason = f"{halt_reason}; {gate.halt_reason}"
    status: ProposalStatus = "halted" if halt_reason else "draft"
    return QualificationProposal(
        status=status,
        halt_reason=halt_reason,
        bundle_name=workflow.name,
        policy_pack=pack.name,
        recording_present=mined.recording_present,
        has_parameters=mined.has_parameters,
        pins=[pin.model_dump(mode="json") for pin in mined.pins],
        failure_matrix=[case.model_dump(mode="json") for case in mined.failure_cases],
        created_at=_now(),
        oracle_gate=gate.model_dump(mode="json"),
        suggestions=suggestions,
    )


def refuse_pin(proposal: QualificationProposal, kind: PinKind) -> QualificationProposal:
    """Mark a pin refused. The proposal HALTs; nothing is guessed."""

    pin = proposal.pin(kind)
    pin["status"] = "missing"
    pin["halt_reason"] = f"operator refused the {kind} pin"
    return proposal.model_copy(
        update={
            "status": "halted",
            "halt_reason": f"operator refused the {kind} pin; refusing to guess",
            "pins": list(proposal.pins),
        }
    )


def _require_draft(proposal: QualificationProposal) -> None:
    if proposal.status == "halted":
        raise QualificationProposalError(
            proposal.halt_reason or "qualification proposal HALTed"
        )
    if proposal.status != "draft":
        raise QualificationProposalError(
            f"qualification proposal status {proposal.status!r} is not draft"
        )
    for pin in proposal.pins:
        if pin.get("status") != "proposed":
            raise QualificationProposalError(
                f"{pin.get('kind')} pin is not proposed; refusing to guess"
            )


def _apply_identity(workflow: Workflow, pin: dict[str, Any]) -> None:
    policies = pin.get("payload", {}).get("policies") or []
    for raw in policies:
        enforcement = raw["enforcement"]
        if enforcement == "canonical_ladder":
            set_identity_policy(
                workflow,
                IdentityPolicy(
                    step_id=raw["step_id"],
                    enforcement=IdentityEnforcement.CANONICAL_LADDER,
                ),
            )
            continue
        signals = [
            IdentitySignalPolicy(
                key=IdentitySignalKey(item["key"]),
                source=IdentityEvidenceSource(item["source"]),
                extract_pattern=item.get("extract_pattern"),
                params=list(item.get("params") or []),
            )
            for item in raw.get("signals") or []
        ]
        set_identity_policy(
            workflow,
            IdentityPolicy(
                step_id=raw["step_id"],
                enforcement=IdentityEnforcement.SIGNAL_QUORUM,
                signals=signals,
                quorum=int(raw.get("quorum") or 1),
            ),
        )
    project = workflow.qualification
    assert project is not None
    for step_id, classification in list(project.action_classifications.items()):
        if (
            classification.classification
            in {
                ActionRiskClass.IRREVERSIBLE,
                ActionRiskClass.CONSEQUENTIAL,
                ActionRiskClass.STATE_CHANGING,
            }
            and not classification.operator_confirmed
        ):
            set_action_classification(
                workflow,
                ActionRiskClassification(
                    step_id=step_id,
                    classification=classification.classification,
                    explanation=classification.explanation,
                    operator_confirmed=True,
                ),
            )


def _apply_effects(workflow: Workflow, pin: dict[str, Any]) -> None:
    for raw in pin.get("payload", {}).get("effects") or []:
        set_effect_policy(
            workflow,
            step_id=raw["step_id"],
            effect_index=int(raw["effect_index"]),
            tier=VerificationTier(int(raw["tier"])),
            actuation_path=raw.get("actuation_path") or "gui",
        )


def _apply_failure_matrix(workflow: Workflow, proposal: QualificationProposal) -> None:
    existing = {
        case.id
        for case in (workflow.qualification.cases if workflow.qualification else [])
    }
    for raw in proposal.failure_matrix:
        case_id = raw["id"]
        if case_id in existing:
            continue
        add_case(
            workflow,
            QualificationCase(
                id=case_id,
                kind=QualificationCaseKind(raw["kind"]),
                description=raw.get("description") or "",
                expected_outcome=QualificationOutcome.HALTED,
            ),
        )


def accept_proposal(
    workflow: Workflow,
    proposal: QualificationProposal,
    *,
    replace: bool = False,
) -> QualificationProposal:
    """Apply every proposed pin. Refused or missing pins HALT.

    Re-runs the --break-it oracle gate and the channel-disjointness check.
    A banner-only oracle that would accept the lying backend stays draft/halted.
    """

    _require_draft(proposal)
    gate = evaluate_oracle_gate(workflow)
    proposal = proposal.model_copy(update={"oracle_gate": gate.model_dump(mode="json")})
    if not gate.passed:
        raise QualificationProposalError(
            gate.halt_reason or "qualification oracle gate HALTed"
        ) from None
    pack = load_policy_pack(proposal.policy_pack)
    application = proposal.pin("application")["payload"]
    environment = proposal.pin("environment")["payload"]
    boundary = EnvironmentBoundary(
        target_kind=application["target_kind"],
        application=application["application"],
        application_identity=application.get("application_identity"),
        application_version=application["application_version"],
        environment_digest=environment["environment_digest"],
        runtime_version=environment["runtime_version"],
        required_capabilities=list(environment.get("required_capabilities") or []),
    )
    init_project(
        workflow,
        environment=boundary,
        minimum_effect_tier=pack.minimum_effect_tier,
        replace=replace or workflow.qualification is not None,
    )
    set_minimum_effect_tier(workflow, pack.minimum_effect_tier)
    _apply_identity(workflow, proposal.pin("identity"))
    _apply_effects(workflow, proposal.pin("effect"))
    _apply_failure_matrix(workflow, proposal)
    for pin in proposal.pins:
        pin["status"] = "confirmed"
    return proposal.model_copy(
        update={
            "status": "accepted",
            "halt_reason": None,
            "pins": list(proposal.pins),
            "oracle_gate": gate.model_dump(mode="json"),
        }
    )


def admit_local_dev(
    workflow: Workflow,
    proposal: QualificationProposal,
    *,
    bundle_dir: Path | str,
) -> LocalDevAdmission:
    """Sign a local-dev admission after pins are confirmed.

    The signer cannot enter a production trust map. This is not Production.
    """

    pack = load_policy_pack(proposal.policy_pack)
    if not pack.allow_local_dev_signer:
        raise QualificationProposalError(
            f"policy pack {pack.name!r} does not allow the local-dev signer"
        )
    if proposal.status != "accepted":
        raise QualificationProposalError(
            "local-dev admission requires an accepted proposal; refusing to guess"
        )
    if workflow.qualification is None:
        raise QualificationProposalError(
            "initialize qualification before local-dev admission"
        )
    from openadapt_flow.bundle_validation import (
        compute_content_digest,
        compute_file_hashes,
    )

    bundle = Path(bundle_dir)
    file_hashes = (
        dict(workflow.manifest.file_hashes)
        if workflow.manifest is not None and workflow.manifest.file_hashes
        else compute_file_hashes(workflow, bundle)
    )
    digest = compute_content_digest(workflow, file_hashes)
    project = workflow.qualification
    identity_contract = hashlib.sha256(
        json.dumps(
            {
                step_id: policy.model_dump(mode="json")
                for step_id, policy in sorted(project.identity_policies.items())
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    effect_contract = hashlib.sha256(
        json.dumps(
            [item.model_dump(mode="json") for item in project.effect_policies],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return sign_local_dev_admission(
        bundle_content_digest=digest,
        proposal_sha256=proposal.sha256(),
        environment_digest=project.environment.environment_digest,
        identity_contract_sha256=identity_contract,
        effect_contract_sha256=effect_contract,
        policy_pack=pack.name,
    )


def write_proposal(proposal: QualificationProposal, path: Path | str) -> Path:
    target = Path(path)
    target.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
    return target


def load_proposal(path: Path | str) -> QualificationProposal:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return QualificationProposal.model_validate(payload)


def save_accepted_bundle(
    workflow: Workflow,
    bundle_dir: Path | str,
    *,
    proposal: QualificationProposal | None = None,
    local_admission: LocalDevAdmission | None = None,
) -> Path:
    """Reseal the bundle and write optional proposal/admission sidecars."""

    path = save_qualified_workflow(workflow, bundle_dir)
    bundle = Path(bundle_dir)
    if proposal is not None:
        write_proposal(proposal, bundle / "qualification-proposal.json")
    if local_admission is not None:
        (bundle / "qualification-admission.local-dev.json").write_text(
            local_admission.model_dump_json(indent=2),
            encoding="utf-8",
        )
    return path


def emit_proposal_json(proposal: QualificationProposal, out: Path | str | None) -> str:
    """Write the proposal to ``out`` when given, and always return the JSON."""

    text = proposal.model_dump_json(indent=2)
    if out is not None:
        write_proposal(proposal, out)
    return text


# Re-export for CLI and tests that want the contract digest helper nearby.
workflow_contract_digest = workflow_contract_sha256
