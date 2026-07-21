from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jsonschema
import pytest

from openadapt_flow import hosted, privacy
from openadapt_flow.bundle_validation import build_runtime_parameter_schema
from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import ParamKind, ParamSpec, RunReport, Workflow
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.runtime_validation import (
    RuntimeValidationError,
    _canonical_bytes,
    _signature,
    create_runtime_validation_attestation,
    normalize_execution_scope,
    request_validation_challenge,
    verify_runtime_validation_attestation,
)
from openadapt_flow.sanitized_artifact import approve_derivative, sanitize_artifact

_TARGET_URL = "https://mockmed.example.com/login"
_TARGET_ORIGIN = "https://mockmed.example.com"


class _StableScrubber:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text.replace("Jane Doe", "[PERSON]")

    def scrub_image(self, image, fill_color=None):
        return image


@pytest.fixture(autouse=True)
def _privacy():
    privacy.set_text_scrubber(_StableScrubber())
    yield
    privacy.reset_scrubbers()


def _approved_artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "meta.json").write_text('{"patient":"Jane Doe"}')
    (recording / "events.jsonl").write_text("")
    recording_derivative = tmp_path / "recording-sanitized"
    sanitize_artifact(recording, recording_derivative, kind="recording")
    approve_derivative(recording_derivative, source=recording, reviewer="alice")

    bundle = tmp_path / "bundle"
    workflow = compile_recording(
        recording_derivative, bundle, name="validated-empty-workflow"
    )
    bundle_derivative = tmp_path / "bundle-sanitized"
    bundle_manifest = sanitize_artifact(bundle, bundle_derivative, kind="bundle")
    assert bundle_manifest["execution_semantics"] == "preserved"
    assert bundle_manifest["runtime_semantics_validated"] is False
    approve_derivative(bundle_derivative, source=bundle, reviewer="alice")

    run_dir = tmp_path / "run"
    report = Replayer(object(), vision=object()).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        execution_origin=_TARGET_ORIGIN,
        execution_entry_url=_TARGET_URL,
    )
    assert report.success is True
    return recording_derivative, bundle_derivative, run_dir


def _challenge(**updates: str) -> dict[str, str]:
    value = {
        "challenge_id": "challenge-1",
        "nonce": "0123456789abcdef0123456789abcdef",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    }
    value.update(updates)
    return value


def _attestation(tmp_path: Path, token: str = "oai_ingest_test") -> tuple[dict, Path]:
    recording, bundle, run_dir = _approved_artifacts(tmp_path)
    value = create_runtime_validation_attestation(
        recording_derivative=recording,
        bundle_derivative=bundle,
        run_dir=run_dir,
        policy_source="permissive",
        risk_class="low",
        environment="local-test/mockmed-v1",
        target_url=_TARGET_URL,
        allowed_hosts=["cdn.mockmed.example.com"],
        host=hosted.DEFAULT_HOST,
        token=token,
        challenge=_challenge(),
    )
    return value, bundle


def test_attestation_binds_real_evidence_and_matches_schema(tmp_path):
    token = "oai_ingest_test"
    value, _ = _attestation(tmp_path, token)

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/runtime-validation-attestation-v1.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(value)
    verify_runtime_validation_attestation(
        value, bundle_sha256=value["bundle_sha256"], token=token
    )
    assert value["source_recording_sha256"] != value["bundle_sha256"]
    assert value["lint"] == {
        "strict": True,
        "passed": True,
        "evidence_sha256": value["lint"]["evidence_sha256"],
    }
    assert value["certification"]["policy"] == "permissive"
    assert value["certification"]["risk_class"] == "low"
    assert value["parameters"] == []
    assert value["execution"] == {
        "entry_url": _TARGET_URL,
        "target_origin": _TARGET_ORIGIN,
        "allowed_hosts": ["cdn.mockmed.example.com", "mockmed.example.com"],
    }
    assert value["replay"]["success"] is True


def test_cross_language_canonicalization_golden_vector():
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures/runtime-validation-canonical-vector.json"
        ).read_text(encoding="utf-8")
    )
    assert _canonical_bytes(fixture["value"]).decode("utf-8") == fixture["canonical"]
    assert _signature(fixture["value"], fixture["token"]) == fixture["signature"]


