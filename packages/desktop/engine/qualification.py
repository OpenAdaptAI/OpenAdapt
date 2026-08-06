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
import json
from datetime import datetime, timezone
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
QualificationIdentityEnforcement = Literal["canonical_ladder", "signal_quorum"]
QualificationTargetKind = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
DEFAULT_QUALIFICATION_POLICY = "clinical-write"
ENVIRONMENT_IDENTIFIER_DERIVATION = "sha256(trimmed UTF-8 operator identifier)"


class QualificationError(RuntimeError):
    """A qualification request was refused without changing the bundle."""


def _flow_api() -> dict[str, Any]:
    """Load the pinned canonical Flow API only when qualification is used."""

    try:
        import openadapt_flow.qualification as flow_qualification
        from openadapt_flow.ir import RunReport, Workflow
        from openadapt_flow.policy import evaluate_policy, lint_workflow, load_policy
        from openadapt_flow.qualification import (
            ActionRiskClass,
            ActionRiskClassification,
            EnvironmentBoundary,
            IdentityEnforcement,
            IdentityEvidenceSource,
            IdentityMatchMode,
            IdentityNormalizer,
            IdentityPolicy,
            IdentitySignalPolicy,
            QualificationActionTarget,
            QualificationCase,
            QualificationCaseKind,
            QualificationCaseResult,
            QualificationOutcome,
            VerificationTier,
            add_case,
            certify_project,
            evaluate_qualification,
            init_project,
            qualification_action_requirements,
            record_case_results,
            save_qualified_workflow,
            set_action_classification,
            set_case_scope,
            set_effect_policy,
            set_identity_policy,
            set_minimum_effect_tier,
            set_trusted_runner_key,
            sign_case_result,
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
        "RunReport": RunReport,
        "evaluate_policy": evaluate_policy,
        "lint_workflow": lint_workflow,
        "load_policy": load_policy,
        "iter_workflow_steps": iter_workflow_steps,
        "build_program_graph": build_program_graph,
        "ActionRiskClass": ActionRiskClass,
        "ActionRiskClassification": ActionRiskClassification,
        "QualificationCase": QualificationCase,
        "QualificationCaseKind": QualificationCaseKind,
        "QualificationCaseResult": QualificationCaseResult,
        "QualificationActionTarget": QualificationActionTarget,
        "QualificationOutcome": QualificationOutcome,
        "EnvironmentBoundary": EnvironmentBoundary,
        "IdentityEnforcement": IdentityEnforcement,
        "IdentityEvidenceSource": IdentityEvidenceSource,
        "IdentitySignalKey": getattr(flow_qualification, "IdentitySignalKey", None),
        "IdentityMatchMode": IdentityMatchMode,
        "IdentityNormalizer": IdentityNormalizer,
        "IdentityPolicy": IdentityPolicy,
        "IdentitySignalPolicy": IdentitySignalPolicy,
        "VerificationTier": VerificationTier,
        "add_case": add_case,
        "certify_project": certify_project,
        "evaluate_qualification": evaluate_qualification,
        "init_project": init_project,
        "qualification_action_requirements": qualification_action_requirements,
        "record_case_results": record_case_results,
        "save_qualified_workflow": save_qualified_workflow,
        "set_trusted_runner_key": set_trusted_runner_key,
        "sign_case_result": sign_case_result,
        "set_action_classification": set_action_classification,
        "set_case_scope": set_case_scope,
        "set_effect_policy": set_effect_policy,
        "set_identity_policy": set_identity_policy,
        "set_minimum_effect_tier": set_minimum_effect_tier,
        "workflow_contract_sha256": workflow_contract_sha256,
    }


def _load(bundle_dir: Path, *, key: str | None = None):
    api = _flow_api()
    try:
        return api["Workflow"].load(bundle_dir, key=key)
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
    available_sources = {member.value for member in _flow_api()["IdentityEvidenceSource"]}
    if "application" in available_sources:
        sources.append(
            {
                "kind": "application",
                "label": "Live application identity",
                "match": "Observed immediately before actuation",
            }
        )
    if "session" in available_sources:
        sources.append(
            {
                "kind": "session",
                "label": "Live session continuity",
                "match": "Observed immediately before actuation",
            }
        )
    if "workflow_state" in available_sources:
        sources.append(
            {
                "kind": "workflow_state",
                "label": "Live workflow state",
                "match": "Observed immediately before actuation",
            }
        )
    return sources


