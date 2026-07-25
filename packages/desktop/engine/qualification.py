"""Desktop adapter for OpenAdapt Flow's qualification mechanisms.

The canonical workflow schema, traversal, graph projection, linter, policy
evaluator, manifest sealing, and encryption remain owned by ``openadapt-flow``.
This module gives the installed Desktop cockpit a small, writable API over
those mechanisms so an operator does not have to edit ``workflow.json``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

QualificationRisk = Literal["reversible", "irreversible"]
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
        "graph": graph.model_dump(mode="json"),
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
    if matches[0].risk != risk:
        matches[0].risk = risk
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
