"""Flow emits exactly the spec @openadapt/program-graph renders.

``packages/program-graph`` is the React view Desktop, Cloud and the website
share. It carries a copy of this package's program-graph JSON Schema and
TypeScript types for every shape in it. These tests fail when the pydantic
models, the committed schema, the package's copy, or the package's types
drift apart, so any bundle ``openadapt-flow visualize`` can emit renders
there. They also check the package's demo fixture against both schemas.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.visualize import ProgramGraphSpec, build_program_graph

_FLOW = Path(__file__).resolve().parent.parent
_PKG = _FLOW.parent / "program-graph"
_SHOWCASE = _FLOW / "docs" / "showcase-openemr" / "bundle"

pytestmark = pytest.mark.skipif(
    not _PKG.is_dir(), reason="needs the monorepo's packages/program-graph"
)

# Keys the committed schema adds on top of model_json_schema().
_ENVELOPE = ("$schema", "$id", "title")


def _package_schema() -> dict:
    return json.loads((_PKG / "schema" / "program-graph-v2.json").read_text())


def _ts_interfaces() -> dict[str, dict[str, bool]]:
    """Interface name -> {property: optional?} from the package's spec.ts."""
    source = (_PKG / "src" / "spec.ts").read_text()
    out: dict[str, dict[str, bool]] = {}
    for name, body in re.findall(r"export interface (\w+) \{(.*?)\n\}", source, re.S):
        props = {}
        for key, optional in re.findall(r"^\s+(\w+)(\??):", body, re.M):
            props[key] = optional == "?"
        out[name] = props
    return out


def _ts_union(name: str) -> list[str]:
    source = (_PKG / "src" / "spec.ts").read_text()
    match = re.search(rf"export type {name} =(.*?);", source, re.S)
    assert match, f"spec.ts has no type {name}"
    return re.findall(r'"(\w+)"', match.group(1))


def test_package_schema_is_flows_schema() -> None:
    committed = (_FLOW / "schemas" / "program-graph-v2.json").read_bytes()
    assert (_PKG / "schema" / "program-graph-v2.json").read_bytes() == committed, (
        "copy packages/flow/schemas/program-graph-v2.json to "
        "packages/program-graph/schema/program-graph-v2.json"
    )
    schema = {k: v for k, v in _package_schema().items() if k not in _ENVELOPE}
    model = {
        k: v
        for k, v in ProgramGraphSpec.model_json_schema().items()
        if k not in _ENVELOPE
    }
    assert schema == model, (
        "regenerate schemas/program-graph-v2.json from ProgramGraphSpec"
    )


def test_package_types_match_the_schema() -> None:
    schema = _package_schema()
    defs = dict(schema["$defs"])
    defs["ProgramGraphSpec"] = schema
    interfaces = _ts_interfaces()
    for name, definition in defs.items():
        if "enum" in definition:
            assert _ts_union(name) == definition["enum"], name
            continue
        assert name in interfaces, f"spec.ts has no interface {name}"
        required = set(definition.get("required", []))
        expected = {key: key not in required for key in definition["properties"]}
        assert interfaces[name] == expected, (
            f"spec.ts {name} differs from the schema: "
            f"{sorted(set(interfaces[name].items()) ^ set(expected.items()))}"
        )


def test_visualize_emits_only_keys_the_package_declares(tmp_path) -> None:
    """The CLI's JSON carries every declared key and nothing undeclared."""
    out = tmp_path / "graph.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "visualize",
            str(_SHOWCASE),
            "--format",
            "json",
            "-o",
            str(out),
        ],
        check=True,
        cwd=_FLOW,
        capture_output=True,
    )
    emitted = json.loads(out.read_text())
    interfaces = _ts_interfaces()

    def keys(obj: dict, name: str) -> None:
        assert set(obj) == set(interfaces[name]), (
            f"{name}: {sorted(set(obj) ^ set(interfaces[name]))}"
        )

    keys(emitted, "ProgramGraphSpec")
    keys(emitted["bundle"], "BundleMeta")
    keys(emitted["bundle"]["provenance"], "ProvenanceInfo")
    for param in emitted["bundle"]["params"]:
        keys(param, "ParamInfo")
    for node in emitted["nodes"]:
        keys(node, "GraphNode")
        if node["resolution"]:
            keys(node["resolution"], "ResolutionInfo")
            for rung in node["resolution"]["rungs"]:
                keys(rung, "ResolutionRung")
        if node["identity"]:
            keys(node["identity"], "IdentityInfo")
        for effect in node["effects"]:
            keys(effect, "EffectInfo")
    for edge in emitted["edges"]:
        keys(edge, "GraphEdge")

    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(emitted, _package_schema())
    jsonschema.validate(
        json.loads(build_program_graph(Workflow.load(_SHOWCASE)).model_dump_json()),
        _package_schema(),
    )


def test_demo_fixture_matches_both_schemas() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    fixture = _PKG / "fixtures" / "openemr-referral"
    spec = json.loads((fixture / "spec.json").read_text())
    jsonschema.validate(spec, _package_schema())
    ProgramGraphSpec.model_validate(spec)
    trace = json.loads((fixture / "trace.json").read_text())
    jsonschema.validate(
        trace, json.loads((_PKG / "schema" / "run-trace-v1.json").read_text())
    )
    ids = {node["id"] for node in spec["nodes"]}
    assert {event["node"] for event in trace["events"]} <= ids
    folds = json.loads((fixture / "folds.json").read_text())
    members = [node for group in folds["groups"] for node in group["nodes"]]
    assert members == [node["id"] for node in spec["nodes"] if node["kind"] == "action"]
