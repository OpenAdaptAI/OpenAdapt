"""Desktop adapter for OpenAdapt Flow's qualification mechanisms.

The canonical workflow schema, traversal, graph projection, linter, policy
evaluator, manifest sealing, and encryption remain owned by ``openadapt-flow``.
This module gives the installed Desktop cockpit a small, writable API over
those mechanisms so an operator does not have to edit ``workflow.json``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

QualificationRisk = Literal["reversible", "irreversible"]
QualificationEffectKind = Literal["record_written", "field_equals"]
DEFAULT_QUALIFICATION_POLICY = "clinical-write"


class QualificationError(RuntimeError):
    """A qualification request was refused without changing the bundle."""


def _flow_api():
    """Load the pinned Flow API only when the qualification surface is used."""

    try:
        from openadapt_flow.ir import Workflow
        from openadapt_flow.policy import evaluate_policy, lint_workflow, load_policy
        from openadapt_flow.traversal import iter_workflow_steps
        from openadapt_flow.visualize import build_program_graph
    except ImportError as exc:  # pragma: no cover - installed app always bundles Flow
        raise QualificationError(
            "The bundled OpenAdapt Flow runtime is unavailable. Reinstall OpenAdapt "
            "Desktop before qualifying this workflow."
        ) from exc
    return (
        Workflow,
        evaluate_policy,
        lint_workflow,
        load_policy,
        iter_workflow_steps,
        build_program_graph,
    )


def _load(bundle_dir: Path):
    Workflow, *_rest = _flow_api()
    try:
        return Workflow.load(bundle_dir)
    except Exception as exc:
        if "duplicate_step_id" in str(exc):
            raise QualificationError(
                "Action identity is ambiguous because a step id is defined more than once; "
                "no qualification change was written."
            ) from exc
        raise QualificationError(f"Cannot open the sealed workflow bundle: {exc}") from exc


def _policy(source: str):
    _Workflow, _evaluate, _lint, load_policy, _iter, _graph = _flow_api()
    try:
        return load_policy(source)
    except (FileNotFoundError, ValueError) as exc:
        raise QualificationError(str(exc)) from exc


def _effect_api():
    """Load Flow's canonical effect models without duplicating their schema."""

    try:
        from openadapt_flow.runtime.effects.effect import (
            Effect,
            EffectKind,
            ValueExpr,
        )
    except ImportError as exc:  # pragma: no cover - installed app bundles Flow
        raise QualificationError(
            "The bundled OpenAdapt Flow effect runtime is unavailable. "
            "Reinstall OpenAdapt Desktop before binding an effect."
        ) from exc
    return Effect, EffectKind, ValueExpr


def _reset_certification(workflow) -> None:
    """Invalidate certification whenever an operator changes workflow intent."""

    manifest = workflow.manifest
    if manifest is None:
        return
    provenance = manifest.provenance
    provenance.policy_name = None
    provenance.certified = False
    provenance.certification_status = None
    provenance.certified_at = None
    provenance.expires_at = None


def _certification_is_current(provenance, *, policy_name: str, policy_passed: bool) -> bool:
    """Return whether the sealed certification matches the live policy result."""

    if (
        provenance is None
        or not provenance.certified
        or provenance.certification_status != "certified"
        or provenance.policy_name != policy_name
        or not policy_passed
    ):
        return False
    if not provenance.expires_at:
        return True
    try:
        expiry_text = provenance.expires_at
        if expiry_text.endswith("Z"):
            expiry_text = f"{expiry_text[:-1]}+00:00"
        expiry = datetime.fromisoformat(expiry_text)
        if expiry.tzinfo is None:
            return False
        return expiry.astimezone(timezone.utc) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


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
            visit(
                body,
                f"{node_id}::",
                ancestors | {state.loop.body},
            )

    visit(workflow.program, "", frozenset())
    return matches


