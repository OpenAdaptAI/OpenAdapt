"""Contract and fail-closed tests for the Stedi eligibility client."""

from __future__ import annotations

import json

import httpx
import pytest

from openadapt_flow.eligibility.client import (
    STEDI_ELIGIBILITY_URL,
    ApplicationMode,
    BenefitSelection,
    EligibilityRequest,
    EligibilityStatus,
    ErrorCategory,
    RetryDisposition,
    StediAccountBoundary,
    StediEligibilityClient,
    parse_271,
)

ENV = {"STEDI_API_KEY": "test-key-123"}
ACCOUNT = StediAccountBoundary(
    practice_account_id="openadapt-sandbox",
    application_mode=ApplicationMode.TEST,
)


def dental_request(**overrides) -> EligibilityRequest:
    base = dict(
        operation_id="check-001",
        payer_id="62308",
        member_id="U3141592653",
        first_name="Jaguar",
        last_name="Dent",
        date_of_birth="19960505",
        provider_npi="1999999984",
        provider_organization="One",
        service_type_codes=["35"],
        date_of_service="20260721",
        benefit_selection=BenefitSelection(network_code="Y", coverage_level_code="IND"),
    )
    base.update(overrides)
    return EligibilityRequest(**base)


ACTIVE_271 = {
    "meta": {"applicationMode": "test"},
    "tradingPartnerServiceId": "62308",
    "subscriber": {
        "memberId": "U3141592653",
        "firstName": "Jaguar",
        "lastName": "Dent",
        "dateOfBirth": "19960505",
    },
    "provider": {"npi": "1999999984"},
    "payer": {"name": "Cigna"},
    "planInformation": {"groupDescription": "DENTAL PPO"},
    "planDateInformation": {"planBegin": "20260101", "planEnd": "20261231"},
    "benefitsInformation": [
        {"code": "1", "serviceTypeCodes": ["35"]},
        {
            "code": "B",
            "benefitAmount": "20",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
        },
        {
            "code": "B",
            "benefitAmount": "80",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "N",
        },
        {
            "code": "A",
            "benefitPercent": "0.2",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
        },
        {
            "code": "C",
            "benefitAmount": "1000",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
            "timeQualifierCode": "23",
        },
        {
            "code": "C",
            "benefitAmount": "500",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
            "timeQualifierCode": "29",
        },
        {
            "code": "G",
            "benefitAmount": "2500",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
            "timeQualifierCode": "23",
        },
        {
            "code": "G",
            "benefitAmount": "1200",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
            "timeQualifierCode": "29",
        },
    ],
}


def make_client(handler, **kwargs) -> StediEligibilityClient:
    return StediEligibilityClient(
        account=ACCOUNT,
        transport=httpx.MockTransport(handler),
        env=ENV,
        sleep=lambda _: None,
        **kwargs,
    )


def test_request_shape_and_phi_safe_repr():
    request = dental_request()
    assert request.to_stedi_body()["encounter"] == {
        "serviceTypeCodes": ["35"],
        "dateOfService": "20260721",
    }
    assert request.to_stedi_body()["tradingPartnerServiceId"] == "62308"
    assert "U3141592653" not in repr(request)
    assert request.safe_summary() == {
        "operation_id": "check-001",
        "payer_id": "62308",
        "service_type_codes": ["35"],
        "date_of_service_present": True,
    }


def test_date_of_service_is_required_for_a_deterministic_answer():
    with pytest.raises(ValueError, match="date_of_service"):
        dental_request(date_of_service=None)


def test_client_uses_current_bare_authorization_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json=ACTIVE_271)

    assert make_client(handler).check(dental_request()).is_answer
    assert seen["authorization"] == "test-key-123"


def test_legacy_key_prefix_is_removed_not_emitted():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json=ACTIVE_271)

    client = StediEligibilityClient(
        account=ACCOUNT,
        transport=httpx.MockTransport(handler),
        env={"STEDI_API_KEY": "Key old-key"},
    )
    client.check(dental_request())
    assert seen["authorization"] == "old-key"


def test_missing_key_and_non_allowlisted_endpoint_fail_loud():
    with pytest.raises(ValueError, match="practice-held credential"):
        StediEligibilityClient(account=ACCOUNT, env={})
    with pytest.raises(ValueError, match="allowlisted"):
        StediEligibilityClient(
            account=ACCOUNT, env=ENV, base_url="https://example.test/eligibility"
        )
    with pytest.raises(ValueError, match="allowlisted"):
        StediEligibilityClient(
            account=ACCOUNT,
            env=ENV,
            base_url=f"{STEDI_ELIGIBILITY_URL}?alternate=true",
        )


