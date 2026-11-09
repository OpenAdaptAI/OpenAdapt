"""Exact payer/account/service routing tests for the eligibility waterfall."""

from __future__ import annotations

import httpx
import pytest

from openadapt_flow.eligibility.client import (
    ApplicationMode,
    EligibilityRequest,
    EligibilityStatus,
    StediAccountBoundary,
    StediEligibilityClient,
)
from openadapt_flow.eligibility.waterfall import (
    DEFAULT_REGISTRY_PATH,
    EligibilityRoute,
    PayerCapability,
    PayerRegistry,
    load_payer_routes,
    resolve_route,
    run_waterfall,
)

DIGEST = "a" * 64
ENV = {"STEDI_API_KEY": "test-key"}
ACCOUNT = StediAccountBoundary(
    practice_account_id="practice-1", application_mode=ApplicationMode.TEST
)


def request_for(payer_id: str = "62308", services=None) -> EligibilityRequest:
    return EligibilityRequest(
        operation_id="route-check-1",
        payer_id=payer_id,
        provider_npi="1999999984",
        provider_organization="One",
        member_id="123",
        service_type_codes=services or ["35"],
        date_of_service="20260721",
    )


def api_capability(**overrides) -> PayerCapability:
    data = dict(
        key="cigna",
        display_name="Cigna Dental",
        route=EligibilityRoute.API,
        request_payer_id="62308",
        stedi_id="HGJLR",
        application_mode=ApplicationMode.TEST,
        practice_account_id="practice-1",
        supported_service_type_codes=["35"],
        payer_record_sha256=DIGEST,
        portal_fallback_reviewed=True,
        verified_on="2026-07-21",
    )
    data.update(overrides)
    return PayerCapability(**data)


def registry(cap=None) -> PayerRegistry:
    item = cap or api_capability()
    return PayerRegistry(payers={item.key: item})


def fake_client(body: dict, *, status=200, account=ACCOUNT, max_attempts=1):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return StediEligibilityClient(
        account=account,
        transport=httpx.MockTransport(handler),
        env=ENV,
        max_attempts=max_attempts,
        sleep=lambda _: None,
    )


ACTIVE = {
    "meta": {"applicationMode": "test"},
    "tradingPartnerServiceId": "62308",
    "subscriber": {"memberId": "123"},
    "provider": {"npi": "1999999984"},
    "planDateInformation": {"planBegin": "20260101", "planEnd": "20261231"},
    "benefitsInformation": [{"code": "1", "serviceTypeCodes": ["35"]}],
}


def test_shipped_registry_is_a_complete_synthetic_test_binding():
    assert DEFAULT_REGISTRY_PATH.exists()
    loaded = load_payer_routes()
    assert loaded.default_route is EligibilityRoute.QUEUE
    cap = loaded.payers["cigna_dental_mock"]
    assert cap.application_mode is ApplicationMode.TEST
    assert cap.request_payer_id == "62308"
    assert cap.stedi_id == "HGJLR"
    assert cap.supported_service_type_codes == ["35"]
    assert len(cap.payer_record_sha256 or "") == 64


def test_incomplete_api_binding_fails_registry_validation():
    with pytest.raises(ValueError, match="exact reviewed binding"):
        PayerCapability(key="unsafe", route=EligibilityRoute.API)


def test_unknown_payer_queues_instead_of_guessing_a_portal():
    decision = resolve_route("Unknown Dental", registry())
    assert decision.route is EligibilityRoute.QUEUE
    assert "no exact reviewed route" in decision.reason


def test_exact_request_and_stedi_ids_resolve_the_same_reviewed_route():
    exact = resolve_route("62308", registry())
    stable = resolve_route("HGJLR", registry())
    assert exact.route is EligibilityRoute.API
    assert stable.route is EligibilityRoute.API
    assert exact.capability == stable.capability


def test_non_queue_default_is_refused():
    with pytest.raises(ValueError, match="attended queue"):
        PayerRegistry(default_route=EligibilityRoute.PORTAL)


def test_alias_collision_fails_loud():
    with pytest.raises(ValueError, match="maps to both"):
        PayerRegistry(
            payers={
                "one": api_capability(key="one", aliases=["shared"]),
                "two": api_capability(
                    key="two",
                    display_name="Other",
                    aliases=["shared"],
                    request_payer_id="99999",
                ),
            }
        )


def test_exact_binding_answer_completes_api_route():
    outcome = run_waterfall(
        request_for(),
        payer="Cigna Dental",
        client=fake_client(ACTIVE),
        registry=registry(),
    )
    assert outcome.final_route is EligibilityRoute.API
    assert outcome.answered_by_api


