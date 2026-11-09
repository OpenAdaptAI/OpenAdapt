"""Composition parent visualization: children stay children.

A composed directory is a sequencer, not a bigger ProgramGraph. These tests
pin that ``visualize`` emits one ``child_bundle`` node per child, sequence
edges for the ``--after`` DAG, handoff edges labelled with effect-bound
param names, and a parent terminal titled ``End of declared steps``.
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.compiler.compose_authoring import author_composition
from openadapt_flow.composition import HandoffBinding, is_composition_artifact
from openadapt_flow.ir import ActionKind, ParamSpec, Step, Workflow
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr
from openadapt_flow.visualize import (
    DECLARED_STEPS_END_TITLE,
    SPEC_VERSION,
    NodeKind,
    PresentationProfile,
    ProgramGraphSpec,
    build_composition_graph,
    project_program_graph,
    render_html,
    render_mermaid,
)

_REPO = Path(__file__).resolve().parent.parent


def _writer() -> Workflow:
    return Workflow(
        name="intake",
        surface="web",
        steps=[
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                param="patient_id",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"patient_id": ValueExpr(param="patient_id")},
                    )
                ],
            )
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="p1")},
    )


def _reader(*, name: str = "posting", surface: str = "linux") -> Workflow:
    return Workflow(
        name=name,
        surface=surface,  # type: ignore[arg-type]
        steps=[
            Step(
                id="type_patient",
                intent="type patient_id into posting",
                action=ActionKind.TYPE,
                param="patient_id",
            )
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="p1")},
    )


def _save(tmp_path: Path, workflow: Workflow, folder: str) -> Path:
    path = tmp_path / folder
    path.mkdir()
    workflow.save(path)
    return path


def _two_child_parent(tmp_path: Path) -> Path:
    intake = _save(tmp_path, _writer(), "intake")
    posting = _save(tmp_path, _reader(), "posting")
    out = tmp_path / "composed"
    author_composition(
        [("intake", intake), ("posting", posting)],
        handoffs=[
            HandoffBinding(
                from_child="intake",
                source="patient_id",
                to_child="posting",
                target="patient_id",
            )
        ],
        name="two-step",
        out=out,
    )
    return out


def test_composition_graph_is_not_a_merged_program(tmp_path: Path) -> None:
    parent = _two_child_parent(tmp_path)
    assert is_composition_artifact(parent)
    spec = build_composition_graph(parent)

    assert spec.bundle.is_composition is True
    assert spec.bundle.is_program is False
    assert spec.bundle.child_count == 2
    assert spec.bundle.action_count == 0
    assert spec.bundle.name == "two-step"

    children = [n for n in spec.nodes if n.kind == NodeKind.CHILD_BUNDLE]
    assert [n.title for n in children] == ["intake", "posting"]
    assert [n.surface for n in children] == ["web", "linux"]
    assert all(n.kind != NodeKind.ACTION for n in spec.nodes)
    # Child step intents must not leak: that would mean we merged recordings.
    titles = " ".join(n.title for n in spec.nodes)
    assert "save encounter" not in titles
    assert "type patient_id into posting" not in titles
    assert "Success" not in [n.title for n in spec.nodes]


def test_composition_terminal_is_end_of_declared_steps(tmp_path: Path) -> None:
    spec = build_composition_graph(_two_child_parent(tmp_path))
    terminals = [n for n in spec.nodes if n.kind == NodeKind.TERMINAL]
    assert len(terminals) == 1
    assert terminals[0].title == DECLARED_STEPS_END_TITLE
    assert terminals[0].title != "Success"
    assert terminals[0].outcome == "success"


def test_composition_handoff_edges_use_param_names(tmp_path: Path) -> None:
    spec = build_composition_graph(_two_child_parent(tmp_path))
    handoffs = [e for e in spec.edges if e.kind.value == "handoff"]
    assert len(handoffs) == 1
    assert handoffs[0].source == "intake"
    assert handoffs[0].target == "posting"
    assert handoffs[0].label == "patient_id"
    assert "window" not in handoffs[0].label.lower()
    sequence = [e for e in spec.edges if e.kind.value == "sequence"]
    assert any(e.source == "intake" and e.target == "posting" for e in sequence)


def test_composition_mermaid_names_children_and_handoffs(tmp_path: Path) -> None:
    spec = build_composition_graph(_two_child_parent(tmp_path))
    src = render_mermaid(spec)
    assert src.splitlines()[0] == "flowchart TD"
    assert "intake" in src
    assert "posting" in src
    assert "web" in src
    assert "linux" in src
    assert "patient_id" in src
    assert DECLARED_STEPS_END_TITLE in src
    assert "Success" not in src


def test_composition_html_is_self_contained(tmp_path: Path) -> None:
    spec = build_composition_graph(_two_child_parent(tmp_path))
    doc = render_html(spec)
    assert doc.lstrip().startswith("<!doctype html>")
    for needle in ("http://", "https://", "src=", "cdn"):
        assert needle not in doc, f"unexpected external reference: {needle}"
    assert "intake" in doc
    assert "patient_id" in doc
    assert DECLARED_STEPS_END_TITLE in doc
    assert '"title": "Success"' not in doc
    assert '"title":"Success"' not in doc.replace(" ", "")


def test_composition_spec_validates_against_v2_schema(tmp_path: Path) -> None:
    schema_path = _REPO / "schemas" / "program-graph-v2.json"
    schema = json.loads(schema_path.read_text())
    spec = build_composition_graph(_two_child_parent(tmp_path))
    jsonschema = __import__("importlib").import_module("jsonschema")
    jsonschema.validate(json.loads(spec.model_dump_json()), schema)
    assert spec.spec_version == SPEC_VERSION


def test_composition_after_dag_edges(tmp_path: Path) -> None:
    intake = _save(tmp_path, _writer(), "intake")
    coding = _save(tmp_path, _reader(name="coding", surface="windows"), "coding")
    posting = _save(tmp_path, _reader(name="posting", surface="linux"), "posting")
    out = tmp_path / "composed"
    author_composition(
        [("intake", intake), ("coding", coding), ("posting", posting)],
        handoffs=[
            HandoffBinding(
                from_child="intake",
                source="patient_id",
                to_child="posting",
                target="patient_id",
            )
        ],
        after={"coding": ["intake"], "posting": ["coding"]},
        name="three-step",
        out=out,
    )
    spec = build_composition_graph(out)
    names = [n.title for n in spec.nodes if n.kind == NodeKind.CHILD_BUNDLE]
    assert names == ["intake", "coding", "posting"]
    sequence = {(e.source, e.target) for e in spec.edges if e.kind.value == "sequence"}
    assert ("intake", "coding") in sequence
    assert ("coding", "posting") in sequence
    handoffs = [e for e in spec.edges if e.kind.value == "handoff"]
    assert len(handoffs) == 1
    assert handoffs[0].source == "intake"
    assert handoffs[0].target == "posting"
    src = render_mermaid(spec)
    assert "coding" in src
    assert "windows" in src
    assert "|patient_id|" in src


def test_cli_visualize_composition(tmp_path: Path) -> None:
    from openadapt_flow.__main__ import main

    parent = _two_child_parent(tmp_path)
    out_mmd = tmp_path / "graph.mmd"
    rc = main(
        [
            "visualize",
            str(parent),
            "--format",
            "mermaid",
            "--out",
            str(out_mmd),
        ]
    )
    assert rc == 0
    text = out_mmd.read_text()
    assert "intake" in text
    assert "posting" in text
    assert "patient_id" in text
    assert DECLARED_STEPS_END_TITLE in text
    assert "Success" not in text

    out_json = tmp_path / "graph.json"
    rc = main(
        [
            "visualize",
            str(parent),
            "--format",
            "json",
            "--out",
            str(out_json),
        ]
    )
    assert rc == 0
    data = json.loads(out_json.read_text())
    assert data["bundle"]["is_composition"] is True
    kinds = {node["kind"] for node in data["nodes"]}
    assert "child_bundle" in kinds
    assert "action" not in kinds
    titles = [node["title"] for node in data["nodes"]]
    assert "Success" not in titles
    assert DECLARED_STEPS_END_TITLE in titles


def test_remote_safe_composition_drops_child_names(tmp_path: Path) -> None:
    source = build_composition_graph(_two_child_parent(tmp_path))
    projected = project_program_graph(source, PresentationProfile.REMOTE_SAFE)
    children = [n for n in projected.nodes if n.kind == NodeKind.CHILD_BUNDLE]
    assert all(n.title == "Run an approved child bundle" for n in children)
    assert {n.surface for n in children} == {"web", "linux"}
    handoffs = [e for e in projected.edges if e.kind.value == "handoff"]
    assert handoffs
    assert all(e.label == "declared handoff" for e in handoffs)
    payload = projected.model_dump_json()
    assert "patient_id" not in payload
    ProgramGraphSpec.model_validate_json(payload)
