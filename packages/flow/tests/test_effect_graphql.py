"""GraphQL read-back adapter: contract + adversarial qualification fixtures.

Adversarial coverage (the platform's per-adapter qualification set): stale
data -> STALE not pass; wrong entity returned -> REFUTED; duplicate rows ->
CONFLICTING; settlement timeout -> failure; credential failure (HTTP 401 AND
the GraphQL-style HTTP-200 ``errors`` body) -> UNAVAILABLE, never a pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import (
    AdapterResult,
    Effect,
    EffectKind,
    GraphQLRecordVerifier,
    Verdict,
    assert_read_only_graphql,
    classify_adapter_result,
)
from openadapt_flow.runtime.effects.graphql import extract_records_path

QUERY = "query Claims($claim: ID!) { claims(id: $claim) { nodes { id claim_id } } }"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append((url, json, headers))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def body(nodes):
    return {"data": {"claims": {"nodes": nodes}}}


def make_verifier(responses, **kwargs):
    kwargs.setdefault("records_path", "data.claims.nodes")
    return GraphQLRecordVerifier(
        "http://sor/graphql",
        query=QUERY,
        session=FakeSession(responses),
        poll_interval_s=0.0,
        **kwargs,
    )


def claim_effect(**kwargs):
    kwargs.setdefault("match", {"claim_id": "c-77"})
    kwargs.setdefault("timeout_s", 0.0)
    return Effect(kind=EffectKind.RECORD_WRITTEN, **kwargs)


class TestReadOnlyGuard:
    def test_mutation_refuses_to_construct(self):
        with pytest.raises(ValueError, match="READ-ONLY"):
            assert_read_only_graphql("mutation { createClaim { id } }")

    def test_subscription_refuses_to_construct(self):
        with pytest.raises(ValueError, match="READ-ONLY"):
            GraphQLRecordVerifier("http://x", query="subscription { claims { id } }")

    @pytest.mark.parametrize(
        "document",
        [
            "# harmless comment\nmutation { createClaim { id } }",
            "query Read { claims { id } } mutation Write { createClaim { id } }",
            "fragment Fields on Claim { id } subscription { claims { id } }",
        ],
    )
    def test_non_read_operation_anywhere_refuses(self, document):
        with pytest.raises(ValueError, match="READ-ONLY"):
            assert_read_only_graphql(document)

    def test_forbidden_word_inside_selection_or_string_is_not_an_operation(self):
        assert_read_only_graphql(
            'query Read($mutation: String) { mutation(value: "subscription") }'
        )

    def test_empty_query_refuses(self):
        with pytest.raises(ValueError, match="non-empty"):
            GraphQLRecordVerifier("http://x", query="  ")

    def test_query_and_bare_selection_accepted(self):
        assert_read_only_graphql(QUERY)
        assert_read_only_graphql("{ claims { id } }")


class TestExtraction:
    def test_dotted_path(self):
        assert extract_records_path(body([{"id": 1}]), "data.claims.nodes") == [
            {"id": 1}
        ]

    def test_missing_path_is_unreadable(self):
        assert extract_records_path({"data": {}}, "data.claims.nodes") is None

    def test_non_list_destination_is_unreadable(self):
        assert extract_records_path({"data": {"claims": {}}}, "data.claims") is None

    def test_non_object_rows_are_unreadable(self):
        assert extract_records_path({"data": [1, 2]}, "data") is None


class TestVerdicts:
    def test_confirmed_exactly_one(self):
        verifier = make_verifier([FakeResponse(200, body([]))])
        before = verifier.capture_pre_state()
        assert before.reachable is True
        verifier._session.responses = [
            FakeResponse(200, body([{"id": 1, "claim_id": "c-77"}]))
        ]
        verdict = verifier.verify(claim_effect(), before)
        assert verdict.verdict is Verdict.CONFIRMED

    def test_wrong_entity_returned_refutes(self):
        # The read succeeds but returns a DIFFERENT claim: the selector must
        # not match, and the effect REFUTEs (missing write for OUR entity).
        verifier = make_verifier(
            [FakeResponse(200, body([{"id": 2, "claim_id": "c-99"}]))]
        )
        before = verifier.capture_pre_state()
        verdict = verifier.verify(claim_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 0
        assert classify_adapter_result(verdict) is AdapterResult.REFUTED

    def test_duplicate_rows_are_conflicting(self):
        rows = [
            {"id": 1, "claim_id": "c-77"},
            {"id": 2, "claim_id": "c-77"},
        ]
        verifier = make_verifier([FakeResponse(200, body(rows))])
        before = verifier.capture_pre_state()
        # count_new_only: both rows are NEW relative to an empty baseline.
        before = before.model_copy(update={"records": []})
        verdict = verifier.verify(claim_effect(count_new_only=True), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 2
        assert classify_adapter_result(verdict) is AdapterResult.CONFLICTING

    def test_settlement_timeout_never_passes(self):
        verifier = make_verifier([FakeResponse(200, body([]))])
        before = verifier.capture_pre_state()
        verdict = verifier.verify(claim_effect(timeout_s=0.0), before)
        assert verdict.should_halt is True

    def test_http_401_credential_failure_is_unavailable(self):
        verifier = make_verifier([FakeResponse(401, {"error": "unauthorized"})])
        before = verifier.capture_pre_state()
        assert before.reachable is False
        verdict = verifier.verify(claim_effect(), before)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert classify_adapter_result(verdict) is AdapterResult.UNAVAILABLE

    def test_graphql_errors_with_http_200_is_unavailable(self):
        # GraphQL servers commonly return auth/validation failures as an
        # `errors` array with HTTP 200 -- that must read as unreachable,
        # never as "zero records" (which would REFUTE-as-absent wrongly).
        verifier = make_verifier(
            [FakeResponse(200, {"errors": [{"message": "not authorized"}]})]
        )
        assert verifier._fetch_records() is None
        before = verifier.capture_pre_state()
        verdict = verifier.verify(claim_effect(), before)
        assert classify_adapter_result(verdict) is AdapterResult.UNAVAILABLE

    def test_transport_exception_is_unavailable(self):
        class ExplodingSession:
            def post(self, *args, **kwargs):
                raise OSError("connection refused")

        verifier = GraphQLRecordVerifier(
            "http://sor/graphql", query=QUERY, session=ExplodingSession()
        )
        assert verifier._fetch_records() is None

    def test_stale_data_is_stale_not_pass(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        rows = [{"id": 1, "claim_id": "c-77", "updated_at": old}]
        verifier = make_verifier(
            [FakeResponse(200, body(rows))],
            freshness_field="updated_at",
            freshness_window_s=60.0,
        )
        before = verifier.capture_pre_state().model_copy(update={"records": []})
        verdict = verifier.verify(claim_effect(), before)
        assert verdict.stale is True
        assert classify_adapter_result(verdict) is AdapterResult.STALE

    def test_fresh_data_confirms(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = [{"id": 1, "claim_id": "c-77", "updated_at": now}]
        verifier = make_verifier(
            [FakeResponse(200, body(rows))],
            freshness_field="updated_at",
            freshness_window_s=3600.0,
        )
        before = verifier.capture_pre_state().model_copy(update={"records": []})
        verdict = verifier.verify(claim_effect(), before)
        assert verdict.verdict is Verdict.CONFIRMED


class TestConfigBinding:
    def test_variables_bind_run_params(self):
        cfg = EffectsConfig(
            kind="graphql",
            base_url="http://sor/graphql",
            graphql_query=QUERY,
            graphql_variables={"claim": {"param": "claim_id"}, "tenant": "t-1"},
            graphql_records_path="data.claims.nodes",
        )
        verifier = build_effect_verifier(cfg, params={"claim_id": "c-77"})
        assert isinstance(verifier, GraphQLRecordVerifier)
        assert verifier.variables == {"claim": "c-77", "tenant": "t-1"}

    def test_unresolved_param_fails_loud(self):
        cfg = EffectsConfig(
            kind="graphql",
            base_url="http://sor/graphql",
            graphql_query=QUERY,
            graphql_variables={"claim": {"param": "claim_id"}},
        )
        with pytest.raises(ValueError):
            build_effect_verifier(cfg, params={})

    def test_auth_headers_are_secret_isolated(self, monkeypatch):
        monkeypatch.setenv("GQL_TOKEN", "tok")
        cfg = EffectsConfig(
            kind="graphql",
            base_url="http://sor/graphql",
            graphql_query=QUERY,
            auth={"bearer_env": "GQL_TOKEN"},
        )
        verifier = build_effect_verifier(cfg)
        assert verifier.headers == {"Authorization": "Bearer tok"}

    def test_missing_query_fails_loud(self):
        with pytest.raises(ValueError, match="graphql_query"):
            build_effect_verifier(
                EffectsConfig(kind="graphql", base_url="http://sor/graphql")
            )