def test_production_account_requires_practice_baa():
    with pytest.raises(ValueError, match="BAA"):
        StediAccountBoundary(
            practice_account_id="practice-1",
            application_mode=ApplicationMode.PRODUCTION,
        )


def test_qualifier_aware_values_do_not_choose_out_of_network_or_remaining_as_total():
    payload = json.dumps(ACTIVE_271).encode()
    result = parse_271(dental_request(), payload, expected_mode=ApplicationMode.TEST)
    assert result.is_answer
    assert result.status is EligibilityStatus.ACTIVE
    assert result.copay == "20"
    assert result.coinsurance_percent == "0.2"
    assert result.deductible_total == "1000"
    assert result.deductible_remaining == "500"
    assert result.out_of_pocket_total == "2500"
    assert result.out_of_pocket_remaining == "1200"
    assert len(result.benefits) == 7
    assert result.raw_271_bytes == payload


@pytest.mark.parametrize(
    ("selection", "mutations", "label"),
    [
        (
            BenefitSelection(network_code="Y", coverage_level_code="IND"),
            {
                "inPlanNetworkIndicatorCode": "N",
                "coverageLevelCode": "FAM",
            },
            "coverage level, network",
        ),
        (
            BenefitSelection(time_qualifier_code="23"),
            {"timeQualifierCode": "29"},
            "time period",
        ),
    ],
)
def test_explicit_qualifier_without_matching_evidence_never_answers(
    selection, mutations, label
):
    body = json.loads(json.dumps(ACTIVE_271))
    for entry in body["benefitsInformation"]:
        if entry.get("code") in {"A", "B", "C", "G"}:
            entry.update(mutations)
    result = parse_271(
        dental_request(benefit_selection=selection),
        json.dumps(body).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    assert result.status is EligibilityStatus.ACTIVE
    assert not result.is_answer
    assert result.error_category is ErrorCategory.RESPONSE_AMBIGUOUS
    assert (
        f"no patient-responsibility evidence matches requested {label} qualifiers"
        in result.ambiguities
    )


def test_raw_phi_response_is_excluded_from_repr_and_serialization():
    body = json.loads(json.dumps(ACTIVE_271))
    body["subscriber"] = {"memberId": "SECRET-MEMBER-123"}
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert not result.is_answer
    assert result.error_category is ErrorCategory.MEMBER_IDENTITY
    assert result.raw_271 is not None
    assert "SECRET-MEMBER-123" not in repr(result)
    assert "SECRET-MEMBER-123" not in result.model_dump_json()


def test_answer_requires_exact_raw_bytes_digest_and_application_mode():
    result = parse_271(
        dental_request(),
        json.dumps(ACTIVE_271).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    assert result.is_answer
    assert not result.model_copy(update={"raw_271_bytes": None}).is_answer
    assert not result.model_copy(update={"raw_271_sha256": "0" * 64}).is_answer
    assert not result.model_copy(update={"application_mode": None}).is_answer
    assert not result.model_copy(update={"application_mode": "test"}).is_answer


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("memberId", "WRONG-MEMBER"),
        ("firstName", "Wrong"),
        ("lastName", "Wrong"),
        ("dateOfBirth", "19800101"),
    ],
)
def test_active_response_for_wrong_subject_never_answers(field, wrong):
    body = json.loads(json.dumps(ACTIVE_271))
    body["subscriber"][field] = wrong
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert not result.is_answer
    assert result.error_category is ErrorCategory.MEMBER_IDENTITY
    assert "SECRET" not in result.reason


def test_subject_matching_is_conservative_but_case_and_whitespace_tolerant():
    body = json.loads(json.dumps(ACTIVE_271))
    body["subscriber"].update(
        {
            "memberId": "  u3141592653  ",
            "firstName": "  JAGUAR ",
            "lastName": "DENT",
        }
    )
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert result.is_answer
    assert result.response_subject_sha256


