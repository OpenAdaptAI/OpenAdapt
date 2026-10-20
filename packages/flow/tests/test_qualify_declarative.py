"""verify.yaml, readiness checks, the fault driver's offline paths and the CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.__main__ import QUALIFY_DEFINITION, _qualify_default_form
from openadapt_flow.ir import ActionKind, Anchor, Step, StructuralLocator, Workflow
from openadapt_flow.qualify import (
    QualifyConfigError,
    QualifyError,
    WebFaultDriver,
    parse_verify_config,
    qualify,
)
from openadapt_flow.qualify.campaign import (
    apply_verify_config,
    check_ready,
    record_effects,
    resolve_save_step,
)
from openadapt_flow.qualify.faults import ScreenOnlyRecordCheck
from openadapt_flow.verification import VerificationTier

H = "a" * 64
APP = {"name": "Demo", "version": "1", "url": "http://127.0.0.1:9301/"}


def _config(**over):
    data = {
        "schema_version": "openadapt.verify/v1",
        "app": {**APP, "reset": {"command": "true"}},
        "verifier": {
            "kind": "rest",
            "base_url": "http://127.0.0.1:9300",
            "records_path": "/api/patient",
            "records_key": "data",
        },
        "record": {
            "name": "patient",
            "match": ["email"],
            "duplicate_key": "email",
            "fields": {"email": "email", "lname": "lname", "state": {"literal": "MA"}},
        },
    }
    data.update(over)
    return parse_verify_config(data, env={})


def _workflow(*, armed=True, risk="irreversible") -> Workflow:
    save = Step(
        id="s3",
        intent="Create patient",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/s3.png",
            region=(10, 10, 80, 20),
            click_point=(20, 20),
            structural=StructuralLocator(selector="#create", role="button"),
            context_text="Create New Patient",
        ),
        identity_armed=armed,
        risk=risk,
    )
    return Workflow(
        name="register",
        steps=[
            Step(id="s1", intent="email", action=ActionKind.TYPE, param="email"),
            Step(id="s2", intent="name", action=ActionKind.TYPE, param="lname"),
            save,
        ],
        params={"email": "a@example.test", "lname": "Doe"},
    )


# -- verify.yaml --------------------------------------------------------------
def test_minimal_config_parses_with_defaults():
    config = _config()
    assert config.campaign.trials == 3
    assert config.campaign.policy == "clinical-write"
    assert len(config.campaign.cases) == 6
    assert config.save_step == "auto"


def test_env_expansion_and_missing_variables():
    data = {
        "schema_version": "openadapt.verify/v1",
        "app": {
            **APP,
            "url": "${UI:-http://127.0.0.1:1/}",
            "reset": {"command": "true"},
        },
        "verifier": {
            "kind": "rest",
            "base_url": "${API}",
            "records_path": "/r",
        },
    }
    config = parse_verify_config(data, env={"API": "http://127.0.0.1:2"})
    assert config.app.url == "http://127.0.0.1:1/"
    with pytest.raises(QualifyConfigError, match="API"):
        parse_verify_config(data, env={})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"verifier": {"kind": "onscreen"}}, "verifier"),
        (
            {"record": {"name": "p", "match": ["email"], "fields": {"lname": "lname"}}},
            "match",
        ),
        (
            {
                "record": {
                    "name": "p",
                    "match": ["email"],
                    "duplicate_key": "mrn",
                    "fields": {"email": "email"},
                }
            },
            "duplicate_key",
        ),
        ({"campaign": {"cases": ["representative", "fault-typo"]}}, "fault-typo"),
        ({"app": {**APP, "reset": {"command": "a", "python": "m:f"}}}, "reset"),
    ],
)
def test_config_errors_name_the_problem(change, message):
    with pytest.raises(QualifyConfigError, match=message):
        _config(**change)


# -- binding and readiness ----------------------------------------------------
def test_record_effects_bind_selector_fields_and_duplicate_key():
    effects = record_effects(_config())
    kinds = [e.kind.value for e in effects]
    assert kinds[0] == "record_written"
    assert set(effects[0].match) == {"email"}
    assert effects[0].idempotency_key is not None
    assert kinds.count("field_equals") == 2  # lname, state


def test_auto_save_step_and_ready_campaign():
    workflow = _workflow()
    config = _config()
    save, notes = apply_verify_config(workflow, config)
    assert save.id == "s3" and save.effects
    problems = check_ready(workflow, config, save, dict(workflow.params))
    assert problems == []


def test_check_ready_lists_every_problem():
    workflow = _workflow(armed=False)
    config = _config(app=APP)  # no reset
    save, _ = apply_verify_config(workflow, config)
    problems = " | ".join(check_ready(workflow, config, save, {"email": "a@x"}))
    assert "reset" in problems
    assert "lname" in problems
    assert "identity" in problems


def test_no_irreversible_step_is_a_clear_error():
    workflow = _workflow(risk="reversible")
    workflow.steps[2].risk = "reversible"
    with pytest.raises(QualifyConfigError, match="save_step"):
        resolve_save_step(workflow, _config())


def test_qualify_refuses_non_web_target_before_running(tmp_path):
    bundle = tmp_path / "bundle"
    _workflow().save(bundle)
    config = _config(app={**APP, "target": "rdp", "reset": {"command": "true"}})
    with pytest.raises(QualifyConfigError, match="rdp"):
        qualify(bundle, config, out=tmp_path / "out", log=lambda _l: None)


def test_qualify_refuses_existing_output(tmp_path):
    bundle = tmp_path / "bundle"
    _workflow().save(bundle)
    (tmp_path / "out").mkdir()
    with pytest.raises(QualifyError, match="already exists"):
        qualify(bundle, _config(), out=tmp_path / "out", log=lambda _l: None)


# -- fault driver -------------------------------------------------------------
GATES = {"weak_effect": "effect_strength", "missing_effect": "effect_verifier"}


def _context(kind, backend=None):
    return SimpleNamespace(
        fault_kind=kind,
        backend=backend,
        actuation_path="gui",
        effect_verifier=object(),
        effects=[],
        project_id="p",
        project_revision=1,
        project_contract_sha256=H,
        campaign_id_sha256=H,
        case_id_sha256=H,
        case_input_sha256=H,
        run_id_sha256=H,
        step_id="s3",
        gate=GATES.get(kind, "identity_verification"),
        before_input_sha256=H,
    )


def test_screen_faults_decline_without_a_browser():
    driver = WebFaultDriver(_workflow())
    for kind in ("ambiguity", "wrong_identity", "stale_identity"):
        assert driver.mutate(_context(kind, backend=SimpleNamespace())) is None
        assert driver.declined == "screen_fault_needs_a_browser"


def test_effect_faults_replace_the_verifier():
    driver = WebFaultDriver(_workflow())
    weak = driver.mutate(_context("weak_effect"))
    assert weak.replace_effect_verifier
    assert isinstance(weak.effect_verifier, ScreenOnlyRecordCheck)
    assert weak.effect_verifier.verification_tier is VerificationTier.IMMEDIATE_SCREEN
    missing = driver.mutate(_context("missing_effect"))
    assert missing.replace_effect_verifier and missing.effect_verifier is None
    assert missing.receipt is not None


# -- CLI ----------------------------------------------------------------------
def test_default_form_routes_bundle_to_campaign():
    assert _qualify_default_form(["qualify", "b", "--verify", "v.yaml"])[:3] == [
        "qualify",
        "campaign",
        "b",
    ]
    assert _qualify_default_form(["qualify", "run", "x"]) == ["qualify", "run", "x"]
    assert _qualify_default_form(["run", "b"]) == ["run", "b"]


def test_qualify_help_opens_with_the_definition():
    out = subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "qualify", "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.splitlines()[0] == QUALIFY_DEFINITION
    assert "https://docs.openadapt.ai/reference/glossary/#qualify" in out


def test_cli_reports_config_errors(tmp_path):
    bundle = tmp_path / "bundle"
    _workflow().save(bundle)
    (tmp_path / "verify.yaml").write_text(
        "schema_version: openadapt.verify/v1\napp: {name: d, version: '1', url: 'http://x/'}\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "qualify", str(bundle),
         "--verify", str(tmp_path / "verify.yaml"), "--out", str(tmp_path / "o")],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )  # fmt: skip
    assert result.returncode == 2
    assert "verifier" in result.stderr


def test_a_qualified_bundle_can_be_qualified_again():
    # A sealed or repaired copy of a qualified bundle carries its project.
    # Qualifying it again replaces the project with its next revision.
    from openadapt_flow.qualify.campaign import configure_project

    workflow = _workflow()
    config = _config()
    save, _notes = apply_verify_config(workflow, config)
    kwargs = dict(
        save=save,
        params=dict(workflow.params),
        environment={"app": "demo"},
        campaign_id="first",
        minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    configure_project(workflow, config, **kwargs)
    first = workflow.qualification
    assert first is not None
    first_digest = first.revision_digest()

    configure_project(workflow, config, **{**kwargs, "campaign_id": "again"})

    again = workflow.qualification
    assert again is not None and again is not first
    assert again.project_id == first.project_id
    # The revision continues past the prior project's (every edit counts).
    assert again.revision > first.revision
    assert again.revision_digest() != first_digest
    assert again.last_certification is None
    assert sorted(c.id for c in again.cases) == sorted(c.id for c in first.cases)
