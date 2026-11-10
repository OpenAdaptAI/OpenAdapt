"""Controlled writer exclusion plus real HTTP and an independent SQLite store."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.fhir_setup import FhirReadContext, learn_fhir_read, search_resources
from openadapt_flow.rest_setup import ReadProbe
from openadapt_flow.runtime.effects.quiescence import (
    QuiescenceAuthority,
    QuiescenceBinding,
    QuiescenceCompletion,
    QuiescenceLease,
    read_scope_sha256,
)


@pytest.fixture
def store(tmp_path):
    db = sqlite3.connect(tmp_path / "synthetic.sqlite", check_same_thread=False)
    db.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY, value TEXT)")

    class Store:
        mode = "normal"
        protocol = "fhir"
        calls = 0
        paths = []
        cap = 200
        lock = threading.RLock()
        pending = False
        lost = False
        active = False
        owner = None
        checks = 0

        def seed(self, count):
            with self.lock:
                db.execute("DELETE FROM rows")
                db.executemany(
                    "INSERT INTO rows VALUES(?, ?)",
                    [(i, "v" + str(i)) for i in range(count)],
                )
                db.commit()

        def snapshot(self):
            return [
                {"id": str(i), "value": v}
                for i, v in db.execute("SELECT id,value FROM rows ORDER BY id")
            ]

    state = Store()
    state.paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.calls += 1
            parsed = urlsplit(self.path)
            state.paths.append(parsed.path)
            query = parse_qs(parsed.query)
            cursor = int(query.get("cursor", ["0"])[0])
            resource_type = parsed.path.strip("/")
            rows = state.snapshot()
            if resource_type != "Observation":
                rows = [{"id": "other", "value": "unrelated"}]
            if self.headers.get("X-Synthetic-View") == "restricted":
                rows = rows[:1]
            if state.mode == "writer-pending" and cursor:
                # A real second-thread write cannot take the held writer lock.
                acquired = state.lock.acquire(blocking=False)
                state.pending = not acquired
                if acquired:
                    state.lock.release()
            if state.mode == "lease-lost" and cursor:
                state.lost = True
            page = copy.deepcopy(rows[cursor : cursor + state.cap])
            if (
                state.mode == "changed-pass"
                and state.calls > (len(rows) + state.cap - 1) // state.cap
            ):
                for row in page:
                    row["value"] = "changed-representation"
            if state.mode == "duplicate" and cursor and page:
                page[0]["id"] = rows[0]["id"]
            body = {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(rows),
                "entry": [
                    {
                        "resource": {
                            "resourceType": resource_type,
                            "id": row["id"],
                            "valueString": row["value"],
                        },
                        "search": {"mode": "match"},
                    }
                    for row in page
                ],
            }
            if cursor + state.cap < len(rows):
                path = resource_type if state.mode != "other-endpoint" else "export"
                body["link"] = [
                    {
                        "relation": "next",
                        "url": state.url
                        + "/"
                        + path
                        + "?cursor="
                        + str(cursor + state.cap),
                    }
                ]
            if state.mode == "missing-total":
                del body["total"]
            elif state.mode == "cap":
                body.pop("link", None)
            if state.protocol == "rest":
                body = {
                    "items": page,
                    "meta": {
                        "size": len(rows),
                        "following": (
                            state.url + "/Observation?cursor=" + str(cursor + state.cap)
                        )
                        if cursor + state.cap < len(rows)
                        else None,
                    },
                }
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        db.close()


class LockedAuthority(QuiescenceAuthority):
    def __init__(self, store):
        self.store = store
        self.contracts = []
        self.generation = 0
        self.current_binding = None
        self.completion_mode = "VALID"
        self.change_after_calls = None

    @contextmanager
    def hold(self, read_contract):
        state = self.store
        assert read_contract["base_url"] == state.url
        self.contracts.append(copy.deepcopy(read_contract))
        with state.lock:
            assert not state.active
            state.active, state.owner = True, threading.get_ident()
            self.generation += 1
            binding = QuiescenceBinding(
                read_scope_sha256(read_contract), "synthetic-window", self.generation
            )
            self.current_binding = binding

            class Lease(QuiescenceLease):
                def binding(self):
                    return binding

                def assert_held(self):
                    state.checks += 1
                    if (
                        not state.active
                        or state.owner != threading.get_ident()
                        or state.pending
                        or state.lost
                    ):
                        raise ValueError("actual exclusion unavailable")

            try:
                yield Lease()
            finally:
                state.active, state.owner = False, None
                self.current_binding = None

    def completion(self):
        binding = self.current_binding
        assert binding is not None and self.store.active
        mode = self.completion_mode
        if (
            self.change_after_calls is not None
            and self.store.calls < self.change_after_calls
        ):
            mode = "VALID"
        if mode == "object":
            return {"state": "VALID", "binding": binding}
        if mode == "scope":
            binding = QuiescenceBinding(
                "sha256:" + "0" * 64, binding.window_ref, binding.generation
            )
        elif mode == "window":
            binding = QuiescenceBinding(
                binding.scope_sha256, "other-window", binding.generation
            )
        elif mode == "generation":
            binding = QuiescenceBinding(
                binding.scope_sha256, binding.window_ref, binding.generation + 1
            )
        elif mode == "bool-generation":
            binding = QuiescenceBinding(binding.scope_sha256, binding.window_ref, True)
        elif mode == "newline-scope":
            binding = QuiescenceBinding(
                binding.scope_sha256 + "\n", binding.window_ref, binding.generation
            )
        elif mode == "newline-window":
            binding = QuiescenceBinding(
                binding.scope_sha256, binding.window_ref + "\n", binding.generation
            )
        elif mode == "lost-during-completion":
            self.store.lost = True
        state = mode if mode in {"PENDING", "INVALID", "UNKNOWN", "valid"} else "VALID"
        return QuiescenceCompletion(state, binding)


def capability(store, resources=("Observation",)):
    return {
        "resourceType": "CapabilityStatement",
        "kind": "instance",
        "fhirVersion": "4.0.1",
        "implementation": {"url": store.url},
        "rest": [
            {
                "mode": "server",
                "resource": [
                    {
                        "type": name,
                        "interaction": [{"code": "read"}, {"code": "search-type"}],
                    }
                    for name in resources
                ],
            }
        ],
    }


def learn(store, authority, client, caps=None):
    return learn_fhir_read(
        caps or capability(store),
        base_url=store.url,
        field_paths={"id": "id", "value": "valueString"},
        probes=[ReadProbe(store.snapshot)],
        session=client,
        quiescence=authority,
    )


@pytest.mark.parametrize("count", [0, 1, 2, 200, 201])
def test_two_complete_passes_under_actual_writer_lock(store, count):
    store.seed(count)
    authority = LockedAuthority(store)
    with httpx.Client(trust_env=False) as client:
        setup = learn(store, authority, client)
        assert setup.observations_match, setup.problems
        assert setup.config["records_consistency"] == "quiescent-two-pass"
        assert "records_complete" not in setup.config
        assert store.calls == 2 * max(1, (count + 199) // 200)
        assert store.checks >= 4 and not store.active
        runtime = build_effect_verifier(
            EffectsConfig.model_validate(setup.config),
            session=client,
            quiescence=authority,
        )
        state = runtime.capture_pre_state()
        assert state.reachable and state.readback_complete is True
        assert state.records == store.snapshot()
        with pytest.raises(ValueError, match="writer-exclusion authority"):
            build_effect_verifier(EffectsConfig.model_validate(setup.config))


@pytest.mark.parametrize(
    "mode",
    [
        "writer-pending",
        "lease-lost",
        "changed-pass",
        "duplicate",
        "missing-total",
        "cap",
        "other-endpoint",
    ],
)
def test_ordinary_reader_refuses_lost_exclusion_or_incomplete_pass(store, mode):
    store.seed(201)
    authority = LockedAuthority(store)
    with httpx.Client(trust_env=False) as client:
        setup = learn(store, authority, client)
        assert setup.observations_match
        store.mode = mode
        store.calls = 0
        store.paths.clear()
        runtime = build_effect_verifier(
            EffectsConfig.model_validate(setup.config),
            session=client,
            quiescence=authority,
        )
        assert not runtime.capture_pre_state().reachable
        assert not store.active
        if mode == "other-endpoint":
            assert store.paths == ["/Observation"]
        refused = learn(store, authority, client)
        assert not refused.observations_match and refused.config is None


def test_two_equal_snapshots_and_candidate_flags_supply_no_authority(store):
    store.seed(2)
    with httpx.Client(trust_env=False) as client:
        for missing in (None, True, {"verified": True}):
            setup = learn(store, missing, client)
            assert not setup.observations_match and setup.config is None
    assert store.calls == 0


def test_resource_discovery_tries_all_declared_candidates_not_application_names(store):
    store.seed(2)
    authority = LockedAuthority(store)
    with httpx.Client(trust_env=False) as client:
        setup = learn(
            store, authority, client, capability(store, ("Encounter", "Observation"))
        )
    assert setup.observations_match
    assert setup.evidence["candidate_resources"] == ["Encounter", "Observation"]
    assert setup.config["resource_type"] == "Observation"
    assert set(store.paths) == {"/Encounter", "/Observation"}


def test_restricted_patient_view_cannot_qualify_population_read(store):
    store.seed(2)
    with httpx.Client(
        headers={"X-Synthetic-View": "restricted"}, trust_env=False
    ) as client:
        setup = learn(store, LockedAuthority(store), client)
    assert not setup.observations_match and setup.config is None


@pytest.mark.parametrize(
    "change", ["version", "base", "client-mode", "no-search", "duplicate"]
)
def test_unsupported_capability_is_refused_before_read(store, change):
    caps = capability(store)
    if change == "version":
        caps["fhirVersion"] = "5.0.0"
    elif change == "base":
        caps["implementation"]["url"] += "/another-site"
    elif change == "client-mode":
        caps["rest"][0]["mode"] = "client"
    elif change == "no-search":
        caps["rest"][0]["resource"][0]["interaction"] = [{"code": "read"}]
    else:
        caps["rest"][0]["resource"] *= 2
    with pytest.raises(ValueError):
        search_resources(caps, store.url)
    with httpx.Client(trust_env=False) as client:
        assert not learn(store, LockedAuthority(store), client, caps).observations_match
    assert store.calls == 0


def test_protocol_context_uses_same_client_and_remains_unusable_when_detached(store):
    store.seed(2)
    authority = LockedAuthority(store)
    with httpx.Client(trust_env=False) as client:
        setup = learn(store, authority, client)
    assert setup.observations_match
    cfg = EffectsConfig.model_validate(
        {**setup.config, "records_read_context": "revalidate"}
    )
    clients = []

    def factory():
        client = httpx.Client(trust_env=False)
        clients.append(client)
        return client

    with FhirReadContext(
        capability(store),
        store.url,
        "",
        factory,
        lambda: [ReadProbe(store.snapshot)],
        quiescence=authority,
        field_paths={"id": "id", "value": "valueString"},
    ) as current:
        runtime = build_effect_verifier(cfg, read_context=current)
        assert len(clients) == 1
        assert runtime.capture_pre_state().records == store.snapshot()
    assert not runtime.capture_pre_state().reachable
    with pytest.raises(
        ValueError, match="^quiescent read requires current writer-exclusion authority$"
    ):
        build_effect_verifier(cfg)
    with pytest.raises(ValueError, match="requires current read-context"):
        build_effect_verifier(cfg, quiescence=authority)


def test_rest_guarded_promotion_reaches_actual_synthesis_read_check(store):
    import requests

    from openadapt_flow.record_check_synthesis import (
        ReadSearch,
        Surface,
        check_reads,
        verifier_section,
    )
    from openadapt_flow.rest_setup import (
        RestReadContext,
        apply_rest_setups,
        learn_rest_read,
    )
    from tests.test_rest_setup import specification

    store.seed(201)
    store.protocol = "rest"
    spec = specification()
    spec["paths"]["/Observation"] = spec["paths"].pop("/objects")
    props = spec["paths"]["/Observation"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["meta"]["properties"]
    del props["snapshot"]
    authority = LockedAuthority(store)
    with requests.Session() as client:
        setup = learn_rest_read(
            spec,
            base_url=store.url,
            path="/Observation",
            probes=[ReadProbe(store.snapshot)],
            session=client,
            consistency="quiescent-two-pass",
            quiescence=authority,
        )
        assert setup.observations_match, setup.problems
        refused = learn_rest_read(
            spec,
            base_url=store.url,
            path="/Observation",
            probes=[ReadProbe(store.snapshot)],
            session=client,
        )
        assert not refused.observations_match and refused.config is None
    original = {
        key: setup.config[key]
        for key in ("kind", "base_url", "records_path", "records_key")
    }
    search = ReadSearch(
        surfaces=[
            Surface("read", original, store.snapshot(), [], True, "synthetic capture")
        ]
    )
    with RestReadContext(
        spec,
        store.url,
        "/Observation",
        requests.Session,
        lambda: [ReadProbe(store.snapshot)],
        consistency="quiescent-two-pass",
        quiescence=authority,
    ) as current:
        apply_rest_setups(search, [setup], contexts={"read": current})
        assert not search.problems
        section = verifier_section(search.surfaces)
        assert not check_reads(
            search.surfaces, section, {}, read_contexts=search.rest_read_contexts
        )
        verifier = build_effect_verifier(
            EffectsConfig.model_validate(section),
            read_context=search.rest_read_contexts,
        )
        assert verifier.capture_pre_state().reachable
        with pytest.raises(
            ValueError,
            match="^quiescent read requires current writer-exclusion authority$",
        ):
            build_effect_verifier(EffectsConfig.model_validate(section))
        with pytest.raises(ValueError, match="requires current read-context"):
            build_effect_verifier(
                EffectsConfig.model_validate(section),
                quiescence={name: authority for name in section["surfaces"]},
            )
    assert not verifier.capture_pre_state().reachable
    assert authority.contracts and store.checks > 0


@pytest.mark.parametrize("protocol", ["rest", "fhir"])
@pytest.mark.parametrize(
    "completion",
    [
        "PENDING",
        "INVALID",
        "UNKNOWN",
        "valid",
        "object",
        "scope",
        "window",
        "generation",
        "bool-generation",
        "newline-scope",
        "newline-window",
        "lost-during-completion",
    ],
)
def test_completion_must_be_valid_for_exact_held_window(store, protocol, completion):
    store.seed(201)
    authority = LockedAuthority(store)
    with httpx.Client(trust_env=False) as client:
        setup = learn(store, authority, client)
        assert setup.observations_match
        config = setup.config
        if protocol == "rest":
            store.protocol = "rest"
            config = {
                "kind": "rest",
                "base_url": store.url,
                "records_path": "/Observation",
                "records_key": "items",
                "records_total_path": "meta.size",
                "records_next_path": "meta.following",
                "records_consistency": "quiescent-two-pass",
            }
        runtime = build_effect_verifier(
            EffectsConfig.model_validate(config), session=client, quiescence=authority
        )
        positive = runtime.capture_pre_state()
        assert positive.reachable and positive.readback_complete is True
        authority.completion_mode = completion
        store.calls = 0
        refused = runtime.capture_pre_state()
        assert not refused.reachable and refused.readback_complete is not True
        assert store.calls == 0 and not store.active


@pytest.mark.parametrize("protocol", ["rest", "fhir"])
@pytest.mark.parametrize("completion", ["PENDING", "INVALID", "generation", "window"])
def test_completion_change_during_pages_never_finishes_complete(
    store, protocol, completion
):
    store.seed(201)
    authority = LockedAuthority(store)
    with httpx.Client(trust_env=False) as client:
        setup = learn(store, authority, client)
        assert setup.observations_match
        config = setup.config
        if protocol == "rest":
            store.protocol = "rest"
            config = {
                "kind": "rest",
                "base_url": store.url,
                "records_path": "/Observation",
                "records_key": "items",
                "records_total_path": "meta.size",
                "records_next_path": "meta.following",
                "records_consistency": "quiescent-two-pass",
            }
        runtime = build_effect_verifier(
            EffectsConfig.model_validate(config), session=client, quiescence=authority
        )
        authority.completion_mode = completion
        authority.change_after_calls = 1
        store.calls = 0
        refused = runtime.capture_pre_state()
        assert not refused.reachable and refused.readback_complete is not True
        assert store.calls == 1 and not store.active


def test_missing_normal_verifier_cannot_promote_a_candidate(store, monkeypatch):
    import openadapt_flow.fhir_setup as module

    store.seed(2)
    authority = LockedAuthority(store)
    with httpx.Client(trust_env=False) as client:
        positive = learn(store, authority, client)
        assert positive.observations_match
        store.calls = 0
        monkeypatch.setattr(
            module, "build_effect_verifier", lambda *args, **kwargs: None
        )
        refused = learn(store, authority, client)
        assert not refused.observations_match and refused.config is None
        assert refused.problems and store.calls == 0