def test_missing_or_dependent_response_subject_never_answers():
    missing = json.loads(json.dumps(ACTIVE_271))
    missing.pop("subscriber")
    result = parse_271(
        dental_request(),
        json.dumps(missing).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    assert not result.is_answer
    assert result.error_category is ErrorCategory.MEMBER_IDENTITY

    dependent = json.loads(json.dumps(ACTIVE_271))
    dependent["dependents"] = [
        {"firstName": "Jaguar", "lastName": "Dent", "dateOfBirth": "19960505"}
    ]
    result = parse_271(
        dental_request(),
        json.dumps(dependent).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    assert not result.is_answer
    assert "dependent" in result.reason


@pytest.mark.parametrize("provider", [None, {"npi": "1000000000"}])
def test_missing_or_wrong_response_provider_never_answers(provider):
    body = json.loads(json.dumps(ACTIVE_271))
    if provider is None:
        body.pop("provider")
    else:
        body["provider"] = provider
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert not result.is_answer
    assert result.error_category is ErrorCategory.PROVIDER_CONFIGURATION


def test_response_application_mode_is_required_even_without_expected_mode():
    body = json.loads(json.dumps(ACTIVE_271))
    body.pop("meta")
    result = parse_271(dental_request(), json.dumps(body).encode())
    assert not result.is_answer
    assert result.error_category is ErrorCategory.AUTH_CONFIGURATION


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (401, ErrorCategory.AUTH_CONFIGURATION, False),
        (429, ErrorCategory.THROTTLED, True),
        (503, ErrorCategory.SERVER_TRANSIENT, True),
    ],
)
def test_http_status_classification_does_not_depend_on_json_body(
    status, category, retryable
):
    result = parse_271(dental_request(), b"upstream text", http_status=status)
    assert result.error_category is category
    assert result.retryable is retryable


def test_conflicting_qualified_benefits_are_not_an_answer():
    body = json.loads(json.dumps(ACTIVE_271))
    body["benefitsInformation"].append(
        {
            "code": "B",
            "benefitAmount": "25",
            "serviceTypeCodes": ["35"],
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
        }
    )
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert result.status is EligibilityStatus.ACTIVE
    assert result.copay is None
    assert not result.is_answer
    assert result.error_category is ErrorCategory.RESPONSE_AMBIGUOUS
    assert result.ambiguities == ["benefit B: conflicting qualified values"]


def test_wrong_service_and_conflicting_coverage_fail_closed():
    wrong = json.loads(json.dumps(ACTIVE_271))
    wrong["benefitsInformation"][0]["serviceTypeCodes"] = ["30"]
    result = parse_271(
        dental_request(), json.dumps(wrong).encode(), expected_mode=ApplicationMode.TEST
    )
    assert result.status is EligibilityStatus.INDETERMINATE
    assert not result.is_answer

    conflict = json.loads(json.dumps(ACTIVE_271))
    conflict["benefitsInformation"].append({"code": "6", "serviceTypeCodes": ["35"]})
    result = parse_271(
        dental_request(),
        json.dumps(conflict).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    assert result.status is EligibilityStatus.INDETERMINATE
    assert "conflicting active/inactive" in result.ambiguities[0]


def test_service_date_outside_plan_and_benefit_window_fails_closed():
    body = json.loads(json.dumps(ACTIVE_271))
    body["planDateInformation"]["planEnd"] = "20260630"
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert result.status is EligibilityStatus.INDETERMINATE
    assert "service date" in result.ambiguities[0]


@pytest.mark.parametrize(
    "plan_dates",
    [
        {"planBegin": "20260722", "planEnd": "20261231"},
        {"planBegin": "20260101", "planEnd": "20260720"},
        {},
    ],
)
def test_active_answer_requires_explicit_interval_containing_service_date(plan_dates):
    body = json.loads(json.dumps(ACTIVE_271))
    body["planDateInformation"] = plan_dates
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert not result.is_answer
    assert result.error_category is ErrorCategory.RESPONSE_AMBIGUOUS
    assert (
        "coverage interval" in result.ambiguities[0]
        or "service date" in result.ambiguities[0]
    )


@pytest.mark.parametrize(
    "plan_dates",
    [
        {"planBegin": "20260101"},
        {"planEnd": "20261231"},
    ],
)
def test_active_answer_accepts_explicit_open_interval_containing_service_date(
    plan_dates,
):
    body = json.loads(json.dumps(ACTIVE_271))
    body["planDateInformation"] = plan_dates
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert result.is_answer


def test_redirect_with_valid_looking_json_is_never_an_answer():
    result = parse_271(
        dental_request(),
        json.dumps(ACTIVE_271).encode(),
        http_status=302,
        expected_mode=ApplicationMode.TEST,
    )
    assert not result.is_answer
    assert result.error_category is ErrorCategory.RESPONSE_INVALID


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_do_not_retry_or_fallback(status):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": "unauthorized"})

    result = make_client(handler).check(dental_request())
    assert calls == 1
    assert result.error_category is ErrorCategory.AUTH_CONFIGURATION
    assert result.retry_disposition is RetryDisposition.NO_RETRY_QUEUE


def test_invalid_response_is_queued_without_retry_or_portal_fallback():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"not-json")

    result = make_client(handler).check(dental_request())
    assert calls == 1
    assert result.error_category is ErrorCategory.RESPONSE_INVALID
    assert result.status is EligibilityStatus.INDETERMINATE
    assert result.retry_disposition is RetryDisposition.NO_RETRY_QUEUE