@pytest.mark.parametrize(
    "url",
    [
        "http://app.example.com",
        "https://user:password@app.example.com",
        "https://app.example.com?query=1",
        "https://app.example.com#fragment",
    ],
)
def test_execution_scope_refuses_unsafe_entry_urls(url):
    with pytest.raises(RuntimeValidationError, match="Target URL must be HTTPS"):
        normalize_execution_scope(url, None)


def test_execution_scope_normalizes_default_port_and_hosts():
    assert normalize_execution_scope(
        "https://APP.EXAMPLE.COM:443/app/login", ["CDN.EXAMPLE.COM", "app.example.com"]
    ) == {
        "entry_url": "https://app.example.com/app/login",
        "target_origin": "https://app.example.com",
        "allowed_hosts": ["app.example.com", "cdn.example.com"],
    }


def test_execution_scope_canonicalizes_idna():
    assert normalize_execution_scope(
        "https://B\u00dcCHER.example.com:8443/patient records",
        ["CDN.B\u00dcCHER.example.com"],
    ) == {
        "entry_url": "https://xn--bcher-kva.example.com:8443/patient%20records",
        "target_origin": "https://xn--bcher-kva.example.com:8443",
        "allowed_hosts": [
            "cdn.xn--bcher-kva.example.com",
            "xn--bcher-kva.example.com",
        ],
    }

    assert normalize_execution_scope("https://faß.de/login", None) == {
        "entry_url": "https://xn--fa-hia.de/login",
        "target_origin": "https://xn--fa-hia.de",
        "allowed_hosts": ["xn--fa-hia.de"],
    }


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.",
        "https://-edge.example",
        "https://edge-.example",
        "https://127.1",
        "https://127.0.0.1",
        "https://10.0.0.8",
        "https://169.254.169.254/latest/meta-data",
        "https://0x7f000001",
        "https://[::1]",
        "https://localhost",
        "https://service.internal",
        "https://app.example",
    ],
)
def test_execution_scope_refuses_ambiguous_cross_parser_hosts(origin):
    with pytest.raises(RuntimeValidationError, match="Invalid target origin hostname"):
        normalize_execution_scope(origin, None)


@pytest.mark.parametrize(
    "allowed",
    [
        "cdn.example.com:443",
        ".cdn.example.com",
        "cdn..example.com",
        "127.0.0.1",
        "metadata.google.internal",
    ],
)
def test_execution_scope_refuses_non_hostname_allowlist_entries(allowed):
    with pytest.raises(RuntimeValidationError, match="Invalid allowed host"):
        normalize_execution_scope("https://app.example.com", [allowed])


def test_runtime_parameter_schema_never_contains_defaults_or_examples():
    workflow = Workflow(
        name="parameter-contract",
        params={"patient": "Jane Doe", "password": "correct horse battery staple"},
        param_specs={
            "patient": ParamSpec(
                name="patient", type=ParamKind.ENTITY_REF, example="Jane Doe"
            ),
            "password": ParamSpec(
                name="password",
                type=ParamKind.STRING,
                example="correct horse battery staple",
            ),
        },
        secret_params=["password"],
    )

    schema = build_runtime_parameter_schema(workflow)
    encoded = json.dumps(schema)

    assert "Jane Doe" not in encoded
    assert "correct horse battery staple" not in encoded
    assert schema == [
        {
            "name": "password",
            "type": "string",
            "required": True,
            "secret": True,
            "choices": [],
        },
        {
            "name": "patient",
            "type": "entity_ref",
            "required": True,
            "secret": False,
            "choices": [],
        },
    ]