def _identity_policy_view(identity_policy) -> dict[str, Any] | None:
    if identity_policy is None:
        return None
    payload = identity_policy.model_dump(mode="json")
    for signal in payload.get("signals", []):
        # Flow 1.23 closes this field to a semantic key. The compatibility
        # projection keeps an older qualified project inspectable while the
        # Desktop release moves atomically to the new runtime.
        if "key" not in signal and "field" in signal:
            signal["key"] = signal.pop("field")
        signal.setdefault("extract_pattern", None)
        signal.setdefault("expected_value", None)
        signal.setdefault("params", [])
    return payload


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

    from openadapt_flow.policy import executable_actuation_paths

    parameter_names = sorted(
        set(workflow.params) | set(workflow.param_specs) | set(workflow.secret_params)
    )
    parameters = []
    for name in parameter_names:
        spec = workflow.param_specs.get(name)
        secret = name in workflow.secret_params
        example = spec.example if spec is not None else workflow.params.get(name)
        parameters.append(
            {
                "name": name,
                "type": spec.type.value if spec is not None else "string",
                "secret": secret,
                "required": spec.required if spec is not None else True,
                # A secret's recorded value and allowed-value list are also
                # secret material. Desktop receives only the schema needed to
                # render the credential reference, never its reusable value.
                "example": None if secret else example,
                "choices": [] if secret or spec is None else list(spec.choices),
            }
        )

    project = workflow.qualification
    effect_policies = (
        {(item.step_id, item.effect_index): item for item in project.effect_policies}
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
        identity_policy = project.identity_policies.get(step.id) if project is not None else None
        actions[node["id"]] = {
            "step_id": step.id,
            "execution_paths": sorted(executable_actuation_paths(step)),
            "classification": (classification.model_dump(mode="json") if classification else None),
            "identity": {
                "can_arm": bool(sources),
                "armed": bool(step.identity_armed),
                "sources": sources,
                "policy": _identity_policy_view(identity_policy),
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


def _capability_coverage(
    bundle_dir: Path,
    *,
    workflow_contract_sha256: str,
    project,
) -> dict[str, Any]:
    """Compare requirements with current signed runtime observations."""

    if project is None:
        return {
            "required": [],
            "observed": [],
            "missing": [],
            "satisfied": False,
            "cases": [],
        }
    from engine.qualification_capabilities import (
        current_signed_capability_observations,
    )

    required = set(project.environment.required_capabilities)
    receipts = current_signed_capability_observations(
        bundle_dir,
        workflow_contract_sha256=workflow_contract_sha256,
        project=project,
    )
    case_views: list[dict[str, Any]] = []
    observed_by_all: set[str] | None = None
    for case in (item for item in project.cases if item.required):
        receipt = receipts.get(case.id)
        observed = set(receipt.observed_capabilities) if receipt is not None else set()
        receipt_relative_path = (
            f"{case.id}/{receipt.run_id}/capability-observation.json"
            if receipt is not None
            else None
        )
        receipt_sha256 = None
        if receipt_relative_path is not None:
            receipt_path = bundle_dir / "qualification-evidence" / receipt_relative_path
            if receipt_path.is_file() and not receipt_path.is_symlink():
                receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        current_results = [
            item for item in case.results if item.project_revision == project.revision
        ]
        latest_result = current_results[-1] if current_results else None
        latest = (
            latest_result
            if latest_result is not None
            and receipt_relative_path is not None
            and receipt_sha256 is not None
            and set(latest_result.runner_capabilities) == observed
            and any(
                evidence.relative_path == receipt_relative_path
                and evidence.sha256 == receipt_sha256
                for evidence in latest_result.evidence
            )
            else None
        )
        observed_by_all = (
            set(observed) if observed_by_all is None else observed_by_all.intersection(observed)
        )
        case_views.append(
            {
                "case_id": case.id,
                "has_current_receipt": receipt is not None,
                "has_current_result": latest is not None,
                "status": latest.status if latest is not None else None,
                "observed": sorted(observed),
                "missing": sorted(required - observed),
                "runtime_version": receipt.runtime_version if receipt is not None else None,
                "target_kind": (receipt.observed_target_kind if receipt is not None else None),
            }
        )
    observed = observed_by_all or set()
    missing = required - observed
    has_required_case_results = bool(case_views) and all(
        item["has_current_result"] for item in case_views
    )
    return {
        "required": sorted(required),
        "observed": sorted(observed),
        "missing": sorted(missing),
        "satisfied": (not required) or (has_required_case_results and not missing),
        "cases": case_views,
    }


def _save(workflow, bundle_dir: Path, *, key: str | None = None) -> None:
    """Reseal the bundle through Flow's canonical qualified-artifact saver."""

    try:
        _flow_api()["save_qualified_workflow"](workflow, bundle_dir, key=key)
        type(workflow).load(bundle_dir, key=key)
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
        and certification.workflow_contract_sha256 == api["workflow_contract_sha256"](workflow)
        and certification.environment_contract_sha256 == project.environment.contract_sha256()
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
    bundle_key: str | None = None,
) -> dict:
    """Return graph, canonical project/report, controls, and exact refusals."""

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
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
    workflow_contract = api["workflow_contract_sha256"](workflow)
    capability_coverage = _capability_coverage(
        bundle_dir,
        workflow_contract_sha256=workflow_contract,
        project=project,
    )
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "policy": policy.name,
        "qualification_schema": (
            project.schema_version if project is not None else "openadapt.qualification-project/v1"
        ),
        "project": project.model_dump(mode="json") if project is not None else None,
        "capability_coverage": capability_coverage,
        "migration_required": project is None,
        "certification_current": (
            capability_coverage["satisfied"]
            and _certification_is_current(workflow, report, policy_name=policy.name)
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
    bundle_key: str | None = None,
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
            raise QualificationError("environment_label or an exact environment_digest is required")
        environment_digest = environment_digest_from_identifier(environment_label)
    elif len(environment_digest) != 64:
        raise QualificationError("environment_digest must be a SHA-256 hex digest")

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
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
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def set_action_risk(
    bundle_dir: Path,
    *,
    workflow_id: str,
    step_id: str,
    risk: QualificationRisk,
    explanation: str | None = None,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Set canonical business risk while preserving executable risk invariants."""

    if risk not in {"read_only", "state_changing", "consequential", "irreversible"}:
        raise QualificationError(
            "risk must be read_only, state_changing, consequential, or irreversible"
        )
    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
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
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def arm_action_identity(
    bundle_dir: Path,
    *,
    workflow_id: str,
    step_id: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Arm retained identity evidence and bind canonical-ladder enforcement."""

    return set_action_identity_policy(
        bundle_dir,
        workflow_id=workflow_id,
        step_id=step_id,
        enforcement="canonical_ladder",
        signals=[],
        quorum=0,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def set_action_identity_policy(
    bundle_dir: Path,
    *,
    workflow_id: str,
    step_id: str,
    enforcement: QualificationIdentityEnforcement,
    signals: list[dict[str, Any]],
    quorum: int,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Arm retained evidence and persist Flow's canonical identity policy."""

    if enforcement not in {"canonical_ladder", "signal_quorum"}:
        raise QualificationError("enforcement must be canonical_ladder or signal_quorum")
    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    if workflow.qualification is None:
        raise QualificationError("Initialize the qualification boundary before setting identity.")
    step = _resolve_action(workflow, step_id)
    available_sources = _identity_sources(step)
    if not available_sources:
        raise QualificationError(
            "This action has no retained structured identity or live context "
            "observation available. Record or teach the target with identity "
            "evidence before arming it."
        )
    try:
        canonical_signals = []
        available_by_kind = {source["kind"]: source for source in available_sources}
        model_fields = api["IdentitySignalPolicy"].model_fields
        for signal in signals:
            if not isinstance(signal, dict):
                raise QualificationError("identity signals must be objects")
            source_name = str(signal.get("source") or "")
            available = available_by_kind.get(source_name)
            if available is None:
                raise QualificationError(
                    f"identity policy references unavailable evidence: {source_name}"
                )
            explicit_region = signal.get("region")
            region = None
            if source_name == "identifier_region":
                region = tuple(available["region"])
                if explicit_region is not None and tuple(explicit_region) != region:
                    raise QualificationError(
                        "identifier_region must use the retained qualified region"
                    )
            elif source_name == "captured_context" and explicit_region is not None:
                region = tuple(explicit_region)
            kwargs: dict[str, Any] = {
                "source": api["IdentityEvidenceSource"](source_name),
                "match": api["IdentityMatchMode"](str(signal.get("match") or "exact")),
                "normalizers": [
                    api["IdentityNormalizer"](str(item))
                    for item in (signal.get("normalizers") or [])
                ],
                "region": region,
            }
            if "key" in model_fields:
                identity_signal_key = api["IdentitySignalKey"]
                if identity_signal_key is None:
                    raise QualificationError(
                        "The bundled Flow runtime cannot author semantic identity keys."
                    )
                kwargs.update(
                    {
                        "key": identity_signal_key(
                            str(signal.get("key") or signal.get("field") or "")
                        ),
                        "extract_pattern": (
                            str(signal["extract_pattern"]).strip()
                            if signal.get("extract_pattern")
                            else None
                        ),
                        "expected_value": (
                            str(signal["expected_value"]).strip()
                            if signal.get("expected_value")
                            else None
                        ),
                        "params": [
                            str(item).strip()
                            for item in (signal.get("params") or [])
                            if str(item).strip()
                        ],
                    }
                )
            else:
                kwargs["field"] = str(signal.get("key") or signal.get("field") or "")
            canonical_signals.append(api["IdentitySignalPolicy"](**kwargs))
        identity_policy = api["IdentityPolicy"](
            step_id=step.id,
            enforcement=api["IdentityEnforcement"](enforcement),
            signals=canonical_signals,
            quorum=quorum,
        )
        step.identity_armed = True
        step.identity_unarmed_reason = None
        api["set_identity_policy"](
            workflow,
            identity_policy,
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (QualificationError, ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def set_action_effect_verification(
    bundle_dir: Path,
    *,
    workflow_id: str,
    step_id: str,
    effect_index: int,
    verification_tier: int,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Set the minimum evidence tier required for one declared Flow effect."""

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    if workflow.qualification is None:
        raise QualificationError(
            "Initialize the qualification boundary before setting effect verification."
        )
    step = _resolve_action(workflow, step_id)
    if effect_index < 0 or effect_index >= len(step.effects):
        raise QualificationError(
            f"Effect index {effect_index} is outside this action's effect inventory"
        )
    try:
        api["set_effect_policy"](
            workflow,
            step_id=step.id,
            effect_index=effect_index,
            tier=api["VerificationTier"](verification_tier),
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def set_project_minimum_effect_tier(
    bundle_dir: Path,
    *,
    workflow_id: str,
    minimum_effect_tier: int,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Version the project's canonical minimum effect-verification strength."""

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    if workflow.qualification is None:
        raise QualificationError(
            "Initialize the qualification boundary before setting minimum effect strength."
        )
    try:
        api["set_minimum_effect_tier"](
            workflow,
            api["VerificationTier"](minimum_effect_tier),
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
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
    bundle_key: str | None = None,
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
    workflow = _load(bundle_dir, key=bundle_key)
    project = workflow.qualification
    if project is None:
        raise QualificationError("Initialize the qualification boundary before binding an effect.")
    step = _resolve_action(workflow, step_id)
    secrets = set(workflow.secret_params)
    parameters = set(workflow.params) | set(workflow.param_specs) | secrets
    requested = {name for name in (match_param, value_param, idempotency_param) if name is not None}
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
        idempotency_key=(ValueExpr(param=idempotency_param) if idempotency_param else None),
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
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def add_qualification_case(
    bundle_dir: Path,
    *,
    workflow_id: str,
    case_id: str,
    kind: str,
    description: str = "",
    input_ref: str | None = None,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Add one typed representative/fault case through Flow's canonical API."""

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    if workflow.qualification is None:
        raise QualificationError("Initialize the qualification boundary before adding cases.")
    try:
        case_kind = api["QualificationCaseKind"](kind)
        expected = "verified" if case_kind.value == "representative" else "halted"
        api["add_case"](
            workflow,
            api["QualificationCase"](
                id=case_id,
                kind=case_kind,
                description=description,
                input_ref=input_ref,
                expected_outcome=expected,
            ),
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def prepare_local_qualification_runner(
    bundle_dir: Path,
    *,
    workflow_id: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Trust this Desktop installation's protected qualification signer."""

    from engine.qualification_keys import KEY_ID, qualification_signer

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    if workflow.qualification is None:
        raise QualificationError("Initialize the qualification boundary before running cases.")
    _private_key, public_key = qualification_signer()
    try:
        api["set_trusted_runner_key"](
            workflow,
            key_id=KEY_ID,
            public_key_base64=public_key,
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def set_local_qualification_case_scope(
    bundle_dir: Path,
    *,
    workflow_id: str,
    case_id: str,
    runtime_input_bytes: bytes,
    fault_target: dict[str, Any] | None = None,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Bind a Desktop case to its canonical inputs and executable action paths."""

    from openadapt_flow.policy import executable_actuation_paths
    from openadapt_flow.runtime.authorization import parse_runtime_inputs_bytes

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    project = workflow.qualification
    if project is None:
        raise QualificationError("Initialize the qualification boundary before running cases.")
    try:
        _params, worklists = parse_runtime_inputs_bytes(runtime_input_bytes, workflow=workflow)
    except ValueError as exc:
        raise QualificationError("Qualification inputs are not canonical governed inputs") from exc
    if worklists:
        raise QualificationError("Desktop qualification cases do not yet support worklists")
    steps = {step.id: step for step in api["iter_workflow_steps"](workflow)}
    selected_fault_target = None
    if fault_target is not None:
        if not isinstance(fault_target, dict) or set(fault_target) != {
            "step_id",
            "actuation_path",
        }:
            raise QualificationError("Fault target must name one exact action and actuation path")
        try:
            selected_fault_target = api["QualificationActionTarget"](
                step_id=fault_target["step_id"],
                actuation_path=fault_target["actuation_path"],
            )
        except (ValueError, TypeError) as exc:
            raise QualificationError(
                "Fault target must name one exact action and actuation path"
            ) from exc
        fault_step = steps.get(selected_fault_target.step_id)
        if (
            fault_step is None
            or selected_fault_target.actuation_path
            not in executable_actuation_paths(fault_step)
        ):
            raise QualificationError("Fault target is outside the executable case scope")
    targets = []
    for step_id, step in sorted(steps.items()):
        paths = executable_actuation_paths(step)
        if not paths:
            continue
        if selected_fault_target is not None and selected_fault_target.step_id == step_id:
            path = selected_fault_target.actuation_path
        elif "gui" in paths:
            path = "gui"
        elif "api" in paths:
            path = "api"
        else:
            path = None
        if path is None:
            raise QualificationError(f"Qualification action {step_id!r} has no executable path")
        targets.append(api["QualificationActionTarget"](step_id=step_id, actuation_path=path))
    try:
        api["set_case_scope"](
            workflow,
            case_id=case_id,
            runtime_input_sha256=hashlib.sha256(runtime_input_bytes).hexdigest(),
            action_targets=targets,
            fault_target=selected_fault_target,
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def record_local_qualification_result(
    bundle_dir: Path,
    *,
    workflow_id: str,
    case_id: str,
    observed_outcome: str,
    evidence: list[dict[str, str]],
    capability_observation: Any,
    detail_code: str | None = None,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Sign and retain one exact local run result for the current revision."""

    from engine.qualification_capabilities import (
        SignedQualificationCapabilityObservation,
        current_signed_capability_observations,
    )
    from engine.qualification_keys import KEY_ID, RUNNER_ID, qualification_signer

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    project = workflow.qualification
    if project is None:
        raise QualificationError(
            "Initialize the qualification boundary before recording case evidence."
        )
    case = next((candidate for candidate in project.cases if candidate.id == case_id), None)
    if case is None:
        raise QualificationError(f"Unknown qualification case {case_id!r}")
    observation = SignedQualificationCapabilityObservation.model_validate(capability_observation)
    workflow_contract = api["workflow_contract_sha256"](workflow)
    current_observations = current_signed_capability_observations(
        bundle_dir,
        workflow_contract_sha256=workflow_contract,
        project=project,
    )
    if current_observations.get(case_id) != observation:
        raise QualificationError(
            "The capability observation is not the current signed receipt for this case"
        )
    capability_relative_path = f"{case_id}/{observation.run_id}/capability-observation.json"
    capability_path = bundle_dir / "qualification-evidence" / capability_relative_path
    capability_sha256 = hashlib.sha256(capability_path.read_bytes()).hexdigest()
    if not any(
        item.get("kind") == "other"
        and item.get("relative_path") == capability_relative_path
        and item.get("sha256") == capability_sha256
        for item in evidence
    ):
        raise QualificationError(
            "The signed case evidence does not hash-bind its capability receipt"
        )
    run_reports = [item for item in evidence if item.get("kind") == "run_report"]
    inputs = [item for item in evidence if item.get("kind") == "case_input"]
    if len(run_reports) != 1 or len(inputs) != 1:
        raise QualificationError(
            "Qualification evidence requires one exact report and one exact input artifact"
        )
    try:
        evidence_root = (bundle_dir / "qualification-evidence").resolve()
        report_path = (evidence_root / str(run_reports[0]["relative_path"])).resolve()
        input_path = (evidence_root / str(inputs[0]["relative_path"])).resolve()
        if (
            not report_path.is_relative_to(evidence_root)
            or not input_path.is_relative_to(evidence_root)
            or report_path.is_symlink()
            or input_path.is_symlink()
        ):
            raise OSError("qualification evidence leaves its local root")
        report_bytes = report_path.read_bytes()
        input_bytes = input_path.read_bytes()
        report = api["RunReport"].model_validate_json(report_bytes)
    except (OSError, ValueError) as exc:
        raise QualificationError("Qualification report evidence is invalid") from exc
    input_sha256 = str(inputs[0]["sha256"])
    if (
        hashlib.sha256(input_bytes).hexdigest() != input_sha256
        or report.governed_qualification_case_input_sha256 != input_sha256
        or report.governed_runtime_inputs_digest != input_sha256
        or report.governed_qualification_run_id_sha256
        != hashlib.sha256(observation.run_id.encode("utf-8")).hexdigest()
    ):
        raise QualificationError("Qualification evidence does not bind this exact input and run")
    if (
        report.governed_qualification_project_id != project.project_id
        or report.governed_qualification_project_revision != project.revision
        or report.governed_qualification_project_contract_sha256 != project.contract_sha256()
        or report.governed_qualification_case_id_sha256
        != hashlib.sha256(case.id.encode("utf-8")).hexdigest()
        or report.governed_qualification_case_kind != case.kind.value
    ):
        raise QualificationError("Qualification report does not bind the current case contract")
    private_key, _public_key = qualification_signer()
    observed = api["QualificationOutcome"](observed_outcome)
    status = "passed" if observed_outcome == case.expected_outcome.value and evidence else "failed"
    try:
        result = api["QualificationCaseResult"](
            case_id=case.id,
            project_id=project.project_id,
            project_revision=project.revision,
            project_contract_sha256=project.contract_sha256(),
            workflow_contract_sha256=workflow_contract,
            environment_contract_sha256=project.environment.contract_sha256(),
            environment_digest=project.environment.environment_digest,
            runtime_version=observation.runtime_version,
            runner_id=RUNNER_ID,
            runner_capabilities=observation.observed_capabilities,
            status=status,
            observed_outcome=observed,
            evidence=evidence,
            detail_code=detail_code,
            attestation_key_id=KEY_ID,
            campaign_id_sha256=report.governed_qualification_campaign_id_sha256,
            case_input_sha256=input_sha256,
            run_id_sha256=report.governed_qualification_run_id_sha256,
        )
        signed = api["sign_case_result"](result, private_key=private_key)
        api["record_case_results"](
            workflow,
            [signed],
            evidence_root=bundle_dir / "qualification-evidence",
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def import_qualification_results(
    bundle_dir: Path,
    *,
    workflow_id: str,
    signed_results_json: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Validate and import signed runner results with local evidence hashes."""

    if len(signed_results_json.encode("utf-8")) > 1_000_000:
        raise QualificationError("Signed qualification results exceed 1 MB")
    try:
        payload = json.loads(signed_results_json)
    except json.JSONDecodeError as exc:
        raise QualificationError("Signed qualification results are not valid JSON") from exc
    raw_results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(raw_results, list) or not raw_results:
        raise QualificationError("Signed qualification results must contain a non-empty list")

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    try:
        results = [api["QualificationCaseResult"].model_validate(item) for item in raw_results]
        api["record_case_results"](
            workflow,
            results,
            evidence_root=bundle_dir / "qualification-evidence",
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    return inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )


def seal_qualification_bundle(
    source: Path,
    destination: Path,
    *,
    workflow_id: str,
    destination_key: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Copy and encrypt one exact bundle through the pinned Flow artifact API."""

    from engine.qualification_lifecycle import copy_bundle_version

    api = _flow_api()
    workflow = _load(source)
    if workflow.encrypted:
        raise QualificationError("This workflow version is already sealed and encrypted")

    project = workflow.qualification
    provenance = workflow.manifest.provenance if workflow.manifest else None
    if project is not None:
        project.last_certification = None
    if provenance is not None and (
        provenance.certified
        or provenance.policy_name is not None
        or provenance.certification_status is not None
    ):
        provenance.certified = False
        provenance.certification_status = "expired"
        provenance.expires_at = datetime.now(timezone.utc).isoformat()

    try:
        copy_bundle_version(source, destination)
        workflow.save(destination, encrypt=True, key=destination_key)
        verified = api["Workflow"].load(destination, key=destination_key)
        if not verified.encrypted or (destination / "workflow.json").exists():
            raise QualificationError("Flow did not produce a ciphertext-only workflow")
        if any((destination / "templates").glob("*.png")):
            raise QualificationError("Flow left a plaintext template in the sealed bundle")
    except Exception as exc:
        if isinstance(exc, QualificationError):
            raise
        raise QualificationError(f"Could not seal the workflow version: {exc}") from exc

    return inspect_bundle(
        destination,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=destination_key,
    )


def certify_bundle(
    bundle_dir: Path,
    *,
    workflow_id: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
    bundle_key: str | None = None,
) -> dict:
    """Run Flow's canonical certification and persist the versioned report."""

    api = _flow_api()
    workflow = _load(bundle_dir, key=bundle_key)
    if workflow.qualification is None:
        raise QualificationError(
            "Initialize the qualification boundary before running certification."
        )
    policy = _policy(policy_source)
    workflow_contract = api["workflow_contract_sha256"](workflow)
    capability_coverage = _capability_coverage(
        bundle_dir,
        workflow_contract_sha256=workflow_contract,
        project=workflow.qualification,
    )
    candidate_report = api["evaluate_qualification"](
        workflow,
        policy=policy,
        evidence_root=bundle_dir / "qualification-evidence",
    )
    if (
        candidate_report.passed
        and capability_coverage["required"]
        and not capability_coverage["satisfied"]
    ):
        details = []
        for case in capability_coverage["cases"]:
            if case["missing"]:
                details.append(f"{case['case_id']}: {', '.join(case['missing'])}")
            elif not case["has_current_result"]:
                details.append(f"{case['case_id']}: signed case result missing")
        raise QualificationError(
            "Certification requires current signed observed-capability evidence for "
            "every required case" + (f" ({'; '.join(details)})" if details else "")
        )
    try:
        report = api["certify_project"](
            workflow,
            policy=policy,
            evidence_root=bundle_dir / "qualification-evidence",
        )
        _save(workflow, bundle_dir, key=bundle_key)
    except (ValueError, TypeError) as exc:
        raise QualificationError(str(exc)) from exc
    result = inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
        bundle_key=bundle_key,
    )
    result["certification_attempt"] = report.model_dump(mode="json")
    return result
