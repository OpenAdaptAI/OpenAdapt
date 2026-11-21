"""Versioned qualification-project contract for governed workflow bundles.

The compiled :class:`~openadapt_flow.ir.Workflow` remains the executable source
of truth.  A qualification project does not copy its actions, identities, or
effects; it attaches review policy and evidence to those existing contracts by
stable step id and effect-contract hash.

The models in this module intentionally contain no customer values, screenshots,
or verifier credentials.  Case inputs and evidence are references plus digests,
so the project can be sealed into the bundle without creating a second data
egress path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import weakref
from base64 import b64decode, b64encode
from collections import Counter
from datetime import datetime, timezone
from enum import Enum  # noqa: F401
from pathlib import Path, PurePosixPath
from typing import (  # noqa: F401
    TYPE_CHECKING,
    Any,
    Callable,
    Final,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Protocol,
)
from urllib.parse import urlsplit  # noqa: F401
from uuid import uuid4  # noqa: F401

from pydantic import (  # noqa: F401
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from openadapt_flow.identity_signals import (  # noqa: F401
    canonical_normalizers,
    parameterize_identity_text,
    signal_hash_key,
)
from openadapt_flow.judgment_cases import (
    JudgmentCaseFindingCode,
    JudgmentCaseQualificationReportV1,
    JudgmentCaseV1,
    JudgmentFactSchemaBindingV1,
    evaluate_judgment_cases,
)
from openadapt_flow.lookup_capabilities import QualifiedLookup
from openadapt_flow.qualified_business_decisions import (
    QualifiedBusinessDecisionDelivery,
    qualified_business_decision_delivery_errors,
    resolve_qualified_business_decision,
)
from openadapt_flow.verification import VerificationTier

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import BusinessDecisionSpec, IdentityTemplate, Step, Workflow
    from openadapt_flow.policy import Policy
    from openadapt_flow.subroutines import SubroutineLibrary

from openadapt_flow.qualification.models import (  # noqa: F401
    _CONTEXT_ID_RE,
    _ID_RE,
    _PARAM_RE,
    _QUALIFICATION_CASE_CONTRACT_DOMAIN,
    _QUALIFIED_ENTITY_LABEL_RE,
    QUALIFICATION_SCHEMA,
    REMOTE_SAFE_ENTITY_LABELS,
    ActionRiskClass,
    ActionRiskClassification,
    EffectVerificationPolicy,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityEvidenceSource,
    IdentityMatchMode,
    IdentityNormalizer,
    IdentityPolicy,
    IdentitySignalKey,
    IdentitySignalPolicy,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationCertification,
    QualificationOutcome,
    QualificationProject,
    QualificationRefusal,
    QualificationRefusalCode,
    QualificationReport,
    QualifiedEntityLabel,
    RequalificationCondition,
    _default_fault_cases,
    _now,
    _qualification_actuation_path,
    _qualification_identifier_sha256,
    _valid_application_identity,
    api_folded_step_ids,
    entity_label_options,
    qualification_campaign_id_sha256,
    qualification_case_contract_sha256,
    qualification_run_id_sha256,
    remote_entity_label,
)

if not TYPE_CHECKING:
    # ``ir`` imports this module as it loads and ``execution_profiles`` imports
    # ``ir``, so the two names this module imported from it load on first use.
    def __getattr__(name: str) -> object:
        if name in ("AUTOMATED_GUI_ACTUATIONS", "build_outcome_envelope"):
            from openadapt_flow import execution_profiles

            return getattr(execution_profiles, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class QualificationError(ValueError):
    """A requested qualification mutation could not be applied."""


def _workflow_contract_hash(workflow: "Workflow", content: dict[str, Any]) -> str:
    payload = {
        "workflow": content,
        "file_hashes": (
            dict(sorted(workflow.manifest.file_hashes.items()))
            if workflow.manifest is not None
            else {}
        ),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


WorkflowContractForm = Literal["current", "sealed_absent_defaults"]


def _bound_workflow_contracts(workflow: "Workflow") -> set[str]:
    """Workflow-contract digests the persisted qualification is bound to."""

    project = workflow.qualification
    if project is None:
        return set()
    bound = {
        result.workflow_contract_sha256
        for case in project.cases
        for result in case.results
    }
    if project.last_certification is not None:
        bound.add(project.last_certification.workflow_contract_sha256)
    return bound


def workflow_contract_digest(
    workflow: "Workflow",
) -> tuple[str, WorkflowContractForm]:
    """Return the workflow-contract digest and which form produced it.

    The current form is computed first and wins whenever the persisted
    qualification is bound to it, or to nothing. Only when it is bound to a
    different digest is the older form tried: the same content with the
    reviewed additive fields the loaded file never held left out
    (:func:`bundle_validation.omit_unset_sealed_defaults`). That form is used
    only if it reproduces a bound digest exactly; otherwise the current digest
    is returned and the caller's comparison fails as before.
    """

    from openadapt_flow.bundle_validation import omit_unset_sealed_defaults

    content = workflow.model_dump(mode="json", exclude={"manifest", "qualification"})
    current = _workflow_contract_hash(workflow, content)
    bound = _bound_workflow_contracts(workflow)
    if not bound or current in bound:
        return current, "current"
    omit_unset_sealed_defaults(
        workflow, content, schema_version=workflow.schema_version
    )
    older = _workflow_contract_hash(workflow, content)
    if older != current and older in bound:
        return older, "sealed_absent_defaults"
    return current, "current"


def workflow_contract_sha256(workflow: "Workflow") -> str:
    """Digest executable intent and its already-sealed visual assets.

    Qualification state and manifest certification metadata are excluded to
    avoid self-reference.  Existing manifest file hashes bind the plaintext
    template assets while keeping their contents inside the declared boundary.
    A certification sealed before a reviewed additive field existed is
    reproduced through :func:`workflow_contract_digest`.
    """

    return workflow_contract_digest(workflow)[0]


_CERTIFIED_CONTRACT_FORMS: dict[int, tuple["weakref.ref[Any]", str]] = {}


def record_certified_contract_form(workflow: "Workflow") -> str:
    """Record which contract form reproduced ``workflow``'s certification.

    Called by ``Workflow.load`` after the governed certification check. Kept
    outside the model so it never enters equality or any digest.
    """

    form = workflow_contract_digest(workflow)[1]
    key = id(workflow)

    def _forget(_ref: "weakref.ref[Any]") -> None:
        _CERTIFIED_CONTRACT_FORMS.pop(key, None)

    _CERTIFIED_CONTRACT_FORMS[key] = (weakref.ref(workflow, _forget), form)
    return form


def certified_contract_form(workflow: "Workflow") -> Optional[str]:
    """The form recorded by ``Workflow.load``, or None if none was checked."""

    entry = _CERTIFIED_CONTRACT_FORMS.get(id(workflow))
    if entry is None or entry[0]() is not workflow:
        return None
    return entry[1]


def _steps_by_id(workflow: "Workflow") -> dict[str, "Step"]:
    from openadapt_flow.traversal import iter_workflow_steps

    steps: dict[str, Step] = {}
    for step in iter_workflow_steps(workflow):
        if step.id in steps:
            raise QualificationError(f"step id {step.id!r} is ambiguous")
        steps[step.id] = step
    return steps


def _inferred_action_classification(step: "Step") -> ActionRiskClassification:
    from openadapt_flow.ir import ActionKind
    from openadapt_flow.risk import infer_step_risk

    effects = _declared_effects(step)
    runtime_inference = infer_step_risk(step)
    if step.risk_review_required:
        classification = ActionRiskClass.UNKNOWN
        explanation = step.risk_explanation or "compiled Flow risk requires review"
    elif step.risk == "irreversible":
        # A compiled irreversible decision is never weaker than the per-step
        # re-inference. Workflow-level evidence (an icon-only Save presumed
        # to commit the demonstrated entry) is invisible to that re-inference,
        # which would otherwise draft the write as "unknown".
        classification = ActionRiskClass.IRREVERSIBLE
        explanation = step.risk_explanation or "compiled Flow risk is irreversible"
    elif runtime_inference.requires_review:
        classification = ActionRiskClass.UNKNOWN
        explanation = runtime_inference.explanation
    elif runtime_inference.risk == "irreversible":
        classification = ActionRiskClass.IRREVERSIBLE
        explanation = runtime_inference.explanation
    elif effects:
        classification = ActionRiskClass.STATE_CHANGING
        explanation = "step declares a business-effect contract"
    elif step.action in {
        ActionKind.WAIT,
        ActionKind.SCROLL,
        ActionKind.SWITCH_SURFACE,
    }:
        classification = ActionRiskClass.READ_ONLY
        explanation = f"{step.action.value} does not actuate business state"
    else:
        classification = ActionRiskClass.UNKNOWN
        explanation = "actuating step requires operator risk classification"
    return ActionRiskClassification(
        step_id=step.id,
        classification=classification,
        explanation=explanation,
        operator_confirmed=classification is ActionRiskClass.READ_ONLY,
    )


def _declared_effects(step: "Step") -> list[Any]:
    """Return effects across every declared alternative actuation path.

    Runtime path selection may give a native API binding precedence over GUI
    actuation; qualification examines the union only to establish a
    conservative risk floor.  It does not imply that both paths execute or
    that one path's evidence can satisfy the other's production contract.
    """

    effects = list(step.effects)
    if step.api_binding is not None:
        effects.extend(step.api_binding.effects)
    return effects


def _effect_risk_floor(step: "Step") -> Optional[ActionRiskClass]:
    """Return the hard floor independently proved by declared effects."""

    effects = _declared_effects(step)
    if any(effect.risk.strip().lower() == "irreversible" for effect in effects):
        return ActionRiskClass.IRREVERSIBLE
    if effects:
        return ActionRiskClass.STATE_CHANGING
    return None


def _executable_risk_floor(step: "Step") -> Optional[ActionRiskClass]:
    """Return the least-permissive class required by executable contracts."""

    effect_floor = _effect_risk_floor(step)
    if step.risk == "irreversible" or effect_floor is ActionRiskClass.IRREVERSIBLE:
        return ActionRiskClass.IRREVERSIBLE
    return effect_floor


def qualification_action_requirements(
    workflow: "Workflow",
) -> tuple[set[str], set[str]]:
    """Return required actuation and identity step IDs for qualification.

    The project classification is authoritative only after operator review.
    Missing, unknown, or unconfirmed classifications remain consequential so
    a caller cannot weaken a campaign authorization while review is incomplete.
    Executable effect/risk declarations always provide a hard lower bound.
    """

    project = workflow.qualification
    if project is None:
        return set(), set()
    required_actions: set[str] = set()
    required_identity: set[str] = set()
    for step in _steps_by_id(workflow).values():
        classification = project.action_classifications.get(step.id)
        if (
            classification is None
            or not classification.operator_confirmed
            or classification.classification is ActionRiskClass.UNKNOWN
        ):
            effective = ActionRiskClass.CONSEQUENTIAL
        else:
            effective = classification.classification
        floor = _executable_risk_floor(step)
        if floor is ActionRiskClass.IRREVERSIBLE:
            effective = ActionRiskClass.IRREVERSIBLE
        elif (
            floor is ActionRiskClass.STATE_CHANGING
            and effective is ActionRiskClass.READ_ONLY
        ):
            effective = ActionRiskClass.STATE_CHANGING
        if effective in {
            ActionRiskClass.STATE_CHANGING,
            ActionRiskClass.CONSEQUENTIAL,
            ActionRiskClass.IRREVERSIBLE,
        }:
            required_actions.add(step.id)
        if effective in {
            ActionRiskClass.STATE_CHANGING,
            ActionRiskClass.CONSEQUENTIAL,
            ActionRiskClass.IRREVERSIBLE,
        }:
            required_identity.add(step.id)
    return required_actions, required_identity


def available_identity_sources(step: "Step") -> set[IdentityEvidenceSource]:
    anchor = step.anchor
    if anchor is None:
        return set()
    out: set[IdentityEvidenceSource] = set()
    template = anchor.identity_template
    if anchor.structured_identity or (template is not None and template.structured):
        out.add(IdentityEvidenceSource.STRUCTURED)
    if anchor.identifier_crop and anchor.identifier_region:
        out.add(IdentityEvidenceSource.IDENTIFIER_REGION)
    if anchor.context_text or (template is not None and template.tokens):
        out.add(IdentityEvidenceSource.CAPTURED_CONTEXT)
    # These sources are supplied live by the selected runner rather than by
    # target-row evidence. Capability negotiation/refusal happens at runtime.
    out.update(
        {
            IdentityEvidenceSource.APPLICATION,
            IdentityEvidenceSource.SESSION,
            IdentityEvidenceSource.WORKFLOW_STATE,
        }
    )
    return out


def identity_policy_independence_errors(policy: IdentityPolicy) -> list[str]:
    """Return fail-closed reasons when quorum votes reuse one observation.

    Each currently retained source is one observation channel per action:
    application structured text, one identifier crop/region, or one captured
    context band. Giving the same channel two field labels must not create two
    quorum votes.
    """

    if policy.enforcement is not IdentityEnforcement.SIGNAL_QUORUM:
        return []
    seen: set[IdentityEvidenceSource] = set()
    errors: list[str] = []
    for signal in policy.signals:
        if signal.source in seen:
            errors.append(
                f"source {signal.source.value!r} is reused by multiple signals"
            )
        seen.add(signal.source)
    pixel_signals = [
        signal
        for signal in policy.signals
        if signal.source
        in {
            IdentityEvidenceSource.IDENTIFIER_REGION,
            IdentityEvidenceSource.CAPTURED_CONTEXT,
        }
        and signal.region is not None
    ]
    for index, left in enumerate(pixel_signals):
        assert left.region is not None
        lx, ly, lw, lh = left.region
        for right in pixel_signals[index + 1 :]:
            assert right.region is not None
            rx, ry, rw, rh = right.region
            if lx < rx + rw and rx < lx + lw and ly < ry + rh and ry < ly + lh:
                errors.append(
                    "pixel regions overlap for semantic keys "
                    f"{left.key.value!r} and {right.key.value!r}"
                )
    return errors


def identity_signal_runtime_available(
    step: "Step",
    signal: IdentitySignalPolicy,
    *,
    actuation_path: Literal["gui", "api"] = "gui",
) -> bool:
    """Whether the shipped runtime can compare this exact retained signal."""

    if actuation_path == "api":
        binding = step.api_binding
        return bool(
            binding is not None
            and binding.identity
            and signal.key.value in {item.key for item in binding.identity}
        )

    anchor = step.anchor
    if anchor is None:
        return False
    if signal.source is IdentityEvidenceSource.IDENTIFIER_REGION:
        return bool(
            anchor.identifier_crop
            and anchor.identifier_region
            and signal.region == anchor.identifier_region
        )
    if signal.source is IdentityEvidenceSource.STRUCTURED:
        if anchor.structured_identity:
            return True
        template = anchor.identity_template
        return bool(
            template
            and signal_hash_key(
                signal.source,
                signal.match,
                signal.normalizers,
                extract_pattern=signal.extract_pattern,
                parameter_names=signal.params,
            )
            in template.signal_hashes
        )
    if signal.source is IdentityEvidenceSource.CAPTURED_CONTEXT:
        if anchor.context_text:
            return True
        template = anchor.identity_template
        return bool(
            template
            and signal_hash_key(
                signal.source,
                signal.match,
                signal.normalizers,
                extract_pattern=signal.extract_pattern,
                parameter_names=signal.params,
            )
            in template.signal_hashes
        )
    if signal.source in {
        IdentityEvidenceSource.APPLICATION,
        IdentityEvidenceSource.SESSION,
        IdentityEvidenceSource.WORKFLOW_STATE,
    }:
        return True
    return False


def _invalidate_certification(workflow: "Workflow") -> None:
    if workflow.qualification is not None:
        workflow.qualification.last_certification = None
    if workflow.manifest is None:
        return
    provenance = workflow.manifest.provenance
    provenance.policy_name = None
    provenance.certified = False
    provenance.certification_status = None
    provenance.certified_at = None
    provenance.expires_at = None
    provenance.governed_authorization_template = None


def _judgment_decision_contracts(
    workflow: "Workflow",
) -> dict[tuple[str, str], tuple[str, tuple[str, ...]]]:
    """Return current graph decision contracts without interpreting examples."""

    from openadapt_flow.ir import StateKind

    graphs = [("__program__", workflow.program), *workflow.subflows.items()]
    return {
        (graph_id, state_id): (
            state.decision.contract_sha256(),
            tuple(option.id for option in state.decision.options),
        )
        for graph_id, graph in graphs
        if graph is not None
        for state_id, state in graph.states.items()
        if state.kind is StateKind.BUSINESS_DECISION and state.decision is not None
    }


def evaluate_judgment_case_qualification(
    workflow: "Workflow",
) -> "JudgmentCaseQualificationReportV1":
    """Evaluate the local judgment-case layer without executing a workflow."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before evaluating judgment cases"
        )
    schemas = {
        (item.graph_id, item.state_id): item.fact_schema
        for item in project.judgment_fact_schemas
    }
    return evaluate_judgment_cases(
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        decisions=_judgment_decision_contracts(workflow),
        fact_schemas=schemas,
        cases=project.judgment_cases,
    )


