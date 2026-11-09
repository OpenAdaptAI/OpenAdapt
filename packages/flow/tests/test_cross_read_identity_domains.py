"""Native identity domains must agree across the whole baseline/read pair."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
import requests
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import Effect, EffectState, Verdict
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from openadapt_flow.runtime.effects.surfaces import SurfaceRoutedVerifier, surface_probe
from tests.e2e.rest_page_cap_server import CappedRecordServer


def _effect(count: int, routed: bool = False) -> Effect:
    return Effect.model_validate(
        {
            "kind": "record_written",
            "match": {"value": "wanted"},
            "count_new_only": True,
            "expected_count": count,
            "timeout_s": 0,
            "probe": surface_probe("records") if routed else None,
        }
    )


def _wire(rows: list[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
    return [
        {**row, "id": str(row["id"]) if domain == "string" else int(row["id"])}
        for row in rows
    ]


def _native(kind: str, domain: str, count: int = 2) -> list[dict[str, Any]]:
    return [
        {"id": (str(i) if domain == "string" else i), "value": "wanted"}
        if kind == "rest"
        else {
            "id": "resource-" + str(i),
            "native_id": str(i) if domain == "string" else i,
            "value": "wanted",
        }
        for i in range(1, count + 1)
    ]


def _readers(
    server: CappedRecordServer, kind: str, style: str, path: str
) -> tuple[Any, Any, requests.Session]:
    session = requests.Session()
    session.trust_env = False
    config: dict[str, Any] = {
        "kind": kind,
        "base_url": server.url if kind == "rest" else server.url + "/fhir",
        "timeout_s": 1,
    }
    if kind == "rest":
        config.update(
            records_path="/records",
            records_key="records",
            records_total_path="page.total",
        )
        if style == "next":
            config.update(
                records_next_path="page.next", records_snapshot_path="page.snapshot"
            )
    else:
        config.update(
            resource_type="Patient", field_paths={"id": "native_id", "value": "value"}
        )
        if style == "next":
            config["records_snapshot_path"] = "page.snapshot"
    if path == "direct":
        child = (
            RestRecordVerifier(
                server.url,
                records_path="/records",
                records_key="records",
                total_path="page.total",
                next_path="page.next" if style == "next" else None,
                snapshot_path="page.snapshot" if style == "next" else None,
                session=session,
                timeout_s=1,
                poll_interval_s=0,
            )
            if kind == "rest"
            else FhirEffectVerifier(
                server.url + "/fhir",
                resource_type="Patient",
                field_paths={"id": "native_id", "value": "value"},
                snapshot_path="page.snapshot" if style == "next" else None,
                session=session,
                timeout_s=1,
                poll_interval_s=0,
            )
        )
    else:
        child = build_effect_verifier(
            EffectsConfig.model_validate(config), session=session
        )
        assert isinstance(child, (RestRecordVerifier, FhirEffectVerifier))
    return (
        (
            SurfaceRoutedVerifier({"records": child})
            if path in ("routed", "serialized")
            else child
        ),
        child,
        session,
    )


@pytest.mark.parametrize("kind", ["rest", "fhir"])
@pytest.mark.parametrize("initial", ["integer", "string"])
@pytest.mark.parametrize("style", ["total", "next"])
@pytest.mark.parametrize("path", ["direct", "config", "routed", "serialized"])
def test_wire_only_domain_change_cannot_confirm_new_records(
    kind: str, initial: str, style: str, path: str
) -> None:
    native = _native(kind, initial)
    with CappedRecordServer(
        rows=deepcopy(native),
        page_size=1 if style == "next" else 10,
        immutable_pages=True,
    ) as server:
        reader, _child, session = _readers(server, kind, style, path)
        try:
            before = reader.capture_pre_state()
            assert before.reachable
            if path == "serialized":
                before = EffectState.model_validate_json(before.model_dump_json())

            def change_wire_type(body: dict[str, Any], _page: int) -> None:
                records = (
                    body["records"]
                    if kind == "rest"
                    else [entry["resource"] for entry in body["entry"]]
                )
                key = "id" if kind == "rest" else "native_id"
                for row in records:
                    row[key] = str(row[key]) if initial == "integer" else int(row[key])

            server.transform_response = change_wire_type
            result = reader.verify(
                _effect(len(native), path in ("routed", "serialized")), before
            )
            assert server.snapshot() == native  # No native commit or value change.
            assert result.verdict is Verdict.INDETERMINATE
            assert result.unavailable is True
            assert "identity domain changed" in result.reason
        finally:
            session.close()


@pytest.mark.parametrize("kind", ["rest", "fhir"])
@pytest.mark.parametrize("domain", ["integer", "string"])
@pytest.mark.parametrize("style", ["total", "next"])
@pytest.mark.parametrize("path", ["config", "serialized"])
def test_same_domain_real_append_keeps_the_positive(
    kind: str, domain: str, style: str, path: str
) -> None:
    native = _native(kind, domain)
    for row in native:
        row["value"] = "prior"
    with CappedRecordServer(
        rows=deepcopy(native),
        page_size=1 if style == "next" else 10,
        immutable_pages=True,
    ) as server:
        reader, _child, session = _readers(server, kind, style, path)
        try:
            before = reader.capture_pre_state()
            assert before.reachable
            if path == "serialized":
                before = EffectState.model_validate_json(before.model_dump_json())
            added = _native(kind, domain, 3)[-1]
            server.append(added)
            result = reader.verify(_effect(1, path == "serialized"), before)
            assert server.snapshot() == [*native, added]
            assert result.verdict is Verdict.CONFIRMED
            assert result.observed_count == 1
        finally:
            session.close()


@pytest.mark.parametrize("substrate", ["rest", "fhir", "graphql"])
@pytest.mark.parametrize(
    "before,current",
    [
        ([], [{"id": 1, "value": "wanted"}]),
        ([], [{"id": "1", "value": "wanted"}]),
        ([{"value": "prior"}], [{"value": "prior"}, {"value": "wanted"}]),
    ],
)
def test_empty_baseline_and_explicit_content_identity_keep_positives(
    substrate: str, before: list[dict[str, Any]], current: list[dict[str, Any]]
) -> None:
    result = judge_records(
        _effect(1),
        EffectState(substrate=substrate, reachable=True, records=before),
        current,
        substrate=substrate,
    )
    assert result.verdict is Verdict.CONFIRMED
    assert result.observed_count == 1


@pytest.mark.parametrize(
    "current",
    [
        [{"id": True, "value": "wanted"}],
        [{"id": None, "value": "wanted"}],
        [{"id": 1.0, "value": "wanted"}],
        [{"id": "", "value": "wanted"}],
        [{"value": "wanted"}, {"id": "other", "value": "wanted"}],
    ],
)
def test_unsupported_or_mixed_identity_mode_is_not_newness(
    current: list[dict[str, Any]],
) -> None:
    result = judge_records(
        _effect(1),
        EffectState(substrate="rest", reachable=True),
        current,
        substrate="rest",
    )
    assert result.verdict is Verdict.INDETERMINATE
    assert result.unavailable


@pytest.mark.parametrize(
    "before,current",
    [
        ([{"value": "wanted"}], [{"id": 1, "value": "wanted"}]),
        ([{"id": "1", "value": "wanted"}], [{"value": "wanted"}]),
    ],
)
def test_content_vs_explicit_identity_change_refuses(
    before: list[dict[str, Any]], current: list[dict[str, Any]]
) -> None:
    result = judge_records(
        _effect(1),
        EffectState(substrate="rest", reachable=True, records=before),
        current,
        substrate="rest",
    )
    assert result.verdict is Verdict.INDETERMINATE
    assert "identity domain changed" in result.reason


def test_current_field_contradiction_does_not_depend_on_baseline_identity() -> None:
    effect = Effect.model_validate(
        {
            "kind": "field_equals",
            "match": {"entity": "target"},
            "field": "value",
            "value": "wanted",
            "timeout_s": 0,
        }
    )
    before = EffectState(
        substrate="rest",
        reachable=True,
        records=[{"id": 1, "entity": "target", "value": "wrong"}],
    )
    result = judge_records(
        effect,
        before,
        [{"id": "1", "entity": "target", "value": "wrong"}],
        substrate="rest",
    )
    assert result.verdict is Verdict.REFUTED
    assert "contradicts" in result.reason


class ReadPairDomains(RuleBasedStateMachine):
    """Independent native IDs never change during wire-only transitions."""

    def __init__(self) -> None:
        super().__init__()
        self.native = [{"id": 1, "value": "wanted"}]
        self.baseline_ids = {1}
        self.baseline_domain = "integer"
        self.before = EffectState(
            substrate="rest", reachable=True, records=deepcopy(self.native)
        )

    @rule(domain=st.sampled_from(["integer", "string"]))
    def capture(self, domain: str) -> None:
        self.baseline_ids = {row["id"] for row in self.native}
        self.baseline_domain = domain
        self.before = EffectState.model_validate_json(
            EffectState(
                substrate="rest", reachable=True, records=_wire(self.native, domain)
            ).model_dump_json()
        )

    @rule()
    def genuine_append(self) -> None:
        self.native.append({"id": len(self.native) + 1, "value": "wanted"})

    @rule(
        domain=st.sampled_from(["integer", "string"]),
        substrate=st.sampled_from(["rest", "fhir", "graphql"]),
    )
    def observe_wire(self, domain: str, substrate: str) -> None:
        current = list(reversed(_wire(self.native, domain)))
        result = judge_records(_effect(1), self.before, current, substrate=substrate)
        if domain != self.baseline_domain:
            assert result.verdict is Verdict.INDETERMINATE
            assert "identity domain changed" in result.reason
        else:
            actually_added = {row["id"] for row in self.native} - self.baseline_ids
            assert result.verdict is (
                Verdict.CONFIRMED if len(actually_added) == 1 else Verdict.REFUTED
            )

    @invariant()
    def native_identities_are_never_coerced(self) -> None:
        assert all(type(row["id"]) is int for row in self.native)
        assert len({row["id"] for row in self.native}) == len(self.native)


TestReadPairDomains = ReadPairDomains.TestCase
TestReadPairDomains.settings = settings(
    max_examples=20, stateful_step_count=15, deadline=None, database=None
)
