"""Actual target sessions and independent SQLite projections, without app data."""

from __future__ import annotations

import copy

import httpx
import pytest
import requests

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.record_check_synthesis import (
    ReadSearch,
    Surface,
    check_reads,
    verifier_section,
)
from openadapt_flow.rest_setup import (
    ReadProbe,
    RestReadContext,
    apply_rest_setups,
    learn_rest_read,
)
from tests.test_rest_setup import service as service
from tests.test_rest_setup import specification


def unpaged_spec():
    spec = specification()
    props = spec["paths"]["/objects"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["meta"]["properties"]
    del props["following"]
    del props["snapshot"]
    return spec


def observed(service, spec):
    with requests.Session() as client:
        setup = learn_rest_read(
            spec,
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot)],
            session=client,
        )
    assert setup.observations_match, setup.problems
    return setup


def context(service, spec, factory):
    return RestReadContext(
        spec, service.url, "/objects", factory, lambda: [ReadProbe(service.snapshot)]
    )


@pytest.mark.parametrize("transport", ["requests", "httpx"])
def test_actual_target_factory_is_same_client_through_consuming_read(
    service, transport
):
    service.seed(3)
    spec = unpaged_spec()
    setup = observed(service, spec)
    clients = []

    def factory():
        client = (
            requests.Session()
            if transport == "requests"
            else httpx.Client(trust_env=False)
        )
        clients.append(client)
        return client

    cfg = EffectsConfig.model_validate(
        {**setup.config, "records_read_context": "revalidate"}
    )
    with context(service, spec, factory) as current:
        verifier = build_effect_verifier(cfg, read_context=current)
        assert len(clients) == 1
        first = verifier.capture_pre_state()
        second = verifier.capture_pre_state()
        assert first.reachable and second.reachable
        assert first.records == second.records == service.snapshot()
        assert len(clients) == 1
    assert not verifier.capture_pre_state().reachable
    with pytest.raises(ValueError, match="open REST read context"):
        build_effect_verifier(cfg, read_context=current)
    with pytest.raises(ValueError, match="requires current read-context"):
        build_effect_verifier(cfg)


@pytest.mark.parametrize("transport", ["requests", "httpx"])
def test_actual_transport_never_follows_redirects_during_learning(service, transport):
    service.seed(1)
    service.mode = "redirect"
    factory = (
        requests.Session
        if transport == "requests"
        else lambda: httpx.Client(follow_redirects=True, trust_env=False)
    )
    with factory() as client:
        setup = learn_rest_read(
            unpaged_spec(),
            base_url=service.url,
            path="/objects",
            probes=[ReadProbe(service.snapshot)],
            session=client,
        )
    assert not setup.observations_match and setup.config is None
    assert service.request_paths == ["/objects"]


def test_current_independent_projection_refuses_changed_reader_context(service):
    service.seed(2)
    spec = unpaged_spec()
    setup = observed(service, spec)
    other_store = copy.deepcopy(service.snapshot())
    other_store[0]["value"] = "another-authorized-store"
    cfg = EffectsConfig.model_validate(
        {**setup.config, "records_read_context": "revalidate"}
    )
    with RestReadContext(
        spec,
        service.url,
        "/objects",
        requests.Session,
        lambda: [ReadProbe(lambda: other_store)],
    ) as current:
        with pytest.raises(ValueError, match="context revalidation failed"):
            build_effect_verifier(cfg, read_context=current)


@pytest.mark.parametrize(
    "field,value",
    [
        ("records_total_path", "other"),
        ("records_complete", True),
        ("records_drop_fields", ["value"]),
        ("timeout_s", 99),
    ],
)
def test_current_context_does_not_authorize_changed_candidate(service, field, value):
    service.seed(2)
    spec = unpaged_spec()
    setup = observed(service, spec)
    cfg = EffectsConfig.model_validate(
        {**setup.config, "records_read_context": "revalidate", field: value}
    )
    with context(service, spec, requests.Session) as current:
        with pytest.raises(
            ValueError,
            match="unsupported projection|differs from the current read contract",
        ):
            build_effect_verifier(cfg, read_context=current)