def test_request_challenge_is_policy_gated_non_redirecting_and_validated(monkeypatch):
    captured: dict = {}

    def post(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return httpx.Response(201, json={"challenge": _challenge()})

    monkeypatch.setattr(httpx, "post", post)
    challenge = request_validation_challenge(
        host=hosted.DEFAULT_HOST, token="oai_ingest_test"
    )

    assert challenge["challenge_id"] == "challenge-1"
    assert captured["url"] == ("https://app.openadapt.ai/api/validation-challenges")
    assert captured["kwargs"]["headers"]["Authorization"] == ("Bearer oai_ingest_test")
    assert captured["kwargs"]["follow_redirects"] is False


def test_request_challenge_wraps_non_json_response(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(201, content=b"not-json"),
    )
    with pytest.raises(RuntimeValidationError, match="non-JSON"):
        request_validation_challenge(host=hosted.DEFAULT_HOST, token="oai_ingest_test")


def test_supplied_expired_challenge_is_refused_before_artifact_access(tmp_path):
    expired = _challenge(
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    )
    with pytest.raises(RuntimeValidationError, match="expired"):
        create_runtime_validation_attestation(
            recording_derivative=tmp_path / "missing-recording",
            bundle_derivative=tmp_path / "missing-bundle",
            run_dir=tmp_path / "missing-run",
            policy_source="permissive",
            risk_class="low",
            environment="local-test/mockmed-v1",
            target_url=_TARGET_URL,
            host=hosted.DEFAULT_HOST,
            token="oai_ingest_test",
            challenge=expired,
        )


def test_attestation_tamper_and_bundle_mismatch_are_refused(tmp_path):
    token = "oai_ingest_test"
    value, _ = _attestation(tmp_path, token)

    tampered = json.loads(json.dumps(value))
    tampered["certification"]["policy"] = "clinical-write"
    with pytest.raises(RuntimeValidationError, match="signature"):
        verify_runtime_validation_attestation(
            tampered, bundle_sha256=value["bundle_sha256"], token=token
        )
    with pytest.raises(RuntimeValidationError, match="different approved bundle"):
        verify_runtime_validation_attestation(
            value, bundle_sha256="0" * 64, token=token
        )


def test_attestation_refuses_operator_risk_downgrade(tmp_path):
    recording, bundle, run_dir = _approved_artifacts(tmp_path)
    with pytest.raises(RuntimeValidationError, match="does not match compiled"):
        create_runtime_validation_attestation(
            recording_derivative=recording,
            bundle_derivative=bundle,
            run_dir=run_dir,
            policy_source="permissive",
            risk_class="consequential",
            environment="local-test/mockmed-v1",
            target_url=_TARGET_URL,
            host=hosted.DEFAULT_HOST,
            token="oai_ingest_test",
            challenge=_challenge(),
        )


def test_attestation_refuses_secret_values_exposed_as_choices(tmp_path, monkeypatch):
    from openadapt_flow import runtime_validation

    recording, bundle, run_dir = _approved_artifacts(tmp_path)
    monkeypatch.setattr(
        runtime_validation,
        "build_runtime_parameter_schema",
        lambda workflow: [
            {
                "name": "password",
                "type": "enum",
                "required": True,
                "secret": True,
                "choices": ["actual-secret-value"],
            }
        ],
    )

    with pytest.raises(RuntimeValidationError, match="expose runtime values"):
        create_runtime_validation_attestation(
            recording_derivative=recording,
            bundle_derivative=bundle,
            run_dir=run_dir,
            policy_source="permissive",
            risk_class="low",
            environment="local-test/mockmed-v1",
            target_url=_TARGET_URL,
            host=hosted.DEFAULT_HOST,
            token="oai_ingest_test",
            challenge=_challenge(),
        )


def test_attestation_refuses_more_parameters_than_cloud_accepts(tmp_path, monkeypatch):
    from openadapt_flow import runtime_validation

    recording, bundle, run_dir = _approved_artifacts(tmp_path)
    monkeypatch.setattr(
        runtime_validation,
        "build_runtime_parameter_schema",
        lambda workflow: [
            {
                "name": f"param_{index}",
                "type": "string",
                "required": False,
                "secret": False,
                "choices": [],
            }
            for index in range(101)
        ],
    )

    with pytest.raises(RuntimeValidationError, match="At most 100"):
        create_runtime_validation_attestation(
            recording_derivative=recording,
            bundle_derivative=bundle,
            run_dir=run_dir,
            policy_source="permissive",
            risk_class="low",
            environment="local-test/mockmed-v1",
            target_url=_TARGET_URL,
            host=hosted.DEFAULT_HOST,
            token="oai_ingest_test",
            challenge=_challenge(),
        )


def test_bundle_push_requires_and_sends_validation_attestation(tmp_path, monkeypatch):
    token = "oai_ingest_test"
    value, bundle = _attestation(tmp_path, token)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("must refuse before network"),
    )
    with pytest.raises(hosted.HostedError, match="runtime-validation attestation"):
        hosted.push(bundle, kind="bundle", token=token)

    captured: dict = {}

    def post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(201, json={"ingest": {"workflow_id": "wf-1"}})

    monkeypatch.setattr(httpx, "post", post)
    result = hosted.push(
        bundle,
        kind="bundle",
        token=token,
        workflow_id="ec726a3e-dcaf-40cf-870a-867d104002dd",
        resolves_run_id="d3ecf64d-0d25-4df7-9264-77bf7d266d77",
        validation_attestation=value,
    )

    assert result["uploaded"] is True
    assert captured["data"]["workflow_id"] == ("ec726a3e-dcaf-40cf-870a-867d104002dd")
    assert captured["data"]["resolves_run_id"] == (
        "d3ecf64d-0d25-4df7-9264-77bf7d266d77"
    )
    assert json.loads(captured["data"]["validation_attestation"]) == value


