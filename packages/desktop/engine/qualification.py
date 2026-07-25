"""Desktop adapter for Flow's versioned qualification-project contract.

``openadapt-flow`` owns the executable workflow, qualification models,
evaluation, certification, sealing, and encryption.  Desktop projects those
canonical objects into a UI-friendly response and supplies writable operations
so operators never need to edit ``workflow.json`` or an internal manifest.

Older bundles remain inspectable.  They expose an explicit initialization path
that creates ``workflow.qualification`` and invalidates any legacy policy-only
certification; Desktop never translates an old certification into evidence it
did not produce.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

QualificationRisk = Literal[
    "read_only",
    "state_changing",
    "consequential",
    "irreversible",
]
QualificationEffectKind = Literal["record_written", "field_equals"]
QualificationTargetKind = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
DEFAULT_QUALIFICATION_POLICY = "clinical-write"
ENVIRONMENT_IDENTIFIER_DERIVATION = "sha256(trimmed UTF-8 operator identifier)"


class QualificationError(RuntimeError):
    """A qualification request was refused without changing the bundle."""


def _flow_api() -> dict[str, Any]:
    """Load the pinned canonical Flow API only when qualification is used."""

    try:
        from openadapt_flow.ir import Workflow
        from openadapt_flow.policy import evaluate_policy, lint_workflow, load_policy
        from openadapt_flow.qualification import (
            ActionRiskClass,
            ActionRiskClassification,
            EnvironmentBoundary,
            IdentityEnforcement,
            IdentityPolicy,
            VerificationTier,
            certify_project,
            evaluate_qualification,
            init_project,
            save_qualified_workflow,
            set_action_classification,
            set_effect_policy,
            set_identity_policy,
            workflow_contract_sha256,
        )
        from openadapt_flow.traversal import iter_workflow_steps
        from openadapt_flow.visualize import build_program_graph
    except (ImportError, AttributeError) as exc:  # pragma: no cover - release gate
        raise QualificationError(
            "This Desktop build requires an OpenAdapt Flow runtime with the "
            "versioned qualification-project contract. Update OpenAdapt Desktop "
            "before qualifying this workflow."
        ) from exc
    return {
        "Workflow": Workflow,
        "evaluate_policy": evaluate_policy,
        "lint_workflow": lint_workflow,
        "load_policy": load_policy,
        "iter_workflow_steps": iter_workflow_steps,
        "build_program_graph": build_program_graph,
        "ActionRiskClass": ActionRiskClass,
        "ActionRiskClassification": ActionRiskClassification,
        "EnvironmentBoundary": EnvironmentBoundary,
        "IdentityEnforcement": IdentityEnforcement,
        "IdentityPolicy": IdentityPolicy,
        "VerificationTier": VerificationTier,
        "certify_project": certify_project,
        "evaluate_qualification": evaluate_qualification,
        "init_project": init_project,
        "save_qualified_workflow": save_qualified_workflow,
        "set_action_classification": set_action_classification,
        "set_effect_policy": set_effect_policy,
        "set_identity_policy": set_identity_policy,
        "workflow_contract_sha256": workflow_contract_sha256,
    }


def _load(bundle_dir: Path):
    api = _flow_api()
    try:
        return api["Workflow"].load(bundle_dir)
    except Exception as exc:
        if "duplicate_step_id" in str(exc):
            raise QualificationError(
                "Action identity is ambiguous because a step id is defined more than "
                "once; no qualification change was written."
            ) from exc
        raise QualificationError(f"Cannot open the sealed workflow bundle: {exc}") from exc


def _policy(source: str):
    try:
        return _flow_api()["load_policy"](source)
    except (FileNotFoundError, ValueError) as exc:
        raise QualificationError(str(exc)) from exc


def _effect_api():
    """Load Flow's canonical effect models without duplicating their schema."""

    try:
        from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr
    except ImportError as exc:  # pragma: no cover - installed app bundles Flow
        raise QualificationError(
            "The bundled OpenAdapt Flow effect runtime is unavailable. "
            "Reinstall OpenAdapt Desktop before binding an effect."
        ) from exc
    return Effect, EffectKind, ValueExpr