def test_http_400_aaa79_is_invalid_payer_and_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"errors": [{"code": "79"}]})

    result = make_client(handler).check(dental_request())
    assert calls == 1
    assert result.error_category is ErrorCategory.INVALID_PAYER
    assert not result.retryable


def test_aaa_member_identity_is_queued_not_automatically_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = {
            "meta": {"applicationMode": "test"},
            "tradingPartnerServiceId": "62308",
            "errors": [{"code": "75", "description": "contains no logged data"}],
        }
        return httpx.Response(200, json=body)

    result = make_client(handler).check(dental_request())
    assert calls == 1
    assert result.status is EligibilityStatus.NOT_FOUND
    assert result.error_category is ErrorCategory.MEMBER_IDENTITY
    assert "description" not in result.reason


@pytest.mark.parametrize(
    "first_status,first_body",
    [
        (429, {"code": "TOO_MANY_REQUESTS"}),
        (503, {"error": "service_unavailable"}),
        (
            200,
            {
                "meta": {"applicationMode": "test"},
                "tradingPartnerServiceId": "62308",
                "errors": [{"code": "42"}],
            },
        ),
    ],
)
def test_only_explicit_transients_retry(first_status, first_body):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(first_status, json=first_body)
        return httpx.Response(200, json=ACTIVE_271)

    result = make_client(handler).check(dental_request())
    assert calls == 2
    assert result.is_answer
    assert result.attempt_count == 2


def test_transport_failure_is_bounded_and_secret_free():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("member U3141592653 key test-key-123", request=request)

    result = make_client(handler, max_attempts=2).check(dental_request())
    assert calls == 2
    assert result.error_category is ErrorCategory.TRANSPORT_TRANSIENT
    assert "U3141592653" not in result.reason
    assert "test-key-123" not in result.reason + result.model_dump_json()


def test_response_size_is_bounded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2049)

    result = make_client(handler, max_response_bytes=1024, max_attempts=1).check(
        dental_request()
    )
    assert result.error_category is ErrorCategory.RESPONSE_INVALID
    assert not result.is_answer


def test_partial_result_with_unclassified_error_is_not_accepted():
    body = {**ACTIVE_271, "error": "partial_failure"}
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert not result.is_answer
    assert result.error_category is ErrorCategory.RESPONSE_INVALID


def test_response_payer_and_mode_must_match_request_boundary():
    wrong_payer = {**ACTIVE_271, "tradingPartnerServiceId": "99999"}
    result = parse_271(
        dental_request(),
        json.dumps(wrong_payer).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    assert result.error_category is ErrorCategory.INVALID_PAYER

    wrong_mode = {**ACTIVE_271, "meta": {"applicationMode": "production"}}
    result = parse_271(
        dental_request(),
        json.dumps(wrong_mode).encode(),
        expected_mode=ApplicationMode.TEST,
    )
    assert result.error_category is ErrorCategory.AUTH_CONFIGURATION


def test_non_json_and_empty_json_never_guess_active():
    for payload in (b"<html>bad</html>", b"{}"):
        result = parse_271(dental_request(), payload)
        assert not result.is_answer
        assert result.status is EligibilityStatus.INDETERMINATE


def test_malformed_qualified_benefit_fails_closed_without_raising():
    body = json.loads(json.dumps(ACTIVE_271))
    body["benefitsInformation"].append(
        {"code": "B", "benefitAmount": "20", "serviceTypeCodes": None}
    )
    result = parse_271(
        dental_request(), json.dumps(body).encode(), expected_mode=ApplicationMode.TEST
    )
    assert not result.is_answer
    assert result.error_category is ErrorCategory.RESPONSE_INVALID