def _identity_sources(step) -> list[dict[str, Any]]:
    """Project only identity evidence the canonical Flow runtime can consume."""

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
                "match": "Exact after case and whitespace normalization",
            }
        )
    if anchor.identifier_crop and anchor.identifier_region:
        sources.append(
            {
                "kind": "identifier_region",
                "label": "Captured identifier region",
                "match": "Conservative pixel comparison before actuation",
                "region": list(anchor.identifier_region),
            }
        )
    if anchor.context_text or (template is not None and template.tokens):
        sources.append(
            {
                "kind": "captured_context",
                "label": "Captured row identity",
                "match": "Conservative OCR identity matching",
            }
        )
    return sources


def _expr_view(expr) -> dict[str, Any] | None:
    if expr is None:
        return None
    if expr.param is not None:
        return {"source": "parameter", "value": expr.param}
    return {"source": "literal", "value": expr.literal}


def _effect_view(index: int, effect) -> dict[str, Any]:
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
    }


def _qualification_controls(workflow, graph: dict[str, Any]) -> dict[str, Any]:
    """Return editable controls projected from the live Flow workflow."""

    parameter_names = sorted(
        set(workflow.params) | set(workflow.param_specs) | set(workflow.secret_params)
    )
    parameters: list[dict[str, Any]] = []
    for name in parameter_names:
        spec = workflow.param_specs.get(name)
        parameters.append(
            {
                "name": name,
                "type": spec.type.value if spec is not None else "string",
                "secret": name in workflow.secret_params,
            }
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
        actions[node["id"]] = {
            "identity": {
                "can_arm": bool(sources),
                "armed": bool(step.identity_armed),
                "sources": sources,
            },
            "effects": [_effect_view(index, effect) for index, effect in enumerate(step.effects)],
        }
    return {"parameters": parameters, "actions": actions}


def _save(workflow, bundle_dir: Path) -> None:
    """Reseal the exact bundle, preserving its existing at-rest mode."""

    try:
        workflow.save(bundle_dir, encrypt=bool(workflow.encrypted))
        # A successful round trip proves that the new digest and asset seal are
        # internally consistent before the UI reports the change as complete.
        type(workflow).load(bundle_dir)
    except Exception as exc:
        raise QualificationError(f"Could not reseal the qualified bundle: {exc}") from exc


def inspect_bundle(
    bundle_dir: Path,
    *,
    workflow_id: str,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Return the canonical graph and structured qualification findings."""

    (
        _Workflow,
        evaluate_policy,
        lint_workflow,
        _load_policy,
        _iter_workflow_steps,
        build_program_graph,
    ) = _flow_api()
    workflow = _load(bundle_dir)
    policy = _policy(policy_source)
    graph = build_program_graph(workflow)
    graph_payload = graph.model_dump(mode="json")
    lint = lint_workflow(workflow)
    certification = evaluate_policy(workflow, policy)
    provenance = workflow.manifest.provenance if workflow.manifest else None
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "policy": policy.name,
        "certification_current": _certification_is_current(
            provenance,
            policy_name=policy.name,
            policy_passed=certification.passed,
        ),
        "graph": graph_payload,
        "controls": _qualification_controls(workflow, graph_payload),
        "lint": lint.model_dump(mode="json"),
        "certification": certification.model_dump(mode="json"),
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


def set_action_risk(
    bundle_dir: Path,
    *,
    workflow_id: str,
    step_id: str,
    risk: QualificationRisk,
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Correct one action's risk and invalidate any prior certification."""

    if risk not in ("reversible", "irreversible"):
        raise QualificationError("risk must be reversible or irreversible")
    if not step_id:
        raise QualificationError("step_id is required")

    _Workflow, _evaluate, _lint, _load_policy, _iter_workflow_steps, _graph = _flow_api()
    workflow = _load(bundle_dir)
    matches = _actions_for_graph_ref(workflow, step_id)
    if len(matches) != 1:
        detail = "not found" if not matches else f"ambiguous ({len(matches)} matches)"
        raise QualificationError(
            f"Action {step_id!r} is {detail}; no qualification change was written."
        )
    step = matches[0]
    if step.risk != risk or any(effect.risk != risk for effect in step.effects):
        step.risk = risk
        # Flow's effect runtime consults Effect.risk when deciding whether a
        # refuted write enters governed reconciliation. Keep the action and
        # every bound effect on the same operator-reviewed classification.
        for effect in step.effects:
            effect.risk = risk
        _reset_certification(workflow)
        _save(workflow, bundle_dir)
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
    """Arm Flow's existing identity ladder from evidence retained at compile time."""

    if not step_id:
        raise QualificationError("step_id is required")
    _Workflow, _evaluate, _lint, _load_policy, _iter, _graph = _flow_api()
    workflow = _load(bundle_dir)
    matches = _actions_for_graph_ref(workflow, step_id)
    if len(matches) != 1:
        detail = "not found" if not matches else f"ambiguous ({len(matches)} matches)"
        raise QualificationError(
            f"Action {step_id!r} is {detail}; no qualification change was written."
        )
    step = matches[0]
    sources = _identity_sources(step)
    if not sources:
        raise QualificationError(
            "This action has no retained structured identity, identifier region, "
            "or captured row identity. Record or teach the action with identity "
            "evidence before arming it."
        )
    if step.identity_armed is not True:
        step.identity_armed = True
        step.identity_unarmed_reason = None
        _reset_certification(workflow)
        _save(workflow, bundle_dir)
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
    policy_source: str = DEFAULT_QUALIFICATION_POLICY,
) -> dict:
    """Bind one canonical Flow ``Effect`` using workflow parameter references."""

    if not step_id:
        raise QualificationError("step_id is required")
    if kind not in ("record_written", "field_equals"):
        raise QualificationError("kind must be record_written or field_equals")
    if not match_field.strip():
        raise QualificationError("match_field is required")
    if expected_count < 0:
        raise QualificationError("expected_count cannot be negative")
    if not key_field.strip():
        raise QualificationError("key_field is required")

    _Workflow, _evaluate, _lint, _load_policy, _iter, _graph = _flow_api()
    Effect, EffectKind, ValueExpr = _effect_api()
    workflow = _load(bundle_dir)
    matches = _actions_for_graph_ref(workflow, step_id)
    if len(matches) != 1:
        detail = "not found" if not matches else f"ambiguous ({len(matches)} matches)"
        raise QualificationError(
            f"Action {step_id!r} is {detail}; no qualification change was written."
        )
    step = matches[0]
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
        if effect not in step.effects:
            step.effects.append(effect)
        else:
            return inspect_bundle(
                bundle_dir,
                workflow_id=workflow_id,
                policy_source=policy_source,
            )
    else:
        if effect_index < 0 or effect_index >= len(step.effects):
            raise QualificationError(
                f"Effect index {effect_index} is outside this action's effect inventory"
            )
        if step.effects[effect_index] == effect:
            return inspect_bundle(
                bundle_dir,
                workflow_id=workflow_id,
                policy_source=policy_source,
            )
        step.effects[effect_index] = effect

    _reset_certification(workflow)
    _save(workflow, bundle_dir)
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
    """Evaluate, persist, and reseal one exact policy-certification attempt."""

    _Workflow, evaluate_policy, _lint, _load_policy, _iter, _graph = _flow_api()
    workflow = _load(bundle_dir)
    policy = _policy(policy_source)
    report = evaluate_policy(workflow, policy)
    workflow.stamp_certification(
        policy_name=report.policy_name,
        passed=report.passed,
        status="certified" if report.passed else "failed",
    )
    _save(workflow, bundle_dir)
    result = inspect_bundle(
        bundle_dir,
        workflow_id=workflow_id,
        policy_source=policy_source,
    )
    result["certification_attempt"] = report.model_dump(mode="json")
    return result
