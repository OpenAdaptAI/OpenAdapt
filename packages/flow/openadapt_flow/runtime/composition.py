"""Fail-closed sequencer for a composition of compiled Flow bundles.

The parent is not a workflow-program interpreter. It runs already-compiled
children in topological order, checks each predecessor's terminal outcome,
and copies only verified handoff facts into the next child's inputs.

``execute`` is the child-run extension point. Its signature matches the
intended public primitive ``execute(capability, admission, inputs, ...)``.
Today callers bind ``child_run`` to the governed ``openadapt-flow run`` path
(admission gate plus Replayer). Raw ``replay`` is not the long-term
primitive; when a public execute() lands, rebind this function rather than
teaching compose to call replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from openadapt_flow.compiler.compose_authoring import effect_bound_param_names
from openadapt_flow.composition import (
    Composition,
    child_bundle_path,
    predecessor_map,
    topological_order,
)
from openadapt_flow.ir import Workflow

ALWAYS_ALLOWED = frozenset({"VERIFIED"})


@dataclass
class ChildRunResult:
    """Live result of one child execution, kept inside the parent run."""

    child: str
    outcome: str
    bound_params: dict[str, str]
    effect_facts: dict[str, str]
    model_calls: int = 0
    halt_class: Optional[str] = None
    success: bool = False
    report_path: Optional[str] = None


class ChildExecutor(Protocol):
    """Bound child runner. CLI supplies the governed run path."""

    def __call__(
        self,
        capability: object | None,
        admission: object | None,
        inputs: Mapping[str, str],
        *,
        workflow: Workflow,
        bundle_dir: Path,
        run_dir: Path,
        child: str,
    ) -> ChildRunResult: ...


@dataclass
class CompositionReport:
    """Parent sequencer result. Not a Workflow RunReport."""

    name: str
    outcome: str
    children: list[ChildRunResult] = field(default_factory=list)
    handoffs: dict[str, str] = field(default_factory=dict)
    halted_at: Optional[str] = None
    reason: str = ""
    model_calls: int = 0

    @property
    def success(self) -> bool:
        return self.outcome == "VERIFIED"


def execute(
    capability: object | None,
    admission: object | None,
    inputs: Mapping[str, str],
    *,
    workflow: Workflow,
    bundle_dir: Path,
    run_dir: Path,
    child: str,
    child_run: ChildExecutor,
) -> ChildRunResult:
    """Run one admitted child.

    This is the extension point compose sits on. ``capability`` and
    ``admission`` are passed through unchanged so a later
    ``execute(capability, admission, inputs, ...)`` primitive can bind here
    without changing the sequencer.
    """

    return child_run(
        capability,
        admission,
        inputs,
        workflow=workflow,
        bundle_dir=bundle_dir,
        run_dir=run_dir,
        child=child,
    )


def _allowed_outcomes(composition: Composition, child_name: str) -> set[str]:
    child = composition.child(child_name)
    return set(ALWAYS_ALLOWED) | set(child.allowed_halt_classes)


def _handoffs_into(composition: Composition, child_name: str) -> list[Any]:
    return [item for item in composition.handoffs if item.to_child == child_name]


def execute_composition(
    composition: Composition,
    *,
    parent_dir: Path | str,
    run_dir: Path | str,
    inputs: Optional[Mapping[str, str]] = None,
    child_run: ChildExecutor,
    capability: object | None = None,
    admission: object | None = None,
) -> CompositionReport:
    """Sequence children fail-closed. Missing handoff evidence HALTs."""

    parent = Path(parent_dir)
    root_run = Path(run_dir)
    root_run.mkdir(parents=True, exist_ok=True)
    parent_inputs = dict(inputs or {})
    order = topological_order(composition)
    preds = predecessor_map(composition)
    results: list[ChildRunResult] = []
    by_name: dict[str, ChildRunResult] = {}
    applied: dict[str, str] = {}
    total_model = 0

    for child_name in order:
        spec = composition.child(child_name)
        bundle = child_bundle_path(parent, spec)
        try:
            workflow = Workflow.load(bundle)
        except Exception as exc:
            report = CompositionReport(
                name=composition.name,
                outcome="FAILED",
                children=results,
                handoffs=applied,
                halted_at=child_name,
                reason=(
                    f"child {child_name!r} bundle could not be loaded "
                    f"({type(exc).__name__})"
                ),
                model_calls=total_model,
            )
            _write_parent_report(root_run, report)
            return report

        for pred in preds[child_name]:
            pred_result = by_name.get(pred)
            if pred_result is None:
                report = CompositionReport(
                    name=composition.name,
                    outcome="HALTED",
                    children=results,
                    handoffs=applied,
                    halted_at=child_name,
                    reason=(
                        f"child {child_name!r} cannot start: predecessor "
                        f"{pred!r} never ran"
                    ),
                    model_calls=total_model,
                )
                _write_parent_report(root_run, report)
                return report
            allowed = _allowed_outcomes(composition, child_name)
            if pred_result.outcome not in allowed:
                report = CompositionReport(
                    name=composition.name,
                    outcome="HALTED",
                    children=results,
                    handoffs=applied,
                    halted_at=child_name,
                    reason=(
                        f"child {child_name!r} cannot start: predecessor "
                        f"{pred!r} ended {pred_result.outcome}; allowed "
                        f"{sorted(allowed)}"
                    ),
                    model_calls=total_model,
                )
                _write_parent_report(root_run, report)
                return report

        child_inputs = dict(parent_inputs)
        for handoff in _handoffs_into(composition, child_name):
            pred_result = by_name.get(handoff.from_child)
            if pred_result is None:
                report = CompositionReport(
                    name=composition.name,
                    outcome="HALTED",
                    children=results,
                    handoffs=applied,
                    halted_at=child_name,
                    reason=(
                        f"missing handoff evidence {handoff.from_child}."
                        f"{handoff.source} -> {child_name}.{handoff.target}; "
                        f"predecessor {handoff.from_child!r} has not run"
                    ),
                    model_calls=total_model,
                )
                _write_parent_report(root_run, report)
                return report
            fact = pred_result.effect_facts.get(handoff.source)
            if fact is None or fact == "":
                report = CompositionReport(
                    name=composition.name,
                    outcome="HALTED",
                    children=results,
                    handoffs=applied,
                    halted_at=child_name,
                    reason=(
                        f"missing handoff evidence {handoff.from_child}."
                        f"{handoff.source} -> {child_name}.{handoff.target}; "
                        "the predecessor did not confirm that effect-bound "
                        "parameter"
                    ),
                    model_calls=total_model,
                )
                _write_parent_report(root_run, report)
                return report
            child_inputs[handoff.target] = fact
            applied_key = (
                f"{handoff.from_child}.{handoff.source}->{child_name}.{handoff.target}"
            )
            applied[applied_key] = fact

        child_run_dir = root_run / "children" / child_name
        child_run_dir.mkdir(parents=True, exist_ok=True)
        result = execute(
            capability,
            admission,
            child_inputs,
            workflow=workflow,
            bundle_dir=bundle,
            run_dir=child_run_dir,
            child=child_name,
            child_run=child_run,
        )
        # Re-derive effect facts fail-closed: only params that the child's
        # declared effects actually bind, present in the live bound_params.
        bound_names = effect_bound_param_names(workflow)
        verified_facts = {
            key: value
            for key, value in {**result.bound_params, **result.effect_facts}.items()
            if key in bound_names and value != ""
        }
        if result.outcome == "VERIFIED":
            result.effect_facts = verified_facts
        else:
            # Non-verified children must not mint handoff facts.
            result.effect_facts = {}
        results.append(result)
        by_name[child_name] = result
        total_model += result.model_calls
        if result.model_calls:
            # A healthy child still makes no model calls. A composition that
            # needed a model on any child is not a $0 parent.
            pass

    last = results[-1] if results else None
    if last is None:
        outcome = "FAILED"
        reason = "composition has no runnable children"
    elif all(item.outcome == "VERIFIED" for item in results) and total_model == 0:
        outcome = "VERIFIED"
        reason = ""
    elif all(item.success or item.outcome == "VERIFIED" for item in results):
        outcome = "COMPLETED_UNVERIFIED"
        reason = "every child finished but the parent is not VERIFIED"
    else:
        outcome = (
            last.outcome
            if last.outcome
            in {
                "HALTED",
                "FAILED",
                "ROLLED_BACK",
                "COMPLETED_UNVERIFIED",
            }
            else "HALTED"
        )
        reason = f"last child {last.child} ended {last.outcome}"

    report = CompositionReport(
        name=composition.name,
        outcome=outcome,
        children=results,
        handoffs=applied,
        reason=reason,
        model_calls=total_model,
    )
    _write_parent_report(root_run, report)
    return report


def _write_parent_report(run_dir: Path, report: CompositionReport) -> None:
    import json

    payload = {
        "name": report.name,
        "outcome": report.outcome,
        "success": report.success,
        "halted_at": report.halted_at,
        "reason": report.reason,
        "model_calls": report.model_calls,
        "handoffs": {key: "<bound>" for key in report.handoffs},
        "children": [
            {
                "child": item.child,
                "outcome": item.outcome,
                "success": item.success,
                "model_calls": item.model_calls,
                "halt_class": item.halt_class,
                "report_path": item.report_path,
                "effect_fact_names": sorted(item.effect_facts),
            }
            for item in report.children
        ],
    }
    (run_dir / "composition-report.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
