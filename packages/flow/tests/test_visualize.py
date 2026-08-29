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
    DECLARED_STEPS_END_TITLE,
    SPEC_VERSION,
    GraphNode,
    PresentationProfile,
    ProgramGraphSpec,
    build_program_graph,
    project_program_graph,
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


def test_linear_terminal_title_is_never_success() -> None:
    """The declared-end terminal is not a live VERIFIED verdict.

    Docs already call this ``End of declared steps``. The spec title must
    match. If this assertion fails, the builder has gone back to ``Success``.
    """
    spec = build_program_graph(_mixed_workflow())
    terminal = spec.nodes[-1]
    assert terminal.kind.value == "terminal"
    assert terminal.title == DECLARED_STEPS_END_TITLE
    assert terminal.title != "Success"
    dumped = spec.model_dump_json()
    assert '"title":"Success"' not in dumped
    mermaid = render_mermaid(spec)
    assert DECLARED_STEPS_END_TITLE in mermaid
    assert "Success" not in mermaid


def test_spec_shape_and_counts() -> None:
    spec = build_program_graph(_mixed_workflow())
    assert spec.spec_version == SPEC_VERSION
    # 4 action nodes + 1 terminal
    assert len(spec.nodes) == 5
    assert spec.nodes[-1].kind.value == "terminal"
    assert spec.nodes[-1].outcome == "success"
    assert spec.nodes[-1].title == DECLARED_STEPS_END_TITLE
    assert spec.nodes[-1].title != "Success"
    assert "Success" not in [n.title for n in spec.nodes]
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


def test_coordinate_intent_is_rendered_as_evidence_resolved_target() -> None:
    workflow = Workflow(
        name="recorded-target",
        steps=[
            Step(
                id="s0",
                intent="click at (214, 195)",
                action=ActionKind.CLICK,
                anchor=_anchor(ocr_text=None),
            )
        ],
    )
    spec = build_program_graph(workflow)
    assert spec.nodes[0].title == "click recorded visual target"
    mermaid = render_mermaid(spec)
    assert "(214, 195)" not in mermaid
    assert "visual template + 1 OCR landmarks" in mermaid


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


def test_remote_safe_projection_keeps_topology_and_drops_recorded_values() -> None:
    source = build_program_graph(_mixed_workflow())
    projected = project_program_graph(source, PresentationProfile.REMOTE_SAFE)

    assert [(edge.source, edge.target, edge.kind) for edge in projected.edges] == [
        (edge.source, edge.target, edge.kind) for edge in source.edges
    ]
    assert [node.id for node in projected.nodes] == [node.id for node in source.nodes]
    assert projected.bundle.name == "Compiled program"
    assert projected.bundle.params[0].name == "input_1"
    assert projected.bundle.params[0].example is None
    assert projected.bundle.provenance.content_digest is None

    payload = projected.model_dump_json().lower()
    for private_value in (
        "unit-mixed",
        "patient row",
        "#row-1",
        "click save",
        "row text too generic",
        '"p1"',
        '"hello"',
    ):
        assert private_value not in payload


def test_operator_projection_is_an_independent_complete_copy() -> None:
    source = build_program_graph(_mixed_workflow())
    projected = project_program_graph(source, PresentationProfile.OPERATOR_LOCAL)
    assert projected == source
    assert projected is not source
    projected.nodes[0].title = "changed"
    assert source.nodes[0].title != "changed"


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
    assert "Compiled topology" in doc
    assert "Program evidence lanes" in doc
    assert "End of declared steps" in doc
    assert '"title":"Success"' not in doc.replace(" ", "")