def test_request_payer_id_mismatch_never_calls_api():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ACTIVE)

    client = StediEligibilityClient(
        account=ACCOUNT, transport=httpx.MockTransport(handler), env=ENV
    )
    outcome = run_waterfall(
        request_for("6238"), payer="Cigna Dental", client=client, registry=registry()
    )
    assert calls == 0
    assert outcome.final_route is EligibilityRoute.QUEUE
    assert "payer ID" in outcome.trail[-1]


def test_unreviewed_service_type_never_calls_api():
    outcome = run_waterfall(
        request_for(services=["30"]),
        payer="Cigna Dental",
        client=fake_client(ACTIVE),
        registry=registry(),
    )
    assert outcome.final_route is EligibilityRoute.QUEUE
    assert "service type" in outcome.trail[-1]


def test_account_or_mode_mismatch_never_calls_api():
    other = StediAccountBoundary(
        practice_account_id="practice-2", application_mode=ApplicationMode.TEST
    )
    outcome = run_waterfall(
        request_for(),
        payer="Cigna Dental",
        client=fake_client(ACTIVE, account=other),
        registry=registry(),
    )
    assert outcome.final_route is EligibilityRoute.QUEUE
    assert "account/mode" in outcome.trail[-1]


def test_transient_failure_falls_to_reviewed_portal_after_retries():
    body = {"code": "TOO_MANY_REQUESTS"}
    outcome = run_waterfall(
        request_for(),
        payer="Cigna Dental",
        client=fake_client(body, status=429, max_attempts=2),
        registry=registry(),
    )
    assert outcome.final_route is EligibilityRoute.PORTAL
    assert outcome.result is not None
    assert outcome.result.status is EligibilityStatus.PAYER_UNAVAILABLE
    assert outcome.result.attempt_count == 2


def test_member_identity_error_queues_without_portal_fallback():
    body = {
        "meta": {"applicationMode": "test"},
        "tradingPartnerServiceId": "62308",
        "errors": [{"code": "75"}],
    }
    outcome = run_waterfall(
        request_for(),
        payer="Cigna Dental",
        client=fake_client(body),
        registry=registry(),
    )
    assert outcome.final_route is EligibilityRoute.QUEUE
    assert outcome.result is not None
    assert outcome.result.status is EligibilityStatus.NOT_FOUND


def test_portal_banned_transient_failure_queues():
    cap = api_capability(portal_banned=True, portal_fallback_reviewed=False)
    outcome = run_waterfall(
        request_for(),
        payer="Cigna Dental",
        client=fake_client({"error": "down"}, status=503),
        registry=registry(cap),
    )
    assert outcome.final_route is EligibilityRoute.QUEUE
    assert "barred" in outcome.trail[-1]


def test_transient_failure_without_reviewed_portal_fallback_queues():
    cap = api_capability(portal_fallback_reviewed=False)
    outcome = run_waterfall(
        request_for(),
        payer="Cigna Dental",
        client=fake_client({"error": "down"}, status=503),
        registry=registry(cap),
    )
    assert outcome.final_route is EligibilityRoute.QUEUE
    assert "no portal fallback is explicitly reviewed" in outcome.trail[-1]


def test_explicit_portal_route_needs_no_api_key():
    cap = PayerCapability(
        key="portal", display_name="Portal Dental", route=EligibilityRoute.PORTAL
    )
    outcome = run_waterfall(
        request_for("PORTAL"), payer="Portal Dental", registry=registry(cap)
    )
    assert outcome.final_route is EligibilityRoute.PORTAL
    assert outcome.result is None


def test_portal_banned_configuration_cannot_select_portal():
    with pytest.raises(ValueError, match="portal-banned"):
        PayerCapability(
            key="contradictory",
            display_name="Contradictory Portal",
            route=EligibilityRoute.PORTAL,
            portal_banned=True,
        )


def test_excluded_route_stays_excluded():
    cap = PayerCapability(
        key="blocked",
        display_name="Blocked Portal",
        route=EligibilityRoute.EXCLUDED,
        portal_banned=True,
    )
    outcome = run_waterfall(
        request_for("BLOCKED"), payer="Blocked Portal", registry=registry(cap)
    )
    assert outcome.final_route is EligibilityRoute.EXCLUDED


def test_malformed_registry_fails_loud(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text("just a string")
    with pytest.raises(ValueError, match="payer mapping"):
        load_payer_routes(path)


def test_lazy_package_exports():
    import openadapt_flow.eligibility as eligibility

    assert eligibility.EligibilityRoute is EligibilityRoute
    assert callable(eligibility.run_waterfall)
    assert callable(eligibility.purge_expired_eligibility_artifacts)
    with pytest.raises(AttributeError):
        eligibility.nope
