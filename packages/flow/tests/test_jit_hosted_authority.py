"""A hosted Standard run of an admitted program rests on its signed admission.

Real bundle, real Ed25519 admission signature, real run gate. No network.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import __main__ as cli
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.jit import hosted_authority as ha
from openadapt_flow.jit import lifecycle
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from openadapt_flow.runtime.effects.effect import Effect, EffectKind

CHECKS = (
    "schema_version: openadapt.verify/v1\n"
    "verifier:\n  kind: rest\n  base_url: https://app.example.com\n"
    "  records_path: /api/notes\n"
    "effects:\n  - kind: record_written\n    match: {type: note}\n    field: note\n"
)


def _keys(path: Path) -> Path:
    from openadapt_flow.execute.keys import load_or_create_private_key

    load_or_create_private_key(path)  # also writes keys/ed25519.pub
    return path


def _admit(bundle: Path, keys: Path, **overrides: Any) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from openadapt_flow.execute.keys import fingerprint_of

    public = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex((keys / "keys" / "ed25519.pub").read_text().strip())
    )
    workflow = Workflow.load(bundle)
    body: dict[str, Any] = {
        "schema": lifecycle.ADMISSION_SCHEMA,
        "signing_key": fingerprint_of(public),
        "admitted_by_principal_id": "Ada Lovelace",
        "admission_mode": "person",
        "state": lifecycle.BundleState.ADMITTED.value,
        "digest": workflow.manifest.content_digest,
        **overrides,
    }
    body["signature"] = lifecycle._sign_record(body, keys)
    (bundle / lifecycle.ADMISSION_FILE).write_text(json.dumps(body))


@pytest.fixture
def program(tmp_path: Path) -> dict[str, Any]:
    bundle = tmp_path / "bundle"
    steps = [
        Step(id="step_000", intent="type", action=ActionKind.TYPE, text="x"),
        Step(
            id="step_001",
            intent="click Save",
            action=ActionKind.CLICK,
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN, match={"type": "note"}, field="note"
                )
            ],
        ),
    ]
    Workflow(name="t", steps=steps).save(bundle)
    (bundle / ha.READBACK_PATH).parent.mkdir(parents=True)
    (bundle / ha.READBACK_PATH).write_text(CHECKS)
    keys = _keys(tmp_path / "keys")
    _admit(bundle, keys)
    workflow = Workflow.load(bundle)
    shape = ha.run_shape(bundle, workflow, policy_source="clinical-write")
    return {"bundle": bundle, "keys": keys, "workflow": workflow, "shape": shape}


def _authorization(shape: dict[str, Any], **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "bundle_content_digest": shape["bundle_content_digest"],
        "runtime_inputs_digest": "0" * 64,
        "admitted_policy_name": shape["admitted_policy_name"],
        "admitted_policy_contract_sha256": shape["admitted_policy_contract_sha256"],
        "execution_profile": shape["execution_profile"],
        "minimum_effect_tier": shape["minimum_effect_tier"],
        "qualified_effect_requirements": shape["qualified_effect_requirements"],
        "required_identity_step_ids": shape["required_identity_step_ids"],
        "approval_source": ha.approval_source(shape["admission_sha256"]),
    }
    values.update(overrides)
    return GovernedRunAuthorization.model_validate(values)


def test_run_shape_is_standard_value_free_and_names_the_admission(
    program: dict[str, Any],
) -> None:
    shape = program["shape"]
    assert shape["schema_version"] == ha.RUN_SHAPE_SCHEMA
    assert shape["execution_profile"] == "standard"
    assert shape["admitted_policy_name"] == "clinical-write"
    assert len(shape["admitted_policy_contract_sha256"]) == 64
    assert shape["admission_sha256"] == ha.admission_sha256(program["bundle"])
    assert shape["bundle_content_digest"] == (
        program["workflow"].manifest.content_digest
    )
    # Value-free: no step text or task value ever enters the shape.
    assert '"x"' not in json.dumps(shape)


def test_an_authorization_built_from_the_shape_matches_it(
    program: dict[str, Any],
) -> None:
    shape = program["shape"]
    assert ha.shape_matches_authorization(shape, _authorization(shape))
    other = _authorization(shape, approval_source=ha.approval_source("e" * 64))
    assert not ha.shape_matches_authorization(shape, other)
    assert not ha.shape_matches_authorization(
        shape, _authorization(shape, minimum_effect_tier=4)
    )


def test_a_signed_admission_authorizes_a_standard_managed_run(
    program: dict[str, Any],
) -> None:
    refusal = ha.managed_admission_refusal(
        program["bundle"],
        program["workflow"],
        _authorization(program["shape"]),
        profile="standard",
        key_dir=program["keys"],
    )
    assert refusal is None


@pytest.mark.parametrize("profile", ["regulated", "demo", None])
def test_only_the_standard_profile_is_accepted(
    program: dict[str, Any], profile: str | None
) -> None:
    refusal = ha.managed_admission_refusal(
        program["bundle"],
        program["workflow"],
        _authorization(program["shape"]),
        profile=profile,
        key_dir=program["keys"],
    )
    assert refusal is not None and "Standard" in refusal


def test_an_admission_signed_by_another_key_is_refused(
    program: dict[str, Any], tmp_path: Path
) -> None:
    other = _keys(tmp_path / "other-keys")
    refusal = ha.managed_admission_refusal(
        program["bundle"],
        program["workflow"],
        _authorization(program["shape"]),
        profile="standard",
        key_dir=other,
    )
    assert refusal == "the program's admission or read-back check does not verify"


def test_no_trusted_key_is_refused(program: dict[str, Any], tmp_path: Path) -> None:
    refusal = ha.managed_admission_refusal(
        program["bundle"],
        program["workflow"],
        _authorization(program["shape"]),
        profile="standard",
        key_dir=tmp_path / "empty",
    )
    assert refusal is not None


def test_a_re_signed_admission_does_not_match_the_authorization(
    program: dict[str, Any],
) -> None:
    # Same key, a different (valid) admission: the authorization named the
    # first one, so the run is refused.
    authorization = _authorization(program["shape"])
    _admit(program["bundle"], program["keys"], admitted_by_principal_id="Grace Hopper")
    refusal = ha.managed_admission_refusal(
        program["bundle"],
        program["workflow"],
        authorization,
        profile="standard",
        key_dir=program["keys"],
    )
    assert refusal == "the run authorization does not name this program's admission"


def test_a_dispatch_with_a_qualification_admission_is_not_this_path(
    program: dict[str, Any],
) -> None:
    authorization = SimpleNamespace(
        qualification_admission=object(),
        execution_profile="standard",
        approval_source=ha.approval_source(program["shape"]["admission_sha256"]),
    )
    refusal = ha.managed_admission_refusal(
        program["bundle"],
        program["workflow"],
        authorization,
        profile="standard",
        key_dir=program["keys"],
    )
    assert refusal == "the dispatch carries a qualification admission"


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_no_valid_read_back_check_is_refused(
    program: dict[str, Any], damage: str
) -> None:
    readback = program["bundle"] / ha.READBACK_PATH
    if damage == "missing":
        readback.unlink()
    else:
        readback.write_text("verifier: {foo: bar}\n")
    refusal = ha.managed_admission_refusal(
        program["bundle"],
        program["workflow"],
        _authorization(program["shape"]),
        profile="standard",
        key_dir=program["keys"],
    )
    assert refusal is not None


def test_managed_run_reads_back_through_the_bundled_check(
    program: dict[str, Any],
) -> None:
    args = argparse.Namespace(managed_dispatch_file="/dispatch.json", config=None)
    assert cli._stage_admitted_program_readback(args, program["bundle"]) is None
    _cfg, effects, _actuation = cli._deployment_sections(args)
    assert effects.kind == "rest"
    # Without a managed dispatch the bundle's file is never wired in.
    local = argparse.Namespace(config=None)
    assert cli._stage_admitted_program_readback(local, program["bundle"]) is None
    assert cli._deployment_sections(local)[1].kind == "none"


def test_an_invalid_bundled_check_refuses_the_managed_run(
    program: dict[str, Any],
) -> None:
    (program["bundle"] / ha.READBACK_PATH).write_text("verifier: {foo: bar}\n")
    args = argparse.Namespace(managed_dispatch_file="/dispatch.json", config=None)
    assert cli._stage_admitted_program_readback(args, program["bundle"]) == 2


def _dispatch(tmp_path: Path, program: dict[str, Any], **overrides: Any) -> Path:
    from openadapt_flow.runner.dispatch_envelope import (
        ManagedDispatchEnvelope,
        dispatch_binding_sha256,
        write_managed_dispatch_envelope_value,
    )
    from openadapt_flow.runtime.authorization import runtime_inputs_digest

    shape = program["shape"]
    inputs = runtime_inputs_digest(program["workflow"], {}, {})
    authorization = _authorization(shape, runtime_inputs_digest=inputs, **overrides)
    run_id = "55555555-5555-4555-8555-555555555555"
    return write_managed_dispatch_envelope_value(
        tmp_path / "managed-dispatch.json",
        ManagedDispatchEnvelope(
            run_id=run_id,
            bundle_content_digest=shape["bundle_content_digest"],
            runtime_inputs_digest=inputs,
            authorization=authorization,
            dispatch_binding_sha256=dispatch_binding_sha256(run_id, authorization),
        ),
    )


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The managed CLI up to the replay, which is recorded instead of run.

    The toy bundle is not certified, so the run gate's refusing gates are
    dropped; its shape fields (policy digest, tier, effect requirements,
    identity steps) are the real ones the dispatch must match.
    """

    from openadapt_flow import run_gate

    real_gate = run_gate.evaluate_run_gate

    def gate(*args: Any, **kwargs: Any) -> Any:
        report = real_gate(*args, **kwargs)
        return report.model_copy(
            update={"gates": [g for g in report.gates if g.passed]}
        )

    seen: dict[str, Any] = {}

    def replay(args: argparse.Namespace) -> int:
        seen["authorization"] = getattr(args, "_governed_run_authorization", None)
        seen["effects"] = cli._deployment_sections(args)[1]
        return 0

    monkeypatch.setattr(run_gate, "evaluate_run_gate", gate)
    monkeypatch.setattr(cli, "_cmd_replay", replay)
    monkeypatch.setattr(cli, "_attach_cloud_seal", lambda _args: None)
    return seen


