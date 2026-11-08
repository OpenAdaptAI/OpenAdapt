"""Repeated JSON discovery and the ordinary filtered reader share one model.

Synthetic fixtures only. These checks grant no completeness or admission.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.record_check_synthesis import (
    _bind_fields,
    _flat,
    _listings,
    _surface_from_listing,
    _write_values,
    param_lookup,
    synthesize,
)
from openadapt_flow.runtime.effects import Verdict
from openadapt_flow.runtime.effects.fhir import extract_path, filter_matches
from openadapt_flow.runtime.effects.surfaces import surface_of
from tests._record_check_fixtures import Capture, write_recording

VALUE = "selected-synthetic-value"


def _rows(order):
    return [
        {"kind": kind, "value": VALUE if kind == "wanted" else f"other-{kind}"}
        for kind in order
    ]


def _assert_every_path_reads_its_leaf(document, flat):
    for path, value in flat.items():
        observed = extract_path(document, path)
        assert observed == value, path
        assert type(observed) is type(value), path


@pytest.mark.parametrize(
    "order", list(itertools.permutations(("first", "wanted", "last")))
)
def test_repeated_discovery_and_sent_values_ignore_element_order(order):
    document = {"rows": _rows(order)}
    flat = _flat(document, excluded_values=[VALUE])
    path = "rows[kind=wanted].value"
    assert flat[path] == VALUE
    assert "rows.value" not in flat
    assert _bind_fields(flat, {VALUE: "input"}) == ({path: "input"}, {})
    sent = _write_values({"body": json.dumps(document)}, excluded_values=[VALUE])
    assert sent[path] == VALUE
    _assert_every_path_reads_its_leaf(document, flat)


@pytest.mark.parametrize(
    "outer_reverse,inner_reverse", itertools.product((False, True), repeat=2)
)
@pytest.mark.parametrize("singleton_discriminator", [False, True])
def test_nested_repetitions_use_unambiguous_dotted_discriminators(
    outer_reverse, inner_reverse, singleton_discriminator
):
    selected = _rows(("first", "wanted", "last"))
    if inner_reverse:
        selected.reverse()
    groups = [
        {"meta": {"kind": "secondary"}, "rows": [{"value": "other"}]},
        {"meta": {"kind": "primary"}, "rows": selected},
    ]
    if singleton_discriminator:
        for group in groups:
            group["meta"] = [group["meta"]]
    if outer_reverse:
        groups.reverse()
    document = {"groups": groups}
    flat = _flat(document, excluded_values=[VALUE])
    path = "groups[meta.kind=primary].rows[kind=wanted].value"
    assert flat[path] == VALUE
    assert _bind_fields(flat, {VALUE: "input"}) == ({path: "input"}, {})
    sent = _write_values({"body": json.dumps(document)}, excluded_values=[VALUE])
    assert sent[path] == VALUE
    _assert_every_path_reads_its_leaf(document, flat)


@pytest.mark.parametrize(
    "rows",
    [
        [{"kind": "same", "value": VALUE}, {"kind": "same", "value": "other"}],
        [{"value": VALUE}, {"value": "other"}],
        [{"kind": "bad]", "value": VALUE}, {"kind": "other", "value": "other"}],
        [{"kind": "bad\nkind", "value": VALUE}, {"kind": "other", "value": "other"}],
        [{"kind": "<secret>", "value": VALUE}, {"kind": "other", "value": "other"}],
        [{"kind": "wanted", "value": VALUE}, "not-an-object"],
        [{"kind": "wanted", "value": VALUE}, None],
    ],
    ids=["duplicate", "self-only", "delimiter", "newline", "redacted", "mixed", "null"],
)
def test_unrepresentable_or_ambiguous_repetitions_do_not_bind(rows):
    document = {"rows": rows}
    flat = _flat(document, excluded_values=[VALUE])
    assert _bind_fields(flat, {VALUE: "input"}) == ({}, {})
    assert (
        VALUE
        not in _write_values(
            {"body": json.dumps(document)}, excluded_values=[VALUE]
        ).values()
    )
    _assert_every_path_reads_its_leaf(document, flat)


@pytest.mark.parametrize("discriminator", ["selector", "Entity/selector"])
def test_parameters_including_duplicate_values_cannot_select_themselves(discriminator):
    params = {"one": "selector", "two": "selector", "input": VALUE}
    lookup, notes = param_lookup(params)
    assert "selector" not in lookup and notes
    document = {
        "rows": [
            {"kind": discriminator, "value": VALUE},
            {"kind": "other", "value": "other"},
        ]
    }
    flat = _flat(document, excluded_values=params.values())
    assert _bind_fields(flat, lookup) == ({}, {})
    assert (
        VALUE
        not in _write_values(
            {"body": json.dumps(document)}, excluded_values=params.values()
        ).values()
    )


def test_competing_repeated_leaves_do_not_choose_a_binding():
    document = {
        "rows": [
            {"kind": "home", "value": VALUE},
            {"kind": "work", "value": VALUE},
        ]
    }
    flat = _flat(document, excluded_values=[VALUE])
    assert flat["rows[kind=home].value"] == flat["rows[kind=work].value"] == VALUE
    assert _bind_fields(flat, {VALUE: "input"}) == ({}, {})
    _assert_every_path_reads_its_leaf(document, flat)


@pytest.mark.parametrize(
    "node",
    [
        None,
        [],
        [1, 2],
        [{"value": VALUE}, {"value": "other"}],
        {
            1: "bad",
            "rows": [
                {1: "bad", "kind": "wanted", "value": VALUE},
                {None: "bad", "kind": "other", "value": "other"},
            ],
        },
        {"bad": object()},
        {"bad": float("nan")},
        {"bad": float("inf")},
    ],
    ids=[
        "null",
        "empty",
        "scalar-array",
        "root-array",
        "mixed-keys",
        "object",
        "nan",
        "inf",
    ],
)
def test_malformed_nodes_do_not_crash_or_emit_unreadable_paths(node):
    flat = _flat(node, excluded_values=[VALUE])
    assert all(isinstance(path, str) for path in flat)
    if isinstance(node, dict):
        _assert_every_path_reads_its_leaf(node, flat)
    else:
        assert flat == {}


def test_single_item_lists_and_unfiltered_reader_paths_remain_unchanged():
    document = {"title": VALUE, "rows": [{"value": "single"}], "names": ["first"]}
    flat = _flat(document)
    assert flat == {"title": VALUE, "rows.value": "single"}
    _assert_every_path_reads_its_leaf(document, flat)
    assert (
        extract_path({"rows": [{"value": "one"}, {"value": "two"}]}, "rows.value")
        == "one"
    )


@pytest.mark.parametrize(
    "value,parameter,expected",
    [
        ("value", "value", True),
        (" value ", "value", False),
        (" value ", " value ", True),
        (1, "1", True),
        ("1", "1", True),
        (1.0, "1", False),
        (True, "True", False),
    ],
    ids=[
        "text",
        "no-trim",
        "exact-spaces",
        "legacy-int",
        "legacy-string",
        "float",
        "bool",
    ],
)
def test_binding_keeps_exact_text_and_characterizes_legacy_scalar_strings(
    value, parameter, expected
):
    exact, references = _bind_fields({"field": value}, {parameter: "input"})
    assert exact == ({"field": "input"} if expected else {})
    assert references == {}


@pytest.mark.parametrize("condition", ["kind=wanted", "kind^=want"])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_filtered_extraction_requires_one_match(condition, count):
    document = {"rows": [{"kind": "wanted", "value": "same"} for _ in range(count)]}
    assert extract_path(document, f"rows[{condition}].value") == (
        "same" if count == 1 else None
    )


@pytest.mark.parametrize("count", [0, 1, 2])
def test_dotted_discriminators_do_not_pick_the_first_nested_item(count):
    item = {"codes": [{"kind": "wanted"} for _ in range(count)], "value": "same"}
    want = ("codes.kind", "wanted", False)
    assert filter_matches(item, want) is (count == 1)
    assert extract_path({"rows": [item]}, "rows[codes.kind=wanted].value") == (
        "same" if count == 1 else None
    )


@pytest.mark.parametrize(
    "value,text", [(1, "1"), ("1", "1"), (True, "True"), ("True", "True")]
)
def test_filter_comparison_keeps_legacy_scalar_strings(value, text):
    item = {"code": value, "value": "selected"}
    assert filter_matches(item, ("code", text, False))
    assert extract_path({"rows": [item]}, f"rows[code={text}].value") == "selected"


@pytest.mark.parametrize("values,text", [([1, "1"], "1"), ([True, "True"], "True")])
def test_scalar_string_aliases_are_multiple_matches(values, text):
    document = {"rows": [{"code": value, "value": "selected"} for value in values]}
    assert extract_path(document, f"rows[code={text}].value") is None


def test_learning_repeated_paths_does_not_enable_nested_rest_fields():
    document = {"data": [{"id": "synthetic", "rows": _rows(("first", "wanted"))}]}
    [listing] = _listings(document)
    surface, reason = _surface_from_listing(
        {"url": "http://fixture.test/records", "t": 1.0},
        listing,
        {VALUE: "input"},
        before_line=None,
        taken=set(),
    )
    assert surface is None
    assert reason is not None and "nested fields" in reason


@pytest.fixture
def repeated_resource_server():
    resources = []
    methods = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            methods.append("GET")
            snapshot = copy.deepcopy(resources)
            self.reply(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": len(snapshot),
                    "entry": [{"resource": row} for row in snapshot],
                },
            )

        def do_POST(self):
            methods.append("POST")
            length = int(self.headers["Content-Length"])
            resources.append(json.loads(self.rfile.read(length)))
            self.reply(201, resources[-1])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fhir", resources, methods
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
        assert not worker.is_alive()


@pytest.mark.parametrize("shape", ["contact", "inventory"])
def test_generated_paths_build_and_read_new_values_over_real_http(
    tmp_path, repeated_resource_server, shape
):
    base, resources, methods = repeated_resource_server
    if shape == "contact":
        resource = {
            "resourceType": "Patient",
            "id": "synthetic-1",
            "label": "subject-a",
            "telecom": [
                {"system": "phone", "value": "other"},
                {"system": "email", "value": VALUE},
            ],
        }
        path = "telecom[system=email].value"
    else:
        resource = {
            "resourceType": "Basic",
            "id": "synthetic-1",
            "label": "subject-a",
            "groups": [
                {"meta": {"kind": "secondary"}, "rows": [{"value": "other"}]},
                {"meta": {"kind": "primary"}, "rows": _rows(("first", "wanted"))},
            ],
        }
        path = "groups[meta.kind=primary].rows[kind=wanted].value"
    params = {"label": "subject-a", "input": VALUE}
    url = f"{base}/{resource['resourceType']}"
    capture = Capture()
    assert capture.get(url, 1.0).status_code == 200
    assert capture.post_json(url, resource, 2.0).status_code == 201
    assert capture.get(url, 3.0).status_code == 200
    recording = write_recording(tmp_path / "recording", params)
    capture.write(recording)
    proposal = synthesize(recording)
    [surface] = proposal.search.surfaces
    fields = surface.config["field_paths"]
    assert path in fields.values()
    field = next(key for key, value in fields.items() if value == path)
    assert surface.bound[field] == "input"
    assert not proposal.search.uncovered
    assert all(effect.needs_operator_confirmation for effect in proposal.effects)
    assert proposal.effects
    verifier = build_effect_verifier(
        EffectsConfig.model_validate(proposal.verifier), params
    )
    assert verifier is not None
    first = verifier.capture_pre_state()
    assert first.reachable and first.detail[surface.name]["records"][0][field] == VALUE
    # Reusing the proposed path must read a fresh value, not select by the old one.
    selected = (
        resources[0]["telecom"][1]
        if shape == "contact"
        else resources[0]["groups"][1]["rows"][1]
    )
    selected["value"] = "fresh-synthetic-value"
    fresh_params = dict(params, input="fresh-synthetic-value")
    fresh = build_effect_verifier(
        EffectsConfig.model_validate(proposal.verifier), fresh_params
    )
    assert fresh is not None
    [fresh_resource] = resources
    resources.clear()
    before = fresh.capture_pre_state()
    assert before.reachable and before.detail[surface.name]["records"] == []
    # The owned fixture models a new resource after the actual empty HTTP read.
    resources.append(fresh_resource)
    [target_effect] = [
        effect.model_copy(update={"timeout_s": 0.2}).resolve(fresh_params)
        for effect in proposal.effects
        if surface_of(effect) == surface.name
    ]
    assert target_effect.needs_operator_confirmation
    clean = fresh.verify(target_effect, before)
    assert clean.verdict is Verdict.CONFIRMED
    assert not clean.should_halt
    observed = fresh.capture_pre_state()
    assert observed.reachable
    assert observed.detail[surface.name]["records"][0][field] == fresh_params["input"]
    duplicate = copy.deepcopy(selected)
    siblings = (
        resources[0]["telecom"]
        if shape == "contact"
        else resources[0]["groups"][1]["rows"]
    )
    siblings.append(duplicate)
    ambiguous = fresh.capture_pre_state()
    assert ambiguous.reachable
    assert ambiguous.detail[surface.name]["records"][0][field] is None
    ambiguous_verdict = fresh.verify(target_effect, before)
    assert ambiguous_verdict.verdict is Verdict.REFUTED
    assert ambiguous_verdict.should_halt
    siblings[:] = [
        item for item in siblings if item is not selected and item is not duplicate
    ]
    missing = fresh.capture_pre_state()
    assert missing.reachable
    assert missing.detail[surface.name]["records"][0][field] is None
    missing_verdict = fresh.verify(target_effect, before)
    assert missing_verdict.verdict is Verdict.REFUTED
    assert missing_verdict.should_halt
    assert methods.count("POST") == 1


def test_real_synthesis_excludes_duplicate_parameter_discriminators(
    tmp_path, repeated_resource_server
):
    base, _resources, methods = repeated_resource_server
    resource = {
        "resourceType": "Patient",
        "id": "synthetic-1",
        "label": "subject-a",
        "telecom": [
            {"system": "phone", "value": "other"},
            {"system": "email", "value": VALUE},
        ],
    }
    params = {
        "label": "subject-a",
        "input": VALUE,
        "context_one": "email",
        "context_two": "email",
    }
    capture = Capture()
    url = f"{base}/Patient"
    capture.get(url, 1.0)
    assert capture.post_json(url, resource, 2.0).status_code == 201
    capture.get(url, 3.0)
    recording = write_recording(tmp_path / "recording", params)
    capture.write(recording)
    proposal = synthesize(recording)
    [surface] = proposal.search.surfaces
    assert "input" not in surface.bound.values()
    assert "telecom[system=email].value" not in surface.config["field_paths"].values()
    assert methods.count("POST") == 1
