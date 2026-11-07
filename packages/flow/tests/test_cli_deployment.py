"""Deployment config (``openadapt_flow.deployment``): load + build verifiers.

One ``deployment.yaml`` configures backend / actuation / effects / runtime /
policy for the whole CLI. These tests pin the schema, the shipped example, and
the object-construction seams the CLI injects — no browser, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.deployment import (
    ActuationConfig,
    DeploymentConfig,
    EffectsConfig,
    build_api_actuator,
    build_effect_verifier,
    load_deployment,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "deployment.example.yaml"


def test_shipped_example_loads_and_is_coherent() -> None:
    cfg = load_deployment(EXAMPLE)
    assert isinstance(cfg, DeploymentConfig)
    assert cfg.effects.kind == "rest"
    assert cfg.runtime.durable is True
    assert cfg.policy.policy == "clinical-write"
    # The example must construct a real verifier (guards schema drift).
    verifier = build_effect_verifier(cfg.effects)
    assert type(verifier).__name__ == "RestRecordVerifier"


def test_empty_config_is_all_default_local() -> None:
    cfg = DeploymentConfig()
    assert build_effect_verifier(cfg.effects) is None
    assert build_api_actuator(cfg.actuation) is None
    assert cfg.runtime.durable is False
    assert cfg.runtime.allow_model_grounding is False
    assert cfg.human_decisions.remote.enabled is False
    assert cfg.human_decisions.remote.tenant_id is None
    assert cfg.human_decisions.remote.runner_id is None


def test_remote_human_decisions_require_explicit_complete_scope() -> None:
    with pytest.raises(ValueError, match="exact tenant_id and runner_id"):
        DeploymentConfig.model_validate(
            {"human_decisions": {"remote": {"enabled": True}}}
        )

    # Identifiers alone never infer or silently enable remote issuance.
    inactive = DeploymentConfig.model_validate(
        {
            "human_decisions": {
                "remote": {
                    "tenant_id": "tenant_exact_01",
                    "runner_id": "runner_exact_01",
                }
            }
        }
    )
    assert inactive.human_decisions.remote.enabled is False

    active = DeploymentConfig.model_validate(
        {
            "human_decisions": {
                "remote": {
                    "enabled": True,
                    "tenant_id": "tenant_exact_01",
                    "runner_id": "runner_exact_01",
                }
            }
        }
    )
    assert active.human_decisions.remote.enabled is True


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_deployment(tmp_path / "nope.yaml")


def test_load_non_mapping_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        load_deployment(bad)


@pytest.mark.parametrize(
    "kind,kwargs,expected",
    [
        (
            "rest",
            {"base_url": "http://sor", "records_path": "/api/db"},
            "RestRecordVerifier",
        ),
        (
            "fhir",
            {
                "base_url": "http://sor/fhir",
                "resource_type": "Observation",
                "field_paths": {"value": "valueString"},
            },
            "FhirEffectVerifier",
        ),
        ("document-hash", {"root": "/tmp/store"}, "DocumentHashVerifier"),
    ],
)
def test_build_each_verifier_kind(kind, kwargs, expected) -> None:
    verifier = build_effect_verifier(EffectsConfig(kind=kind, **kwargs))
    assert type(verifier).__name__ == expected


def test_build_verifier_none_kind_is_none() -> None:
    assert build_effect_verifier(EffectsConfig(kind="none")) is None


def test_rest_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        build_effect_verifier(EffectsConfig(kind="rest", records_path="/api/db"))


def test_document_hash_requires_root() -> None:
    with pytest.raises(ValueError, match="root"):
        build_effect_verifier(EffectsConfig(kind="document-hash"))


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown effects.kind"):
        build_effect_verifier(EffectsConfig(kind="bogus"))


def test_api_actuator_off_by_default_on_when_enabled() -> None:
    assert build_api_actuator(ActuationConfig(api=False)) is None
    act = build_api_actuator(ActuationConfig(api=True, base_url="http://api"))
    assert type(act).__name__ == "ApiActuator"
    assert act.base_url == "http://api"


@pytest.mark.parametrize(
    ("transaction", "execution", "success", "profile", "label", "exit_code"),
    [
        ("VERIFIED", "VERIFIED", True, "standard", "Done and checked", 0),
        (
            "COMPLETED_UNVERIFIED",
            "COMPLETED_UNVERIFIED",
            True,
            "demo",
            "Finished, not checked",
            0,
        ),
        (
            "COMPLETED_UNVERIFIED",
            "COMPLETED_UNVERIFIED",
            True,
            "standard",
            "Finished, not checked",
            1,
        ),
        (
            "HALTED_BEFORE_EFFECT",
            "HALTED",
            False,
            "standard",
            "Stopped before saving",
            1,
        ),
        ("RECONCILIATION_REQUIRED", "HALTED", False, "standard", "Check the record", 1),
        ("ROLLED_BACK", "HALTED", False, "standard", "Check the record", 1),
        (
            "SAVED_AWAITING_CHECK",
            "SAVED_AWAITING_CHECK",
            False,
            "standard",
            "Saved, waiting for its check",
            1,
        ),
        (None, "HALTED", False, "standard", "Check the record", 1),
        ("UNKNOWN_NEW_STATE", "HALTED", False, "standard", "Check the record", 1),
    ],
)
def test_run_summary_keeps_result_safety_and_exit_policy(
    tmp_path,
    monkeypatch,
    capsys,
    transaction,
    execution,
    success,
    profile,
    label,
    exit_code,
):
    from types import SimpleNamespace

    from openadapt_flow import __main__ as cli
    from openadapt_flow import report as report_module
    from openadapt_flow import rollback

    retained = tmp_path / "report.json"
    original = b'{"transaction_outcome":"RECONCILIATION_REQUIRED","unchanged":true}'
    retained.write_bytes(original)
    monkeypatch.setattr(
        report_module, "render_run_report", lambda folder: folder / "REPORT.md"
    )
    for helper in (
        "_maybe_report_break",
        "_maybe_report_run",
        "_maybe_attest_run",
    ):
        monkeypatch.setattr(cli, helper, lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_replay_outcome_epilogue", lambda *args: "")
    monkeypatch.setattr(cli, "_change_ledger_epilogue", lambda *args: "")
    monkeypatch.setattr(rollback, "rollback_lines", lambda report: [])
    report = SimpleNamespace(
        transaction_outcome=transaction,
        execution_outcome=execution,
        success=success,
        execution_profile=profile,
        screenshots_may_leave_box=False,
    )

    assert cli._finish_replay(tmp_path, report) == exit_code
    output = capsys.readouterr().out
    assert output.startswith(f"Result: {label}.")
    assert "Next: " in output
    assert f"Report: {tmp_path / 'REPORT.md'}" in output
    assert retained.read_bytes() == original
    if label == "Check the record":
        assert "Check the record before anyone runs this again." in output
        assert "Nothing was written" not in output
        assert "run it again" not in output
    if transaction == "SAVED_AWAITING_CHECK":
        assert "Don't run it again." in output
        assert "openadapt-flow deferred settle" in output
        assert "openadapt flow deferred settle" not in output
    if transaction:
        assert transaction not in output