def test_render_mermaid_is_valid_flowchart() -> None:
    spec = build_program_graph(_mixed_workflow())
    src = render_mermaid(spec)
    lines = src.splitlines()
    assert lines[0] == "flowchart TD"
    # one node line per graph node, plus edges + classDefs
    assert sum(1 for ln in lines if ln.strip().startswith("n")) >= len(spec.nodes)
    assert "-->" in src
    assert "classDef irreversible" in src
    assert DECLARED_STEPS_END_TITLE in src
    assert "Success" not in src


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
    schema_path = _REPO / "schemas" / "program-graph-v2.json"
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text())
    # The committed schema must match what the model currently produces
    # (regenerate schemas/program-graph-v2.json if this fails).
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
    ``worklist exhausted`` edge to a terminal titled
    ``End of declared steps`` -- the real cyclic structure the interpreter
    walks, not a dangling subflow reference."""
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
    assert terminals[0].title == DECLARED_STEPS_END_TITLE
    assert terminals[0].title != "Success"


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
    rc = main(
        [
            "visualize",
            str(_SHOWCASE),
            "--format",
            "json",
            "--profile",
            "remote-safe",
            "--out",
            str(out_json),
        ]
    )
    assert rc == 0
    data = json.loads(out_json.read_text())
    assert data["spec_version"] == SPEC_VERSION
    assert data["bundle"]["name"] == "Compiled program"
    assert "admin" not in out_json.read_text().lower()


# --------------------------------------------------------------------------
# The non-local boundary is a closed ALLOW-LIST.
#
# A deny-list projection ships every newly added spec field to public surfaces
# by default. These tests pin the inverse: a field leaves only when it is
# explicitly enumerated, and an unclassified field is refused outright.
# --------------------------------------------------------------------------

_PUBLIC_PROFILES = (
    PresentationProfile.REMOTE_SAFE,
    PresentationProfile.PUBLIC_SYNTHETIC,
    PresentationProfile.SANITIZED_DERIVATIVE,
)


def _risk_explanation_workflow(explanation: str) -> Workflow:
    """A one-step workflow whose risk provenance carries operator free text."""
    return Workflow(
        name="risk-provenance",
        steps=[
            Step(
                id="s0",
                intent="click Save",
                action=ActionKind.CLICK,
                anchor=_anchor(),
                risk="irreversible",
                risk_explanation=explanation,
            )
        ],
    )


def test_operator_risk_explanation_never_crosses_the_local_boundary() -> None:
    """``risk_explanation`` is operator free text (ir.py: up to 512 chars) and
    can name a customer and a record id. It must not reach a public surface."""
    explanation = "irreversible - posts to Acme's live billing API for patient 4417"
    source = build_program_graph(_risk_explanation_workflow(explanation))
    # The operator-local view still carries it; it is provenance, not a leak.
    local = project_program_graph(source, PresentationProfile.OPERATOR_LOCAL)
    assert local.nodes[0].risk_explanation == explanation

    for profile in _PUBLIC_PROFILES:
        projected = project_program_graph(source, profile)
        assert projected.nodes[0].risk_explanation is None, profile
        # The spec is embedded verbatim in the HTML export, so the string must
        # be absent from the rendered artifact too, not merely unrendered.
        assert explanation not in projected.model_dump_json(), profile
        assert "Acme" not in render_html(projected), profile
        assert "4417" not in render_html(projected), profile


def test_projection_carries_only_allow_listed_node_fields() -> None:
    """Every field a projected node actually sets is on the node allow-list."""
    from openadapt_flow.visualize.projection import _NODE_LOCAL, _NODE_PUBLIC

    source = build_program_graph(_mixed_workflow())
    for profile in _PUBLIC_PROFILES:
        projected = project_program_graph(source, profile)
        for node in projected.nodes:
            defaults = GraphNode(id=node.id, index=node.index, title="")
            set_fields = {
                name
                for name in GraphNode.model_fields
                if getattr(node, name) != getattr(defaults, name)
            }
            assert set_fields <= _NODE_PUBLIC, (profile, node.id, set_fields)
            # Nothing on the local list is ever populated.
            for name in _NODE_LOCAL:
                assert getattr(node, name) == getattr(defaults, name), (profile, name)


def test_field_boundary_classifies_every_crossing_model_field() -> None:
    """The live guard: adding a field to spec.py without classifying it fails
    here (and at import) instead of silently reaching a public surface."""
    from openadapt_flow.visualize.projection import assert_field_boundary_is_closed

    assert_field_boundary_is_closed()


def test_an_unclassified_new_field_is_refused() -> None:
    """Proof the guard has teeth: a GraphNode grown a new field is refused
    until an author puts it on the public or the local list."""
    import pytest
    from pydantic import create_model

    from openadapt_flow.visualize.projection import (
        _NODE_LOCAL,
        _NODE_PUBLIC,
        ProjectionBoundaryError,
        check_model_partition,
    )

    grown = create_model(
        "GraphNodeWithNewField",
        __base__=GraphNode,
        operator_note=(str, ""),
    )
    with pytest.raises(ProjectionBoundaryError) as excinfo:
        check_model_partition(grown, _NODE_PUBLIC, _NODE_LOCAL)
    assert "operator_note" in str(excinfo.value)

    # risk_explanation specifically is classified local, not public.
    assert "risk_explanation" in _NODE_LOCAL
    assert "risk_explanation" not in _NODE_PUBLIC


def test_projected_rung_labels_match_the_builder() -> None:
    """The projection derives each rung label from the closed rung id rather
    than carrying it across, so the two label tables must not drift."""
    from openadapt_flow.visualize.builder import _RUNG_LABELS as BUILT
    from openadapt_flow.visualize.projection import _RUNG_LABELS as PROJECTED

    assert dict(BUILT) == PROJECTED


def test_projection_drops_values_outside_a_closed_vocabulary() -> None:
    """Closed vocabularies are enforced at the boundary, so widening one
    upstream cannot by itself widen what leaves."""
    source = build_program_graph(_mixed_workflow())
    node = source.nodes[0]
    node.action = "exfiltrate patient 4417"
    node.risk = "free text risk"
    node.postconditions = ["text_present", "smuggled free text"]
    node.badges = ["irreversible", "3 authorized roles", "smuggled free text"]

    projected = project_program_graph(source, PresentationProfile.REMOTE_SAFE)
    out = projected.nodes[0]
    assert out.action is None
    assert out.risk is None
    assert out.postconditions == ["text_present"]
    assert out.badges == ["irreversible", "3 authorized roles"]
    assert "4417" not in projected.model_dump_json()


def test_projection_refuses_an_out_of_vocabulary_effect_fact() -> None:
    """A required governance field has no safe silent fallback: emitting the
    unenumerated value risks leaking free text, and substituting the default
    would understate risk. It fails closed instead."""
    import pytest

    from openadapt_flow.visualize.projection import ProjectionBoundaryError

    source = build_program_graph(_mixed_workflow())
    effect = next(n for n in source.nodes if n.effects).effects[0]
    effect.risk = "irreversible"
    projected = project_program_graph(source, PresentationProfile.REMOTE_SAFE)
    assert (
        next(n for n in projected.nodes if n.effects).effects[0].risk == "irreversible"
    )

    effect.risk = "sort-of reversible, ask Acme"
    with pytest.raises(ProjectionBoundaryError) as excinfo:
        project_program_graph(source, PresentationProfile.REMOTE_SAFE)
    assert "EffectInfo.risk" in str(excinfo.value)
    # The rejected value is never echoed: an exception message travels into
    # logs, and that value is the very thing suspected of carrying local data.
    assert "Acme" not in str(excinfo.value)


# --------------------------------------------------------------------------
# A PUBLIC program graph must carry a PROJECTED spec.
#
# render_html embeds the whole spec as JSON, so an unprojected graph ships
# recorded titles, DOM selectors, and template paths inside the published file
# even though the rendered page never displays them.
#
# Deliberately NOT asserted over public-demo/evidence-packs/mockmed-triage-v1,
# -v2 and -v3. Those are immutable retained packs in a format the exporter will
# never produce again, pinned byte-for-byte by LEGACY_RETAINED_PACKS in
# scripts/export_public_demo_evidence.py. Editing them to satisfy a test would
# trip the anti-tamper control that exists to stop exactly that, so the remedy
# for a retained pack is withdrawal, never rewriting.
# --------------------------------------------------------------------------

_SHOWCASE_GRAPH_DIR = _REPO / "docs" / "showcase-openemr"


def _embedded_spec(html_path: Path) -> dict:
    """The spec render_html embeds in the page, as a dict."""
    import re

    match = re.search(
        r'id="program-graph-spec">(.*?)</script>', html_path.read_text(), re.S
    )
    assert match is not None, f"{html_path} has no embedded spec"
    return json.loads(match.group(1).replace("<\\/", "</"))


def _assert_spec_is_projected(spec: dict, where: object) -> None:
    """Fail if any local diagnostic content survived into ``spec``."""
    from openadapt_flow.visualize.projection import _NODE_LOCAL
    from openadapt_flow.visualize.spec import PROJECTED_BUNDLE_NAME

    assert spec["bundle"]["name"] == PROJECTED_BUNDLE_NAME, where
    assert spec["bundle"].get("created_at") is None, where
    assert spec["bundle"]["provenance"].get("content_digest") is None, where
    for node in spec["nodes"]:
        for rung in (node.get("resolution") or {}).get("rungs", []):
            # detail carries the selector, template path, or OCR text.
            assert not rung.get("detail"), (where, node["id"], rung["name"])
        for field in _NODE_LOCAL:
            assert not node.get(field), (where, node["id"], field)
    for edge in spec["edges"]:
        assert edge.get("guard") is None, where


def test_committed_showcase_graph_carries_a_projected_spec() -> None:
    """The committed showcase graph is a public artifact in both formats."""
    json_path = _SHOWCASE_GRAPH_DIR / "program-graph.json"
    html_path = _SHOWCASE_GRAPH_DIR / "program-graph.html"
    assert json_path.exists() and html_path.exists()
    _assert_spec_is_projected(json.loads(json_path.read_text()), json_path)
    _assert_spec_is_projected(_embedded_spec(html_path), html_path)


def test_newly_exported_pack_graph_is_projected(tmp_path: Path) -> None:
    """The evidence exporter must cross the boundary, not publish the local
    graph. Exercises the real write path rather than grepping the source."""
    from scripts.export_public_demo_evidence import write_compiled_graph

    workflow = _mixed_workflow()
    write_compiled_graph(tmp_path, workflow)

    written_json = json.loads((tmp_path / "program-graph.json").read_text())
    _assert_spec_is_projected(written_json, "exported program-graph.json")
    _assert_spec_is_projected(
        _embedded_spec(tmp_path / "program-graph.html"), "exported program-graph.html"
    )

    # The recorded content of the source workflow must not appear anywhere in
    # either published byte stream.
    for name in ("program-graph.json", "program-graph.html"):
        blob = (tmp_path / name).read_text()
        for secret in ("unit-mixed", "patient row", "#row-1", "click Save"):
            assert secret not in blob, (name, secret)