def _run(tmp_path: Path, program: dict[str, Any], dispatch: Path, profile: str) -> int:
    params = tmp_path / "params.json"
    params.write_text("{}")
    return cli.main(
        [
            "run",
            str(program["bundle"]),
            "--profile",
            profile,
            "--policy",
            "clinical-write",
            "--params-file",
            str(params),
            "--allow-unencrypted",
            "--managed-dispatch-file",
            str(dispatch),
            "--backend",
            "web",
            "--url",
            "https://app.example.com/",
        ]
    )


def test_cli_admits_the_published_shape_through_the_managed_gate(
    program: dict[str, Any],
    runner: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The runner stages the organization's registered admission key here.
    monkeypatch.setenv(lifecycle.KEY_DIR_ENV, str(program["keys"]))
    rc = _run(tmp_path, program, _dispatch(tmp_path, program), "standard")
    out = capsys.readouterr().out
    assert rc == 0, out
    authorization = runner["authorization"]
    assert authorization is not None
    assert authorization.approval_source == ha.approval_source(
        program["shape"]["admission_sha256"]
    )
    assert ha.shape_matches_authorization(program["shape"], authorization)
    # The replay reads back through the bundled verify.yaml.
    assert runner["effects"].kind == "rest"


@pytest.mark.parametrize(
    "case", ["regulated", "foreign_key", "other_admission", "shape_mismatch"]
)
def test_cli_refuses_a_managed_admitted_program_that_does_not_bind(
    program: dict[str, Any],
    runner: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    monkeypatch.setenv(lifecycle.KEY_DIR_ENV, str(program["keys"]))
    overrides: dict[str, Any] = {}
    profile = "standard"
    if case == "regulated":
        profile = "regulated"
    elif case == "foreign_key":
        monkeypatch.setenv(lifecycle.KEY_DIR_ENV, str(_keys(tmp_path / "foreign")))
    elif case == "other_admission":
        overrides["approval_source"] = ha.approval_source("e" * 64)
    else:
        overrides["minimum_effect_tier"] = 4
    rc = _run(tmp_path, program, _dispatch(tmp_path, program, **overrides), profile)
    out = capsys.readouterr().out
    assert rc == 2, out
    assert "Nothing was executed" in out
    assert "authorization" not in runner
    expected = {
        # Refused before the admission check: Regulated needs encryption and
        # a qualification campaign (unit-tested above).
        "regulated": "regulated profile",
        "foreign_key": "admission or read-back check does not verify",
        "other_admission": "does not name this program's admission",
        "shape_mismatch": "managed dispatch does not match this exact run",
    }[case]
    assert expected in out
