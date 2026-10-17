"""Exact parsing contracts for governed runtime-input artifacts."""

from __future__ import annotations

import json

import pytest

from openadapt_flow.ir import Interstitial, Predicate, PredicateKind, Workflow
from openadapt_flow.runtime.authorization import (
    parse_runtime_inputs_bytes,
    runtime_inputs_bytes,
)


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _blocking_interstitial() -> Interstitial:
    return Interstitial(
        name="unexpected dialog",
        detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Review required"),
    )


def test_parser_accepts_exact_serialized_interstitial() -> None:
    workflow = Workflow(name="runtime-input", steps=[])
    artifact = runtime_inputs_bytes(
        workflow,
        {"record_id": "42"},
        None,
        interstitials=[_blocking_interstitial()],
    )

    params, worklists = parse_runtime_inputs_bytes(artifact, workflow=workflow)

    assert params == {"record_id": "42"}
    assert worklists == {}


@pytest.mark.parametrize(
    "interstitials",
    [
        [{}],
        [42],
        [{**_blocking_interstitial().model_dump(mode="json"), "extra": True}],
    ],
)
def test_parser_rejects_invalid_or_noncanonical_interstitial_items(
    interstitials: list[object],
) -> None:
    artifact = _canonical(
        {"params": {}, "worklists": {}, "interstitials": interstitials}
    )

    with pytest.raises(ValueError, match="interstitials"):
        parse_runtime_inputs_bytes(
            artifact,
            workflow=Workflow(name="runtime-input", steps=[]),
        )


def test_parser_rejects_empty_interstitial_field_that_runtime_omits() -> None:
    workflow = Workflow(name="runtime-input", steps=[])
    artifact = _canonical({"params": {}, "worklists": {}, "interstitials": []})

    with pytest.raises(ValueError, match="canonical form"):
        parse_runtime_inputs_bytes(artifact, workflow=workflow)


def test_parser_requires_effective_workflow_defaults() -> None:
    workflow = Workflow(
        name="runtime-input",
        params={"record_id": "default-record"},
        steps=[],
    )
    omitted_default = _canonical({"params": {}, "worklists": {}})
    exact = runtime_inputs_bytes(workflow, None, None)

    with pytest.raises(ValueError, match="canonical form"):
        parse_runtime_inputs_bytes(omitted_default, workflow=workflow)
    params, worklists = parse_runtime_inputs_bytes(exact, workflow=workflow)
    assert params == {"record_id": "default-record"}
    assert worklists == {}