def _runtime_version() -> str:
    try:
        return version("openadapt-flow")
    except PackageNotFoundError:  # editable/source development
        try:
            from openadapt_flow import __version__

            return str(__version__)
        except (ImportError, AttributeError):
            return "source"


def environment_digest_from_identifier(identifier: str) -> str:
    """Derive a reproducible contract digest from an operator-defined identifier.

    This is not an automatic machine measurement.  A runner can reproduce the
    digest only when it is configured with the same trimmed UTF-8 identifier.
    Environments with a measured identity should pass ``environment_digest``
    directly instead.
    """

    normalized = identifier.strip()
    if not normalized:
        raise QualificationError("environment identifier cannot be blank")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _actions_for_graph_ref(workflow, action_ref: str) -> list:
    """Resolve the exact action-node id emitted by Flow's graph projection."""

    if workflow.program is None:
        return [step for step in workflow.steps if step.id == action_ref]

    matches: list = []

    def visit(graph, prefix: str, ancestors: frozenset[str]) -> None:
        for state_id, state in graph.states.items():
            node_id = f"{prefix}{state_id}"
            if state.kind.value == "action" and state.step is not None:
                if node_id == action_ref:
                    matches.append(state.step)
            if state.kind.value != "loop" or state.loop is None:
                continue
            body = workflow.subflows.get(state.loop.body)
            if body is None or state.loop.body in ancestors:
                continue
            visit(body, f"{node_id}::", ancestors | {state.loop.body})

    visit(workflow.program, "", frozenset())
    return matches


def _resolve_action(workflow, action_ref: str):
    if not action_ref:
        raise QualificationError("step_id is required")
    matches = _actions_for_graph_ref(workflow, action_ref)
    if len(matches) != 1:
        detail = "not found" if not matches else f"ambiguous ({len(matches)} matches)"
        raise QualificationError(
            f"Action {action_ref!r} is {detail}; no qualification change was written."
        )
    return matches[0]


def _identity_sources(step) -> list[dict[str, Any]]:
    """Project only identity evidence the canonical Flow runtime consumes."""

    anchor = step.anchor
    if anchor is None:
        return []
    sources: list[dict[str, Any]] = []
    template = anchor.identity_template
    if anchor.structured_identity or (template is not None and template.structured):
        sources.append(
            {
                "kind": "structured",
                "label": "Application identity fields",
                "match": "Canonical Flow identity ladder",
            }
        )
    if anchor.identifier_crop and anchor.identifier_region:
        sources.append(
            {
                "kind": "identifier_region",
                "label": "Captured identifier region",
                "match": "Canonical Flow identity ladder",
                "region": list(anchor.identifier_region),
            }
        )
    if anchor.context_text or (template is not None and template.tokens):
        sources.append(
            {
                "kind": "captured_context",
                "label": "Captured row identity",
                "match": "Canonical Flow identity ladder",
            }
        )
    return sources


def _expr_view(expr) -> dict[str, Any] | None:
    if expr is None:
        return None
    if expr.param is not None:
        return {"source": "parameter", "value": expr.param}
    return {"source": "literal", "value": expr.literal}


def _effect_view(index: int, effect, verification_policy) -> dict[str, Any]:
    return {
        "index": index,
        "kind": effect.kind.value,
        "match": {key: _expr_view(value) for key, value in effect.match.items()},
        "field": effect.field,
        "value": _expr_view(effect.value),
        "expected_count": effect.expected_count,
        "idempotency_key": _expr_view(effect.idempotency_key),
        "key_field": effect.key_field,
        "count_new_only": effect.count_new_only,
        "risk": effect.risk,
        "needs_operator_confirmation": effect.needs_operator_confirmation,
        "verification_tier": (
            int(verification_policy.tier) if verification_policy is not None else None
        ),
        "effect_contract_hash": effect.contract_hash(),
    }


