"""Author a parent composition from named, already-compiled child bundles.

This is the compile-time wire for ``openadapt-flow compose``. It copies each
child bundle into the parent artifact, records the handoff contract, and
validates that every handoff source is an effect-bound parameter on the
predecessor and every target is a real parameter on the successor.

It invents no ProgramGraph, no subflow, and no process contract. Child
bundles remain the executable programs; the parent is only the sequencer
spec plus the copied children.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping, Optional, Sequence

from openadapt_flow.composition import (
    CHILDREN_DIR,
    TERMINAL_OUTCOMES,
    ChildSpec,
    Composition,
    CompositionError,
    HandoffBinding,
    topological_order,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime.effects.effect import ValueExpr
from openadapt_flow.traversal import iter_workflow_steps


def workflow_param_names(workflow: Workflow) -> set[str]:
    """Parameter names a child can bind: specs, demo params, and Step.param."""

    names = set(workflow.param_specs) | set(workflow.params)
    secret = set(workflow.secret_params or [])
    for step in iter_workflow_steps(workflow):
        if step.param and not step.secret:
            names.add(step.param)
    return names - secret


def _expr_param(expr: object) -> Optional[str]:
    if isinstance(expr, ValueExpr):
        return expr.param
    return None


def effect_bound_param_names(workflow: Workflow) -> set[str]:
    """Parameters referenced by a declared system-of-record effect.

    Those are the only facts a handoff may copy. A window title, URL, or
    unbound input is not evidence.
    """

    names: set[str] = set()
    for step in iter_workflow_steps(workflow):
        for effect in step.effects or []:
            for expr in effect.match.values():
                param = _expr_param(expr)
                if param:
                    names.add(param)
            param = _expr_param(effect.value)
            if param:
                names.add(param)
            param = _expr_param(effect.idempotency_key)
            if param:
                names.add(param)
            for record in effect.new_records or []:
                for expr in record.values():
                    param = _expr_param(expr)
                    if param:
                        names.add(param)
    return names


def _copy_child_bundle(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", ".venv", "*.pyc"),
    )


def author_composition(
    children: Sequence[tuple[str, Path]],
    *,
    handoffs: Sequence[HandoffBinding] = (),
    after: Optional[Mapping[str, Sequence[str]]] = None,
    allowed_halt_classes: Optional[Mapping[str, Sequence[str]]] = None,
    name: Optional[str] = None,
    out: Path | str,
) -> Composition:
    """Copy named child bundles into ``out`` and write composition.json.

    ``children`` is an ordered list of ``(name, bundle_dir)``. Default
    sequencing is that order. ``after`` overrides predecessors per child.
    """

    if len(children) < 2:
        raise CompositionError("compose needs at least two named child bundles")
    names = [item[0] for item in children]
    if len(names) != len(set(names)):
        raise CompositionError("compose child names must be unique")

    out_dir = Path(out)
    if out_dir.exists() and any(out_dir.iterdir()):
        # Allow an empty existing directory; refuse a dirty one so we never
        # mix two compositions.
        raise CompositionError(
            f"compose output {out_dir} already exists and is not empty"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    loaded: dict[str, Workflow] = {}
    specs: list[ChildSpec] = []
    errors: list[str] = []
    after_map = {key: list(value) for key, value in (after or {}).items()}
    halt_map = {key: list(value) for key, value in (allowed_halt_classes or {}).items()}

    for child_name, src in children:
        src_path = Path(src)
        if (
            not (src_path / "workflow.json").is_file()
            and not (src_path / "workflow.json.enc").is_file()
        ):
            errors.append(
                f"child {child_name!r} at {src_path} is not a workflow bundle"
            )
            continue
        try:
            workflow = Workflow.load(src_path)
        except Exception as exc:  # integrity / decrypt / structure
            errors.append(
                f"child {child_name!r} could not be loaded: {type(exc).__name__}"
            )
            continue
        loaded[child_name] = workflow
        rel = f"{CHILDREN_DIR}/{child_name}"
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy_child_bundle(src_path, dest)
        halt_classes = halt_map.get(child_name, [])
        unknown_halts = [item for item in halt_classes if item not in TERMINAL_OUTCOMES]
        if unknown_halts:
            errors.append(
                f"child {child_name!r} allowed halt classes {unknown_halts} "
                "are not terminal outcomes"
            )
        valid_halts = [item for item in halt_classes if item in TERMINAL_OUTCOMES]
        try:
            specs.append(
                ChildSpec(
                    name=child_name,
                    bundle=rel,
                    surface=workflow.surface,
                    after=list(after_map.get(child_name, [])),
                    allowed_halt_classes=valid_halts,  # type: ignore[arg-type]
                )
            )
        except Exception as exc:
            errors.append(f"child {child_name!r} spec is invalid: {exc}")

    for extra in set(after_map) | set(halt_map):
        if extra not in names:
            errors.append(f"after/allow-halt refers to unknown child {extra!r}")

    for handoff in handoffs:
        src_wf = loaded.get(handoff.from_child)
        dst_wf = loaded.get(handoff.to_child)
        if src_wf is None or dst_wf is None:
            continue
        bound = effect_bound_param_names(src_wf)
        if handoff.source not in bound:
            errors.append(
                f"handoff {handoff.from_child}.{handoff.source} is not a "
                f"parameter bound by a declared effect on {handoff.from_child} "
                f"(effect-bound params: {sorted(bound) or 'none'})"
            )
        dest_params = workflow_param_names(dst_wf)
        if handoff.target not in dest_params:
            errors.append(
                f"handoff target {handoff.to_child}.{handoff.target} is not a "
                f"parameter of {handoff.to_child} "
                f"(known: {sorted(dest_params) or 'none'})"
            )
        if handoff.target in set(dst_wf.secret_params or []):
            errors.append(
                f"handoff target {handoff.to_child}.{handoff.target} is a "
                "SECRET parameter; secrets are never copied between children"
            )

    if errors:
        shutil.rmtree(out_dir, ignore_errors=True)
        joined = "\n - ".join(errors)
        raise CompositionError(
            "cannot author composition -- contract mismatch:\n - " + joined
        )

    composition = Composition(
        name=name or "composed",
        children=specs,
        handoffs=list(handoffs),
    )
    # Fail closed on a cyclic after-graph before writing the artifact.
    try:
        order = topological_order(composition)
    except CompositionError:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    index = {child_name: i for i, child_name in enumerate(order)}
    for handoff in composition.handoffs:
        if index[handoff.from_child] >= index[handoff.to_child]:
            errors.append(
                f"handoff {handoff.from_child}.{handoff.source} -> "
                f"{handoff.to_child}.{handoff.target} runs backwards: "
                f"{handoff.from_child!r} is not before {handoff.to_child!r} "
                "in composition order"
            )
    if errors:
        shutil.rmtree(out_dir, ignore_errors=True)
        joined = "\n - ".join(errors)
        raise CompositionError(
            "cannot author composition -- contract mismatch:\n - " + joined
        )
    composition.save(out_dir)
    return composition
