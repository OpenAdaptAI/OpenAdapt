"""Fail-closed admission gate for ``openadapt-flow run`` (openadapt_flow.run_gate).

A fully-armed + certified + effect-covered + encrypted + integrity-sealed bundle
is ADMITTED; each individually broken gate (uncertified / unarmed consequential
click / screen-only write / unverifiable write with no approval / unencrypted /
tampered manifest / version-pin mismatch) is REFUSED, naming the failing gate.
A CLI test confirms ``run`` refuses without executing (and ``--dry-run`` reports).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.deployment import (
    DeploymentConfig,
    EffectsConfig,
    PolicySection,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ApiBinding,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.run_gate import (
    GATE_APPROVAL,
    GATE_CERTIFICATION,
    GATE_EFFECT,
    GATE_ENCRYPTION,
    GATE_IDENTITY,
    GATE_MANIFEST,
    build_runtime_authorization,
    evaluate_run_gate,
)
from openadapt_flow.runtime.effects import Effect, EffectKind

_KEY = "correct horse battery staple"
_PC = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved OK")]


def _click(
    step_id: str,
    intent: str,
    *,
    ocr: str = "Button",
    armed: bool = True,
    risk: str = "reversible",
    effects: list[Effect] | None = None,
    expect=None,
) -> Step:
    return Step(
        id=step_id,
        intent=intent,
        action=ActionKind.CLICK,
        risk=risk,
        anchor=Anchor(
            template=f"{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text=ocr,
            context_text="Row 42 Jane Doe" if armed else None,
        ),
        identity_armed=armed,
        identity_unarmed_reason=None if armed else "no readable row band",
        effects=list(effects) if effects is not None else [],
        expect=list(expect) if expect is not None else list(_PC),
    )


def _write_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": "p1"},
        idempotency_key="run-1",
        risk="irreversible",
    )


def _good_workflow(name: str = "good") -> Workflow:
    """A bundle that PASSES clinical-write: an armed navigation click and an
    armed irreversible write declaring a keyed system-of-record effect."""
    return Workflow(
        name=name,
        steps=[
            _click("s0", "click 'Open chart'", ocr="Open"),
            _click(
                "s1",
                "click 'Save note'",
                ocr="Save",
                risk="irreversible",
                effects=[_write_effect()],
            ),
        ],
    )


def _seal(
    wf: Workflow, tmp_path: Path, *, encrypt: bool = True
) -> tuple[Workflow, Path]:
    """Save ``wf`` (optionally encrypted), then load it back so it carries a
    sealed manifest and the real at-rest ``encrypted`` flag."""
    bundle = tmp_path / wf.name
    if encrypt:
        wf.save(bundle, encrypt=True, key=_KEY)
        loaded = Workflow.load(bundle, key=_KEY)
    else:
        wf.save(bundle)
        loaded = Workflow.load(bundle)
    return loaded, bundle


def _deployment(
    *, verifier: bool = True, policy: str = "clinical-write"
) -> DeploymentConfig:
    effects = (
        EffectsConfig(kind="rest", base_url="http://sor.local")
        if verifier
        else EffectsConfig(kind="none")
    )
    return DeploymentConfig(effects=effects, policy=PolicySection(policy=policy))


def _run(
    wf: Workflow,
    bundle: Path,
    *,
    verifier: bool = True,
    **kw,
):
    dep = _deployment(verifier=verifier)
    return evaluate_run_gate(
        wf,
        bundle_dir=bundle,
        deployment=dep,
        effect_verifier=object() if verifier else None,
        **kw,
    )


# ---------------------------------------------------------------------------
# The pass path
# ---------------------------------------------------------------------------


def test_fully_covered_bundle_is_admitted(tmp_path):
    wf, bundle = _seal(_good_workflow(), tmp_path)
    report = _run(wf, bundle)
    assert report.passed, report.render()
    assert report.refusals == []
    # Every gate individually passed.
    assert all(g.passed for g in report.gates)


# ---------------------------------------------------------------------------
# Gate 1: certification
# ---------------------------------------------------------------------------


def test_uncertified_bundle_refused(tmp_path):
    # An unarmed navigation click + unarmed vacuous write: fails clinical-write.
    wf = Workflow(
        name="uncertified",
        steps=[
            _click("s0", "click 'Jane Doe'", ocr="Jane", armed=False),
            _click(
                "s1",
                "click 'Save'",
                ocr="Save",
                armed=False,
                risk="irreversible",
                effects=[],
                expect=[],
            ),
        ],
    )
    wf, bundle = _seal(wf, tmp_path)
    report = _run(wf, bundle)
    assert not report.passed
    g = report.gate(GATE_CERTIFICATION)
    assert g is not None and not g.passed
    assert "not certified" in g.detail.lower()


def test_unknown_policy_refused(tmp_path):
    wf, bundle = _seal(_good_workflow(), tmp_path)
    report = _run(wf, bundle, policy_source="no-such-policy")
    g = report.gate(GATE_CERTIFICATION)
    assert g is not None and not g.passed
    assert "could not be loaded" in g.detail


# ---------------------------------------------------------------------------
# Gate 2: identity coverage
# ---------------------------------------------------------------------------


def test_unarmed_consequential_click_refused(tmp_path):
    wf = _good_workflow("unarmed")
    # Disarm the irreversible write step's identity check.
    wf.steps[1].identity_armed = False
    wf.steps[1].anchor.context_text = None
    wf, bundle = _seal(wf, tmp_path)
    report = _run(wf, bundle)
    assert not report.passed
    g = report.gate(GATE_IDENTITY)
    assert g is not None and not g.passed
    assert "UNARMED" in g.detail
    assert "s1" in g.offenders


# ---------------------------------------------------------------------------
# Gate 3: effect coverage
# ---------------------------------------------------------------------------


def test_screen_only_write_refused(tmp_path):
    wf = _good_workflow("screen_only")
    # Strip the system-of-record effect: the write is verified by screen only.
    wf.steps[1].effects = []
    wf, bundle = _seal(wf, tmp_path)
    report = _run(wf, bundle)
    assert not report.passed
    g = report.gate(GATE_EFFECT)
    assert g is not None and not g.passed
    assert "SCREEN only" in g.detail
    assert "s1" in g.offenders


def test_unconfirmed_effect_binding_refused(tmp_path):
    wf = _good_workflow("unconfirmed")
    eff = _write_effect()
    eff.needs_operator_confirmation = True
    wf.steps[1].effects = [eff]
    wf, bundle = _seal(wf, tmp_path)
    report = _run(wf, bundle)
    g = report.gate(GATE_EFFECT)
    assert g is not None and not g.passed
    assert "UNCONFIRMED" in g.detail
    assert "s1" in g.offenders


# ---------------------------------------------------------------------------
# Gate 4: approval fallback
# ---------------------------------------------------------------------------


def test_unverifiable_write_without_approval_refused(tmp_path):
    # Bundle is fine, but the deployment configures NO verifier.
    wf, bundle = _seal(_good_workflow("no_verifier"), tmp_path)
    report = _run(wf, bundle, verifier=False)
    assert not report.passed
    g = report.gate(GATE_APPROVAL)
    assert g is not None and not g.passed
    assert "cannot be independently verified" in g.detail
    assert "s1" in g.offenders


def test_unverifiable_write_with_explicit_approval_admitted(tmp_path):
    wf, bundle = _seal(_good_workflow("approved"), tmp_path)
    report = _run(wf, bundle, verifier=False, approval_available=True)
    g = report.gate(GATE_APPROVAL)
    assert g is not None and g.passed
    assert "EXPLICITLY approved" in g.detail
    # With the fallback satisfied, the whole run is admitted.
    assert report.passed, report.render()


def test_unverified_approval_requires_screen_postcondition(tmp_path):
    wf = _good_workflow("vacuous_approval")
    wf.steps[1].expect = []
    wf, bundle = _seal(wf, tmp_path)
    report = _run(
        wf,
        bundle,
        verifier=False,
        approval_available=True,
        policy_source="permissive",
    )

    gate = report.gate(GATE_APPROVAL)
    assert gate is not None and not gate.passed
    assert gate.offenders == ["s1"]
    assert "no screen postcondition" in gate.detail


def test_approval_is_bound_to_bundle_steps_and_effect_contracts(tmp_path):
    wf, bundle = _seal(_good_workflow("bound_approval"), tmp_path)
    report = _run(wf, bundle, verifier=False, approval_available=True)
    authorization = build_runtime_authorization(
        wf,
        report,
    )

    assert authorization.validate_workflow(wf) is None
    assert authorization.required_identity_step_ids == ("s0", "s1")
    assert authorization.approves_unverified_write(wf.steps[1])

    changed = wf.model_copy(deep=True)
    changed.steps[1].effects[0].match = {"patient_id": "different"}
    assert "in-memory workflow semantics" in (
        authorization.validate_workflow(changed) or ""
    )


def test_authorization_factory_rejects_report_from_other_workflow(tmp_path):
    wf_a, bundle_a = _seal(_good_workflow("workflow_a"), tmp_path)
    wf_b, _bundle_b = _seal(_good_workflow("workflow_b"), tmp_path)
    report_a = _run(wf_a, bundle_a, verifier=False, approval_available=True)

    with pytest.raises(ValueError, match="different workflow"):
        build_runtime_authorization(wf_b, report_a)


def test_verifier_admission_cannot_be_reinterpreted_as_unverified_approval(tmp_path):
    wf, bundle = _seal(_good_workflow("verified_only"), tmp_path)
    report = _run(wf, bundle, verifier=True, approval_available=True)
    authorization = build_runtime_authorization(wf, report)

    assert report.unverified_write_approval_granted is False
    assert authorization.unverified_write_approvals == ()


def test_direct_api_write_cannot_use_unverified_approval(tmp_path):
    wf = _good_workflow("api_unverified")
    wf.steps[1].api_binding = ApiBinding(url_template="/api/encounter")
    wf, bundle = _seal(wf, tmp_path)
    report = evaluate_run_gate(
        wf,
        bundle_dir=bundle,
        deployment=_deployment(verifier=False),
        effect_verifier=None,
        api_actuator=object(),
        approval_available=True,
    )

    gate = report.gate(GATE_APPROVAL)
    assert gate is not None and not gate.passed
    assert "direct API write" in gate.detail


# ---------------------------------------------------------------------------
# Gate 5: encryption
# ---------------------------------------------------------------------------


def test_unencrypted_bundle_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("plain"), tmp_path, encrypt=False)
    report = _run(wf, bundle)
    assert not report.passed
    g = report.gate(GATE_ENCRYPTION)
    assert g is not None and not g.passed
    assert "NOT encrypted" in g.detail


def test_unencrypted_allowed_with_escape_hatch(tmp_path):
    wf, bundle = _seal(_good_workflow("plain2"), tmp_path, encrypt=False)
    report = _run(wf, bundle, require_encryption=False)
    g = report.gate(GATE_ENCRYPTION)
    assert g is not None and g.passed
    assert report.passed, report.render()


def test_unsealed_templates_warn_by_default_refuse_when_strict(tmp_path):
    wf, bundle = _seal(_good_workflow("tmpl"), tmp_path, encrypt=False)
    (bundle / "templates").mkdir(exist_ok=True)
    (bundle / "templates" / "s0.png").write_bytes(b"\x89PNG plaintext crop")

    warn = _run(wf, bundle, require_encryption=False)
    g = warn.gate(GATE_ENCRYPTION)
    assert g is not None and g.passed and g.warning
    assert warn.passed  # a warning does not fail the run
    assert "WARNING: 1 template/screenshot asset(s) are unsealed" in g.detail

    strict = _run(wf, bundle, strict_templates=True, require_encryption=False)
    gs = strict.gate(GATE_ENCRYPTION)
    assert gs is not None and not gs.passed
    assert not strict.passed
    assert any("s0.png" in o for o in gs.offenders)


def test_encrypted_templates_pass_strict_gate(tmp_path):
    workflow = _good_workflow("sealed-templates")
    bundle = tmp_path / workflow.name
    templates = bundle / "templates"
    templates.mkdir(parents=True)
    (templates / "s0.png").write_bytes(b"\x89PNG sealed patient crop")
    workflow.save(bundle, encrypt=True, key=_KEY)
    loaded = Workflow.load(bundle, key=_KEY)

    assert not (templates / "s0.png").exists()
    assert (templates / "s0.png.enc").is_file()
    report = _run(loaded, bundle, strict_templates=True)
    gate = report.gate(GATE_ENCRYPTION)

    assert gate is not None and gate.passed and not gate.warning
    assert gate.offenders == []
    assert "1 template/screenshot asset(s) encrypted at rest" in gate.detail
    assert report.passed, report.render()


def test_encrypted_bundle_with_plaintext_template_leak_is_always_refused(tmp_path):
    workflow = _good_workflow("mixed-templates")
    bundle = tmp_path / workflow.name
    templates = bundle / "templates"
    templates.mkdir(parents=True)
    (templates / "s0.png").write_bytes(b"\x89PNG sealed patient crop")
    workflow.save(bundle, encrypt=True, key=_KEY)
    loaded = Workflow.load(bundle, key=_KEY)
    (templates / "leaked.png").write_bytes(b"\x89PNG unexpected cleartext")

    report = _run(loaded, bundle, strict_templates=False)
    gate = report.gate(GATE_ENCRYPTION)

    assert gate is not None and not gate.passed and not gate.warning
    assert gate.offenders == ["templates/leaked.png"]
    assert "mixed encrypted/plaintext bundles are refused" in gate.detail
    assert "[REFUSE] Encrypted bundle" in report.render()


def test_encrypted_bundle_missing_declared_ciphertext_is_refused(tmp_path):
    workflow = _good_workflow("missing-ciphertext")
    bundle = tmp_path / workflow.name
    templates = bundle / "templates"
    templates.mkdir(parents=True)
    (templates / "s0.png").write_bytes(b"\x89PNG sealed patient crop")
    workflow.save(bundle, encrypt=True, key=_KEY)
    loaded = Workflow.load(bundle, key=_KEY)
    (templates / "s0.png.enc").unlink()

    report = _run(loaded, bundle, strict_templates=True)
    gate = report.gate(GATE_ENCRYPTION)

    assert gate is not None and not gate.passed
    assert gate.offenders == ["templates/s0.png"]
    assert "lack authenticated ciphertext coverage" in gate.detail


# ---------------------------------------------------------------------------
# Gate 6: sealed manifest + version pin
# ---------------------------------------------------------------------------


def test_tampered_manifest_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("tampered"), tmp_path)
    # Corrupt the loaded manifest's sealed digest so re-verification fails.
    assert wf.manifest is not None
    wf.manifest.content_digest = "0" * 64
    report = _run(wf, bundle)
    g = report.gate(GATE_MANIFEST)
    assert g is not None and not g.passed
    assert "integrity FAILED" in g.detail


def test_missing_manifest_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("nomanifest"), tmp_path)
    wf.manifest = None
    report = _run(wf, bundle)
    g = report.gate(GATE_MANIFEST)
    assert g is not None and not g.passed
    assert "no integrity-sealed manifest" in g.detail


def test_version_pin_mismatch_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("pin"), tmp_path)
    report = _run(wf, bundle, pinned_compiler_version="99.99.99")
    g = report.gate(GATE_MANIFEST)
    assert g is not None and not g.passed
    assert "does not match the pinned version" in g.detail


def test_digest_pin_mismatch_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("dpin"), tmp_path)
    report = _run(wf, bundle, pinned_content_digest="f" * 64)
    g = report.gate(GATE_MANIFEST)
    assert g is not None and not g.passed
    assert "pinned digest" in g.detail


def test_matching_version_pin_admitted(tmp_path):
    wf, bundle = _seal(_good_workflow("pinok"), tmp_path)
    assert wf.manifest is not None
    report = _run(
        wf,
        bundle,
        pinned_content_digest=wf.manifest.content_digest,
        pinned_compiler_version=wf.manifest.provenance.compiler_version or None,
    )
    g = report.gate(GATE_MANIFEST)
    assert g is not None and g.passed
    assert report.passed, report.render()


# ---------------------------------------------------------------------------
# CLI: `run` refuses without executing; `--dry-run` reports only
# ---------------------------------------------------------------------------


def _no_execute(monkeypatch):
    """Make _cmd_replay explode if ever called, so a refusal that still executes
    is caught."""
    import openadapt_flow.__main__ as main

    def boom(_args):  # pragma: no cover - must never run in these tests
        raise AssertionError("run executed a bundle it should have refused")

    monkeypatch.setattr(main, "_cmd_replay", boom)


def test_cli_run_refuses_uncertified_without_executing(tmp_path, monkeypatch, capsys):
    import openadapt_flow.__main__ as main

    _no_execute(monkeypatch)
    # A plaintext, uncertified bundle: refused at the first failing gate.
    wf = Workflow(
        name="cli_bad",
        steps=[_click("s0", "click 'Jane'", ocr="Jane", armed=False)],
    )
    _, bundle = _seal(wf, tmp_path, encrypt=False)

    parser = main.build_parser()
    args = parser.parse_args(["run", str(bundle), "--policy", "clinical-write"])
    rc = args.func(args)
    assert rc == 2
    out = capsys.readouterr().out
    assert "REFUSE" in out
    assert "Nothing was executed" in out


def test_cli_run_dry_run_reports_without_executing(tmp_path, monkeypatch, capsys):
    import openadapt_flow.__main__ as main

    _no_execute(monkeypatch)
    wf, bundle = _seal(_good_workflow("cli_good"), tmp_path)

    parser = main.build_parser()
    args = parser.parse_args(
        [
            "run",
            str(bundle),
            "--config",  # not used; policy via flag
            *[],
        ][:2]
        + [
            "--policy",
            "clinical-write",
            "--effects-kind",
            "rest",
            "--effects-base-url",
            "http://sor.local",
            "--dry-run",
        ]
    )
    # Provide the decryption key via the environment (bundle is encrypted).
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ADMIT" in out
    # Dry-run must NOT execute even on an admitted bundle.
    assert "Replay" not in out


def test_cli_run_hands_bound_authorization_to_replay(tmp_path, monkeypatch, capsys):
    import openadapt_flow.__main__ as main

    wf, bundle = _seal(_good_workflow("cli_approved"), tmp_path)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    captured = {}

    def capture(args):
        captured["authorization"] = args._governed_run_authorization
        return 0

    monkeypatch.setattr(main, "_cmd_replay", capture)
    parser = main.build_parser()
    args = parser.parse_args(
        [
            "run",
            str(bundle),
            "--policy",
            "clinical-write",
            "--approve-unverified-writes",
        ]
    )

    assert args.func(args) == 0
    authorization = captured["authorization"]
    assert authorization.validate_workflow(wf) is None
    assert authorization.approves_unverified_write(wf.steps[1])
    assert "EXPLICITLY approved" in capsys.readouterr().out


def test_cli_run_params_file_binds_authorization_without_values_in_argv(
    tmp_path, monkeypatch
):
    import openadapt_flow.__main__ as main
    from openadapt_flow.runtime.authorization import runtime_inputs_digest

    wf, bundle = _seal(_good_workflow("cli_params_file"), tmp_path)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    params_file = tmp_path / "runtime-params.json"
    secret_value = "patient-secret-not-on-argv"
    params_file.write_text(
        json.dumps(
            {
                "patient_id": secret_value,
                "count": 2,
                "approved": True,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def capture(args):
        captured["authorization"] = args._governed_run_authorization
        return 0

    monkeypatch.setattr(main, "_cmd_replay", capture)
    argv = [
        "run",
        str(bundle),
        "--policy",
        "clinical-write",
        "--approve-unverified-writes",
        "--params-file",
        str(params_file),
        "--param",
        "count=3",
    ]
    assert secret_value not in " ".join(argv)
    args = main.build_parser().parse_args(argv)
    assert args.params_file == str(params_file)
    assert secret_value not in repr(vars(args))
    assert args.func(args) == 0

    expected = {
        "patient_id": secret_value,
        "count": "3",
        "approved": "True",
    }
    authorization = captured["authorization"]
    assert authorization.runtime_inputs_digest == runtime_inputs_digest(
        wf, expected, None
    )


@pytest.mark.parametrize(
    "payload,error",
    [
        pytest.param("{", "could not be read as JSON", id="malformed"),
        pytest.param("[]", "must contain one JSON object", id="non-object"),
        pytest.param(
            json.dumps({f"p{i}": i for i in range(101)}),
            "may contain at most 100 parameters",
            id="too-many-parameters",
        ),
        pytest.param(
            json.dumps({"patient_id": None}),
            "value for 'patient_id' must be a scalar",
            id="null-value",
        ),
        pytest.param(
            json.dumps({"patient_id": ["p1"]}),
            "value for 'patient_id' must be a scalar",
            id="list-value",
        ),
        pytest.param(
            json.dumps({"patient_id": {"id": "p1"}}),
            "value for 'patient_id' must be a scalar",
            id="object-value",
        ),
    ],
)
def test_cli_run_rejects_invalid_params_file_without_executing(
    tmp_path, monkeypatch, payload, error
):
    import openadapt_flow.__main__ as main

    _no_execute(monkeypatch)
    _wf, bundle = _seal(_good_workflow("cli_invalid_params"), tmp_path)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    params_file = tmp_path / "runtime-params.json"
    params_file.write_text(payload, encoding="utf-8")
    args = main.build_parser().parse_args(
        [
            "run",
            str(bundle),
            "--policy",
            "clinical-write",
            "--approve-unverified-writes",
            "--params-file",
            str(params_file),
        ]
    )

    with pytest.raises(SystemExit, match=error):
        args.func(args)


@pytest.mark.parametrize("encrypt", [True, False])
def test_cli_run_encryption_key_required_for_encrypted_bundle(
    tmp_path, monkeypatch, capsys, encrypt
):
    import openadapt_flow.__main__ as main

    _no_execute(monkeypatch)
    wf, bundle = _seal(
        _good_workflow(f"cli_key_{int(encrypt)}"), tmp_path, encrypt=encrypt
    )
    monkeypatch.delenv("OPENADAPT_BUNDLE_KEY", raising=False)

    parser = main.build_parser()
    args = parser.parse_args(
        [
            "run",
            str(bundle),
            "--policy",
            "clinical-write",
            "--effects-kind",
            "rest",
            "--effects-base-url",
            "http://sor.local",
            "--allow-unencrypted",  # isolate the KEY-at-load behavior from gate 5
            "--dry-run",
        ]
    )
    rc = args.func(args)
    out = capsys.readouterr().out
    if encrypt:
        # No key configured -> the encrypted bundle cannot even be loaded: refused.
        assert rc == 2
        assert "could not be loaded safely" in out
    else:
        assert rc == 0
        assert "ADMIT" in out