def _qualification_controls(workflow, graph: dict[str, Any]) -> dict[str, Any]:
    """Project writable controls from the executable workflow and canonical project."""

    parameter_names = sorted(
        set(workflow.params) | set(workflow.param_specs) | set(workflow.secret_params)
    )
    parameters = []
    for name in parameter_names:
        spec = workflow.param_specs.get(name)
        parameters.append(
            {
                "name": name,
                "type": spec.type.value if spec is not None else "string",
                "secret": name in workflow.secret_params,
            }
        )

    project = workflow.qualification
    effect_policies = (
        {
            (item.step_id, item.effect_index): item
            for item in project.effect_policies
        }
        if project is not None
        else {}
    )
    actions: dict[str, dict[str, Any]] = {}
    for node in graph["nodes"]:
        if node["kind"] != "action":
            continue
        matches = _actions_for_graph_ref(workflow, node["id"])
        if len(matches) != 1:
            continue
        step = matches[0]
        sources = _identity_sources(step)
        classification = (
            project.action_classifications.get(step.id) if project is not None else None
        )
        identity_policy = (
            project.identity_policies.get(step.id) if project is not None else None
        )
        actions[node["id"]] = {
            "step_id": step.id,
            "classification": (
                classification.model_dump(mode="json") if classification else None
            ),
            "identity": {
                "can_arm": bool(sources),
                "armed": bool(step.identity_armed),
                "sources": sources,
                "policy": (
                    identity_policy.model_dump(mode="json") if identity_policy else None
                ),
            },
            "effects": [
                _effect_view(
                    index,
                    effect,
                    effect_policies.get((step.id, index)),
                )
                for index, effect in enumerate(step.effects)
            ],
        }
    return {"parameters": parameters, "actions": actions}


def _save(workflow, bundle_dir: Path) -> None:
    """Reseal the bundle through Flow's canonical qualified-artifact saver."""

    try:
        _flow_api()["save_qualified_workflow"](workflow, bundle_dir)
        type(workflow).load(bundle_dir)
    except Exception as exc:
        raise QualificationError(f"Could not reseal the qualified bundle: {exc}") from exc


def _certification_is_current(workflow, report, *, policy_name: str) -> bool:
    project = workflow.qualification
    if project is None or project.last_certification is None or not report.passed:
        return False
    certification = project.last_certification
    provenance = workflow.manifest.provenance if workflow.manifest else None
    api = _flow_api()
    return bool(
        certification.passed
        and certification.project_revision == project.revision
        and certification.project_contract_sha256 == project.contract_sha256()
        and certification.workflow_contract_sha256
        == api["workflow_contract_sha256"](workflow)
        and certification.environment_contract_sha256
        == project.environment.contract_sha256()
        and certification.policy_name == policy_name
        and certification.report_sha256 == report.report_sha256()
        and provenance is not None
        and provenance.certified
        and provenance.certification_status == "certified"
        and provenance.policy_name == policy_name
    )