def test_missing_factory_client_cannot_fall_back_to_anonymous(service):
    service.seed(1)
    spec = unpaged_spec()
    setup = observed(service, spec)
    cfg = EffectsConfig.model_validate(
        {**setup.config, "records_read_context": "revalidate"}
    )
    service.calls = 0
    with context(service, spec, lambda: None) as current:
        with pytest.raises(ValueError, match="opened no client"):
            build_effect_verifier(cfg, read_context=current)
    assert service.calls == 0


def test_synthesis_routes_current_context_to_record_and_content_reads(service):
    service.seed(3)
    spec = unpaged_spec()
    setup = observed(service, spec)
    original = {
        k: setup.config[k] for k in ("kind", "base_url", "records_path", "records_key")
    }
    surface = Surface(
        "read", original, service.snapshot(), [], True, "synthetic network capture"
    )
    search = ReadSearch(surfaces=[surface])
    with context(service, spec, requests.Session) as current:
        apply_rest_setups(search, [setup], contexts={"read": current})
        assert not search.problems
        assert surface.config["records_read_context"] == "revalidate"
        section = verifier_section(search.surfaces)
        assert not check_reads(
            search.surfaces, section, {}, read_contexts=search.rest_read_contexts
        )
        composite = build_effect_verifier(
            EffectsConfig.model_validate(section),
            read_context=search.rest_read_contexts,
        )
        assert composite.capture_pre_state().reachable
        with pytest.raises(ValueError, match="requires current read-context"):
            build_effect_verifier(EffectsConfig.model_validate(section))
    assert not composite.capture_pre_state().reachable
    assert setup.config.get("records_read_context") is None
    assert setup.evidence["status"] == "draft"


def test_serialized_context_marker_preserves_old_config_identity(service):
    cfg = EffectsConfig(
        kind="rest", base_url=service.url, records_path="/objects", records_key="items"
    )
    assert "records_read_context" not in cfg.model_dump(mode="json")
    marked = cfg.model_copy(update={"records_read_context": "revalidate"})
    assert marked.model_dump(mode="json")["records_read_context"] == "revalidate"
    with pytest.raises(ValueError, match="requires current read-context"):
        build_effect_verifier(
            EffectsConfig.model_validate(marked.model_dump(mode="json"))
        )


def test_actual_synthesis_revalidates_but_exports_no_context_authority(
    service, tmp_path
):
    import yaml

    from openadapt_flow.record_check_synthesis import render, synthesize
    from openadapt_flow.verify_run import VerifyRunError, parse_verify_config
    from tests._record_check_fixtures import Capture, write_recording

    service.seed(1)
    spec = unpaged_spec()
    setup = observed(service, spec)
    recording = write_recording(tmp_path / "recording", {"value": "v0"})
    capture = Capture()
    capture.get(service.url + "/objects", 3.0)
    capture.write(recording)
    detached = synthesize(recording, rest_setups=[setup])
    assert any("read-context revalidation" in m for m in detached.misses)
    names = [surface.name for surface in detached.search.surfaces]
    with context(service, spec, requests.Session) as current:
        proposal = synthesize(
            recording,
            rest_setups=[setup],
            rest_read_contexts={name: current for name in names},
        )
        assert not any(
            "current REST context" in m
            or "generated read" in m
            or "read-context revalidation" in m
            for m in proposal.misses
        )
        assert all(
            c["records_read_context"] == "revalidate"
            for c in proposal.verifier["surfaces"].values()
        )
        assert not proposal.proof()["approved"]
        assert not proposal.working  # Whole-write/frame scope was not granted.
        doc = yaml.safe_load(render(proposal))
        with pytest.raises(VerifyRunError, match="unconfirmed placeholder"):
            parse_verify_config(doc)
        with pytest.raises(ValueError, match="requires current read-context"):
            build_effect_verifier(EffectsConfig.model_validate(doc["verifier"]))
