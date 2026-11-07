"""Generated REST reads against HTTP and an independent synthetic SQL store."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import yaml

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.record_check_synthesis import render, synthesize
from openadapt_flow.rest_setup import ReadProbe, learn_rest_read
from openadapt_flow.verify_run import VerifyRunError, parse_verify_config
from tests._record_check_fixtures import Capture, write_recording


def specification():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/objects": {
                "get": {
                    "parameters": [
                        {
                            "name": "subject",
                            "in": "query",
                            "schema": {"type": "string"},
                            "x-openadapt-equals-field": "subject",
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "items": {
                                                "type": "array",
                                                "x-openadapt-read-role": "records",
                                            },
                                            "meta": {
                                                "type": "object",
                                                "properties": {
                                                    "size": {
                                                        "type": "integer",
                                                        "x-openadapt-read-role": "matching-total",
                                                    },
                                                    "snapshot": {
                                                        "type": "string",
                                                        "x-openadapt-read-role": "immutable-snapshot-token",
                                                    },
                                                    "following": {
                                                        "type": ["string", "null"],
                                                        "format": "uri-reference",
                                                        "x-openadapt-read-role": "next-url-null-terminal",
                                                    },
                                                },
                                            },
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }


@pytest.fixture
def service(tmp_path):
    db = sqlite3.connect(tmp_path / "store.sqlite", check_same_thread=False)
    db.execute("CREATE TABLE objects(id INTEGER, subject TEXT, value TEXT)")

    class Service:
        mode = "normal"
        cap = 200
        calls = 0
        challenge_remaining = 0
        change_unrelated = False
        frozen = {}

        def seed(self, count):
            db.execute("DELETE FROM objects")
            db.executemany(
                "INSERT INTO objects VALUES(?, ?, ?)",
                [(i, f"s{i % 3}", f"v{i}") for i in range(count)],
            )
            db.commit()

        def snapshot(self):
            return [
                dict(zip(("id", "subject", "value"), row))
                for row in db.execute(
                    "SELECT id, subject, value FROM objects ORDER BY id"
                )
            ]

    state = Service()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.calls += 1
            query = parse_qs(urlsplit(self.path).query)
            rows = state.snapshot()
            if "subject" in query and state.mode != "ignore-filter":
                rows = [r for r in rows if r["subject"] == query["subject"][0]]
            start = int(query.get("cursor", ["0"])[0])
            token = query.get("snapshot", [None])[0]
            if token is None:
                token = "snapshot-" + str(state.calls)
                state.frozen[token] = copy.deepcopy(rows)
                if state.challenge_remaining and rows:
                    # Fixture-controlled write, independent of the setup learner.
                    # Change a matching tail row so a live next page exposes it.
                    changed_id = rows[-1]["id"]
                    if state.change_unrelated:
                        changed_id = next(
                            r["id"]
                            for r in state.snapshot()
                            if r["subject"] != query["subject"][0]
                        )
                    db.execute(
                        "UPDATE objects SET value = ? WHERE id = ?",
                        ("moved-" + str(state.calls), changed_id),
                    )
                    db.commit()
                    state.challenge_remaining -= 1
            if state.mode != "live-offset-constant-token":
                rows = state.frozen[token]
            page = rows[start : start + state.cap]
            following = None
            if start + state.cap < len(rows):
                following = (
                    "/objects?cursor=" + str(start + state.cap) + "&snapshot=" + token
                )
                if "subject" in query:
                    following += "&subject=" + query["subject"][0]
            payload = {
                "items": page,
                "meta": {"size": len(rows), "following": following, "snapshot": token},
            }
            if state.mode == "cap":
                payload["meta"]["following"] = None
            elif state.mode == "repeat":
                payload["meta"]["following"] = self.path
            elif state.mode == "missing-next":
                del payload["meta"]["following"]
            elif state.mode == "missing-total":
                del payload["meta"]["size"]
            elif state.mode == "bool-total":
                payload["meta"]["size"] = True
            elif state.mode == "page-total":
                payload["meta"]["size"] = len(page)
            elif state.mode == "changed-total" and start:
                payload["meta"]["size"] += 1
            elif state.mode == "malformed":
                payload["items"] = [None]
            elif state.mode == "off-origin":
                payload["meta"]["following"] = "http://unapproved.invalid/objects"
            elif state.mode == "token-cursor":
                payload["meta"]["following"] = 123
            elif state.mode == "empty":
                payload["items"] = []
                payload["meta"]["following"] = None
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        db.close()


def learn(service, **kwargs):
    service.challenge_remaining = 1
    with requests.Session() as session:
        return learn_rest_read(
            specification(),
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot, snapshot_isolation=True)],
            session=session,
            **kwargs,
        )


@pytest.mark.parametrize("count", [0, 1, 2, 200, 201])
def test_generated_contract_reads_complete_store(service, count):
    service.seed(201)
    result = learn(service)
    assert result.observations_match, result.problems
    assert result.config["records_total_path"] == "meta.size"
    assert result.config["records_next_path"] == "meta.following"
    assert result.config["records_snapshot_path"] == "meta.snapshot"
    service.seed(count)
    verifier = build_effect_verifier(EffectsConfig.model_validate(result.config))
    state = verifier.capture_pre_state()
    assert state.reachable and state.records == service.snapshot()
    assert len(state.records) == count
    assert result.evidence["probe_counts"][0]["returned"] == 201
    assert result.evidence["snapshot_isolation_observed"] is True
    assert result.evidence["specification_sha256"].startswith("sha256:")


@pytest.mark.parametrize(
    "mode",
    [
        "cap",
        "repeat",
        "missing-next",
        "missing-total",
        "bool-total",
        "page-total",
        "changed-total",
        "malformed",
        "off-origin",
        "token-cursor",
        "empty",
    ],
)
def test_bad_paging_stays_draft(service, mode):
    service.seed(201)
    service.mode = mode
    result = learn(service)
    assert not result.observations_match and result.config is None and result.problems


def test_many_capped_pages_follow_real_cursor_chain(service):
    service.seed(201)
    service.cap = 7
    result = learn(service)
    assert result.observations_match
    assert result.evidence["probe_counts"] == [
        {"expected": 201, "returned": 201, "pages": 29}
    ]


def test_no_field_name_or_short_page_implies_semantics(service):
    service.seed(1)
    spec = specification()
    del spec["paths"]["/objects"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["meta"]["properties"]["size"]["x-openadapt-read-role"]
    with requests.Session() as session:
        result = learn_rest_read(
            spec,
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot)],
            session=session,
        )
    assert not result.observations_match and service.calls == 0


@pytest.mark.parametrize("mode,valid", [("normal", True), ("ignore-filter", False)])
def test_identity_semantics_with_collisions_and_absence(service, mode, valid):
    service.seed(7)
    service.cap = 2
    service.mode = mode
    service.challenge_remaining = 1
    probes = [
        ReadProbe(service.snapshot, identity=v, snapshot_isolation=(v == "s0"))
        for v in ("s0", "s1", "absent")
    ]
    with requests.Session() as session:
        result = learn_rest_read(
            specification(),
            base_url=service.url,
            path="/objects",
            probes=probes,
            session=session,
            identity_parameter="subject",
        )
    assert result.observations_match is valid
    if valid:
        assert [r["returned"] for r in result.evidence["probe_counts"]] == [3, 2, 0]
        verifier = build_effect_verifier(
            EffectsConfig.model_validate(result.config), {"record_identity": "s0"}
        )
        assert len(verifier.capture_pre_state().records) == 3  # no first-match collapse


def test_query_name_without_equality_declaration_is_refused(service):
    service.seed(3)
    spec = specification()
    del spec["paths"]["/objects"]["get"]["parameters"][0]["x-openadapt-equals-field"]
    with requests.Session() as session:
        result = learn_rest_read(
            spec,
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot, "s0")],
            session=session,
            identity_parameter="subject",
        )
    assert not result.observations_match and service.calls == 0


def test_store_movement_is_not_http_validation(service):
    service.seed(1)
    calls = 0

    def moving_snapshot():
        nonlocal calls
        calls += 1
        if calls == 2:
            service.seed(2)
        return service.snapshot()

    with requests.Session() as session:
        result = learn_rest_read(
            specification(),
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(moving_snapshot)],
            session=session,
        )
    assert not result.observations_match


def test_synthesis_keeps_unbound_generated_data_detached_and_preserves_draft_gates(
    service, tmp_path
):
    service.seed(201)
    setup = learn(service)
    service.seed(1)
    recording = write_recording(tmp_path / "rec", {"value": "v0"})
    capture = Capture()
    capture.get(service.url + "/objects", 3.0)
    capture.write(recording)
    proposal = synthesize(recording, rest_setups=[setup])
    configs = proposal.verifier["surfaces"]
    assert all("records_total_path" not in c for c in configs.values())
    assert all("records_snapshot_path" not in c for c in configs.values())
    assert proposal.proof()["rest_setup_observations"] == [
        {"evidence": setup.evidence, "problems": []}
    ]
    assert setup.evidence["read_context"] == "revalidation-required"
    assert any("read-context revalidation" in m for m in proposal.misses)
    path = tmp_path / "proposed.yaml"
    path.write_text(render(proposal))
    with pytest.raises(VerifyRunError, match="unconfirmed placeholder"):
        parse_verify_config(yaml.safe_load(path.read_text()))
    # Read completeness grants no collateral scope or operator authority.
    assert not proposal.working
    assert not proposal.proof()["approved"]
    assert any("collateral" in m for m in proposal.misses)
    changed = copy.deepcopy(setup)
    changed.config["records_total_path"] = "other"
    refused = synthesize(recording, rest_setups=[changed])
    assert any("changed after observation" in m for m in refused.misses)
    assert not refused.working


def test_declared_read_matches_independent_multiset_across_page_partitions(service):
    from hypothesis import given, settings
    from hypothesis import strategies as st

    @settings(max_examples=16, deadline=None, derandomize=True)
    @given(count=st.integers(0, 201), cap=st.integers(5, 200), truncated=st.booleans())
    def check(count, cap, truncated):
        service.seed(201)
        service.cap = cap
        service.mode = "normal"
        result = learn(service)
        assert result.observations_match
        service.seed(count)
        service.mode = "cap" if truncated else "normal"
        actual = build_effect_verifier(
            EffectsConfig.model_validate(result.config)
        ).capture_pre_state()
        assert actual.reachable is (not truncated or count <= cap)
        if actual.reachable:
            assert actual.records == service.snapshot()

    check()


def test_identity_validation_requires_absent_and_multiple_present_challenges(service):
    service.seed(3)
    with requests.Session() as session:
        result = learn_rest_read(
            specification(),
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot, "s0")],
            session=session,
            identity_parameter="subject",
        )
    assert not result.observations_match and result.config is None


def test_total_only_contract_can_validate_an_unpaged_response(service):
    service.seed(201)
    service.cap = 1000
    spec = specification()
    del spec["paths"]["/objects"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["meta"]["properties"]["following"]
    with requests.Session() as session:
        result = learn_rest_read(
            spec,
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot)],
            session=session,
        )
    assert result.observations_match
    assert "records_next_path" not in result.config
    assert result.evidence["probe_counts"] == [
        {"expected": 201, "returned": 201, "pages": 1}
    ]


def test_constant_token_on_live_offset_pages_does_not_validate_isolation(service):
    service.seed(201)
    service.cap = 7
    service.mode = "live-offset-constant-token"
    result = learn(service)
    assert not result.observations_match and result.config is None
    assert service.calls > 1
    assert service.snapshot()[-1]["value"].startswith("moved-")


def test_stable_pages_without_an_isolation_challenge_stay_draft(service):
    service.seed(201)
    with requests.Session() as session:
        result = learn_rest_read(
            specification(),
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot)],
            session=session,
        )
    assert not result.observations_match and result.config is None
    assert service.calls > 1


def test_isolation_flag_without_actual_store_movement_stays_draft(service):
    service.seed(201)
    with requests.Session() as session:
        result = learn_rest_read(
            specification(),
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot, snapshot_isolation=True)],
            session=session,
        )
    assert not result.observations_match and result.config is None
    assert service.calls > 1


def test_next_and_total_without_snapshot_semantics_stay_draft(service):
    service.seed(201)
    spec = specification()
    props = spec["paths"]["/objects"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["meta"]["properties"]
    del props["snapshot"]["x-openadapt-read-role"]
    with requests.Session() as session:
        result = learn_rest_read(
            spec,
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot, snapshot_isolation=True)],
            session=session,
        )
    assert not result.observations_match and result.config is None
    assert service.calls == 0


def test_runtime_without_snapshot_contract_cannot_accept_paged_setup(
    service, monkeypatch
):
    service.seed(201)
    monkeypatch.delitem(
        EffectsConfig.model_fields, "records_snapshot_path", raising=False
    )
    result = learn(service)
    assert not result.observations_match and result.config is None
    assert service.calls == 0


@pytest.mark.parametrize("identity", [True, False, 42, {}, ["s0"]])
def test_identity_challenges_are_strict_strings(service, identity):
    service.seed(3)
    spec = specification()
    props = spec["paths"]["/objects"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["meta"]["properties"]
    del props["following"]
    with requests.Session() as session:
        result = learn_rest_read(
            spec,
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot, identity)],
            session=session,
            identity_parameter="subject",
        )
    assert not result.observations_match and result.config is None
    assert service.calls == 0


def test_unrelated_store_change_cannot_validate_filtered_snapshot_isolation(service):
    service.seed(201)
    service.cap = 7
    service.challenge_remaining = 1
    service.change_unrelated = True
    with requests.Session() as session:
        result = learn_rest_read(
            specification(),
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot, "s0", snapshot_isolation=True)],
            session=session,
            identity_parameter="subject",
        )
    assert not result.observations_match and result.config is None
    assert service.calls > 1


def test_snapshot_token_must_match_its_declared_schema_type(service):
    service.seed(201)
    service.challenge_remaining = 1
    spec = specification()
    props = spec["paths"]["/objects"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["meta"]["properties"]
    props["snapshot"]["type"] = "integer"
    with requests.Session() as session:
        result = learn_rest_read(
            spec,
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot, snapshot_isolation=True)],
            session=session,
        )
    assert not result.observations_match and result.config is None
    assert service.calls == 1


@pytest.mark.parametrize(
    "auth",
    [None, {"bearer_env": "SYNTHETIC_READER_A"}, {"bearer_env": "SYNTHETIC_READER_B"}],
)
def test_detached_setup_never_rebinds_a_reader_from_endpoint_or_auth_names(
    service, auth
):
    from openadapt_flow.record_check_synthesis import ReadSearch, Surface
    from openadapt_flow.rest_setup import apply_rest_setups

    service.seed(201)
    setup = learn(service)
    assert setup.observations_match
    original_observations = copy.deepcopy(setup.evidence)
    config = {
        "kind": "rest",
        "base_url": service.url,
        "records_path": "/objects",
        "records_key": "items",
    }
    if auth is not None:
        config["auth"] = auth
    before = copy.deepcopy(config)
    surface = Surface("recorded", config, [], [], False, "synthetic recorded read")
    search = ReadSearch(surfaces=[surface])
    apply_rest_setups(search, [setup])
    # A matching endpoint, absent auth, or a named auth reference cannot bind
    # the previous session. No credentials or imagined principal are supplied.
    assert surface.config == before
    assert any("read-context revalidation" in problem for problem in search.problems)
    assert search.rest_setup_observations == [
        {"evidence": original_observations, "problems": []}
    ]
    assert setup.evidence == original_observations
    setup.evidence["probe_counts"].clear()
    assert search.rest_setup_observations[0]["evidence"] == original_observations
