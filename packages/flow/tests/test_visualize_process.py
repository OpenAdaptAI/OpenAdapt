"""Visualizer for a ProcessContract parent (not a ProgramGraph, not compose)."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.__main__ import main
from openadapt_flow.admitted_composition import author_process_contract
from openadapt_flow.composition import HandoffBinding
from openadapt_flow.visualize.admitted_composition import (
    END_OF_DECLARED_STEPS,
    PROCESS_GRAPH_SPEC_VERSION,
    build_process_graph,
    render_process_html,
    render_process_mermaid,
)
from tests.test_admitted_composition_authoring import _two_admitted

_REPO = Path(__file__).resolve().parent.parent


def _author(tmp_path: Path):
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    author_process_contract(
        [
            ("intake", intake_env, intake),
            ("posting", posting_env, posting),
        ],
        handoffs=[
            HandoffBinding(
                from_child="intake",
                source="patient_id",
                to_child="posting",
                target="patient_id",
            )
        ],
        name="claim-post",
        out=out,
    )
    return out


def test_process_graph_nodes_are_admitted_capabilities(tmp_path: Path) -> None:
    spec = build_process_graph(_author(tmp_path))
    assert spec.spec_version == PROCESS_GRAPH_SPEC_VERSION
    admitted = [node for node in spec.nodes if node.kind == "admitted_capability"]
    assert [node.id for node in admitted] == ["intake", "posting"]
    assert all(node.kind == "admitted_capability" for node in admitted)
    assert all(node.kind != "action" for node in spec.nodes)
    assert all(getattr(node, "kind", None) != "child_bundle" for node in spec.nodes)
    assert admitted[0].admission_id_short
    assert admitted[0].digest_short
    terminal = [node for node in spec.nodes if node.kind == "terminal"]
    assert len(terminal) == 1
    assert terminal[0].title == END_OF_DECLARED_STEPS
    assert terminal[0].title != "Success"
    assert spec.terminal_title == END_OF_DECLARED_STEPS


def test_process_graph_edges_order_and_handoff(tmp_path: Path) -> None:
    spec = build_process_graph(_author(tmp_path))
    order = [edge for edge in spec.edges if edge.kind == "order"]
    handoffs = [edge for edge in spec.edges if edge.kind == "handoff"]
    assert [edge.source for edge in order] == ["intake", "posting"]
    assert order[-1].target == "end_declared_steps"
    assert len(handoffs) == 1
    assert handoffs[0].source == "intake"
    assert handoffs[0].target == "posting"
    assert handoffs[0].label == "patient_id"


def test_process_html_is_self_contained(tmp_path: Path) -> None:
    spec = build_process_graph(_author(tmp_path))
    page = render_process_html(spec)
    assert page.startswith("<!doctype html>")
    assert "http://" not in page and "https://" not in page
    assert END_OF_DECLARED_STEPS in page
    assert "Success" not in page
    assert "intake" in page
    assert "patient_id" in page
    mermaid = render_process_mermaid(spec)
    assert mermaid.splitlines()[0] == "flowchart TD"
    assert END_OF_DECLARED_STEPS in mermaid
    assert "patient_id" in mermaid
    assert "Success" not in mermaid


def test_emitted_process_spec_validates_against_committed_schema(
    tmp_path: Path,
) -> None:
    schema_path = _REPO / "schemas" / "process-contract-graph-v0.json"
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    from openadapt_flow.visualize.admitted_composition import ProcessContractSpec

    current = ProcessContractSpec.model_json_schema()
    for key in ("properties", "$defs"):
        assert schema.get(key, {}).keys() == current.get(key, {}).keys()
    jsonschema = __import__("importlib").import_module("jsonschema")
    spec = build_process_graph(_author(tmp_path))
    jsonschema.validate(json.loads(spec.model_dump_json()), schema)


def test_cli_visualize_process_writes_outputs(tmp_path: Path) -> None:
    parent = _author(tmp_path)
    out_html = tmp_path / "process.html"
    rc = main(["visualize", str(parent), "--out", str(out_html)])
    assert rc == 0
    page = out_html.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert END_OF_DECLARED_STEPS in page

    out_json = tmp_path / "process.json"
    rc = main(["visualize", str(parent), "--format", "json", "--out", str(out_json)])
    assert rc == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["spec_version"] == PROCESS_GRAPH_SPEC_VERSION
    assert data["terminal_title"] == END_OF_DECLARED_STEPS
    assert data["nodes"][0]["kind"] == "admitted_capability"