def inspect_bundle(
    bundle_dir: Path,
    *,
    workflow_id: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Return graph, canonical project/report, controls, and exact refusals."""

    api = _flow_api()
    workflow = _load(bundle_dir)
    policy = _policy(policy_source)
    graph_payload = api["build_program_graph"](workflow).model_dump(mode="json")
    lint = api["lint_workflow"](workflow)
    runtime_policy = api["evaluate_policy"](workflow, policy)
    report = api["evaluate_qualification"](
        workflow,
        policy=policy,
        evidence_root=bundle_dir / "qualification-evidence",
    )
    provenance = workflow.manifest.provenance if workflow.manifest else None
    project = workflow.qualification
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "policy": policy.name,
        "qualification_schema": (
            project.schema_version if project is not None else "openadapt.qualification-project/v1"
        ),
        "project": project.model_dump(mode="json") if project is not None else None,
        "migration_required": project is None,
        "certification_current": _certification_is_current(
            workflow,
            report,
            policy_name=policy.name,
        ),
        "report": report.model_dump(mode="json"),
        "graph": graph_payload,
        "controls": _qualification_controls(workflow, graph_payload),
        "lint": lint.model_dump(mode="json"),
        "runtime_policy": runtime_policy.model_dump(mode="json"),
        # Compatibility alias for the existing frontend while the canonical
        # report is now the certification source of truth.
        "certification": runtime_policy.model_dump(mode="json"),
        "provenance": (
            provenance.model_dump(mode="json")
            if provenance is not None
            else {
                "certified": False,
                "certification_status": None,
                "policy_name": None,
            }
        ),
    }


def initialize_qualification(
    bundle_dir: Path,
    *,
    workflow_id: str,
    target_kind: QualificationTargetKind,
    application: str,
    application_version: str,
    environment_label: str | None = None,
    environment_digest: str | None = None,
    required_capabilities: list[str] | None = None,
    minimum_effect_tier: int = 3,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Attach the canonical v1 project to an existing compiled workflow."""

    if target_kind not in {"web", "windows", "macos", "linux", "rdp", "citrix"}:
        raise QualificationError("target_kind is not a supported execution surface")
    application = application.strip()
    application_version = application_version.strip()
    if not application or not application_version:
        raise QualificationError("application and application_version are required")
    if environment_digest is None:
        if environment_label is None:
            raise QualificationError(
                "environment_label or an exact environment_digest is required"
            )
        environment_digest = environment_digest_from_identifier(environment_label)
    elif len(environment_digest) != 64:
        raise QualificationError("environment_digest must be a SHA-256 hex digest")

    api = _flow_api()
    workflow = _load(bundle_dir)
    if workflow.qualification is not None:
        raise QualificationError(
            "This workflow already has a qualification project; reopen it to continue."
        )
    try:
        environment = api["EnvironmentBoundary"](
            target_kind=target_kind,
            application=application,
            application_version=application_version,
            environment_digest=environment_digest,
            runtime_version=_runtime_version(),
            required_capabilities=required_capabilities or [],
        )
        api["init_project"](
            workflow,
            environment=environment,
            minimum_effect_tier=api["VerificationTier"](minimum_effect_tier),
        )
        _save(workflow, bundle_dir)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
    )


def set_action_risk(
    bundle_dir: Path,
    *,
    workflow_id: str,
    step_id: str,
    risk: QualificationRisk,
    explanation: str | None = None,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Set canonical business risk while preserving executable risk invariants."""

    if risk not in {"read_only", "state_changing", "consequential", "irreversible"}:
        raise QualificationError(
            "risk must be read_only, state_changing, consequential, or irreversible"
        )
    api = _flow_api()
    workflow = _load(bundle_dir)
    if workflow.qualification is None:
        raise QualificationError(
            "Initialize the qualification boundary before reviewing action risk."
        )
    step = _resolve_action(workflow, step_id)
    if risk == "irreversible":
        step.risk = "irreversible"
        for effect in step.effects:
            effect.risk = "irreversible"
    try:
        api["set_action_classification"](
            workflow,
            api["ActionRiskClassification"](
                step_id=step.id,
                classification=api["ActionRiskClass"](risk),
                explanation=(
                    (explanation or "").strip()
                    or f"Operator reviewed this action as {risk.replace('_', ' ')}"
                ),
                operator_confirmed=True,
            ),
        )
        _save(workflow, bundle_dir)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
    )


def arm_action_identity(
    bundle_dir: Path,
    *,
    workflow_id: str,
    step_id: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Arm retained identity evidence and bind canonical-ladder enforcement."""

    api = _flow_api()
    workflow = _load(bundle_dir)
    if workflow.qualification is None:
        raise QualificationError(
            "Initialize the qualification boundary before arming identity."
        )
    step = _resolve_action(workflow, step_id)
    sources = _identity_sources(step)
    if not sources:
        raise QualificationError(
            "This action has no retained structured identity, identifier region, "
            "or captured row identity. Record or teach the action with identity "
            "evidence before arming it."
        )
    step.identity_armed = True
    step.identity_unarmed_reason = None
    try:
        api["set_identity_policy"](
            workflow,
            api["IdentityPolicy"](
                step_id=step.id,
                enforcement=api["IdentityEnforcement"].CANONICAL_LADDER,
            ),
        )
        _save(workflow, bundle_dir)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
    )