def test_failed_or_wrong_workflow_report_cannot_attest(tmp_path):
    recording, bundle, run_dir = _approved_artifacts(tmp_path)
    report_path = run_dir / "report.json"
    report = RunReport.model_validate_json(report_path.read_text())
    report.workflow_name = "different-workflow"
    report.save(run_dir)

    with pytest.raises(RuntimeValidationError, match="does not match"):
        create_runtime_validation_attestation(
            recording_derivative=recording,
            bundle_derivative=bundle,
            run_dir=run_dir,
            policy_source="permissive",
            risk_class="low",
            environment="local-test/mockmed-v1",
            target_url=_TARGET_URL,
            host=hosted.DEFAULT_HOST,
            token="oai_ingest_test",
            challenge=_challenge(),
        )


def test_report_from_a_different_browser_origin_cannot_attest(tmp_path):
    recording, bundle, run_dir = _approved_artifacts(tmp_path)
    report_path = run_dir / "report.json"
    report = RunReport.model_validate_json(report_path.read_text())
    report.execution_origin = "https://lookalike.example"
    report.save(run_dir)

    with pytest.raises(RuntimeValidationError, match="browser origin"):
        create_runtime_validation_attestation(
            recording_derivative=recording,
            bundle_derivative=bundle,
            run_dir=run_dir,
            policy_source="permissive",
            risk_class="low",
            environment="local-test/mockmed-v1",
            target_url=_TARGET_URL,
            host=hosted.DEFAULT_HOST,
            token="oai_ingest_test",
            challenge=_challenge(),
        )


def test_unrelated_recording_or_same_name_report_cannot_attest(tmp_path):
    recording, bundle, run_dir = _approved_artifacts(tmp_path)
    other_source = tmp_path / "other-recording"
    other_source.mkdir()
    (other_source / "meta.json").write_text('{"patient":"Different"}')
    (other_source / "events.jsonl").write_text("")
    other_recording = tmp_path / "other-recording-sanitized"
    sanitize_artifact(other_source, other_recording, kind="recording")
    approve_derivative(other_recording, source=other_source, reviewer="alice")

    common = {
        "bundle_derivative": bundle,
        "run_dir": run_dir,
        "policy_source": "permissive",
        "risk_class": "low",
        "environment": "local-test/mockmed-v1",
        "target_url": _TARGET_URL,
        "host": hosted.DEFAULT_HOST,
        "token": "oai_ingest_test",
        "challenge": _challenge(),
    }
    with pytest.raises(RuntimeValidationError, match="different approved recording"):
        create_runtime_validation_attestation(
            recording_derivative=other_recording, **common
        )

    second_bundle_source = tmp_path / "second-bundle"
    compile_recording(recording, second_bundle_source, name="validated-empty-workflow")
    second_bundle = tmp_path / "second-bundle-sanitized"
    sanitize_artifact(second_bundle_source, second_bundle, kind="bundle")
    approve_derivative(second_bundle, source=second_bundle_source, reviewer="alice")
    with pytest.raises(RuntimeValidationError, match="different bundle"):
        create_runtime_validation_attestation(
            recording_derivative=recording,
            bundle_derivative=second_bundle,
            **{
                key: value
                for key, value in common.items()
                if key != "bundle_derivative"
            },
        )
