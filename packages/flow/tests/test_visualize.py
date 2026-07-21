"""Tests for the compiled-program visualizer.

Covers the three responsibilities of :mod:`openadapt_flow.visualize`:

1. ``build_program_graph`` projects a compiled bundle onto the shared spec,
   surfacing the load-bearing structure (resolution ladder, identity gate,
   effect check, verification, risk, control guards, HALT points) honestly.
2. The renderers emit a self-contained HTML page and a valid Mermaid source.
3. The emitted spec validates against the committed JSON Schema, and the CLI
   ``visualize`` subcommand wires it all together.
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Guard,
    Landmark,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.visualize import (
    SPEC_VERSION,
    ProgramGraphSpec,
    build_program_graph,
    render_html,
    render_mermaid,
)

_REPO = Path(__file__).resolve().parent.parent
_SHOWCASE = _REPO / "docs" / "showcase-openemr" / "bundle"


def _anchor(**over) -> Anchor:
    base = dict(
        template="templates/s.png",
        region=(0, 0, 10, 10),
        click_point=(5, 5),
        ocr_text="Save",
        landmarks=[Landmark(relation="left_of", ocr_text="Name", distance_px=10)],
    )
    base.update(over)
    return Anchor(**base)


def _mixed_workflow() -> Workflow:
    """A small workflow exercising every annotation the builder projects."""
    return Workflow(
        name="unit-mixed",
        params={"note": "hello"},
        steps=[
            # reversible click, identity armed, structural rung present
            Step(
                id="s0",
                intent="click patient row",
                action=ActionKind.CLICK,
                anchor=_anchor(structural=StructuralLocator(selector="#row-1")),
                identity_armed=True,
                expect=[Postcondition(kind=PostconditionKind.REGION_STABLE)],
            ),
            # irreversible click, identity UNARMED -> a halt point
            Step(
                id="s1",
                intent="click Save",
                action=ActionKind.CLICK,
                anchor=_anchor(),
                risk="irreversible",
                identity_armed=False,
                identity_unarmed_reason="row text too generic",
            ),
            # effect-bearing step -> effect check + halt
            Step(
                id="s2",
                intent="confirm write",
                action=ActionKind.CLICK,
                anchor=_anchor(),
                effects=[
                    Effect(kind=EffectKind.RECORD_WRITTEN, match={"patient_id": "p1"})
                ],
            ),
            # guarded (skippable) type step from a param
            Step(
                id="s3",
                intent="type note",
                action=ActionKind.TYPE,
                param="note",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT, text="Note field"
                    ),
                    on_unmet="skip",
                ),
            ),
        ],
    )


def test_spec_shape_and_counts() -> None:
    spec = build_program_graph(_mixed_workflow())
    assert spec.spec_version == SPEC_VERSION
    # 4 action nodes + 1 terminal
    assert len(spec.nodes) == 5
    assert spec.nodes[-1].kind.value == "terminal"
    assert spec.nodes[-1].outcome == "success"
    b = spec.bundle
    assert b.action_count == 4
    assert b.irreversible_count == 1
    assert b.identity_armed_count == 1
    assert b.identity_unarmed_count == 1
    assert b.effect_count == 1
    assert [p.name for p in b.params] == ["note"]


def test_resolution_ladder_top_rung() -> None:
    spec = build_program_graph(_mixed_workflow())
    s0 = spec.nodes[0]
    assert s0.resolution is not None
    # structural present -> it is the strongest (top) rung; api absent
    assert s0.resolution.top_rung == "structural"
    rung_names = {r.name: r.present for r in s0.resolution.rungs}
    assert rung_names["structural"] is True
    assert rung_names["template"] is True
    assert rung_names["ocr"] is True
    assert rung_names["landmarks"] is True
    assert rung_names["api"] is False
    # s1 has no structural rung -> template is top
    assert spec.nodes[1].resolution.top_rung == "template"


def test_identity_gate_projection() -> None:
    spec = build_program_graph(_mixed_workflow())
    armed = spec.nodes[0].identity
    assert armed is not None and armed.applicable and armed.armed is True
    unarmed = spec.nodes[1].identity
    assert unarmed is not None and unarmed.armed is False
    assert unarmed.reason == "row text too generic"


def test_halt_points_surfaced() -> None:
    spec = build_program_graph(_mixed_workflow())
    # irreversible + unarmed identity is a distinguished halt point
    assert any("identity gate" in h for h in spec.nodes[1].halts)
    # effect-bearing step halts if the effect is not confirmed
    assert any("effect" in h for h in spec.nodes[2].halts)
    assert spec.bundle.halt_point_count >= 2


def test_guard_and_param_projection() -> None:
    spec = build_program_graph(_mixed_workflow())
    s3 = spec.nodes[3]
    assert s3.param == "note"
    assert s3.guard is not None
    assert s3.guard_on_unmet == "skip"
    assert "optional (skippable)" in s3.badges


def test_spec_is_json_serializable_and_roundtrips() -> None:
    spec = build_program_graph(_mixed_workflow())
    payload = spec.model_dump_json()
    data = json.loads(payload)
    assert data["spec_version"] == SPEC_VERSION
    # re-parse through the model (the wire contract other surfaces rely on)
    again = ProgramGraphSpec.model_validate_json(payload)
    assert len(again.nodes) == len(spec.nodes)


def test_render_html_is_self_contained() -> None:
    spec = build_program_graph(_mixed_workflow())
    doc = render_html(spec)
    assert doc.lstrip().startswith("<!doctype html>")
    # no external network references (self-contained / CSP-safe)
    for needle in ("http://", "https://", "src=", "cdn"):
        assert needle not in doc, f"unexpected external reference: {needle}"
    # the shared renderer + spec are inlined
    assert "OpenAdaptProgramGraph.render" in doc
    assert "program-graph-spec" in doc
    assert "click Save" in doc


def test_render_mermaid_is_valid_flowchart() -> None:
    spec = build_program_graph(_mixed_workflow())
    src = render_mermaid(spec)
    lines = src.splitlines()
    assert lines[0] == "flowchart TD"
    # one node line per graph node, plus edges + classDefs
    assert sum(1 for ln in lines if ln.strip().startswith("n")) >= len(spec.nodes)
    assert "-->" in src
    assert "classDef irreversible" in src


def test_showcase_bundle_projects() -> None:
    """The committed flagship bundle projects to a linear program with the
    expected safety structure (regression against the real artifact)."""
    if not _SHOWCASE.exists():  # pragma: no cover - defensive
        return
    spec = build_program_graph(Workflow.load(_SHOWCASE))
    assert spec.bundle.is_program is False
    assert spec.bundle.action_count == 18
    assert spec.bundle.irreversible_count >= 1
    assert spec.bundle.identity_armed_count >= 1
    # at least one irreversible step compiled without an identity gate -> halt
    assert spec.bundle.halt_point_count >= 1


def test_emitted_spec_validates_against_committed_schema() -> None:
    """The shared JSON Schema stays in sync with the pydantic model, so
    non-Python surfaces can validate the same shape."""
    schema_path = _REPO / "schemas" / "program-graph-v1.json"
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text())
    # The committed schema must match what the model currently produces
    # (regenerate schemas/program-graph-v1.json if this fails).
    current = ProgramGraphSpec.model_json_schema()
    for key in ("properties", "$defs"):
        assert schema.get(key, {}).keys() == current.get(key, {}).keys()

    jsonschema = __import__("importlib").import_module("jsonschema")  # optional dep
    spec = build_program_graph(_mixed_workflow())
    jsonschema.validate(json.loads(spec.model_dump_json()), schema)


def _authored_loop_workflow() -> Workflow:
    """A real ``program:true`` loop authored the same way the ``for-each`` CLI
    authors one: a two-step demonstrated body wrapped in a ``LOOP`` over a
    two-record worklist (the shape ``docs/showcase-encounter-loop`` ships)."""
    from openadapt_flow.compiler.loop_authoring import author_data_driven_loop
    from openadapt_flow.ir import ParamSpec

    body = Workflow(
        name="note-body",
        steps=[
            Step(
                id="type_patient",
                intent="type <patient_id>",
                action=ActionKind.TYPE,
                param="patient_id",
            ),
            Step(
                id="type_note",
                intent="type <note>",
                action=ActionKind.TYPE,
                param="note",
            ),
        ],
        param_specs={
            "patient_id": ParamSpec(name="patient_id", example="p1"),
            "note": ParamSpec(name="note", example="n1"),
        },
    )
    return author_data_driven_loop(
        body,
        [{"patient_id": "a", "note": "x"}, {"patient_id": "b", "note": "y"}],
        loop_var="encounter",
    )


def test_loop_body_is_expanded_inline() -> None:
    """A ``loop`` state's per-row body subflow projects as its own expanded
    action nodes, linked by a ``loop_body`` edge, with the body's return drawn
    as a ``next record`` loop-back edge and the loop's own exit as a
    ``worklist exhausted`` edge to a single ``success`` terminal -- the real
    cyclic structure the interpreter walks, not a dangling subflow reference."""
    spec = build_program_graph(_authored_loop_workflow())
    assert spec.bundle.is_program is True

    loop_nodes = [n for n in spec.nodes if n.kind.value == "loop"]
    assert len(loop_nodes) == 1
    loop = loop_nodes[0]

    # The demonstrated body's action steps render inline (with their real
    # annotations), so the body is counted, not hidden behind a subflow id.
    titles = [n.title for n in spec.nodes]
    assert "type <patient_id>" in titles
    assert "type <note>" in titles
    assert spec.bundle.action_count == 2

    # loop_body edge leaves the loop node into the (namespaced) body entry.
    body_edges = [
        e for e in spec.edges if e.kind.value == "loop_body" and e.source == loop.id
    ]
    assert len(body_edges) == 1
    assert any(n.id == body_edges[0].target for n in spec.nodes)

    # the body returns to the loop node once per row (loop-back edge).
    assert any(e.target == loop.id and e.label == "next record" for e in spec.edges)

    # the loop exits to exactly ONE success terminal when the worklist empties.
    exhausted = [e for e in spec.edges if e.label == "worklist exhausted"]
    assert len(exhausted) == 1
    terminals = [n for n in spec.nodes if n.kind.value == "terminal"]
    assert len(terminals) == 1  # the body's success return is an edge, not a node
    assert terminals[0].id == exhausted[0].target
    assert terminals[0].outcome == "success"


def test_render_mermaid_shows_loop_structure() -> None:
    src = render_mermaid(build_program_graph(_authored_loop_workflow()))
    assert src.splitlines()[0] == "flowchart TD"
    assert "per row of worklist" in src
    assert "next record" in src
    assert "worklist exhausted" in src


def test_cli_visualize_writes_outputs(tmp_path) -> None:
    from openadapt_flow.__main__ import main

    if not _SHOWCASE.exists():  # pragma: no cover
        return
    out_html = tmp_path / "graph.html"
    rc = main(["visualize", str(_SHOWCASE), "--out", str(out_html)])
    assert rc == 0
    assert out_html.exists() and out_html.read_text().startswith("<!doctype html>")

    out_json = tmp_path / "graph.json"
    rc = main(["visualize", str(_SHOWCASE), "--format", "json", "--out", str(out_json)])
    assert rc == 0
    data = json.loads(out_json.read_text())
    assert data["spec_version"] == SPEC_VERSION
    assert data["bundle"]["name"] == "openemr-showcase"