def set_judgment_cases(
    workflow: "Workflow",
    *,
    schemas: Iterable[JudgmentFactSchemaBindingV1],
    cases: Iterable[JudgmentCaseV1],
) -> QualificationProject:
    """Replace the local reviewed case set and invalidate certification.

    This API deliberately accepts reviewed finite decisions only. It never
    generates a rule from facts or promotes a written reason into execution.
    """

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before setting judgment cases"
        )
    proposed_schemas = list(schemas)
    proposed_cases = list(cases)
    candidate = QualificationProject.model_validate(
        {
            **project.model_dump(mode="json"),
            "judgment_fact_schemas": [
                item.model_dump(mode="json") for item in proposed_schemas
            ],
            "judgment_cases": [item.model_dump(mode="json") for item in proposed_cases],
        }
    )
    original_schemas = project.judgment_fact_schemas
    original_cases = project.judgment_cases
    project.judgment_fact_schemas = candidate.judgment_fact_schemas
    project.judgment_cases = candidate.judgment_cases
    report = evaluate_judgment_case_qualification(workflow)
    project.judgment_fact_schemas = original_schemas
    project.judgment_cases = original_cases
    strict_codes = {
        JudgmentCaseFindingCode.BINDING_MISMATCH,
        JudgmentCaseFindingCode.FACT_SCHEMA_MISMATCH,
    }
    strict = [item for item in report.findings if item.code in strict_codes]
    if strict:
        raise QualificationError(
            "invalid judgment case binding: "
            + "; ".join(item.message for item in strict)
        )
    if (
        project.judgment_fact_schemas == candidate.judgment_fact_schemas
        and project.judgment_cases == candidate.judgment_cases
    ):
        return project
    previous = project.revision_digest()
    project.judgment_fact_schemas = candidate.judgment_fact_schemas
    project.judgment_cases = candidate.judgment_cases
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_business_decision_deliveries(
    workflow: "Workflow",
    bindings: Iterable[QualifiedBusinessDecisionDelivery],
) -> QualificationProject:
    """Replace reviewed mobile delivery bindings and invalidate certification.

    This operation stores no key or live application value. It also does not
    make a local decision remotely answerable by itself. The customer runner
    must supply the exact deployment key bindings and build a signed task from
    an active durable request.
    """

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before setting business decision delivery"
        )
    proposed = list(bindings)
    for binding in proposed:
        try:
            resolve_qualified_business_decision(workflow, binding)
        except ValueError as exc:
            raise QualificationError(str(exc)) from exc
    candidate = QualificationProject.model_validate(
        {
            **project.model_dump(mode="json"),
            "business_decision_deliveries": [
                item.model_dump(mode="json") for item in proposed
            ],
        }
    )
    if project.business_decision_deliveries == candidate.business_decision_deliveries:
        return project
    previous = project.revision_digest()
    project.business_decision_deliveries = candidate.business_decision_deliveries
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def _touch(project: QualificationProject, previous_digest: str) -> None:
    project.previous_revision_sha256 = previous_digest
    project.revision += 1
    project.updated_at = _now()
    project.last_certification = None
    project.lookup = None


def init_project(
    workflow: "Workflow",
    *,
    environment: EnvironmentBoundary,
    minimum_effect_tier: VerificationTier = (
        VerificationTier.PERSISTED_STATE_REACQUISITION
    ),
    replace: bool = False,
) -> QualificationProject:
    """Attach a new qualification project to ``workflow``."""

    if workflow.qualification is not None and not replace:
        raise QualificationError("workflow already has a qualification project")
    classifications = {
        step_id: _inferred_action_classification(step)
        for step_id, step in _steps_by_id(workflow).items()
    }
    project = QualificationProject(
        environment=environment,
        minimum_effect_tier=minimum_effect_tier,
        action_classifications=classifications,
    )
    workflow.qualification = project
    _invalidate_certification(workflow)
    return project


def set_minimum_effect_tier(
    workflow: "Workflow",
    tier: VerificationTier,
) -> QualificationProject:
    """Set the project's minimum accepted effect-verification strength."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before setting minimum effect strength"
        )
    candidate = VerificationTier(tier)
    if project.minimum_effect_tier == candidate:
        return project
    previous = project.revision_digest()
    project.minimum_effect_tier = candidate
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_entity_label(
    workflow: "Workflow", label: QualifiedEntityLabel
) -> QualificationProject:
    """Set one qualification-owned entity label and invalidate certification."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before setting an entity label"
        )
    if label.step_id not in _steps_by_id(workflow):
        raise QualificationError(f"unknown step id {label.step_id!r}")
    if project.entity_labels.get(label.step_id) == label:
        return project
    previous = project.revision_digest()
    project.entity_labels[label.step_id] = label
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def remove_entity_label(workflow: "Workflow", step_id: str) -> QualificationProject:
    """Remove one qualification-owned entity label and invalidate certification."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before removing an entity label"
        )
    if step_id not in project.entity_labels:
        raise QualificationError(f"no entity label is set for step id {step_id!r}")
    previous = project.revision_digest()
    del project.entity_labels[step_id]
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_business_decision(
    workflow: "Workflow",
    *,
    graph_id: str,
    state_id: str,
    decision: "BusinessDecisionSpec",
    insert_before_state_id: Optional[str] = None,
) -> "Workflow":
    """Add or update one reviewed typed decision in an executable graph.

    This is the canonical qualification-authoring mutation for Desktop and API
    clients.  It consumes the exact IR model, derives the only admitted
    transition shape, and advances the qualification revision once.  It does
    not submit an answer, prove an effect, or change production decision
    semantics.

    A new node is inserted immediately before one existing state.  The target
    must be the graph entry or have exactly one normal inbound transition.  The
    narrow insertion rule prevents an authoring client from silently changing
    several paths that happen to converge on the same state.
    """

    from openadapt_flow import bundle_validation as _bv
    from openadapt_flow.ir import (
        BusinessDecisionSpec,
        ParamKind,
        ParamSpec,
        ProgramGraph,
        State,
        StateKind,
        Workflow,
        business_decision_transitions,
        lift_to_program,
    )

    if workflow.qualification is None:
        raise QualificationError(
            "initialize qualification before authoring a business decision"
        )
    if not isinstance(decision, BusinessDecisionSpec):
        raise QualificationError("decision must use Flow's BusinessDecisionSpec")
    if not _ID_RE.fullmatch(state_id):
        raise QualificationError("business decision state id is invalid")
    if not 2 <= len(decision.options) <= 4:
        raise QualificationError("a business decision must have 2 to 4 options")

    candidate = workflow.model_copy(deep=True)
    graph: Optional[ProgramGraph]
    if graph_id == "__program__":
        if candidate.program is None:
            candidate.program = lift_to_program(candidate)
        graph = candidate.program
    else:
        graph = candidate.subflows.get(graph_id)
    if graph is None:
        raise QualificationError(f"unknown executable graph {graph_id!r}")

    existing_state = graph.states.get(state_id)
    if existing_state is not None:
        if insert_before_state_id is not None:
            raise QualificationError(
                "insert_before_state_id is valid only when adding a decision"
            )
        if (
            existing_state.kind is not StateKind.BUSINESS_DECISION
            or existing_state.decision is None
        ):
            raise QualificationError(
                f"state {state_id!r} exists and is not a business decision"
            )
        if existing_state.decision.output_param != decision.output_param:
            raise QualificationError(
                "an existing business decision cannot change its output parameter"
            )
    else:
        if not insert_before_state_id:
            raise QualificationError(
                "insert_before_state_id is required when adding a decision"
            )
        if insert_before_state_id not in graph.states:
            raise QualificationError(
                f"unknown insertion state {insert_before_state_id!r}"
            )
        inbound = [
            (source, transition)
            for source in graph.states.values()
            for transition in source.transitions
            if transition.target == insert_before_state_id
        ]
        if graph.entry == insert_before_state_id:
            if inbound:
                raise QualificationError(
                    "the graph entry also has inbound transitions; choose an "
                    "unambiguous insertion point"
                )
            graph.entry = state_id
        else:
            if len(inbound) != 1:
                raise QualificationError(
                    "the insertion state must have exactly one normal inbound "
                    "transition"
                )
            predecessor, transition = inbound[0]
            if predecessor.kind is StateKind.BUSINESS_DECISION:
                predecessor_decision = predecessor.decision
                if predecessor_decision is None:
                    raise QualificationError(
                        "the predecessor business decision has no decision contract"
                    )
                matching_options = [
                    option
                    for option in predecessor_decision.options
                    if option.target == insert_before_state_id
                ]
                if len(matching_options) != 1:
                    raise QualificationError(
                        "the predecessor business decision contract does not match "
                        "its transition"
                    )
                matching_id = matching_options[0].id
                updated_options = tuple(
                    option.model_copy(update={"target": state_id})
                    if option.id == matching_id
                    else option
                    for option in predecessor_decision.options
                )
                updated_predecessor_decision = predecessor_decision.model_copy(
                    update={"options": updated_options}
                )
                graph.states[predecessor.id] = predecessor.model_copy(
                    update={
                        "decision": updated_predecessor_decision,
                        "transitions": business_decision_transitions(
                            updated_predecessor_decision
                        ),
                    }
                )
            else:
                transition.target = state_id

    target_ids = {option.target for option in decision.options}
    unknown_targets = sorted(target_ids - set(graph.states))
    if unknown_targets:
        raise QualificationError(
            "business decision options name unknown successor states: "
            + ", ".join(unknown_targets)
        )
    if state_id in target_ids:
        raise QualificationError("a business decision cannot target itself")
    if decision.output_param in candidate.secret_params:
        raise QualificationError("a business decision output cannot be a secret")

    values = [option.value for option in decision.options]
    other_decisions = [
        state.decision
        for candidate_graph_id, candidate_graph in [
            ("__program__", candidate.program),
            *candidate.subflows.items(),
        ]
        if candidate_graph is not None
        for candidate_state_id, state in candidate_graph.states.items()
        if state.kind is StateKind.BUSINESS_DECISION
        and state.decision is not None
        and state.decision.output_param == decision.output_param
        and (candidate_graph_id, candidate_state_id) != (graph_id, state_id)
    ]
    if any(
        {option.value for option in other.options} != set(values)
        for other in other_decisions
    ):
        raise QualificationError(
            "cannot change business decision output choices while another "
            "decision shares the output parameter"
        )
    current_param = candidate.param_specs.get(decision.output_param)
    if existing_state is None and current_param is not None:
        raise QualificationError(
            "the business decision output conflicts with an existing parameter"
        )
    if current_param is None:
        if decision.output_param in candidate.params:
            raise QualificationError(
                "the business decision output conflicts with an existing parameter"
            )
        candidate.param_specs[decision.output_param] = ParamSpec(
            name=decision.output_param,
            type=ParamKind.ENUM,
            required=False,
            choices=values,
        )
    elif current_param.type is not ParamKind.ENUM or current_param.required:
        raise QualificationError(
            "the business decision output parameter must be an optional enum"
        )
    else:
        candidate.param_specs[decision.output_param] = current_param.model_copy(
            update={"choices": values}
        )

    graph.states[state_id] = State(
        id=state_id,
        kind=StateKind.BUSINESS_DECISION,
        decision=decision,
        transitions=business_decision_transitions(decision),
    )

    project = candidate.qualification
    assert project is not None
    previous = project.revision_digest()
    _touch(project, previous)
    _invalidate_certification(candidate)
    return _bv.validate_verified_derivative(
        candidate, Workflow, QualificationError, "the authored business decision"
    )


def list_entity_labels(workflow: "Workflow") -> list[QualifiedEntityLabel]:
    """Return qualification-owned labels in stable step-id order."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before listing entity labels"
        )
    return [project.entity_labels[key] for key in sorted(project.entity_labels)]


