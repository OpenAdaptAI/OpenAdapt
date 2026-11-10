"""Native identity-domain coverage against actual immutable HTTP views."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.runtime.effects.effect import Effect, Verdict
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from tests.e2e.rest_page_cap_server import CappedRecordServer


def reader(server: CappedRecordServer) -> RestRecordVerifier:
    return RestRecordVerifier(
        server.url,
        records_path="/records",
        records_key="records",
        next_path="page.next",
        total_path="page.total",
        snapshot_path="page.snapshot",
        timeout_s=1,
    )


def expected() -> Effect:
    return Effect.model_validate(
        {
            "kind": "exact_new_set",
            "match": {},
            "new_records": [{"value": "wanted"}] * 3,
            "expected_count": 3,
            "identity_field": "id",
            "timeout_s": 0,
        }
    )


def test_same_frozen_view_cannot_count_one_native_id_as_string_and_integer() -> None:
    rows = [
        {"id": 1, "value": "wanted"},
        {"id": 2, "value": "wanted"},
        {"id": 3, "value": "wrong"},
    ]

    def change_wire_type(body: dict[str, Any], page: int) -> None:
        if page == 1:
            body["records"][0]["id"] = str(body["records"][0]["id"])

    with CappedRecordServer(page_size=1, immutable_pages=True) as server:
        verifier = reader(server)
        try:
            before = verifier.capture_pre_state()
            assert before.reachable and before.readback_complete is True
            server.replace(rows)
            server.repeat_boundary = True
            server.transform_response = change_wire_type
            result = verifier.verify(expected(), before)
            assert server.snapshot() == rows
            assert sum(row["value"] == "wanted" for row in server.snapshot()) == 2
            assert result.verdict is Verdict.INDETERMINATE
            assert result.readback_complete is False
        finally:
            verifier._get_session().close()


@pytest.mark.parametrize("domain", ["integer", "string"])
def test_homogeneous_native_ids_remain_a_complete_positive(domain: str) -> None:
    rows = [
        {"id": i if domain == "integer" else str(i), "value": "wanted"}
        for i in range(3)
    ]
    with CappedRecordServer(page_size=1, immutable_pages=True) as server:
        verifier = reader(server)
        try:
            before = verifier.capture_pre_state()
            assert before.reachable
            server.replace(rows)
            result = verifier.verify(expected(), before)
            assert server.snapshot() == rows
            assert result.verdict is Verdict.CONFIRMED
            assert result.observed_count == 3 and result.readback_complete is True
        finally:
            verifier._get_session().close()


@pytest.mark.parametrize("page_size", [1, 2, 3])
@given(order=st.permutations((0, 1, 2)))
@settings(max_examples=6, database=None, deadline=None)
def test_mixed_identity_domain_refusal_is_permutation_and_page_invariant(
    page_size: int, order: Sequence[int]
) -> None:
    # These are fixture-native identities, not a guessed coercion between them.
    native = [
        {"id": 1, "value": "wanted"},
        {"id": "2", "value": "wanted"},
        {"id": 3, "value": "wanted"},
    ]
    rows = [native[index] for index in order]
    with CappedRecordServer(page_size=page_size, immutable_pages=True) as server:
        verifier = reader(server)
        try:
            before = verifier.capture_pre_state()
            assert before.reachable and before.readback_complete is True
            server.replace(rows)
            result = verifier.verify(expected(), before)
            assert server.snapshot() == rows
            assert result.verdict is Verdict.INDETERMINATE
            assert result.readback_complete is False
        finally:
            verifier._get_session().close()
