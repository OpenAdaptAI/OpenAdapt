"""Project a composition parent onto :class:`ProgramGraphSpec`.

A composition is a sequencer of compiled child bundles. This module does NOT
lift the parent to a ProgramGraph, does NOT merge children into one Workflow,
and does NOT expand a child into its action steps. Each child is one
``child_bundle`` node. Sequence edges follow the ``--after`` DAG (or declared
child order). Handoff edges are labelled with effect-bound parameter names,
never window titles or URLs.
"""

from __future__ import annotations

from pathlib import Path

from openadapt_flow.composition import (
    COMPOSITION_SCHEMA,
    Composition,
    CompositionError,
    HandoffBinding,
    child_bundle_path,
    predecessor_map,
    topological_order,
)
from openadapt_flow.visualize.spec import (
    DECLARED_STEPS_END_TITLE,
    BundleMeta,
    EdgeKind,
    GraphEdge,
    GraphNode,
    NodeKind,
    ProgramGraphSpec,
    ProvenanceInfo,
)

_END_ID = "__end__"


def _handoff_label(handoff: HandoffBinding) -> str:
    """Label a handoff with the effect-bound param names it copies."""

    if handoff.source == handoff.target:
        return handoff.source
    return f"{handoff.source} -> {handoff.target}"


def build_composition_graph(path: Path | str) -> ProgramGraphSpec:
    """Emit a :class:`ProgramGraphSpec` for a composed parent directory.

    The parent stays a parent. Children stay children. The spec is the same
    wire contract a single bundle uses, with ``child_bundle`` / ``handoff``
    kinds so a renderer does not have to pretend this is a linear Workflow.
    """

    parent = Path(path)
    composition = Composition.load(parent)
    order = topological_order(composition)
    preds = predecessor_map(composition)

    nodes: list[GraphNode] = []
    for index, name in enumerate(order):
        child = composition.child(name)
        # Surface comes from the copied ChildSpec (stamped at compose time).
        # Do not open the child bundle to invent a window title.
        surface = child.surface
        badges = ["child bundle"]
        if surface:
            badges.append(surface)
        nodes.append(
            GraphNode(
                id=name,
                index=index,
                kind=NodeKind.CHILD_BUNDLE,
                title=name,
                surface=surface,
                badges=badges,
            )
        )

    edges: list[GraphEdge] = []
    successors: dict[str, list[str]] = {name: [] for name in order}
    for name in order:
        for pred in preds[name]:
            edges.append(GraphEdge(source=pred, target=name, kind=EdgeKind.SEQUENCE))
            successors[pred].append(name)

    for name in order:
        if not successors[name]:
            edges.append(GraphEdge(source=name, target=_END_ID, kind=EdgeKind.SEQUENCE))

    for handoff in composition.handoffs:
        edges.append(
            GraphEdge(
                source=handoff.from_child,
                target=handoff.to_child,
                kind=EdgeKind.HANDOFF,
                label=_handoff_label(handoff),
            )
        )

    nodes.append(
        GraphNode(
            id=_END_ID,
            index=len(order),
            kind=NodeKind.TERMINAL,
            title=DECLARED_STEPS_END_TITLE,
            outcome="success",
        )
    )

    # Refuse a dangling ChildSpec.bundle without loading the child Workflow
    # (loading it would be the start of merging two recordings).
    for child in composition.children:
        resolved = child_bundle_path(parent, child)
        if not resolved.is_dir():
            raise CompositionError(
                f"child {child.name!r} bundle path {child.bundle!r} is missing"
            )

    meta = BundleMeta(
        name=composition.name,
        schema_version=1,
        is_program=False,
        is_composition=True,
        composition_schema=COMPOSITION_SCHEMA,
        step_count=len(order),
        child_count=len(order),
        action_count=0,
        provenance=ProvenanceInfo(
            certified=composition.provenance.certified,
            policy_name=composition.provenance.policy_name,
        ),
    )
    return ProgramGraphSpec(bundle=meta, nodes=nodes, edges=edges)