def set_identity_policy(
    workflow: "Workflow",
    policy: IdentityPolicy,
    *,
    recorded_signal_values: Optional[dict[str, str]] = None,
) -> QualificationProject:
    """Set review policy for a step's existing executable identity ladder.

    ``recorded_signal_values`` supplies operator-selected values only when the
    corresponding plaintext source has already been removed from the bundle.
    Qualification converts each value directly into an extractor-scoped HMAC;
    neither this mapping nor its values are retained in the workflow.
    """

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before setting identity")
    step = _steps_by_id(workflow).get(policy.step_id)
    if step is None:
        raise QualificationError(f"unknown step id {policy.step_id!r}")
    from openadapt_flow.policy import executable_actuation_paths

    executable_paths = executable_actuation_paths(step)
    gui_required = "gui" in executable_paths
    api_required = "api" in executable_paths
    available = available_identity_sources(step)
    pending_hashes: list[tuple["IdentityTemplate", str, str]] = []
    if policy.enforcement is IdentityEnforcement.CANONICAL_LADDER:
        if recorded_signal_values:
            raise QualificationError(
                "recorded identity values apply only to signal-quorum policies"
            )
        from openadapt_flow.policy import is_identity_armed

        if api_required and not gui_required:
            raise QualificationError(
                "API actuation requires a signal-quorum identity policy bound "
                "to the request and effect"
            )
        if gui_required and (not is_identity_armed(step) or not available):
            raise QualificationError(
                "canonical identity ladder is not armed with retained evidence"
            )
    else:
        independence_errors = identity_policy_independence_errors(policy)
        if independence_errors:
            raise QualificationError(
                "identity quorum signals are not independent: "
                + "; ".join(independence_errors)
            )
        unavailable = (
            sorted(
                {signal.source for signal in policy.signals} - available,
                key=lambda source: source.value,
            )
            if gui_required
            else []
        )
        if unavailable:
            raise QualificationError(
                "identity policy references unavailable evidence: "
                + ", ".join(source.value for source in unavailable)
            )
        supplied_values = recorded_signal_values or {}
        pending_keys: set[tuple[int, str]] = set()
        accepted_value_keys: set[str] = set()
        for signal in policy.signals:
            unknown_params = sorted(set(signal.params).difference(workflow.params))
            if unknown_params:
                raise QualificationError(
                    f"identity signal {signal.key.value!r} references unknown "
                    "workflow parameter(s): " + ", ".join(unknown_params)
                )
            if not gui_required:
                continue
            anchor = step.anchor
            assert anchor is not None
            recorded = (
                anchor.structured_identity
                if signal.source is IdentityEvidenceSource.STRUCTURED
                else anchor.context_text
                if signal.source is IdentityEvidenceSource.CAPTURED_CONTEXT
                else None
            )
            if recorded is not None and signal.extract_pattern is not None:
                extracted = re.search(
                    signal.extract_pattern,
                    recorded,
                    flags=re.IGNORECASE,
                )
                if extracted is None:
                    raise QualificationError(
                        f"identity signal {signal.key.value!r} extract_pattern "
                        "does not match the retained source"
                    )
                recorded = extracted.group("value")
            if (
                recorded is None
                and signal.source
                in {
                    IdentityEvidenceSource.STRUCTURED,
                    IdentityEvidenceSource.CAPTURED_CONTEXT,
                }
                and signal.key.value in supplied_values
            ):
                template = anchor.identity_template
                if template is None or signal.extract_pattern is None:
                    raise QualificationError(
                        f"identity signal {signal.key.value!r} has no PHI-free "
                        "identity template to bind"
                    )
                from openadapt_flow.runtime.identity_template import (
                    qualified_signal_hash,
                )

                hash_key, digest, used = qualified_signal_hash(
                    template,
                    source=signal.source.value,
                    match=signal.match.value,
                    normalizers=signal.normalizers,
                    extract_pattern=signal.extract_pattern,
                    recorded_value=supplied_values[signal.key.value],
                    param_examples=workflow.params,
                    parameter_names=signal.params,
                )
                missing = sorted(set(signal.params).difference(used))
                if missing:
                    raise QualificationError(
                        f"identity signal {signal.key.value!r} parameter binding "
                        "does not occupy a complete value boundary in the "
                        "operator-selected recorded value: " + ", ".join(missing)
                    )
                pending_hashes.append((template, hash_key, digest))
                pending_keys.add((id(template), hash_key))
                accepted_value_keys.add(signal.key.value)
            if recorded is not None and signal.params:
                _parameterized, used = parameterize_identity_text(
                    recorded,
                    workflow.params,
                    names=signal.params,
                    minimum_chars=4,
                    case_sensitive=True,
                )
                missing = sorted(set(signal.params).difference(used))
                if missing:
                    raise QualificationError(
                        f"identity signal {signal.key.value!r} parameter binding "
                        "does not occupy a complete value boundary in the retained "
                        "source: " + ", ".join(missing)
                    )
        unused_value_keys = sorted(set(supplied_values).difference(accepted_value_keys))
        if unused_value_keys:
            raise QualificationError(
                "recorded identity values were supplied for signals that do not "
                "need a PHI-free binding: " + ", ".join(unused_value_keys)
            )
        runtime_unavailable: list[tuple[str, IdentitySignalPolicy]] = []
        for actuation_path in executable_paths:
            if actuation_path == "api":
                # Qualification is an editing surface. Permit the operator to
                # save the intended semantic policy before the request/effect
                # binding is complete; evaluate_qualification still refuses
                # certification until the exact API binding exists.
                continue
            for signal in policy.signals:
                if identity_signal_runtime_available(
                    step,
                    signal,
                    actuation_path=actuation_path,
                ):
                    continue
                if actuation_path == "gui":
                    anchor = step.anchor
                    assert anchor is not None
                    template = anchor.identity_template
                    pending_key = signal_hash_key(
                        signal.source,
                        signal.match,
                        signal.normalizers,
                        extract_pattern=signal.extract_pattern,
                        parameter_names=signal.params,
                    )
                    if (
                        template is not None
                        and (
                            id(template),
                            pending_key,
                        )
                        in pending_keys
                    ):
                        continue
                runtime_unavailable.append((actuation_path, signal))
        if runtime_unavailable:
            raise QualificationError(
                "identity policy references retained evidence without the "
                "requested executable comparison: "
                + ", ".join(
                    f"{signal.key.value} ({signal.source.value}, {actuation_path})"
                    for actuation_path, signal in runtime_unavailable
                )
            )
    hashes_changed = any(
        template.signal_hashes.get(key) != digest
        for template, key, digest in pending_hashes
    )
    if project.identity_policies.get(policy.step_id) == policy and not hashes_changed:
        return project
    previous = project.revision_digest()
    for template, key, digest in pending_hashes:
        template.signal_hashes[key] = digest
    project.identity_policies[policy.step_id] = policy
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_action_classification(
    workflow: "Workflow",
    classification: ActionRiskClassification,
) -> QualificationProject:
    """Persist an operator-reviewed classification for one executable action."""

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before classifying actions")
    step = _steps_by_id(workflow).get(classification.step_id)
    if step is None:
        raise QualificationError(f"unknown step id {classification.step_id!r}")
    if not classification.operator_confirmed:
        raise QualificationError("an operator classification must be confirmed")
    effect_floor = _effect_risk_floor(step)
    if (
        effect_floor is ActionRiskClass.IRREVERSIBLE
        and classification.classification is not ActionRiskClass.IRREVERSIBLE
    ):
        raise QualificationError(
            "an action with an irreversible effect contract cannot be down-classified"
        )
    if (
        effect_floor is ActionRiskClass.STATE_CHANGING
        and classification.classification is ActionRiskClass.READ_ONLY
    ):
        raise QualificationError(
            "an action with a declared business effect cannot be read-only"
        )
    runtime_risk: Literal["reversible", "irreversible"] = (
        "irreversible"
        if classification.classification
        in {ActionRiskClass.CONSEQUENTIAL, ActionRiskClass.IRREVERSIBLE}
        else "reversible"
    )
    runtime_explanation = f"operator-qualified override: {runtime_risk}"
    unchanged = (
        project.action_classifications.get(classification.step_id) == classification
        and step.risk == runtime_risk
        and step.risk_explanation == runtime_explanation
        and not step.risk_review_required
    )
    if unchanged:
        return project
    previous = project.revision_digest()
    step.risk = runtime_risk
    step.risk_explanation = runtime_explanation
    step.risk_review_required = False
    project.action_classifications[classification.step_id] = classification
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_trusted_runner_key(
    workflow: "Workflow",
    *,
    key_id: str,
    public_key_base64: str,
) -> QualificationProject:
    """Trust an Ed25519 qualification runner public key."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before trusting a qualification runner"
        )
    candidate = dict(project.trusted_runner_keys)
    candidate[key_id] = public_key_base64
    # Re-validate the closed schema before mutating the live project.
    QualificationProject.model_validate(
        {**project.model_dump(mode="json"), "trusted_runner_keys": candidate}
    )
    if project.trusted_runner_keys.get(key_id) == public_key_base64:
        return project
    previous = project.revision_digest()
    project.trusted_runner_keys = candidate
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_trusted_fault_driver_key(
    workflow: "Workflow",
    *,
    key_id: str,
    public_key_base64: str,
) -> QualificationProject:
    """Trust one environment-owned qualification fault-driver key."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before trusting a fault driver"
        )
    candidate = dict(project.trusted_fault_driver_keys)
    candidate[key_id] = public_key_base64
    QualificationProject.model_validate(
        {**project.model_dump(mode="json"), "trusted_fault_driver_keys": candidate}
    )
    if project.trusted_fault_driver_keys.get(key_id) == public_key_base64:
        return project
    previous = project.revision_digest()
    project.trusted_fault_driver_keys = candidate
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_effect_policy(
    workflow: "Workflow",
    *,
    step_id: str,
    effect_index: int,
    tier: VerificationTier,
    actuation_path: Literal["gui", "api"] = "gui",
) -> QualificationProject:
    """Assign verification strength to an existing effect contract."""

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before setting effects")
    step = _steps_by_id(workflow).get(step_id)
    if step is None:
        raise QualificationError(f"unknown step id {step_id!r}")
    from openadapt_flow.policy import executable_actuation_paths

    if actuation_path not in executable_actuation_paths(step):
        raise QualificationError(
            f"step {step_id!r} has no executable {actuation_path} actuation path"
        )
    effects = (
        step.effects
        if actuation_path == "gui"
        else (step.api_binding.effects if step.api_binding is not None else [])
    )
    if effect_index < 0 or effect_index >= len(effects):
        raise QualificationError(
            f"effect index {effect_index} is outside {actuation_path} path "
            f"for step {step_id!r}"
        )
    binding = EffectVerificationPolicy(
        step_id=step_id,
        actuation_path=actuation_path,
        effect_index=effect_index,
        effect_contract_hash=effects[effect_index].contract_hash(),
        tier=tier,
    )
    current = next(
        (
            existing
            for existing in project.effect_policies
            if (
                existing.step_id,
                existing.actuation_path,
                existing.effect_index,
            )
            == (step_id, actuation_path, effect_index)
        ),
        None,
    )
    if current == binding:
        return project
    previous = project.revision_digest()
    project.effect_policies = [
        existing
        for existing in project.effect_policies
        if (
            existing.step_id,
            existing.actuation_path,
            existing.effect_index,
        )
        != (step_id, actuation_path, effect_index)
    ]
    project.effect_policies.append(binding)
    project.effect_policies.sort(
        key=lambda item: (item.step_id, item.actuation_path, item.effect_index)
    )
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def add_case(workflow: "Workflow", case: QualificationCase) -> QualificationProject:
    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before adding cases")
    if any(existing.id == case.id for existing in project.cases):
        raise QualificationError(f"qualification case {case.id!r} already exists")
    previous = project.revision_digest()
    project.cases.append(case)
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def set_case_scope(
    workflow: "Workflow",
    *,
    case_id: str,
    runtime_input_sha256: str,
    action_targets: Iterable[QualificationActionTarget],
    fault_target: Optional[QualificationActionTarget] = None,
) -> QualificationProject:
    """Bind a qualification case to its approved input and action scope."""

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before scoping cases")
    case = next((item for item in project.cases if item.id == case_id), None)
    if case is None:
        raise QualificationError(f"unknown qualification case {case_id!r}")
    steps = _steps_by_id(workflow)
    from openadapt_flow.policy import executable_actuation_paths

    targets = list(action_targets)
    target_step_ids = [target.step_id for target in targets]
    unknown = sorted(set(target_step_ids).difference(steps))
    if unknown:
        raise QualificationError(
            "qualification case targets unknown workflow actions: " + ", ".join(unknown)
        )
    invalid_api_targets = sorted(
        target.step_id
        for target in targets
        if target.actuation_path == "api" and steps[target.step_id].api_binding is None
    )
    if invalid_api_targets:
        raise QualificationError(
            "qualification case targets missing API paths: "
            + ", ".join(invalid_api_targets)
        )
    invalid_targets = sorted(
        f"{target.step_id}:{target.actuation_path}"
        for target in targets
        if target.actuation_path
        not in executable_actuation_paths(steps[target.step_id])
    )
    if invalid_targets:
        raise QualificationError(
            "qualification case targets non-executable paths: "
            + ", ".join(invalid_targets)
        )
    if case.kind is QualificationCaseKind.REPRESENTATIVE:
        if not targets:
            raise QualificationError("representative cases require an action scope")
        if len(target_step_ids) != len(set(target_step_ids)):
            raise QualificationError(
                "a representative case can exercise only one path per step"
            )
    else:
        if fault_target is None:
            if len(targets) != 1:
                raise QualificationError(
                    "fault cases with multiple permitted actions require one exact "
                    "fault target"
                )
            fault_target = targets[0]
        if fault_target not in targets:
            raise QualificationError(
                "fault target must be inside the permitted action scope"
            )
    updated = QualificationCase.model_validate(
        case.model_copy(
            update={
                "runtime_input_sha256": runtime_input_sha256,
                "action_targets": targets,
                "fault_target": fault_target,
            }
        ).model_dump(mode="python")
    )
    if updated == case:
        return project
    previous = project.revision_digest()
    project.cases = [updated if item.id == case_id else item for item in project.cases]
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def _attestation_payload(result: QualificationCaseResult) -> bytes:
    payload = result.model_dump(
        mode="json",
        exclude={"attestation_signature"},
    )
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def sign_case_result(
    result: QualificationCaseResult,
    *,
    private_key: bytes,
) -> QualificationCaseResult:
    """Sign one result with a raw 32-byte Ed25519 private key."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signer = Ed25519PrivateKey.from_private_bytes(private_key)
    signature = signer.sign(_attestation_payload(result))
    return result.model_copy(
        update={"attestation_signature": b64encode(signature).decode("ascii")}
    )


class _HashBoundEvidenceRef(Protocol):
    """The fields required to verify any local evidence reference."""

    relative_path: str
    sha256: str


def _sealed_evidence_bytes(
    *,
    root: Path,
    candidate: Path,
    evidence: _HashBoundEvidenceRef,
) -> tuple[Optional[bytes], Optional[str]]:
    """Read a file a sealed run directory holds by its logical name.

    A run of a sealed bundle writes a sealed run directory. Its report is
    bound by the digest of its plaintext; the sealed set authenticates the
    ciphertext on disk and decrypts it in memory with the run's key.
    """

    from openadapt_flow import sealed_set

    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError:
        return None, f"evidence file is missing: {evidence.relative_path}"
    if not parent.is_relative_to(root):
        return None, f"evidence path leaves its root: {evidence.relative_path}"
    try:
        payload = sealed_set.read_bytes(candidate)
    except Exception:  # noqa: BLE001 - an unreadable sealed file is not evidence
        return None, f"evidence file is missing: {evidence.relative_path}"
    if hashlib.sha256(payload).hexdigest() != evidence.sha256:
        return None, f"evidence hash mismatch: {evidence.relative_path}"
    return payload, None


def _read_evidence_bytes(
    *,
    root: Path,
    evidence: _HashBoundEvidenceRef,
) -> tuple[Optional[bytes], Optional[str]]:
    """Read one exact hash-bound file without following a symlink."""

    parts = PurePosixPath(evidence.relative_path).parts
    candidate = root.joinpath(*parts)
    cursor = root
    for part in parts:
        cursor /= part
        if cursor.is_symlink():
            return None, f"evidence path contains a symlink: {evidence.relative_path}"
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return _sealed_evidence_bytes(root=root, candidate=candidate, evidence=evidence)
    if not resolved.is_relative_to(root) or candidate.is_symlink():
        return None, f"evidence path leaves its root: {evidence.relative_path}"
    if not resolved.is_file():
        return None, f"evidence is not a regular file: {evidence.relative_path}"
    try:
        payload = resolved.read_bytes()
    except OSError:
        return None, f"evidence file is unreadable: {evidence.relative_path}"
    if hashlib.sha256(payload).hexdigest() != evidence.sha256:
        return None, f"evidence hash mismatch: {evidence.relative_path}"
    return payload, None


def _one_evidence(
    result: QualificationCaseResult,
    kind: str,
) -> tuple[Optional[EvidenceRef], Optional[str]]:
    matches = [item for item in result.evidence if item.kind == kind]
    if len(matches) != 1:
        return None, f"qualification case requires exactly one {kind} evidence artifact"
    return matches[0], None


def _surface_environment_evidence_refusal(
    report: Any, workflow: "Workflow", project: "QualificationProject"
) -> Optional[str]:
    """Check a multi-surface case's per-surface environment observations.

    Every non-primary surface a step ran on must carry an observation that
    matches that surface's qualified boundary exactly (the same fields the
    primary check compares), and no observation may name a surface the run
    never used. Single-surface reports carry none and pass unchanged.
    """

    from openadapt_flow.qualification_environment import (
        qualification_environment_binding_sha256,
    )

    observed = dict(getattr(report, "observed_surface_environments", {}) or {})
    if not workflow.surfaces:
        return (
            "case report carries surface environments for a single-surface workflow"
            if observed
            else None
        )
    primary = workflow.surfaces[0].id
    used = {
        result.surface
        for result in report.results
        if result.surface is not None and result.surface != primary
    }
    if set(observed) != used:
        return "case report surface environments do not match the surfaces it ran on"
    for surface_id in sorted(used):
        boundary = project.surface_environments.get(surface_id)
        item = observed[surface_id]
        expected_identity = (
            None if boundary is None else boundary.expected_application_identity
        )
        if boundary is None or expected_identity is None:
            return f"surface '{surface_id}' has no qualified environment boundary"
        binding = qualification_environment_binding_sha256(
            target_kind=item.target_kind,
            observer_id=item.observer_id,
            observer_contract_sha256=item.observer_contract_sha256,
            application_identity_sha256=item.application_sha256,
            application_version_sha256=item.application_version_sha256,
            environment_digest=item.environment_digest,
            session_identity_sha256=item.session_sha256,
        )
        if (
            item.target_kind != boundary.target_kind
            or item.application_sha256
            != hashlib.sha256(expected_identity.encode("utf-8")).hexdigest()
            or item.application_version_sha256
            != hashlib.sha256(boundary.application_version.encode("utf-8")).hexdigest()
            or item.environment_digest != boundary.environment_digest
            or item.observer_id != boundary.environment_observer_id
            or item.observer_contract_sha256
            != boundary.environment_observer_contract_sha256
            or item.binding_sha256 != binding
        ):
            return (
                f"case report environment for surface '{surface_id}' does not "
                "match the project"
            )
    return None


def _case_run_report_integrity_error(
    *,
    workflow: "Workflow",
    project: QualificationProject,
    case: QualificationCase,
    result: QualificationCaseResult,
    evidence_root: Optional[Path],
    policy: Optional["Policy"] = None,
    transition_predicate_vision: Any | None = None,
) -> Optional[tuple[QualificationRefusalCode, str]]:
    """Bind one signed case result to its exact retained run and input bytes."""
    from openadapt_flow.execution_profiles import (
        AUTOMATED_GUI_ACTUATIONS,
        build_outcome_envelope,
    )
    from openadapt_flow.ir import RunReport
    from openadapt_flow.qualification_environment import (
        qualification_environment_binding_sha256,
    )
    from openadapt_flow.qualification_faults import sha256_bytes

    if evidence_root is None:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            "qualification certification requires the local evidence root",
        )
    if None in (
        result.campaign_id_sha256,
        result.case_input_sha256,
        result.run_id_sha256,
    ):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result lacks its campaign, input, or run binding",
        )

    root = evidence_root.resolve()
    report_ref, report_ref_error = _one_evidence(result, "run_report")
    input_ref, input_ref_error = _one_evidence(result, "case_input")
    if report_ref_error is not None or report_ref is None:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            report_ref_error or "case has no exact run report",
        )
    if input_ref_error is not None or input_ref is None:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            input_ref_error or "case has no exact input artifact",
        )
    report_bytes, report_error = _read_evidence_bytes(
        root=root,
        evidence=report_ref,
    )
    input_bytes, input_error = _read_evidence_bytes(
        root=root,
        evidence=input_ref,
    )
    if report_error is not None or report_bytes is None:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            report_error or "case run report is unreadable",
        )
    if input_error is not None or input_bytes is None:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            input_error or "case input is unreadable",
        )
    if (
        input_ref.sha256 != result.case_input_sha256
        or hashlib.sha256(input_bytes).hexdigest() != result.case_input_sha256
        or case.runtime_input_sha256 != result.case_input_sha256
    ):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case-input bytes do not match the signed case-result binding",
        )
    from openadapt_flow.runtime.authorization import (
        parse_runtime_inputs_bytes,
        runtime_param_text,
        runtime_params_for_gui,
    )

    try:
        case_params, case_worklists = parse_runtime_inputs_bytes(
            input_bytes,
            workflow=workflow,
        )
    except ValueError:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            "case input is not a valid canonical governed-input artifact",
        )

    def scoped_case_params(
        item: Any,
    ) -> Optional[dict[str, str | bool | int | float]]:
        if workflow.program is None:
            return dict(case_params) if not item.program_scope else None
        if not item.program_scope or item.program_scope[0].graph_id != "__program__":
            return None
        allowed_graphs = {"__program__", *workflow.subflows}
        scoped = dict(case_params)
        for frame in item.program_scope:
            if frame.graph_id not in allowed_graphs:
                return None
            if frame.relation is None:
                continue
            rows = case_worklists.get(frame.relation)
            if rows is None:
                relation = workflow.data_sources.get(frame.relation)
                if relation is None:
                    return None
                rows = relation.rows
            if frame.row_index is None or frame.row_index >= len(rows):
                return None
            scoped.update(rows[frame.row_index])
        return scoped

    try:
        report = RunReport.model_validate_json(report_bytes)
    except ValueError:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            "case contains an invalid run report",
        )
    run_evidence_root = (root / report_ref.relative_path).resolve().parent
    steps_by_id = _steps_by_id(workflow)
    required_actions, required_identity = qualification_action_requirements(workflow)
    effect_policies = {
        (binding.step_id, binding.actuation_path, binding.effect_index): binding
        for binding in project.effect_policies
    }
    minimum_tier = VerificationTier(project.minimum_effect_tier)

    def exact_step_metadata_error(item: Any, step: "Step") -> Optional[str]:
        """Bind a retained action row to the exact compiled step contract."""

        if (
            item.intent != step.intent
            or item.risk != step.risk
            or item.risk_explanation != step.risk_explanation
            or item.risk_review_required != step.risk_review_required
        ):
            return "action intent or qualified risk metadata differs from the workflow"
        return None

    def retained_before_frame_size(
        item: Any,
    ) -> tuple[Optional[tuple[int, int]], Optional[str], Optional[str]]:
        """Read the exact hash-bound pre-action frame for one GUI result."""

        if not item.before_png or "\\" in item.before_png:
            return None, None, "GUI resolution lacks its retained pre-action frame"
        relative = PurePosixPath(item.before_png)
        if relative.is_absolute() or ".." in relative.parts:
            return None, None, "GUI pre-action frame path is not run-relative"
        report_parent = PurePosixPath(report_ref.relative_path).parent
        evidence_path = (report_parent / relative).as_posix()
        matches = [
            evidence
            for evidence in result.evidence
            if evidence.kind == "other" and evidence.relative_path == evidence_path
        ]
        if len(matches) != 1:
            return None, None, "GUI pre-action frame is not exact hash-bound evidence"
        payload, error = _read_evidence_bytes(root=root, evidence=matches[0])
        if error is not None or payload is None:
            return None, None, error or "GUI pre-action frame is unreadable"
        try:
            from io import BytesIO

            from PIL import Image, UnidentifiedImageError

            with Image.open(BytesIO(payload)) as image:
                if image.format != "PNG":
                    return None, None, "GUI pre-action frame is not a PNG"
                width, height = image.size
        except (OSError, UnidentifiedImageError):
            return None, None, "GUI pre-action frame is not a valid image"
        if width < 1 or height < 1:
            return None, None, "GUI pre-action frame has invalid dimensions"
        expected_path = (run_evidence_root / relative).resolve()
        if expected_path != (root / evidence_path).resolve():
            return None, None, "GUI pre-action frame is outside its run evidence root"
        return (width, height), hashlib.sha256(payload).hexdigest(), None

    def retained_run_artifact(
        relative_path: str,
        expected_sha256: str,
        *,
        label: str,
    ) -> tuple[Optional[bytes], Optional[str]]:
        """Read one exact signed artifact relative to the retained run report."""

        if "\\" in relative_path:
            return None, f"{label} path is not run-relative"
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            return None, f"{label} path is not run-relative"
        report_parent = PurePosixPath(report_ref.relative_path).parent
        evidence_path = (report_parent / relative).as_posix()
        matches = [
            evidence
            for evidence in result.evidence
            if evidence.kind == "other"
            and evidence.relative_path == evidence_path
            and evidence.sha256 == expected_sha256
        ]
        if len(matches) != 1:
            return None, f"{label} is not exact hash-bound evidence"
        payload, error = _read_evidence_bytes(root=root, evidence=matches[0])
        if error is not None or payload is None:
            return None, error or f"{label} is unreadable"
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            return None, f"{label} bytes do not match their digest"
        expected_path = (run_evidence_root / relative).resolve()
        if expected_path != (root / evidence_path).resolve():
            return None, f"{label} is outside its run evidence root"
        return payload, None

    def exact_visual_resolution_error(
        resolution: Any,
        anchor: Any,
        *,
        step: "Step",
    ) -> Optional[str]:
        """Re-run one visual resolve from its exact signed runtime inputs."""

        evidence = resolution.visual_evidence
        if evidence is None:
            return "visual resolution lacks independently reproducible evidence"
        from openadapt_flow import vision as vision_module
        from openadapt_flow.runtime.resolver import (
            resolve as resolve_target,
        )
        from openadapt_flow.runtime.resolver import (
            visual_resolution_evaluator_contract_sha256,
        )

        if (
            evidence.evaluator_contract_sha256
            != visual_resolution_evaluator_contract_sha256()
        ):
            return "visual resolution used a different evaluator contract"
        from openadapt_flow.runtime.resolver import (
            visual_resolution_anchor_contract_sha256,
        )

        if evidence.anchor_contract_sha256 != visual_resolution_anchor_contract_sha256(
            anchor,
            template_sha256=evidence.template_sha256,
            allow_target_ocr=evidence.allow_target_ocr,
        ):
            return "visual resolution differs from the compiled anchor contract"
        frame_png, frame_error = retained_run_artifact(
            evidence.frame_inventory_ref,
            evidence.frame_sha256,
            label="visual resolution frame",
        )
        template_png, template_error = retained_run_artifact(
            evidence.template_inventory_ref,
            evidence.template_sha256,
            label="visual resolution template",
        )
        if frame_error is not None or frame_png is None:
            return frame_error or "visual resolution frame is unavailable"
        if template_error is not None or template_png is None:
            return template_error or "visual resolution template is unavailable"
        if (
            workflow.manifest is None
            or workflow.manifest.file_hashes.get(anchor.template)
            != evidence.template_sha256
        ):
            return "visual resolution template differs from the compiled bundle"
        try:
            reproduced = resolve_target(
                anchor,
                frame_png,
                vision_module,
                None,
                step.intent,
                template_png=template_png,
                structural=None,
                allow_target_ocr=evidence.allow_target_ocr,
            )
        except Exception as exc:  # the retained proof must reproduce cleanly
            return (
                "visual resolution evaluator refused retained inputs: "
                f"{type(exc).__name__}"
            )
        if reproduced is None:
            return "visual resolution does not reproduce from retained inputs"
        actual, matched_region = reproduced
        if (
            actual.rung != resolution.rung
            or actual.point != resolution.point
            or abs(actual.confidence - resolution.confidence) > 1e-9
            or matched_region != evidence.matched_region
        ):
            return "visual resolution differs from exact evaluator output"
        return None

    def one_resolution_error(
        resolution: Any,
        anchor: Any,
        *,
        frame_size: tuple[int, int],
        step: "Step",
    ) -> Optional[str]:
        """Validate one target resolution against its anchor and exact frame."""

        width, height = frame_size
        x, y = resolution.point
        if (
            not math.isfinite(resolution.confidence)
            or not 0.0 < resolution.confidence <= 1.0
            or not math.isfinite(resolution.elapsed_ms)
            or resolution.elapsed_ms < 0.0
            or resolution.elapsed_ms > (step.timeout_s * 1000.0 + 1000.0)
            or not (0 <= x < width and 0 <= y < height)
        ):
            return "resolution values are outside the exact retained frame"

        if resolution.rung == "structural":
            handle = resolution.structural_handle
            if (
                anchor.structural is None
                or handle is None
                or resolution.visual_evidence is not None
                or handle.candidate_count != 1
                or resolution.point != handle.point
                or abs(resolution.confidence - handle.confidence) > 1e-9
            ):
                return "structural resolution does not match its exact target handle"
            if handle.region is not None:
                hx, hy = handle.point
                rx, ry, rw, rh = handle.region
                if (
                    rw < 1
                    or rh < 1
                    or rx < 0
                    or ry < 0
                    or rx + rw > width
                    or ry + rh > height
                    or not (rx <= hx < rx + rw and ry <= hy < ry + rh)
                ):
                    return "structural target handle is outside the exact frame"
            return None

        if resolution.structural_handle is not None:
            return "visual resolution contains an incompatible structural handle"
        visual_error = exact_visual_resolution_error(
            resolution,
            anchor,
            step=step,
        )
        if visual_error is not None:
            return visual_error
        if resolution.rung in {"template", "template_global"}:
            from openadapt_flow.runtime.resolver import TEMPLATE_THRESHOLD

            if anchor.template is None or resolution.confidence < TEMPLATE_THRESHOLD:
                return "template resolution is not supported by the compiled anchor"
            if resolution.rung == "template":
                rx, ry, rw, rh = anchor.region
                pad = anchor.search_pad
                if not (
                    max(0, rx - pad) <= x < min(width, rx + rw + pad)
                    and max(0, ry - pad) <= y < min(height, ry + rh + pad)
                ):
                    return "local template resolution lies outside its search region"
            return None
        if resolution.rung == "ocr":
            from openadapt_flow.runtime.resolver import OCR_MIN_RATIO

            if anchor.ocr_text is None or resolution.confidence < OCR_MIN_RATIO:
                return "OCR resolution is not supported by the compiled anchor"
            return None
        if resolution.rung == "geometry":
            if not anchor.landmarks or resolution.confidence > 0.9:
                return "geometry resolution is not supported by retained landmarks"
            return None
        if resolution.rung == "grounder":
            if report.model_calls < 1:
                return "grounder resolution lacks its exact model-call accounting"
            return None
        return "GUI action uses an unknown resolution rung"

    def resolution_shape_error(
        item: Any,
        step: "Step",
        *,
        actuation_path: Literal["gui", "api"],
        require_resolution: bool,
        require_drag_end: bool,
        allow_optional_resolution: bool = False,
    ) -> Optional[str]:
        """Reject resolution records that the selected runtime path cannot emit."""

        from openadapt_flow.ir import ActionKind

        if actuation_path == "api":
            if (
                item.resolution is not None
                or item.drag_end_resolution is not None
                or item.starting_state_settled is not None
            ):
                return "API action contains GUI resolution or settling evidence"
            return None

        if item.starting_state_settled is not True:
            return "GUI action lacks the exact settled-state observation"
        resolution = item.resolution
        if require_resolution and resolution is None:
            return "GUI action lacks its required target resolution"
        if (
            not require_resolution
            and resolution is not None
            and not allow_optional_resolution
        ):
            return (
                "GUI refusal claims a target resolution that its detector cannot emit"
            )
        if resolution is None:
            if item.drag_end_resolution is not None:
                return "GUI action has a drag endpoint without a source resolution"
            return None

        anchor = step.anchor
        if anchor is None:
            return "GUI action claims a resolution for an anchorless step"
        frame_size, before_frame_sha256, frame_error = retained_before_frame_size(item)
        if frame_error is not None or frame_size is None:
            return frame_error or "GUI action lacks its exact pre-action frame"
        if resolution.rung != "structural":
            visual = resolution.visual_evidence
            if visual is None:
                return "GUI action lacks exact visual resolution evidence"
            allowed_observation_hashes = {before_frame_sha256}
            if (
                report.qualification_fault_mutations
                and report.qualification_fault_mutations[0].step_id_sha256
                == sha256_bytes(step.id.encode("utf-8"))
            ):
                allowed_observation_hashes.add(
                    report.qualification_fault_mutations[0].before_input_sha256
                )
            if visual.frame_sha256 not in allowed_observation_hashes:
                return "visual resolution frame is not bound to the fault sequence"
        source_error = one_resolution_error(
            resolution,
            anchor,
            frame_size=frame_size,
            step=step,
        )
        if source_error is not None:
            return source_error

        if step.action is not ActionKind.DRAG:
            if item.drag_end_resolution is not None:
                return "non-drag action contains a drag endpoint resolution"
            return None
        if not require_drag_end:
            if item.drag_end_resolution is not None:
                return "pre-delivery drag refusal contains an unreached endpoint"
            return None
        if item.drag_end_resolution is None or step.drag_end_anchor is None:
            return "drag action lacks its independently resolved endpoint"
        endpoint_error = one_resolution_error(
            item.drag_end_resolution,
            step.drag_end_anchor,
            frame_size=frame_size,
            step=step,
        )
        if endpoint_error is not None:
            return f"drag endpoint: {endpoint_error}"
        return None

    def delivery_receipt_error(item: Any, step: "Step") -> Optional[str]:
        """Bind one GUI delivery receipt to its exact action and resolution."""

        from openadapt_flow.ir import ActionKind

        receipt = item.delivery_receipt
        if receipt is None:
            return "GUI action lacks its exact delivery receipt"
        allowed_receipts = {
            ("uia", ActionKind.CLICK): {
                ("uia_invoke", True),
                ("win32_bm_click", True),
                ("uia_focus", True),
                ("uia_toggle", True),
                ("uia_select", True),
                ("atspi_invoke", True),
                ("atspi_focus", True),
                ("atspi_toggle", True),
                ("atspi_select", True),
            },
            ("dom", ActionKind.CLICK): {
                ("dom_click", False),
                ("physical_click", False),
            },
            ("guarded_coordinate", ActionKind.CLICK): {
                ("guarded_coordinate_click", False),
                ("physical_click", False),
            },
            ("dom", ActionKind.DOUBLE_CLICK): {
                ("dom_double_click", False),
                ("physical_double_click", False),
            },
            ("guarded_coordinate", ActionKind.DOUBLE_CLICK): {
                ("guarded_coordinate_double_click", False),
                ("physical_double_click", False),
            },
            ("guarded_coordinate", ActionKind.RIGHT_CLICK): {
                ("guarded_coordinate_right_click", False),
                ("physical_right_click", False),
            },
            ("remote_guarded", ActionKind.CLICK): {
                ("rdp_click", False),
                ("remote_click", False),
            },
            ("remote_guarded", ActionKind.DOUBLE_CLICK): {
                ("rdp_double_click", False),
                ("remote_double_click", False),
            },
            ("remote_guarded", ActionKind.RIGHT_CLICK): {
                ("rdp_right_click", False),
                ("remote_right_click", False),
            },
            ("dom", ActionKind.DRAG): {
                ("guarded_dom_drag", False),
            },
            ("guarded_coordinate", ActionKind.DRAG): {
                ("guarded_coordinate_drag", False),
                ("physical_drag", False),
            },
            ("remote_guarded", ActionKind.DRAG): {
                ("rdp_drag", False),
                ("remote_drag", False),
            },
            ("guarded_keyboard", ActionKind.TYPE): {
                ("guarded_dom_type", False),
                ("guarded_atspi_type", True),
                ("physical_type_text", False),
            },
            ("remote_guarded", ActionKind.SELECT_OPTION): {
                ("rdp_select_option", False),
                ("remote_select_option", False),
            },
            ("guarded_keyboard", ActionKind.SELECT_OPTION): {
                ("guarded_select_option", False),
            },
            ("guarded_keyboard", ActionKind.KEY): {
                ("guarded_dom_key", False),
                ("guarded_atspi_key", False),
                ("physical_press", False),
            },
            ("guarded_keyboard", ActionKind.HOTKEY): {
                ("guarded_dom_key", False),
                ("guarded_atspi_key", False),
                ("physical_press", False),
            },
        }.get((item.actuation, step.action), set())
        if step.choice is not None:
            # docs/CHOICES.md: proven by its read-back, not a delivery receipt.
            return "dropdown choice steps carry no delivery receipt; refusing"
        if (
            step.action is ActionKind.SELECT_OPTION
            and step.native_selection is not None
        ):
            allowed_receipts = (
                {("guarded_dom_select", False)} if item.actuation == "dom" else set()
            )
        if (receipt.operation, receipt.native) not in allowed_receipts:
            return "delivery receipt operation conflicts with the compiled action"

        source_handle = (
            item.resolution.structural_handle
            if item.resolution is not None and item.resolution.rung == "structural"
            else None
        )
        if (
            step.action is not ActionKind.DRAG
            and step.native_selection is None
            and item.actuation in {"uia", "dom"}
            and source_handle is not None
        ):
            if (
                source_handle.target_fingerprint is None
                or receipt.target_fingerprint != source_handle.target_fingerprint
            ):
                return "delivery receipt does not bind the resolved structural target"

        if (
            step.action
            in {
                ActionKind.CLICK,
                ActionKind.DOUBLE_CLICK,
                ActionKind.RIGHT_CLICK,
            }
            and item.actuation == "remote_guarded"
        ):
            source = item.resolution
            if source is None or source.visual_evidence is None:
                return "remote delivery receipt lacks exact visual target proof"
            from openadapt_flow.runtime.resolver import (
                visual_resolution_point_fingerprint,
            )

            expected_target = visual_resolution_point_fingerprint(
                source.visual_evidence.frame_sha256,
                source.point,
            )
            if receipt.target_fingerprint != expected_target:
                return "remote delivery receipt does not bind the resolved target"

        if step.action is ActionKind.DRAG:
            from openadapt_flow.runtime.resolver import (
                structural_resolution_fingerprint,
                visual_resolution_point_fingerprint,
            )

            source = item.resolution
            if source is None or step.anchor is None:
                return "drag delivery receipt lacks its resolved source"
            if source.rung == "structural":
                source_observation = source.structural_handle
                source_locator = step.anchor.structural
                if source_observation is None or source_locator is None:
                    return "drag delivery receipt lacks exact structural source proof"
                expected_source = structural_resolution_fingerprint(
                    source_locator,
                    source_observation,
                )
            else:
                source_visual = source.visual_evidence
                if source_visual is None:
                    return "drag delivery receipt lacks exact visual source proof"
                expected_source = visual_resolution_point_fingerprint(
                    source_visual.frame_sha256,
                    source.point,
                )
            if receipt.target_fingerprint != expected_source:
                return "delivery receipt does not bind the resolved drag source"
            endpoint = item.drag_end_resolution
            if endpoint is None:
                return "drag delivery receipt lacks its resolved destination"
            if endpoint.rung == "structural":
                endpoint_handle = endpoint.structural_handle
                endpoint_locator = (
                    step.drag_end_anchor.structural
                    if step.drag_end_anchor is not None
                    else None
                )
                expected_destination = (
                    structural_resolution_fingerprint(
                        endpoint_locator,
                        endpoint_handle,
                    )
                    if endpoint_handle is not None and endpoint_locator is not None
                    else None
                )
            else:
                visual = endpoint.visual_evidence
                if visual is None:
                    return "drag delivery receipt lacks exact visual destination proof"
                expected_destination = visual_resolution_point_fingerprint(
                    visual.frame_sha256,
                    endpoint.point,
                )
            if (
                expected_destination is None
                or receipt.destination_fingerprint != expected_destination
            ):
                return "delivery receipt does not bind the resolved drag destination"
        elif receipt.destination_fingerprint is not None:
            return "non-drag delivery receipt contains a destination fingerprint"

        if step.action is not ActionKind.SELECT_OPTION and (
            receipt.selection_value_sha256 is not None
            or receipt.selection_commit_key is not None
        ):
            return "non-selection delivery receipt contains selection metadata"
        if step.action is ActionKind.SELECT_OPTION:
            if step.native_selection is not None:
                if (
                    step.param is not None
                    or step.selection_commit_key is not None
                    or step.selection_region is not None
                    or step.text != step.native_selection.label
                ):
                    return (
                        "native selection delivery receipt has a mixed input contract"
                    )
                selected_text = step.native_selection.value
            else:
                scoped = scoped_case_params(item)
                if scoped is None:
                    return "selection delivery receipt lacks exact parameter scope"
                selected_value = (
                    scoped.get(step.param) if step.param is not None else step.text
                )
                if selected_value is None or step.selection_commit_key is None:
                    return (
                        "selection delivery receipt lacks its compiled input contract"
                    )
                selected_text = runtime_param_text(selected_value)
            if (
                receipt.selection_value_sha256
                != hashlib.sha256(selected_text.encode("utf-8")).hexdigest()
                or receipt.selection_commit_key != step.selection_commit_key
            ):
                return "selection delivery receipt differs from the compiled input"
            selection_resolution = item.resolution
            if selection_resolution is None or step.anchor is None:
                return "selection delivery receipt lacks its resolved target"
            if selection_resolution.rung == "structural":
                selection_handle = selection_resolution.structural_handle
                selection_locator = step.anchor.structural
                if (
                    selection_handle is None
                    or selection_locator is None
                    or (
                        step.native_selection is not None
                        and selection_handle.target_fingerprint is None
                    )
                ):
                    return "selection delivery receipt lacks structural target proof"
                from openadapt_flow.runtime.resolver import (
                    structural_resolution_fingerprint,
                )

                expected_target = structural_resolution_fingerprint(
                    selection_locator,
                    selection_handle,
                )
            else:
                if step.native_selection is not None:
                    return "native selection delivery receipt lacks structural target proof"
                selection_visual = selection_resolution.visual_evidence
                if selection_visual is None:
                    return "selection delivery receipt lacks visual target proof"
                from openadapt_flow.runtime.resolver import (
                    visual_resolution_point_fingerprint,
                )

                expected_target = visual_resolution_point_fingerprint(
                    selection_visual.frame_sha256,
                    selection_resolution.point,
                )
            if receipt.target_fingerprint != expected_target:
                return "selection delivery receipt does not bind its resolved target"

        try:
            delivered_at = datetime.fromisoformat(receipt.delivered_at)
            report_started = datetime.fromisoformat(report.started_at)
            result_completed = datetime.fromisoformat(result.completed_at)
        except ValueError:
            return "delivery receipt timestamp is not an exact ISO instant"
        if (
            delivered_at.tzinfo is None
            or report_started.tzinfo is None
            or result_completed.tzinfo is None
            or delivered_at < report_started
            or delivered_at > result_completed
        ):
            return "delivery receipt timestamp is outside the exact run interval"
        return None

    def identity_evidence_error(
        item: Any,
        step: "Step",
        *,
        actuation_path: Literal["gui", "api"],
        expected_status: Literal["verified", "mismatch"],
    ) -> Optional[str]:
        identity_policy = project.identity_policies.get(step.id)
        scoped = scoped_case_params(item)
        if identity_policy is None or scoped is None:
            return "action lacks its exact qualified identity policy or parameter scope"
        from openadapt_flow.qualification_identity_evidence import (
            qualification_identity_evidence_error,
            qualification_identity_refusal_evidence_error,
        )

        recorded_asset_sha256 = (
            workflow.manifest.file_hashes.get(step.anchor.identifier_crop)
            if workflow.manifest is not None
            and step.anchor is not None
            and step.anchor.identifier_crop is not None
            else None
        )

        if expected_status == "mismatch":
            return qualification_identity_refusal_evidence_error(
                policy=identity_policy,
                check=item.identity,
                step=step,
                actuation_path=actuation_path,
                runtime_params=runtime_params_for_gui(scoped),
                recorded_params=workflow.params,
                evidence_root=run_evidence_root,
                recorded_asset_sha256=recorded_asset_sha256,
            )
        return qualification_identity_evidence_error(
            policy=identity_policy,
            check=item.identity,
            step=step,
            actuation_path=actuation_path,
            runtime_params=runtime_params_for_gui(scoped),
            recorded_params=workflow.params,
            evidence_root=run_evidence_root,
            recorded_asset_sha256=recorded_asset_sha256,
        )

    def resolved_effect_contracts(
        item: Any,
        step: "Step",
        *,
        actuation_path: Literal["gui", "api"],
    ) -> Optional[list[tuple[int, str, Any]]]:
        from openadapt_flow.policy import effects_for_actuation

        runtime_actuation = "api" if actuation_path == "api" else None
        effects = effects_for_actuation(step, runtime_actuation)
        scoped = scoped_case_params(item)
        if not effects or scoped is None:
            return None
        if any(
            effect.referenced_params().intersection(workflow.secret_params)
            for effect in effects
        ):
            return None
        try:
            resolved = [
                (
                    index,
                    effect.resolved_contract_hash(
                        runtime_params_for_gui(scoped),
                        opaque_param_sha256={"__run_id__": result.run_id_sha256 or ""},
                    ),
                    effect_policies.get((step.id, actuation_path, index)),
                )
                for index, effect in enumerate(effects)
            ]
        except ValueError:
            return None
        if any(
            binding is None
            or binding.effect_contract_hash != effects[index].contract_hash()
            for index, _effect_hash, binding in resolved
        ):
            return None
        return resolved

    def exact_confirmed_effect_error(
        item: Any,
        step: "Step",
        *,
        actuation_path: Literal["gui", "api"],
    ) -> Optional[str]:
        resolved = resolved_effect_contracts(
            item,
            step,
            actuation_path=actuation_path,
        )
        if resolved is None:
            return "action effects do not match the exact qualified contracts"
        expected_hashes = Counter(effect_hash for _index, effect_hash, _ in resolved)
        if Counter(item.effect_contract_hashes) != expected_hashes:
            return "action effect hashes do not match the exact resolved contracts"

        required_by_hash: dict[str, list[VerificationTier]] = {}
        for _index, effect_hash, binding in resolved:
            assert binding is not None
            required_by_hash.setdefault(effect_hash, []).append(
                min(binding.tier, minimum_tier)
            )
        observed_by_hash: dict[str, list[VerificationTier]] = {}
        for evidence in item.effect_evidence:
            if (
                evidence.initial_verdict != "confirmed"
                or evidence.final_verdict != "confirmed"
                or evidence.observed_effect != "present"
                or evidence.reconciliation_completed
                or evidence.reconciliation_actions
                or evidence.verification_tier is None
            ):
                return "action contains non-confirming effect evidence"
            try:
                tier = VerificationTier(evidence.verification_tier)
            except ValueError:
                return "action contains an invalid effect evidence tier"
            observed_by_hash.setdefault(evidence.effect_contract_hash, []).append(tier)
        if set(observed_by_hash) != set(required_by_hash):
            return "action effect evidence does not cover the exact contract multiset"
        for effect_hash, required_tiers in required_by_hash.items():
            observed_tiers = observed_by_hash[effect_hash]
            if len(observed_tiers) != len(required_tiers) or any(
                not observed.satisfies(required)
                for observed, required in zip(
                    sorted(observed_tiers), sorted(required_tiers), strict=True
                )
            ):
                return "action effect evidence is weaker than its qualified contract"
        return None

    expected_case_sha256 = sha256_bytes(case.id.encode("utf-8"))
    expected_action_paths = {
        target.step_id: target.actuation_path for target in case.action_targets
    }
    expected_workflow_contract = workflow_contract_sha256(workflow)
    expected_outcome = result.observed_outcome.value.upper()
    expected_bindings = (
        report.workflow_name == workflow.name,
        report.workflow_contract_sha256
        == result.workflow_contract_sha256
        == expected_workflow_contract,
        report.governed_qualification_project_id == project.project_id,
        report.governed_qualification_project_revision == project.revision,
        report.governed_qualification_project_contract_sha256
        == project.contract_sha256(),
        report.governed_qualification_campaign_id_sha256 == result.campaign_id_sha256,
        report.governed_qualification_case_id_sha256 == expected_case_sha256,
        report.governed_qualification_case_input_sha256 == result.case_input_sha256,
        report.governed_runtime_inputs_digest == result.case_input_sha256,
        report.governed_qualification_run_id_sha256 == result.run_id_sha256,
        report.run_id_sha256 == result.run_id_sha256,
        report.governed_qualification_case_kind == case.kind.value,
        report.governed_qualification_case_action_paths == expected_action_paths,
        report.execution_outcome == expected_outcome,
        report.execution_profile == "standard",
        report.governed_minimum_effect_tier == int(project.minimum_effect_tier),
        report.params == case_params,
    )
    if not all(expected_bindings):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case report, input, signed result, and project bindings differ",
        )

    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        classify_execution_outcome,
    )

    recomputed_outcome = classify_execution_outcome(
        report,
        workflow,
        ExecutionProfile.STANDARD,
        runtime_worklists=case_worklists,
        transition_evidence_root=run_evidence_root,
        transition_predicate_vision=transition_predicate_vision,
    ).value
    if (
        recomputed_outcome != report.execution_outcome
        or recomputed_outcome != expected_outcome
    ):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case report outcome does not recompute from its exact step evidence",
        )
    try:
        expected_envelope = build_outcome_envelope(
            report,
            workflow,
            runtime_worklists=case_worklists,
        )
    except ValueError:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case report outcome envelope cannot be recomputed",
        )
    if report.outcome_envelope != expected_envelope:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case report outcome envelope differs from its exact contract evidence",
        )
    if case.kind is not QualificationCaseKind.REPRESENTATIVE:
        fault_target = case.resolved_fault_target()
        if fault_target is None:
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                "fault case does not identify its exact refusal target",
            )
        if report.execution_completed is not False or report.success:
            return (
                QualificationRefusalCode.CASE_NOT_PASSED,
                "fault case did not retain an incomplete halted execution state",
            )
        if workflow.program is None:
            if report.terminal_outcome is not None:
                return (
                    QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                    "linear fault case reports an inconsistent terminal outcome",
                )
        elif report.terminal_outcome != "halt":
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                "program fault case did not terminate at the detector halt",
            )
        if report.halt is not None and report.halt.outcome != (
            report.terminal_outcome or "halt"
        ):
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                "fault case halt evidence conflicts with its terminal outcome",
            )
        action_results = (
            report.results[:-1]
            if workflow.program is not None
            and report.results
            and report.results[-1].step_id == "<terminal>"
            else report.results
        )
        api_folded_ids = api_folded_step_ids(workflow, expected_action_paths)
        prior_action_results = [
            item
            for item in action_results[:-1]
            if not (
                item.actuation == "api_folded"
                and item.skipped
                and item.step_id in api_folded_ids
                and item.step_id not in expected_action_paths
            )
        ]
        if any(
            item.step_id in expected_action_paths
            and expected_action_paths[item.step_id]
            != _qualification_actuation_path(item.actuation)
            for item in prior_action_results
        ):
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                "fault case executed an action outside its authorized path map",
            )
        # The profile classifier proves the ordered action trace and the broad
        # production outcome. Qualification also proves the exact compiled
        # action, path, identity policy, and resolved effect contracts.
        from openadapt_flow.ir import ActionKind
        from openadapt_flow.policy import executable_actuation_paths

        for item in prior_action_results:
            step = steps_by_id.get(item.step_id)
            if (
                step is not None
                and step.action in {ActionKind.WAIT, ActionKind.SCROLL}
                and item.actuation is None
                and item.step_id not in expected_action_paths
                and item.step_id not in required_actions
            ):
                # A viewport scroll or wait is classified READ_ONLY (it never
                # actuates business state) and carries no actuation receipt,
                # so it has no qualified path to prove.
                continue
            observed_path = _qualification_actuation_path(item.actuation)
            actuation_path = expected_action_paths.get(item.step_id, observed_path)
            if (
                step is None
                or actuation_path is None
                or actuation_path not in executable_actuation_paths(step)
                or (
                    item.step_id in required_actions
                    and item.step_id not in expected_action_paths
                )
            ):
                return (
                    QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                    "fault case prior action is outside its exact qualified scope",
                )
            metadata_error = exact_step_metadata_error(item, step)
            if metadata_error is not None:
                return (
                    QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                    f"fault case prior action {item.step_id!r}: {metadata_error}",
                )
            if actuation_path == "api":
                path_shape_invalid = (
                    item.actuation != "api"
                    or item.delivery_receipt is not None
                    or item.delivery_uncertainty is not None
                )
                delivery_error = None
            else:
                path_shape_invalid = (
                    item.actuation not in AUTOMATED_GUI_ACTUATIONS
                    or item.delivery_uncertainty is not None
                )
                delivery_error = delivery_receipt_error(item, step)
            resolution_error = resolution_shape_error(
                item,
                step,
                actuation_path=actuation_path,
                require_resolution=(
                    actuation_path == "gui" and step.anchor is not None
                ),
                require_drag_end=True,
            )
            if (
                path_shape_invalid
                or delivery_error is not None
                or resolution_error is not None
                or not item.ok
                or item.safety_halt
                or item.failure_category is not None
                or item.error is not None
                or item.delivery_attempted is not True
                or item.safety_refusal_evidence is not None
            ):
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    "fault case prior action lacks its exact delivery and resolution "
                    f"proof: {delivery_error or resolution_error or item.step_id}",
                )
            if (
                item.step_id in required_identity
                or item.step_id in project.identity_policies
            ):
                identity_error = identity_evidence_error(
                    item,
                    step,
                    actuation_path=actuation_path,
                    expected_status="verified",
                )
                if identity_error is not None:
                    return (
                        QualificationRefusalCode.CASE_NOT_PASSED,
                        "fault case prior action identity is not exact: "
                        f"{identity_error}",
                    )
            if item.step_id in required_actions:
                if item.effect_verified is not True or item.effect_approved_unverified:
                    return (
                        QualificationRefusalCode.CASE_NOT_PASSED,
                        "fault case prior action did not confirm its required effect",
                    )
                effect_error = exact_confirmed_effect_error(
                    item,
                    step,
                    actuation_path=actuation_path,
                )
                if effect_error is not None:
                    return (
                        QualificationRefusalCode.CASE_NOT_PASSED,
                        f"fault case prior action effect is not exact: {effect_error}",
                    )

        prior_contract_outcome = classify_execution_outcome(
            report,
            workflow,
            ExecutionProfile.STANDARD,
            runtime_worklists=case_worklists,
            transition_evidence_root=run_evidence_root,
            transition_predicate_vision=transition_predicate_vision,
            _qualification_fault_target_step_id=fault_target.step_id,
        ).value
        if prior_contract_outcome != "VERIFIED":
            return (
                QualificationRefusalCode.CASE_NOT_PASSED,
                "fault case prior actions lack complete production evidence",
            )

        if not action_results:
            return (
                QualificationRefusalCode.CASE_NOT_PASSED,
                "fault case contains no refusal target result",
            )
        target_result = action_results[-1]
        target_step = steps_by_id.get(fault_target.step_id)
        if target_step is None or target_result.step_id != fault_target.step_id:
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                "fault refusal row does not match the exact qualified target",
            )
        metadata_error = exact_step_metadata_error(target_result, target_step)
        if metadata_error is not None:
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                f"fault refusal target: {metadata_error}",
            )
        target_resolution_required = (
            fault_target.actuation_path == "gui"
            and case.kind
            in {
                QualificationCaseKind.WRONG_IDENTITY,
                QualificationCaseKind.WEAK_EFFECT,
                QualificationCaseKind.MISSING_EFFECT,
            }
        )
        target_resolution_error = resolution_shape_error(
            target_result,
            target_step,
            actuation_path=fault_target.actuation_path,
            require_resolution=target_resolution_required,
            allow_optional_resolution=(
                fault_target.actuation_path == "gui"
                and case.kind is QualificationCaseKind.STALE_IDENTITY
            ),
            require_drag_end=False,
        )
        if target_resolution_error is not None:
            return (
                QualificationRefusalCode.CASE_NOT_PASSED,
                "fault refusal target resolution is not exact: "
                f"{target_resolution_error}",
            )

        if case.kind is QualificationCaseKind.AMBIGUITY:
            if target_result.identity is not None:
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    "ambiguity refusal contains identity evidence from an unreached gate",
                )
        elif case.kind is QualificationCaseKind.WRONG_IDENTITY:
            identity_error = identity_evidence_error(
                target_result,
                target_step,
                actuation_path=fault_target.actuation_path,
                expected_status="mismatch",
            )
            if identity_error is not None:
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    f"wrong-identity refusal is not exact: {identity_error}",
                )
        elif case.kind is QualificationCaseKind.STALE_IDENTITY:
            status = (
                target_result.identity.status
                if target_result.identity is not None
                else None
            )
            if status not in {"verified", "mismatch"}:
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    "stale-identity refusal lacks an exact definitive identity verdict",
                )
            if status == "mismatch" and target_result.resolution is None:
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    "stale-identity refusal claims an identity mismatch without "
                    "the target resolution required to observe it",
                )
            stale_status: Literal["verified", "mismatch"] = (
                "mismatch" if status == "mismatch" else "verified"
            )
            identity_error = identity_evidence_error(
                target_result,
                target_step,
                actuation_path=fault_target.actuation_path,
                expected_status=stale_status,
            )
            if identity_error is not None:
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    f"stale-identity refusal is not exact: {identity_error}",
                )
        else:
            identity_error = identity_evidence_error(
                target_result,
                target_step,
                actuation_path=fault_target.actuation_path,
                expected_status="verified",
            )
            if identity_error is not None:
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    f"fault refusal target identity is not exact: {identity_error}",
                )

        if case.kind is QualificationCaseKind.WEAK_EFFECT:
            resolved = resolved_effect_contracts(
                target_result,
                target_step,
                actuation_path=fault_target.actuation_path,
            )
            expected_hashes = (
                Counter(effect_hash for _index, effect_hash, _binding in resolved)
                if resolved is not None
                else None
            )
            if (
                expected_hashes is None
                or Counter(target_result.effect_contract_hashes) != expected_hashes
                or target_result.effect_verified is not False
                or target_result.effect_approved_unverified
                or target_result.effect_evidence
                or target_result.effect_results != [target_result.error]
            ):
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    "weak-effect refusal does not bind the exact resolved effect set",
                )
        elif case.kind is QualificationCaseKind.MISSING_EFFECT:
            resolved = resolved_effect_contracts(
                target_result,
                target_step,
                actuation_path=fault_target.actuation_path,
            )
            expected_message = (
                "API binding present but no EffectVerifier is configured to confirm "
                "the write (fail-safe HALT)"
                if fault_target.actuation_path == "api"
                else "no EffectVerifier configured for a step that declares effects "
                "(fail-safe HALT)"
            )
            if (
                resolved is None
                or target_result.effect_contract_hashes
                or target_result.effect_verified is not False
                or target_result.effect_approved_unverified
                or target_result.effect_evidence
                or target_result.effect_results != [expected_message]
            ):
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    "missing-effect refusal does not bind its exact negative artifact",
                )
        elif case.kind is QualificationCaseKind.STALE_IDENTITY:
            resolved = resolved_effect_contracts(
                target_result,
                target_step,
                actuation_path=fault_target.actuation_path,
            )
            expected_hashes = (
                Counter(effect_hash for _index, effect_hash, _binding in resolved)
                if resolved is not None
                else None
            )
            if (
                expected_hashes is None
                or Counter(target_result.effect_contract_hashes) != expected_hashes
                or target_result.effect_verified is not None
                or target_result.effect_approved_unverified
                or target_result.effect_results
                or target_result.effect_evidence
            ):
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    "stale-identity refusal does not bind its exact pre-actuation "
                    "effect contracts",
                )
        elif (
            target_result.effect_verified is not None
            or target_result.effect_results
            or target_result.effect_contract_hashes
            or target_result.effect_evidence
        ):
            return (
                QualificationRefusalCode.CASE_NOT_PASSED,
                "pre-effect fault refusal contains evidence from an unreached gate",
            )
    if policy is not None:
        from openadapt_flow.policy import policy_contract_sha256

        if (
            report.governed_policy_name != policy.name
            or report.governed_policy_contract_sha256 != policy_contract_sha256(policy)
        ):
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                "case report was not governed by the certification policy",
            )

    environment_values = (
        report.execution_target_kind,
        report.observed_application_sha256,
        report.observed_application_version_sha256,
        report.observed_session_sha256,
        report.observed_environment_digest,
        report.qualification_environment_observer_id,
        report.qualification_environment_observer_contract_sha256,
    )
    if any(value is None for value in environment_values):
        return (
            QualificationRefusalCode.CASE_ENVIRONMENT_CHANGED,
            "case report lacks its exact environment observation",
        )
    assert report.execution_target_kind is not None
    assert report.observed_application_sha256 is not None
    assert report.observed_application_version_sha256 is not None
    assert report.observed_session_sha256 is not None
    assert report.observed_environment_digest is not None
    assert report.qualification_environment_observer_id is not None
    assert report.qualification_environment_observer_contract_sha256 is not None
    observed_binding = qualification_environment_binding_sha256(
        target_kind=report.execution_target_kind,
        observer_id=report.qualification_environment_observer_id,
        observer_contract_sha256=(
            report.qualification_environment_observer_contract_sha256
        ),
        application_identity_sha256=report.observed_application_sha256,
        application_version_sha256=report.observed_application_version_sha256,
        environment_digest=report.observed_environment_digest,
        session_identity_sha256=report.observed_session_sha256,
    )
    expected_application_identity = project.environment.expected_application_identity
    if (
        report.execution_target_kind != project.environment.target_kind
        or expected_application_identity is None
        or report.observed_application_sha256
        != hashlib.sha256(expected_application_identity.encode("utf-8")).hexdigest()
        or report.observed_application_version_sha256
        != hashlib.sha256(
            project.environment.application_version.encode("utf-8")
        ).hexdigest()
        or report.observed_environment_digest != project.environment.environment_digest
        or report.qualification_environment_observer_id
        != project.environment.environment_observer_id
        or report.qualification_environment_observer_contract_sha256
        != project.environment.environment_observer_contract_sha256
        or report.observed_environment_binding_sha256 != observed_binding
    ):
        return (
            QualificationRefusalCode.CASE_ENVIRONMENT_CHANGED,
            "case report environment binding does not match the project",
        )
    surface_refusal = _surface_environment_evidence_refusal(report, workflow, project)
    if surface_refusal is not None:
        return (QualificationRefusalCode.CASE_ENVIRONMENT_CHANGED, surface_refusal)

    if (
        not report.qualification_evidence_only
        or report.production_eligible
        or (case.kind is QualificationCaseKind.REPRESENTATIVE and not report.success)
        or (case.kind is not QualificationCaseKind.REPRESENTATIVE and report.success)
    ):
        return (
            QualificationRefusalCode.CASE_NOT_PASSED,
            "case report outcome is inconsistent with a qualification-only run",
        )
    if (
        case.kind is QualificationCaseKind.REPRESENTATIVE
        and report.qualification_fault_mutations
    ):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "representative case report contains a qualification fault mutation",
        )
    if case.kind is QualificationCaseKind.REPRESENTATIVE:
        required_actions, required_identity = qualification_action_requirements(
            workflow
        )
        declared_targets = expected_action_paths
        steps = _steps_by_id(workflow)
        effect_policies = {
            (binding.step_id, binding.actuation_path, binding.effect_index): binding
            for binding in project.effect_policies
        }
        if not declared_targets or not set(declared_targets).issubset(steps):
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                "representative case targets are missing or outside the workflow",
            )
        if not required_identity.issubset(set(report.required_identity_step_ids)):
            return (
                QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                "case report omits required project identity gates",
            )
        for step_id, actuation_path in sorted(declared_targets.items()):
            step_results = [
                item
                for item in report.results
                if item.step_id == step_id
                and not item.skipped
                and not item.exception_handled
                and _qualification_actuation_path(item.actuation) == actuation_path
            ]
            if not step_results:
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    "representative case did not execute target action "
                    f"{step_id!r} through its {actuation_path} path",
                )
            step = steps[step_id]
            from openadapt_flow.policy import effects_for_actuation
            from openadapt_flow.qualification_identity_evidence import (
                qualification_identity_evidence_error,
            )

            minimum_tier = VerificationTier(project.minimum_effect_tier)
            identity_policy = project.identity_policies.get(step_id)

            def result_has_sufficient_effect_evidence(item: Any) -> bool:
                if step_id not in required_actions:
                    return True
                effects = effects_for_actuation(step, item.actuation)
                if any(
                    effect.referenced_params().intersection(workflow.secret_params)
                    for effect in effects
                ):
                    return False
                resolved_params = scoped_case_params(item)
                if resolved_params is None:
                    return False
                try:
                    expected_effects = [
                        (
                            index,
                            effect.resolved_contract_hash(
                                runtime_params_for_gui(resolved_params),
                                opaque_param_sha256={
                                    "__run_id__": result.run_id_sha256 or ""
                                },
                            ),
                            effect_policies.get((step_id, actuation_path, index)),
                        )
                        for index, effect in enumerate(effects)
                    ]
                except ValueError:
                    return False
                if not expected_effects or any(
                    binding is None
                    for _index, _effect_hash, binding in expected_effects
                ):
                    return False
                if any(
                    binding is None
                    or binding.effect_contract_hash != effects[index].contract_hash()
                    for index, _effect_hash, binding in expected_effects
                ):
                    return False
                expected_hashes = Counter(
                    effect_hash for _index, effect_hash, _binding in expected_effects
                )
                retained_hashes = Counter(item.effect_contract_hashes)
                if retained_hashes != expected_hashes:
                    return False

                # Evidence carries the resolved contract hash, not its list
                # index.  For duplicate contracts, exact one-to-one binding is
                # therefore a multiset match by hash and strength.  Pair the
                # strongest required tier with the strongest observed tier;
                # this accepts exactly when a valid bijection exists.
                required_tiers_by_hash: dict[str, list[VerificationTier]] = {}
                for _index, effect_hash, binding in expected_effects:
                    assert binding is not None
                    required_tiers_by_hash.setdefault(effect_hash, []).append(
                        min(binding.tier, minimum_tier)
                    )

                observed_tiers_by_hash: dict[str, list[VerificationTier]] = {}
                for evidence in item.effect_evidence:
                    if (
                        evidence.final_verdict != "confirmed"
                        or evidence.initial_verdict != "confirmed"
                        or evidence.observed_effect != "present"
                        or evidence.verification_tier is None
                    ):
                        return False
                    try:
                        observed_tier = VerificationTier(evidence.verification_tier)
                    except ValueError:
                        return False
                    observed_tiers_by_hash.setdefault(
                        evidence.effect_contract_hash, []
                    ).append(observed_tier)

                if set(observed_tiers_by_hash) != set(required_tiers_by_hash):
                    return False
                for effect_hash, required_tiers in required_tiers_by_hash.items():
                    observed_tiers = observed_tiers_by_hash[effect_hash]
                    if len(observed_tiers) != len(required_tiers):
                        return False
                    for observed_tier, required_tier in zip(
                        sorted(observed_tiers),
                        sorted(required_tiers),
                    ):
                        if not observed_tier.satisfies(required_tier):
                            return False
                return True

            if any(
                not item.ok
                or item.delivery_attempted is not True
                or (step.expect and item.postconditions_ok is not True)
                or (
                    step_id in required_actions
                    and (
                        item.effect_approved_unverified
                        or item.effect_verified is not True
                        or not result_has_sufficient_effect_evidence(item)
                    )
                )
                or (
                    step_id in required_identity
                    and (
                        identity_policy is None
                        or qualification_identity_evidence_error(
                            policy=identity_policy,
                            check=item.identity,
                            step=step,
                            actuation_path=actuation_path,
                            runtime_params=(
                                runtime_params_for_gui(scoped)
                                if (scoped := scoped_case_params(item)) is not None
                                else None
                            ),
                            recorded_params=workflow.params,
                            evidence_root=run_evidence_root,
                            recorded_asset_sha256=(
                                workflow.manifest.file_hashes.get(
                                    step.anchor.identifier_crop
                                )
                                if workflow.manifest is not None
                                and step.anchor is not None
                                and step.anchor.identifier_crop is not None
                                else None
                            ),
                        )
                        is not None
                    )
                )
                for item in step_results
            ):
                return (
                    QualificationRefusalCode.CASE_NOT_PASSED,
                    f"representative action {step_id!r} lacks complete verified evidence",
                )
        for item in report.results:
            if (
                item.step_id in required_actions
                and not item.skipped
                and not item.exception_handled
                and expected_action_paths.get(item.step_id)
                != _qualification_actuation_path(item.actuation)
            ):
                return (
                    QualificationRefusalCode.CASE_ATTESTATION_INVALID,
                    "representative report executed a qualified write outside its "
                    "authorized actuation path map",
                )
    return None


def _fault_case_integrity_error(
    *,
    project: QualificationProject,
    case: QualificationCase,
    result: QualificationCaseResult,
    evidence_root: Optional[Path],
) -> Optional[tuple[QualificationRefusalCode, str]]:
    """Verify the signed receipt, detector refusal, and exact case artifacts."""

    from openadapt_flow.ir import RunReport
    from openadapt_flow.qualification_faults import (
        FaultMutationReceipt,
        fault_detector_contract_error,
        sha256_bytes,
        verify_fault_mutation_receipt,
    )

    if evidence_root is None:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            "fault-case certification requires the local evidence root",
        )
    root = evidence_root.resolve()
    refs: dict[str, EvidenceRef] = {}
    payloads: dict[str, bytes] = {}
    for kind in ("run_report", "fault_receipt", "fault_mutation"):
        ref, error = _one_evidence(result, kind)
        if error is not None or ref is None:
            return QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED, error or kind
        payload, read_error = _read_evidence_bytes(root=root, evidence=ref)
        if read_error is not None or payload is None:
            return (
                QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
                read_error or f"evidence is unreadable: {ref.relative_path}",
            )
        refs[kind] = ref
        payloads[kind] = payload

    try:
        report = RunReport.model_validate_json(payloads["run_report"])
        receipt = FaultMutationReceipt.model_validate_json(payloads["fault_receipt"])
    except ValueError:
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            "fault case contains an invalid run report or mutation receipt",
        )
    if payloads["fault_receipt"] != receipt.artifact_bytes():
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            "fault mutation receipt is not in its canonical signed form",
        )
    if report.qualification_fault_mutations != [receipt]:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "run report does not contain the exact signed fault receipt",
        )

    expected_case_sha256 = sha256_bytes(case.id.encode("utf-8"))
    target = case.resolved_fault_target()
    expected_target_sha256 = (
        sha256_bytes(target.step_id.encode("utf-8")) if target is not None else None
    )
    expected_action_paths = {
        action.step_id: action.actuation_path for action in case.action_targets
    }
    expected_bindings = (
        receipt.fault_kind == case.kind.value,
        report.governed_qualification_case_kind == case.kind.value,
        result.observed_outcome is case.expected_outcome,
        report.governed_qualification_campaign_id_sha256
        == result.campaign_id_sha256
        == receipt.campaign_id_sha256,
        report.governed_qualification_case_id_sha256
        == expected_case_sha256
        == receipt.case_id_sha256,
        report.governed_qualification_case_input_sha256
        == result.case_input_sha256
        == receipt.case_input_sha256,
        report.governed_qualification_run_id_sha256
        == result.run_id_sha256
        == receipt.run_id_sha256,
        report.run_id_sha256 == receipt.run_id_sha256,
        report.governed_qualification_fault_driver_id == receipt.driver_id,
        report.governed_qualification_fault_driver_contract_sha256
        == receipt.driver_contract_sha256,
        report.governed_qualification_fault_driver_key_id == receipt.attestation_key_id,
        report.governed_qualification_fault_step_id_sha256
        == receipt.step_id_sha256
        == expected_target_sha256,
        report.governed_qualification_case_action_paths == expected_action_paths,
        receipt.actuation_path
        == (target.actuation_path if target is not None else None),
    )
    if not all(expected_bindings):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "fault case report, receipt, input, and signed result bindings differ",
        )
    if (
        receipt.project_id != project.project_id
        or receipt.project_revision != project.revision
        or receipt.project_contract_sha256 != project.contract_sha256()
    ):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "fault mutation receipt is bound to another qualification project",
        )

    trusted_key = project.trusted_fault_driver_keys.get(receipt.attestation_key_id)
    if trusted_key is None or not verify_fault_mutation_receipt(
        receipt,
        trusted_public_key_base64=trusted_key,
    ):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "fault mutation receipt signature is invalid or untrusted",
        )
    if refs["fault_receipt"].sha256 != receipt.receipt_sha256():
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            "fault receipt evidence digest does not match its exact bytes",
        )
    if (
        refs["fault_mutation"].sha256 != receipt.mutation_artifact_sha256
        or hashlib.sha256(payloads["fault_mutation"]).hexdigest()
        != receipt.mutation_artifact_sha256
    ):
        return (
            QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
            "fault mutation artifact bytes do not match the signed receipt",
        )

    detector_error = fault_detector_contract_error(report, receipt)
    if detector_error is not None:
        return (
            QualificationRefusalCode.CASE_NOT_PASSED,
            f"fault detector contract failed: {detector_error}",
        )
    return None


def _case_result_integrity_error(
    workflow: "Workflow",
    project: QualificationProject,
    case: QualificationCase,
    result: QualificationCaseResult,
    *,
    evidence_root: Optional[Path],
    evidence_preverified: bool = False,
    policy: Optional["Policy"] = None,
    transition_predicate_vision: Any | None = None,
) -> Optional[tuple[QualificationRefusalCode, str]]:
    """Return the first fail-closed attestation/evidence error."""

    if result.case_id != case.id:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result does not match its containing qualification case",
        )
    if result.observed_outcome is not case.expected_outcome:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result outcome does not match its containing qualification case",
        )
    if result.project_id != project.project_id:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result project id does not match the qualification project",
        )
    if result.project_revision != project.revision:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result project revision is stale",
        )
    if result.workflow_contract_sha256 != workflow_contract_sha256(workflow):
        return (
            QualificationRefusalCode.CASE_WORKFLOW_CHANGED,
            "case result is bound to a different executable workflow",
        )
    if (
        result.environment_contract_sha256 != project.environment.contract_sha256()
        or result.environment_digest != project.environment.environment_digest
    ):
        return (
            QualificationRefusalCode.CASE_ENVIRONMENT_CHANGED,
            "case result is bound to a different qualification environment",
        )
    if result.runtime_version != project.environment.runtime_version:
        return (
            QualificationRefusalCode.CASE_RUNTIME_CHANGED,
            "case result runtime version is outside the qualified boundary",
        )
    missing_capabilities = sorted(
        set(project.environment.required_capabilities) - set(result.runner_capabilities)
    )
    if missing_capabilities:
        return (
            QualificationRefusalCode.CASE_CAPABILITY_MISSING,
            "runner lacks required capabilities: " + ", ".join(missing_capabilities),
        )
    if result.project_contract_sha256 != project.contract_sha256():
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result is bound to a different qualification contract",
        )
    public_key_base64 = project.trusted_runner_keys.get(result.attestation_key_id)
    if public_key_base64 is None or not result.attestation_signature:
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result is not signed by a trusted qualification runner",
        )
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        verifier = Ed25519PublicKey.from_public_bytes(
            b64decode(public_key_base64, validate=True)
        )
        verifier.verify(
            b64decode(result.attestation_signature, validate=True),
            _attestation_payload(result),
        )
    except (InvalidSignature, ValueError):
        return (
            QualificationRefusalCode.CASE_ATTESTATION_INVALID,
            "case result signature is invalid",
        )
    # The runner signature binds every evidence digest and relative path.  A
    # certification operation supplies ``evidence_root`` and verifies the
    # actual bytes.  Later admission can independently recompute the exact
    # qualification decision from the signed, hash-bound references without
    # copying sensitive evidence into the portable bundle.
    if not evidence_preverified:
        report_error = _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=case,
            result=result,
            evidence_root=evidence_root,
            policy=policy,
            transition_predicate_vision=transition_predicate_vision,
        )
        if report_error is not None:
            return report_error
    if (
        case.kind is not QualificationCaseKind.REPRESENTATIVE
        and not evidence_preverified
    ):
        fault_error = _fault_case_integrity_error(
            project=project,
            case=case,
            result=result,
            evidence_root=evidence_root,
        )
        if fault_error is not None:
            return fault_error
    if evidence_root is None:
        return None
    root = evidence_root.resolve()
    for evidence in result.evidence:
        _payload, error = _read_evidence_bytes(root=root, evidence=evidence)
        if error is not None:
            return (
                QualificationRefusalCode.CASE_EVIDENCE_UNVERIFIED,
                error,
            )
    return None


def _case_evidence_contract_sha256(project: QualificationProject) -> str:
    """Digest the exact signed current case results and their evidence refs."""

    payload: list[dict[str, Any]] = []
    for case in sorted(project.cases, key=lambda item: item.id):
        if not case.required:
            continue
        result = _latest_result(case, project.revision)
        payload.append(
            {
                "case_id": case.id,
                "result": (
                    result.model_dump(mode="json") if result is not None else None
                ),
            }
        )
    payload.append(
        {
            "judgment_cases": [
                {
                    "case_id": case.id,
                    "local_evidence": [
                        item.model_dump(mode="json") for item in case.local_evidence
                    ],
                    "review_note_ref": (
                        case.review_note_ref.model_dump(mode="json")
                        if case.review_note_ref is not None
                        else None
                    ),
                }
                for case in sorted(project.judgment_cases, key=lambda item: item.id)
            ]
        }
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _judgment_evidence_errors(
    project: QualificationProject,
    *,
    evidence_root: Optional[Path | str],
    evidence_preverified: bool = False,
) -> list[tuple[str, str]]:
    """Verify every local judgment artifact before certification.

    The artifacts remain local. The sealed project stores only relative paths
    and digests, which this check verifies under the declared local root.
    """

    if not project.judgment_cases:
        return []
    if evidence_root is None:
        if evidence_preverified:
            return []
        return [
            (
                "qualification.judgment_cases",
                "judgment-case certification requires the local evidence root",
            )
        ]
    root = Path(evidence_root).resolve()
    errors: list[tuple[str, str]] = []
    for case in project.judgment_cases:
        refs = [*case.local_evidence]
        if case.review_note_ref is not None:
            refs.append(case.review_note_ref)
        for evidence in refs:
            _payload, error = _read_evidence_bytes(root=root, evidence=evidence)
            if error is not None:
                errors.append(
                    (
                        f"qualification.judgment_cases.{case.id}.local_evidence",
                        error,
                    )
                )
    return errors


def record_case_results(
    workflow: "Workflow",
    results: Iterable[QualificationCaseResult],
    *,
    evidence_root: Path | str,
    transition_predicate_vision: Any | None = None,
) -> QualificationProject:
    """Record results produced by a local/Desktop/customer-controlled runner."""

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before recording results")
    cases = {case.id: case for case in project.cases}
    pending = list(results)
    for result in pending:
        case = cases.get(result.case_id)
        if case is None:
            raise QualificationError(f"unknown qualification case {result.case_id!r}")
        error = _case_result_integrity_error(
            workflow,
            project,
            case,
            result,
            evidence_root=Path(evidence_root),
            transition_predicate_vision=transition_predicate_vision,
        )
        if error is not None:
            raise QualificationError(f"{error[0].value}: {error[1]}")
    for result in pending:
        case = cases[result.case_id]
        case.results.append(result)
    project.updated_at = _now()
    # New case evidence makes an earlier lookup record stale; the campaign
    # that produced these results writes a fresh one.
    project.lookup = None
    _invalidate_certification(workflow)
    return project


def set_qualified_lookup(
    workflow: "Workflow", lookup: QualifiedLookup
) -> QualificationProject:
    """Record which lookups the recorded cases used and on which surface.

    Call after the case results are recorded and before certifying, so the
    certification binds this record's digest.
    """

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before recording lookups")
    project.lookup = lookup.model_copy(deep=True)
    project.updated_at = _now()
    _invalidate_certification(workflow)
    return project


def add_requalification_condition(
    workflow: "Workflow",
    condition: RequalificationCondition,
) -> QualificationProject:
    """Add a requalification trigger and advance the semantic revision."""

    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "initialize qualification before adding requalification conditions"
        )
    if condition in project.requalification_conditions:
        return project
    previous = project.revision_digest()
    project.requalification_conditions.append(condition)
    _touch(project, previous)
    _invalidate_certification(workflow)
    return project


def run_cases(
    workflow: "Workflow",
    executor: Callable[
        [QualificationCase, QualificationProject, str],
        QualificationCaseResult,
    ],
    *,
    case_ids: Optional[set[str]] = None,
    evidence_root: Path | str,
    transition_predicate_vision: Any | None = None,
) -> list[QualificationCaseResult]:
    """Execute selected cases through a caller-supplied local runner.

    Flow owns the schema and certification rules; Desktop or a deployment owns
    the environment-specific executor.  This keeps raw observations in the
    declared execution boundary while still providing one typed API.
    """

    project = workflow.qualification
    if project is None:
        raise QualificationError("initialize qualification before running cases")
    selected = [
        case for case in project.cases if case_ids is None or case.id in case_ids
    ]
    if case_ids is not None:
        missing = sorted(case_ids - {case.id for case in selected})
        if missing:
            raise QualificationError(
                "unknown qualification cases: " + ", ".join(missing)
            )
    contract = workflow_contract_sha256(workflow)
    results = [executor(case, project, contract) for case in selected]
    record_case_results(
        workflow,
        results,
        evidence_root=evidence_root,
        transition_predicate_vision=transition_predicate_vision,
    )
    return results


def _latest_result(
    case: QualificationCase,
    project_revision: int,
) -> Optional[QualificationCaseResult]:
    matching = [
        result for result in case.results if result.project_revision == project_revision
    ]
    return matching[-1] if matching else None


def evaluate_qualification(
    workflow: "Workflow",
    *,
    policy: Optional["Policy"] = None,
    evidence_root: Optional[Path | str] = None,
    transition_predicate_vision: Any | None = None,
    _certified_evidence_contract_sha256: Optional[str] = None,
) -> QualificationReport:
    """Evaluate qualification coverage without mutating or executing a workflow."""

    from openadapt_flow.policy import evaluate_policy, is_identity_armed
    from openadapt_flow.traversal import iter_workflow_steps

    steps = list(iter_workflow_steps(workflow))
    workflow_contract = workflow_contract_sha256(workflow)
    project = workflow.qualification
    if project is None:
        refusal = QualificationRefusal(
            code=QualificationRefusalCode.PROJECT_MISSING,
            path="qualification",
            message="workflow has no qualification project",
        )
        return QualificationReport(
            workflow_name=workflow.name,
            workflow_contract_sha256=workflow_contract,
            environment_contract_sha256=None,
            project_id=None,
            project_revision=None,
            policy_name=policy.name if policy else None,
            passed=False,
            action_count=len(steps),
            state_changing_action_count=0,
            consequential_action_count=len(steps),
            identity_covered_action_count=0,
            effect_required_action_count=len(steps),
            effect_covered_action_count=0,
            minimum_effect_tier=None,
            case_count=0,
            passed_case_count=0,
            refusals=[refusal],
        )

    refusals: list[QualificationRefusal] = []
    state_changing_steps: list[Step] = []
    consequential_steps: list[Step] = []
    for step in steps:
        classification = project.action_classifications.get(step.id)
        executable_floor = _executable_risk_floor(step)
        if classification is None:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.ACTION_CLASSIFICATION_MISSING,
                    path=f"qualification.action_classifications.{step.id}",
                    step_id=step.id,
                    message="action has no reviewed business-risk classification",
                )
            )
            effective_classification = executable_floor
        elif (
            not classification.operator_confirmed
            or classification.classification is ActionRiskClass.UNKNOWN
        ):
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.ACTION_CLASSIFICATION_UNCONFIRMED,
                    path=f"qualification.action_classifications.{step.id}",
                    step_id=step.id,
                    message="action risk is unknown or has not been operator-confirmed",
                )
            )
            effective_classification = executable_floor
        else:
            effective_classification = classification.classification
            if (
                executable_floor is ActionRiskClass.IRREVERSIBLE
                and effective_classification is not ActionRiskClass.IRREVERSIBLE
            ):
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.ACTION_CLASSIFICATION_CONFLICT,
                        path=f"qualification.action_classifications.{step.id}",
                        step_id=step.id,
                        message=(
                            "operator classification is weaker than the executable "
                            "irreversible action/effect contract"
                        ),
                    )
                )
                effective_classification = ActionRiskClass.IRREVERSIBLE
            elif (
                executable_floor is ActionRiskClass.STATE_CHANGING
                and effective_classification is ActionRiskClass.READ_ONLY
            ):
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.ACTION_CLASSIFICATION_CONFLICT,
                        path=f"qualification.action_classifications.{step.id}",
                        step_id=step.id,
                        message=(
                            "an action with a declared business effect cannot be "
                            "classified read-only"
                        ),
                    )
                )
                effective_classification = ActionRiskClass.STATE_CHANGING

        if effective_classification is ActionRiskClass.STATE_CHANGING:
            state_changing_steps.append(step)
        elif effective_classification in {
            ActionRiskClass.CONSEQUENTIAL,
            ActionRiskClass.IRREVERSIBLE,
        }:
            consequential_steps.append(step)

    identity_covered = 0
    effect_covered = 0
    effect_bindings = {
        (binding.step_id, binding.actuation_path, binding.effect_index): binding
        for binding in project.effect_policies
    }

    from openadapt_flow.policy import executable_actuation_paths

    for step in [*state_changing_steps, *consequential_steps]:
        executable_paths = executable_actuation_paths(step)
        gui_required = "gui" in executable_paths
        api_required = "api" in executable_paths
        gui_armed = not gui_required or is_identity_armed(step)
        api_armed = not api_required or bool(
            step.api_binding is not None and step.api_binding.identity
        )
        identity_policy = project.identity_policies.get(step.id)
        if not gui_armed or not api_armed:
            missing_paths = []
            if not gui_armed:
                missing_paths.append("gui")
            if not api_armed:
                missing_paths.append("api")
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.STEP_IDENTITY_UNARMED,
                    path=f"steps.{step.id}.identity_armed",
                    step_id=step.id,
                    message=(
                        "state-changing action has no exact identity contract for "
                        "executable path(s): " + ", ".join(missing_paths)
                    ),
                )
            )
        if identity_policy is None:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.IDENTITY_POLICY_MISSING,
                    path=f"qualification.identity_policies.{step.id}",
                    step_id=step.id,
                    message="state-changing action has no identity match policy",
                )
            )
        elif identity_policy.enforcement is IdentityEnforcement.CANONICAL_LADDER:
            available = available_identity_sources(step)
            if api_required:
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.IDENTITY_SIGNAL_UNAVAILABLE,
                        path=f"qualification.identity_policies.{step.id}",
                        step_id=step.id,
                        message=(
                            "API actuation requires an explicit signal-quorum "
                            "identity policy bound to the request and effect"
                        ),
                    )
                )
            if gui_required and not available:
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.IDENTITY_SIGNAL_UNAVAILABLE,
                        path=f"qualification.identity_policies.{step.id}",
                        step_id=step.id,
                        message="canonical identity ladder has no retained evidence",
                    )
                )
            elif gui_required and gui_armed and not api_required:
                identity_covered += 1
        else:
            independence_errors = identity_policy_independence_errors(identity_policy)
            if independence_errors:
                refusals.append(
                    QualificationRefusal(
                        code=(
                            QualificationRefusalCode.IDENTITY_SIGNALS_NOT_INDEPENDENT
                        ),
                        path=f"qualification.identity_policies.{step.id}.signals",
                        step_id=step.id,
                        message=(
                            "identity quorum signals reuse correlated retained evidence"
                        ),
                        details={"error_count": len(independence_errors)},
                    )
                )
            unavailable_signals: list[tuple[str, IdentitySignalPolicy]] = []
            for identity_path in executable_paths:
                unavailable_signals.extend(
                    (identity_path, signal)
                    for signal in identity_policy.signals
                    if not identity_signal_runtime_available(
                        step,
                        signal,
                        actuation_path=identity_path,
                    )
                )
            api_key_mismatch = False
            if api_required and step.api_binding is not None:
                api_key_mismatch = [item.key for item in step.api_binding.identity] != [
                    signal.key.value for signal in identity_policy.signals
                ]
                if api_key_mismatch:
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.IDENTITY_SIGNAL_UNAVAILABLE,
                            path=f"qualification.identity_policies.{step.id}",
                            step_id=step.id,
                            message=(
                                "API identity bindings do not match the exact "
                                "qualified signal set and order"
                            ),
                        )
                    )
            for actuation_path, signal in unavailable_signals:
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.IDENTITY_SIGNAL_UNAVAILABLE,
                        path=(
                            f"qualification.identity_policies.{step.id}."
                            f"signals.{signal.key.value}.{actuation_path}"
                        ),
                        step_id=step.id,
                        message=(
                            "qualified identity signal has no executable retained "
                            f"comparison on the {actuation_path} path"
                        ),
                        details={
                            "signal": signal.key.value,
                            "source": signal.source.value,
                        },
                    )
                )
            if (
                gui_armed
                and api_armed
                and not independence_errors
                and not unavailable_signals
                and not api_key_mismatch
            ):
                identity_covered += 1

    effect_required_steps = state_changing_steps + consequential_steps
    consequential_ids = {step.id for step in consequential_steps}
    for step in effect_required_steps:
        step_effects_covered = True
        from openadapt_flow.policy import iter_effect_paths

        for actuation_path, path_effects in iter_effect_paths(step):
            if not path_effects:
                step_effects_covered = False
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.EFFECT_CONTRACT_MISSING,
                        path=f"steps.{step.id}.{actuation_path}.effects",
                        step_id=step.id,
                        message=(
                            f"state-changing {actuation_path} path declares no "
                            "effect contract; one path cannot cover another"
                        ),
                    )
                )
                continue
            for index, effect in enumerate(path_effects):
                binding = effect_bindings.get((step.id, actuation_path, index))
                path = (
                    f"qualification.effect_policies.{step.id}.{actuation_path}.{index}"
                )
                if binding is None:
                    step_effects_covered = False
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.EFFECT_POLICY_MISSING,
                            path=path,
                            step_id=step.id,
                            message=(
                                f"{actuation_path} effect {index} has no "
                                "verification-strength policy"
                            ),
                        )
                    )
                    continue
                if binding.effect_contract_hash != effect.contract_hash():
                    step_effects_covered = False
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.EFFECT_CONTRACT_CHANGED,
                            path=f"{path}.effect_contract_hash",
                            step_id=step.id,
                            message=(
                                f"{actuation_path} effect {index} changed after "
                                "its tier was assigned"
                            ),
                        )
                    )
                if not binding.tier.satisfies(project.minimum_effect_tier):
                    step_effects_covered = False
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.EFFECT_TIER_INSUFFICIENT,
                            path=f"{path}.tier",
                            step_id=step.id,
                            message=(
                                f"{actuation_path} effect {index} tier "
                                f"{int(binding.tier)} is weaker than required tier "
                                f"{int(project.minimum_effect_tier)}"
                            ),
                            details={
                                "actual_tier": int(binding.tier),
                                "minimum_tier": int(project.minimum_effect_tier),
                            },
                        )
                    )
                if (
                    step.id in consequential_ids
                    and binding.tier is VerificationTier.IMMEDIATE_SCREEN
                ):
                    step_effects_covered = False
                    refusals.append(
                        QualificationRefusal(
                            code=QualificationRefusalCode.HIGH_RISK_SCREEN_ONLY,
                            path=f"{path}.tier",
                            step_id=step.id,
                            message=(
                                f"consequential {actuation_path} effect {index} "
                                "cannot qualify with immediate screen confirmation "
                                "alone"
                            ),
                        )
                    )
        if step_effects_covered:
            effect_covered += 1

    required_gui_action_steps = [
        step
        for step in effect_required_steps
        if "gui" in executable_actuation_paths(step)
    ]
    required_gui_consequential_steps = [
        step
        for step in consequential_steps
        if "gui" in executable_actuation_paths(step)
    ]
    required_kinds: set[QualificationCaseKind] = set()
    if effect_required_steps:
        required_kinds.update(
            {
                QualificationCaseKind.WEAK_EFFECT,
                QualificationCaseKind.MISSING_EFFECT,
            }
        )
    if required_gui_action_steps:
        required_kinds.add(QualificationCaseKind.AMBIGUITY)
    if required_gui_consequential_steps:
        required_kinds.update(
            {
                QualificationCaseKind.WRONG_IDENTITY,
                QualificationCaseKind.STALE_IDENTITY,
            }
        )
    required_kinds_present = {case.kind for case in project.cases if case.required}
    if QualificationCaseKind.REPRESENTATIVE not in required_kinds_present:
        refusals.append(
            QualificationRefusal(
                code=QualificationRefusalCode.REPRESENTATIVE_CASE_MISSING,
                path="qualification.cases",
                message=(
                    "at least one required representative VERIFIED case is required"
                ),
            )
        )
    for kind in sorted(
        required_kinds - required_kinds_present,
        key=lambda item: item.value,
    ):
        refusals.append(
            QualificationRefusal(
                code=QualificationRefusalCode.FAULT_CASE_MISSING,
                path="qualification.cases",
                message=f"required {kind.value} fault case is missing",
                details={"kind": kind.value},
            )
        )

    required_cases = [
        case
        for case in project.cases
        if case.required
        and (
            case.kind is QualificationCaseKind.REPRESENTATIVE
            or case.kind in required_kinds
        )
    ]
    required_gui_targets: set[tuple[str, Literal["gui", "api"]]] = {
        (step.id, "gui") for step in required_gui_action_steps
    }
    required_api_targets: set[tuple[str, Literal["gui", "api"]]] = {
        (step.id, "api")
        for step in effect_required_steps
        if "api" in executable_actuation_paths(step)
        and step.api_binding is not None
        and bool(step.api_binding.effects)
    }
    required_representative_targets = required_gui_targets | required_api_targets
    required_actions, _required_identity_steps = qualification_action_requirements(
        workflow
    )
    required_fault_targets: dict[
        QualificationCaseKind,
        set[tuple[str, Literal["gui", "api"]]],
    ] = {
        QualificationCaseKind.AMBIGUITY: set(required_gui_targets),
        QualificationCaseKind.WEAK_EFFECT: (
            set(required_gui_targets) | set(required_api_targets)
        ),
        QualificationCaseKind.MISSING_EFFECT: (
            set(required_gui_targets) | set(required_api_targets)
        ),
        QualificationCaseKind.WRONG_IDENTITY: {
            (step.id, "gui") for step in required_gui_consequential_steps
        },
        QualificationCaseKind.STALE_IDENTITY: {
            (step.id, "gui") for step in required_gui_consequential_steps
        },
    }
    for case in required_cases:
        if case.runtime_input_sha256 is None:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.CASE_INPUT_UNBOUND,
                    path=f"qualification.cases.{case.id}.runtime_input_sha256",
                    case_id=case.id,
                    message=(
                        f"required case {case.id!r} has no approved runtime-input "
                        "digest"
                    ),
                )
            )
        targets = {
            (target.step_id, target.actuation_path) for target in case.action_targets
        }
        valid_path_targets = {
            (step.id, path)
            for step in steps
            for path in executable_actuation_paths(step)
        }
        if case.kind is QualificationCaseKind.REPRESENTATIVE:
            invalid_targets = not targets or len(targets) != len(case.action_targets)
        else:
            fault_target = case.resolved_fault_target()
            fault_key = (
                (fault_target.step_id, fault_target.actuation_path)
                if fault_target is not None
                else None
            )
            allowed_fault_scope = {
                (step.id, path)
                for step in steps
                if step.id in required_actions
                for path in executable_actuation_paths(step)
                if (
                    path == "gui"
                    or (step.api_binding is not None and bool(step.api_binding.effects))
                )
            }
            invalid_targets = (
                not targets
                or len(targets) != len(case.action_targets)
                or fault_key is None
                or fault_key not in targets
                or fault_key not in required_fault_targets[case.kind]
                or not targets.issubset(allowed_fault_scope)
            )
        invalid_targets = invalid_targets or not targets.issubset(valid_path_targets)
        if invalid_targets:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.CASE_TARGET_INVALID,
                    path=f"qualification.cases.{case.id}.action_targets",
                    case_id=case.id,
                    message=(
                        f"case {case.id!r} has missing or ineligible qualified "
                        "action scope"
                    ),
                    details={
                        "action_targets": ",".join(
                            f"{step_id}:{path}" for step_id, path in sorted(targets)
                        )
                    },
                )
            )

    representative_coverage = {
        (target.step_id, target.actuation_path)
        for case in required_cases
        if case.kind is QualificationCaseKind.REPRESENTATIVE
        for target in case.action_targets
    }
    for step_id, target_path in sorted(
        required_representative_targets - representative_coverage
    ):
        refusals.append(
            QualificationRefusal(
                code=QualificationRefusalCode.REPRESENTATIVE_ACTION_UNCOVERED,
                path="qualification.cases",
                step_id=step_id,
                message=(
                    f"qualified write action {step_id!r} has no required "
                    f"representative case for its {target_path} path"
                ),
                details={"actuation_path": target_path},
            )
        )
    fault_coverage: set[tuple[QualificationCaseKind, str, Literal["gui", "api"]]] = (
        set()
    )
    for case in required_cases:
        target = case.resolved_fault_target()
        if target is not None:
            fault_coverage.add((case.kind, target.step_id, target.actuation_path))
    for kind in sorted(required_kinds, key=lambda item: item.value):
        for step_id, target_path in sorted(required_fault_targets[kind]):
            if (kind, step_id, target_path) not in fault_coverage:
                refusals.append(
                    QualificationRefusal(
                        code=QualificationRefusalCode.FAULT_ACTION_UNCOVERED,
                        path="qualification.cases",
                        step_id=step_id,
                        message=(
                            f"consequential action {step_id!r} has no required "
                            f"{kind.value} fault case for its {target_path} path"
                        ),
                        details={
                            "kind": kind.value,
                            "actuation_path": target_path,
                        },
                    )
                )

    passed_cases = 0
    evidence_preverified = bool(
        evidence_root is None
        and _certified_evidence_contract_sha256 is not None
        and _certified_evidence_contract_sha256
        == _case_evidence_contract_sha256(project)
    )
    for case in required_cases:
        result = _latest_result(case, project.revision)
        if result is None or result.status != "passed":
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.CASE_NOT_PASSED,
                    path=f"qualification.cases.{case.id}.results",
                    case_id=case.id,
                    message=f"required case {case.id!r} has no passing current result",
                )
            )
            continue
        integrity_error = _case_result_integrity_error(
            workflow,
            project,
            case,
            result,
            evidence_root=Path(evidence_root) if evidence_root is not None else None,
            evidence_preverified=evidence_preverified,
            policy=policy,
            transition_predicate_vision=transition_predicate_vision,
        )
        if integrity_error is not None:
            refusals.append(
                QualificationRefusal(
                    code=integrity_error[0],
                    path=f"qualification.cases.{case.id}.results",
                    case_id=case.id,
                    message=integrity_error[1],
                )
            )
            continue
        if result.observed_outcome is not case.expected_outcome:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.CASE_NOT_PASSED,
                    path=f"qualification.cases.{case.id}.results",
                    case_id=case.id,
                    message=(
                        f"case {case.id!r} observed {result.observed_outcome.value}; "
                        f"expected {case.expected_outcome.value}"
                    ),
                )
            )
            continue
        if not result.evidence:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.CASE_EVIDENCE_MISSING,
                    path=f"qualification.cases.{case.id}.results",
                    case_id=case.id,
                    message=f"case {case.id!r} has no evidence reference",
                )
            )
            continue
        passed_cases += 1

    if policy is not None:
        policy_report = evaluate_policy(workflow, policy)
        for violation in policy_report.violations:
            refusals.append(
                QualificationRefusal(
                    code=QualificationRefusalCode.POLICY_VIOLATION,
                    path=(
                        f"steps.{violation.step_id}"
                        if violation.step_id
                        else "workflow"
                    ),
                    step_id=violation.step_id,
                    message=violation.reason,
                    details={"rule": violation.rule},
                )
            )

    judgment_report = evaluate_judgment_case_qualification(workflow)
    judgment_refusal_codes = {
        JudgmentCaseFindingCode.BINDING_MISMATCH: (
            QualificationRefusalCode.JUDGMENT_CASE_BINDING_INVALID,
            "judgment case binding is invalid",
        ),
        JudgmentCaseFindingCode.FACT_SCHEMA_MISMATCH: (
            QualificationRefusalCode.JUDGMENT_CASE_BINDING_INVALID,
            "judgment case fact schema is invalid",
        ),
        JudgmentCaseFindingCode.CONFLICT: (
            QualificationRefusalCode.JUDGMENT_CASE_CONFLICT,
            "judgment cases conflict",
        ),
        JudgmentCaseFindingCode.MISSING_CONTRAST_COVERAGE: (
            QualificationRefusalCode.JUDGMENT_CASE_CONTRAST_MISSING,
            "judgment automatic rule lacks contrast coverage",
        ),
        JudgmentCaseFindingCode.MORE_EVIDENCE_REQUIRED: (
            QualificationRefusalCode.JUDGMENT_CASE_MORE_EVIDENCE_REQUIRED,
            "judgment case requires more evidence",
        ),
    }
    for finding in judgment_report.findings:
        mapped = judgment_refusal_codes.get(finding.code)
        if mapped is None:
            # Retained human authority is a result, not a certification failure.
            continue
        refusals.append(
            QualificationRefusal(
                code=mapped[0],
                path="qualification.judgment_cases",
                case_id=finding.case_id,
                message=f"{mapped[1]}: {finding.message}",
            )
        )
    for path, message in _judgment_evidence_errors(
        project,
        evidence_root=evidence_root,
        evidence_preverified=evidence_preverified,
    ):
        refusals.append(
            QualificationRefusal(
                code=QualificationRefusalCode.JUDGMENT_CASE_EVIDENCE_UNVERIFIED,
                path=path,
                message=message,
            )
        )
    for path, message in qualified_business_decision_delivery_errors(workflow):
        refusals.append(
            QualificationRefusal(
                code=(QualificationRefusalCode.BUSINESS_DECISION_DELIVERY_INVALID),
                path=path,
                message=message,
            )
        )

    return QualificationReport(
        workflow_name=workflow.name,
        workflow_contract_sha256=workflow_contract,
        environment_contract_sha256=project.environment.contract_sha256(),
        project_id=project.project_id,
        project_revision=project.revision,
        policy_name=policy.name if policy else None,
        passed=not refusals,
        action_count=len(steps),
        state_changing_action_count=len(state_changing_steps),
        consequential_action_count=len(consequential_steps),
        identity_covered_action_count=identity_covered,
        effect_required_action_count=len(effect_required_steps),
        effect_covered_action_count=effect_covered,
        minimum_effect_tier=project.minimum_effect_tier,
        case_count=len(required_cases),
        passed_case_count=passed_cases,
        refusals=refusals,
    )


def subroutine_certification_refusals(
    workflow: "Workflow",
    library: Optional["SubroutineLibrary"],
    *,
    workflow_id: Optional[str] = None,
) -> list[QualificationRefusal]:
    """Certification refusals for the subroutines ``workflow`` embeds.

    Runs :func:`openadapt_flow.subroutines.certify_subroutine_uses`, which
    registers the workflow as a user of each embedded digest and then admits
    every use. Empty for a workflow that embeds no subroutine. Messages name
    subroutines, inputs and codes only.
    """

    from openadapt_flow.subroutines import certify_subroutine_uses

    admission = certify_subroutine_uses(workflow, library, workflow_id=workflow_id)
    return [
        QualificationRefusal(
            code=QualificationRefusalCode.SUBROUTINE_NOT_ADMITTED,
            path=f"subflows.{refusal.subflow}",
            message=refusal.message,
            details={"subroutine_code": refusal.code},
        )
        for refusal in admission.refusals
    ]


def certify_project(
    workflow: "Workflow",
    *,
    policy: "Policy",
    evidence_root: Path | str,
    transition_predicate_vision: Any | None = None,
    execution_profile: Literal["standard", "regulated"] = "standard",
    subroutine_library: Optional["SubroutineLibrary"] = None,
    subroutine_workflow_id: Optional[str] = None,
) -> QualificationReport:
    """Evaluate and persist the exact qualification decision in memory.

    A workflow that embeds subroutines (``subroutine/<name>/<sha256>``
    subflows) is certified only when every use is admitted by
    ``subroutine_library``; certifying also registers the workflow with the
    library so a later version of any of those subroutines re-qualifies it.
    Without a library such a workflow is refused. A workflow that embeds no
    subroutine is evaluated exactly as before.
    """

    project = workflow.qualification
    report = evaluate_qualification(
        workflow,
        policy=policy,
        evidence_root=evidence_root,
        transition_predicate_vision=transition_predicate_vision,
    )
    subroutine_refusals = subroutine_certification_refusals(
        workflow, subroutine_library, workflow_id=subroutine_workflow_id
    )
    if subroutine_refusals:
        report = report.model_copy(
            update={
                "passed": False,
                "refusals": [*report.refusals, *subroutine_refusals],
            }
        )
    if project is not None:
        from openadapt_flow.policy import policy_contract_sha256

        project.last_certification = QualificationCertification(
            project_revision=project.revision,
            project_contract_sha256=project.contract_sha256(),
            workflow_contract_sha256=report.workflow_contract_sha256,
            environment_contract_sha256=project.environment.contract_sha256(),
            policy_name=policy.name,
            policy_contract_sha256=policy_contract_sha256(policy),
            policy_contract=policy.model_dump(mode="json"),
            passed=report.passed,
            report_sha256=report.report_sha256(),
            case_evidence_contract_sha256=_case_evidence_contract_sha256(project),
            execution_profile=execution_profile,
            lookup_sha256=(
                project.lookup.contract_sha256() if project.lookup is not None else None
            ),
        )
    # A failed campaign must remove any template from an earlier accepted
    # campaign.  Manifest provenance is outside the bundle content digest, so
    # retaining that template would leave stale production authority attached
    # to a newly failed qualification decision.
    if not report.passed and workflow.manifest is not None:
        workflow.manifest.provenance.governed_authorization_template = None
    workflow.stamp_certification(
        policy_name=policy.name,
        passed=report.passed,
        status="certified" if report.passed else "failed",
    )
    return report


def _authorization_parameter_contract(
    workflow: "Workflow",
) -> tuple[tuple[Any, ...], str]:
    """Return value-free parameter metadata and its exact contract digest."""

    from openadapt_flow.bundle_validation import (
        build_runtime_parameter_schema,
        compute_parameter_schema_digest,
    )
    from openadapt_flow.ir import GovernedAuthorizationParameter

    records: list[GovernedAuthorizationParameter] = []
    for parameter in build_runtime_parameter_schema(workflow):
        name = parameter["name"]
        spec = workflow.param_specs.get(name)
        choice_values = parameter["choices"]
        choices_bytes = json.dumps(
            choice_values, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        records.append(
            GovernedAuthorizationParameter(
                name=name,
                type=parameter["type"],
                required=parameter["required"],
                secret=parameter["secret"],
                has_default=(
                    name in workflow.params
                    or (spec is not None and spec.example is not None)
                ),
                choices_count=len(spec.choices) if spec is not None else 0,
                choices_sha256=hashlib.sha256(choices_bytes).hexdigest(),
            )
        )
    return tuple(records), compute_parameter_schema_digest(workflow)


def build_governed_authorization_template(
    workflow: "Workflow",
) -> "Any":
    """Build the value-free template for an accepted production certification."""

    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        qualified_effect_requirements,
    )
    from openadapt_flow.ir import GovernedAuthorizationTemplate

    project = workflow.qualification
    certification = project.last_certification if project is not None else None
    manifest = workflow.manifest
    if project is None or certification is None or not certification.passed:
        raise QualificationError(
            "governed authorization template requires certification"
        )
    if manifest is None or not manifest.content_digest:
        raise QualificationError(
            "governed authorization template requires a sealed bundle"
        )
    if certification.policy_contract_sha256 is None:
        raise QualificationError(
            "governed authorization template requires policy digest"
        )
    if certification.case_evidence_contract_sha256 is None:
        raise QualificationError(
            "governed authorization template requires case evidence digest"
        )
    from openadapt_flow.policy import Policy

    try:
        certified_policy = Policy.model_validate(certification.policy_contract)
    except ValueError as exc:
        raise QualificationError(
            "governed authorization template requires a valid certification policy"
        ) from exc
    if not current_certification_matches(workflow, policy=certified_policy):
        raise QualificationError(
            "governed authorization template requires a current certification"
        )
    profile = ExecutionProfile(certification.execution_profile)
    requirements = qualified_effect_requirements(workflow, profile)
    required_actions, required_identity = qualification_action_requirements(workflow)
    if not required_actions:
        raise QualificationError("governed authorization template requires actions")
    identity_payload = {
        "required_identity_step_ids": sorted(required_identity),
        "identity_policies": {
            step_id: project.identity_policies[step_id].model_dump(mode="json")
            for step_id in sorted(project.identity_policies)
        },
    }
    identity_digest = hashlib.sha256(
        json.dumps(
            identity_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    parameters, parameter_digest = _authorization_parameter_contract(workflow)
    return GovernedAuthorizationTemplate.create(
        bundle_content_digest=manifest.content_digest,
        workflow_contract_sha256=certification.workflow_contract_sha256,
        qualification_project_id=project.project_id,
        qualification_project_revision=project.revision,
        qualification_project_contract_sha256=project.contract_sha256(),
        qualification_environment_contract_sha256=project.environment.contract_sha256(),
        qualification_report_sha256=certification.report_sha256,
        qualification_case_evidence_contract_sha256=(
            certification.case_evidence_contract_sha256
        ),
        policy_name=certification.policy_name,
        policy_contract_sha256=certification.policy_contract_sha256,
        execution_profile=profile.value,
        minimum_effect_tier=int(project.minimum_effect_tier),
        qualified_effect_requirements=requirements,
        required_identity_step_ids=tuple(sorted(required_identity)),
        identity_contract_sha256=identity_digest,
        parameters=parameters,
        parameter_contract_sha256=parameter_digest,
    )


def current_certification_matches(
    workflow: "Workflow",
    *,
    policy: Optional["Policy"] = None,
    policy_contract_digest: Optional[str] = None,
) -> bool:
    """Independently recompute the persisted production qualification.

    The persisted ``passed`` bit is never authority.  The exact policy,
    workflow, environment, project, signed case attestations, and resulting
    qualification-report digest must all reproduce from current bundle state.
    """

    project = workflow.qualification
    if project is None or project.last_certification is None:
        return False
    certification = project.last_certification
    if not certification.passed:
        return False
    manifest = workflow.manifest
    if manifest is None:
        return False
    expires_at = manifest.provenance.expires_at
    if expires_at is not None:
        try:
            parsed_expiry = datetime.fromisoformat(
                expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
            )
        except (TypeError, ValueError):
            return False
        if parsed_expiry.tzinfo is None:
            return False
        if datetime.now(timezone.utc) >= parsed_expiry.astimezone(timezone.utc):
            return False

    from openadapt_flow.policy import Policy, policy_contract_sha256

    try:
        embedded_policy = (
            Policy.model_validate(certification.policy_contract)
            if certification.policy_contract is not None
            else None
        )
    except ValueError:
        return False
    if embedded_policy is None:
        return False
    embedded_digest = policy_contract_sha256(embedded_policy)
    if policy is not None:
        effective_policy = policy
        digest = policy_contract_sha256(policy)
        if policy_contract_digest is not None and policy_contract_digest != digest:
            return False
    elif policy_contract_digest is not None:
        effective_policy = embedded_policy
        digest = policy_contract_digest
    else:
        # The bundle-local copy is evidence of what was evaluated, not an
        # authority that can bootstrap its own production policy decision.
        return False
    if (
        certification.policy_name != effective_policy.name
        or certification.policy_name != embedded_policy.name
        or certification.policy_contract_sha256 != digest
        or embedded_digest != digest
    ):
        return False

    if certification.case_evidence_contract_sha256 is None:
        return False
    report = evaluate_qualification(
        workflow,
        policy=effective_policy,
        _certified_evidence_contract_sha256=(
            certification.case_evidence_contract_sha256
        ),
    )
    return bool(
        report.passed
        and certification.project_revision == project.revision
        and certification.project_contract_sha256 == project.contract_sha256()
        and certification.workflow_contract_sha256 == report.workflow_contract_sha256
        and certification.environment_contract_sha256
        == project.environment.contract_sha256()
        and certification.report_sha256 == report.report_sha256()
        and certification.lookup_sha256
        == (project.lookup.contract_sha256() if project.lookup is not None else None)
    )


def persist_registration_before_certificate(
    workflow: "Workflow",
    bundle_dir: Path | str,
    persist: Optional[Callable[[], None]],
) -> None:
    """Write certification's subroutine registration before any certificate.

    ``certify_project`` registers the workflow with the subroutine library in
    memory; ``persist`` writes that library. It must succeed before a new
    certificate becomes durable, or a later version of a subroutine could not
    list this workflow for re-qualification. When it raises, the certificate
    is withdrawn in memory, ``bundle_dir`` is saved uncertified and the error
    is re-raised, so no valid new certificate is left behind.
    """

    if persist is None:
        return
    try:
        persist()
    except BaseException:
        _invalidate_certification(workflow)
        save_qualified_workflow(workflow, bundle_dir)
        raise


def save_qualified_workflow(
    workflow: "Workflow",
    bundle_dir: Path | str,
    *,
    key: Optional[str] = None,
) -> Path:
    """Reseal a mutated workflow while preserving its at-rest mode."""

    project = workflow.qualification
    certification = project.last_certification if project is not None else None
    if project is not None and certification is not None:
        if (
            certification.project_revision != project.revision
            or certification.project_contract_sha256 != project.contract_sha256()
            or certification.workflow_contract_sha256
            != workflow_contract_sha256(workflow)
            or certification.environment_contract_sha256
            != project.environment.contract_sha256()
        ):
            _invalidate_certification(workflow)
            raise QualificationError(
                "workflow, environment, or project changed after certification"
            )
        if certification.passed:
            from openadapt_flow.policy import Policy

            try:
                embedded_policy = Policy.model_validate(certification.policy_contract)
            except ValueError:
                _invalidate_certification(workflow)
                raise QualificationError(
                    "persisted certification policy contract is invalid"
                ) from None
            if not current_certification_matches(workflow, policy=embedded_policy):
                _invalidate_certification(workflow)
                raise QualificationError(
                    "persisted certification cannot be independently reproduced"
                )
    if workflow.encrypted:
        path = workflow.save(bundle_dir, encrypt=True, key=key)
    else:
        path = workflow.save(bundle_dir, encrypt=False)
    if certification is not None and (
        certification.workflow_contract_sha256 != workflow_contract_sha256(workflow)
    ):
        _invalidate_certification(workflow)
        if workflow.encrypted:
            workflow.save(bundle_dir, encrypt=True, key=key)
        else:
            workflow.save(bundle_dir, encrypt=False)
        raise QualificationError(
            "sealed asset inventory changed after the qualification campaign"
        )
    if certification is not None and certification.passed:
        template = build_governed_authorization_template(workflow)
        assert workflow.manifest is not None
        initial_digest = workflow.manifest.content_digest
        workflow.manifest.provenance.governed_authorization_template = template
        if workflow.encrypted:
            workflow.save(bundle_dir, encrypt=True, key=key)
        else:
            workflow.save(bundle_dir, encrypt=False)
        if workflow.manifest.content_digest != initial_digest:
            raise QualificationError(
                "governed authorization template changed the bundle content digest"
            )
    from openadapt_flow import sealed_set

    workflow_py = Path(bundle_dir) / "workflow.py"
    if sealed_set.is_file(workflow_py):
        from openadapt_flow.compiler.codegen import render_workflow_py

        # Resealed in memory when the bundle is a sealed set.
        sealed_set.write_text(workflow_py, render_workflow_py(workflow), key=key)
    return path


def project_schema() -> dict[str, Any]:
    """JSON Schema for Desktop/CLI/API consumers."""

    return QualificationProject.model_json_schema()