def bind_action_effect(
    bundle_dir: Path,
    *,
    workflow_id: str,
    step_id: str,
    kind: QualificationEffectKind,
    match_field: str,
    match_param: str,
    field: str | None = None,
    value_param: str | None = None,
    idempotency_param: str | None = None,
    key_field: str = "key",
    expected_count: int = 1,
    count_new_only: bool = False,
    effect_index: int | None = None,
    verification_tier: int = 3,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Bind an executable Flow effect and its canonical evidence-strength policy."""

    if kind not in ("record_written", "field_equals"):
        raise QualificationError("kind must be record_written or field_equals")
    if not match_field.strip():
        raise QualificationError("match_field is required")
    if expected_count < 0:
        raise QualificationError("expected_count cannot be negative")
    if not key_field.strip():
        raise QualificationError("key_field is required")

    api = _flow_api()
    Effect, EffectKind, ValueExpr = _effect_api()
    workflow = _load(bundle_dir)
    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "Initialize the qualification boundary before binding an effect."
        )
    step = _resolve_action(workflow, step_id)
    secrets = set(workflow.secret_params)
    parameters = set(workflow.params) | set(workflow.param_specs) | secrets
    requested = {
        name
        for name in (match_param, value_param, idempotency_param)
        if name is not None
    }
    unknown = sorted(requested - parameters)
    if unknown:
        raise QualificationError(
            "Effect references unknown workflow parameter(s): " + ", ".join(unknown)
        )
    secret_refs = sorted(requested & secrets)
    if secret_refs:
        raise QualificationError(
            "Secret parameters cannot identify a persisted business effect: "
            + ", ".join(secret_refs)
        )
    if kind == "field_equals" and (not field or not value_param):
        raise QualificationError(
            "field_equals requires both a persisted field and a value parameter"
        )
    if count_new_only and kind != "record_written":
        raise QualificationError("count_new_only applies only to record_written")

    effect = Effect(
        kind=EffectKind(kind),
        match={match_field.strip(): ValueExpr(param=match_param)},
        field=field.strip() if field else None,
        value=ValueExpr(param=value_param) if value_param else None,
        expected_count=expected_count,
        idempotency_key=(
            ValueExpr(param=idempotency_param) if idempotency_param else None
        ),
        key_field=key_field.strip(),
        count_new_only=count_new_only,
        risk=step.risk,
        needs_operator_confirmation=False,
    )

    if effect_index is None:
        placeholders = [
            index
            for index, existing in enumerate(step.effects)
            if existing.needs_operator_confirmation
        ]
        if len(placeholders) == 1:
            effect_index = placeholders[0]
    if effect_index is None:
        if effect in step.effects:
            effect_index = step.effects.index(effect)
        else:
            step.effects.append(effect)
            effect_index = len(step.effects) - 1
    else:
        if effect_index < 0 or effect_index >= len(step.effects):
            raise QualificationError(
                f"Effect index {effect_index} is outside this action's effect inventory"
            )
        step.effects[effect_index] = effect

    try:
        classification = project.action_classifications.get(step.id)
        if classification is None or classification.classification.value in {
            "unknown",
            "read_only",
        }:
            inferred_risk = "irreversible" if step.risk == "irreversible" else "state_changing"
            api["set_action_classification"](
                workflow,
                api["ActionRiskClassification"](
                    step_id=step.id,
                    classification=api["ActionRiskClass"](inferred_risk),
                    explanation="Operator bound a persisted business-effect contract",
                    operator_confirmed=True,
                ),
            )
        api["set_effect_policy"](
            workflow,
            step_id=step.id,
            effect_index=effect_index,
            tier=api["VerificationTier"](verification_tier),
        )
        _save(workflow, bundle_dir)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
    )


def certify_bundle(
    bundle_dir: Path,
    *,
    workflow_id: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Run Flow's canonical certification and persist the versioned report."""

    api = _flow_api()
    workflow = _load(bundle_dir)
    if workflow.qualification is None:
        raise QualificationError(
            "Initialize the qualification boundary before running certification."
        )
    policy = _policy(policy_source)
    try:
        report = api["certify_project"](
            workflow,
            policy=policy,
            evidence_root=bundle_dir / "qualification-evidence",
        )
        _save(workflow, bundle_dir)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    result = inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
    )
    result["certification_attempt"] = report.model_dump(mode="json")
    return result
