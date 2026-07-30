"""Headless contract coverage for the real-RDP ladder qualification harness.

These tests never start Docker, capture a display, or inject input. They guard
the evidence acceptance logic, explicit identity marking, honest committed
partial result, and manual-only release-lane workflow.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.policy import load_policy

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "benchmark" / "rdp_ladder" / "run_rdp_ladder_qualification.py"
SPEC = importlib.util.spec_from_file_location("rdp_ladder_qualification", HARNESS)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qualification
SPEC.loader.exec_module(qualification)
RENDERER = REPO / "benchmark" / "rdp_ladder" / "render_presentation.py"
RENDER_SPEC = importlib.util.spec_from_file_location("rdp_ladder_renderer", RENDERER)
assert RENDER_SPEC is not None and RENDER_SPEC.loader is not None
renderer = importlib.util.module_from_spec(RENDER_SPEC)
sys.modules[RENDER_SPEC.name] = renderer
RENDER_SPEC.loader.exec_module(renderer)


def _healthy(condition_trial: int) -> dict:
    return {
        "condition_trial": condition_trial,
        "passed": True,
        "model_calls": 0,
        "structural_rung_used": 0,
        "visual_rungs_used": {"template": 3},
        "effect_confirmed": True,
        "runtime_effect_verified": True,
        "policy_admitted": True,
        "identity_required": True,
        "identity_verified": True,
        "silent_incorrect_success": False,
        "over_halt": False,
    }


def _drift(condition_trial: int) -> dict:
    return {
        "condition_trial": condition_trial,
        "passed": True,
        "model_calls": 0,
        "halted": True,
        "silent_write": False,
        "false_completion": False,
        "policy_bound": True,
    }


def test_acceptance_requires_exact_fail_closed_three_plus_three() -> None:
    healthy = [_healthy(i) for i in range(1, 4)]
    drift = [_drift(i) for i in range(1, 4)]
    assert qualification._accepted_contract(healthy, drift)

    for collection, index, key, value in (
        (healthy, 0, "runtime_effect_verified", False),
        (healthy, 1, "identity_verified", False),
        (healthy, 2, "silent_incorrect_success", True),
        (healthy, 2, "visual_rungs_used", {}),
        (drift, 0, "silent_write", True),
        (drift, 1, "false_completion", True),
        (drift, 2, "policy_bound", False),
    ):
        broken_healthy = copy.deepcopy(healthy)
        broken_drift = copy.deepcopy(drift)
        target = broken_healthy if collection is healthy else broken_drift
        target[index][key] = value
        assert not qualification._accepted_contract(broken_healthy, broken_drift)

    assert not qualification._accepted_contract(healthy[:2], drift)
    assert not qualification._accepted_contract(healthy, drift[:2])


def test_source_provenance_requires_full_lowercase_shas() -> None:
    with pytest.raises(RuntimeError, match="candidate commit must be a full"):
        qualification._validate_source_provenance("abc123", "0" * 40)
    with pytest.raises(RuntimeError, match="base commit must be a full"):
        qualification._validate_source_provenance("0" * 40, "A" * 40)


def test_presentation_is_hash_bound_and_renders_without_staged_video_frames(
    tmp_path: Path,
) -> None:
    for index, (phase, outcome) in enumerate(
        (
            ("Demonstration", "RECORDED"),
            ("Governed replay", "VERIFIED"),
            ("Changed-screen refusal", "HALTED"),
        ),
        start=1,
    ):
        capture = qualification.PresentationCapture(
            tmp_path
            / f"0{index}-{'demonstration' if index == 1 else 'verified-replay' if index == 2 else 'safe-halt'}",
            phase=phase,
        )
        image = Image.new("RGB", qualification.VIEWPORT, (index * 30, 60, 90))
        capture.observe_image(image, source="test-frame")
        capture.observe_image(image, source="duplicate-test-frame")
        capture.finalize(outcome=outcome, summary={"model_calls": 0})

    output = tmp_path / "rdp-demo.mp4"
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is not installed")
    result = renderer.render(tmp_path, output)

    assert output.stat().st_size > 1_000
    assert result["video_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert [phase["outcome"] for phase in result["phases"]] == [
        "RECORDED",
        "VERIFIED",
        "HALTED",
    ]
    assert list(tmp_path.rglob("*.png")) == [
        tmp_path / "01-demonstration" / "frames" / "0000.png",
        tmp_path / "02-verified-replay" / "frames" / "0000.png",
        tmp_path / "03-safe-halt" / "frames" / "0000.png",
    ]


@pytest.mark.parametrize(("character", "keysym"), [("-", "minus"), ("/", "slash")])
def test_rdp_fixture_transport_uses_unambiguous_punctuation_keysyms(
    character: str, keysym: str
) -> None:
    transport = qualification.DockerX11RdpTransport("synthetic-fixture")
    commands: list[list[str]] = []
    transport._exec = lambda args, **_kwargs: commands.append(args)  # type: ignore[method-assign]

    transport.key(character, True)
    transport.key(character, False)

    assert commands == [
        ["xdotool", "keydown", "--clearmodifiers", keysym],
        ["xdotool", "keyup", "--clearmodifiers", keysym],
    ]


def test_recorded_identity_regions_cover_every_pointer_action(tmp_path: Path) -> None:
    recording = tmp_path / "recording"
    recording.mkdir()
    events = [
        {"kind": "click", "x": 1, "y": 2},
        {"kind": "click", "x": 3, "y": 4},
        {"kind": "type", "text": "example"},
        {"kind": "click", "x": 5, "y": 6},
    ]
    (recording / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    qualification._arm_recorded_identifiers(recording)

    updated = [
        json.loads(line)
        for line in (recording / "events.jsonl").read_text().splitlines()
    ]
    assert updated[0]["identifier_region"] == list(qualification.ADA_IDENTIFIER_REGION)
    assert updated[1]["identifier_region"] == list(
        qualification.ACTIVE_PATIENT_IDENTIFIER_REGION
    )
    assert "identifier_region" not in updated[2]
    assert updated[3]["identifier_region"] == list(
        qualification.ACTIVE_PATIENT_IDENTIFIER_REGION
    )


def test_identity_marking_refuses_an_unexpected_recording_shape(tmp_path: Path) -> None:
    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "events.jsonl").write_text(
        "".join(json.dumps({"kind": "click"}) + "\n" for _ in range(3)),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="event 3"):
        qualification._arm_recorded_identifiers(recording)


def test_committed_result_is_exact_accepted_six_trial_evidence() -> None:
    result = json.loads(
        (REPO / "benchmark" / "rdp_ladder" / "results.json").read_text()
    )
    assert result["schema_version"] == "openadapt.rdp-ladder-qualification.v2"
    assert result["candidate_commit"] == ("6031fde559b942a1d8b1a560d8b6cee8a6bfc800")
    assert result["base_commit"] == ("d952c363d1910f1699c1a4690002879b1990d743")
    assert result["run_count"] == 6
    assert result["successes"] == 6
    assert result["accepted"] is True
    assert qualification._accepted_contract(
        result["trials"][: qualification.TRIALS_PER_CONDITION],
        result["trials"][qualification.TRIALS_PER_CONDITION :],
    )


def test_qualification_workflow_is_path_filtered_manual_and_fail_loud() -> None:
    workflow = (
        REPO / ".github" / "workflows" / "docker-rdp-vision-ladder.yml"
    ).read_text()
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "|| pip install" not in workflow
    assert 'pip install -e ".[rdp]"' in workflow
    assert "pip check" in workflow


def test_fixture_policy_keeps_identity_effect_and_idempotency_gates() -> None:
    policy = load_policy(qualification.POLICY_PATH)
    assert policy.prohibit_unarmed_clicks is True
    assert policy.require_identity_for == ["entity_navigation", "write"]
    assert policy.require_system_effects_for == ["write"]
    assert policy.require_idempotency_key_for == ["write"]
    assert policy.prohibit_unconfirmed_effect_bindings is True
    assert policy.require_screen_postconditions_for == ["write"]


def test_headless_bundle_is_encrypted_and_admitted_before_any_replay(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    templates = bundle / "templates"
    identifiers = templates / "identifiers"
    identifiers.mkdir(parents=True)
    image = Image.new("RGB", (20, 20), "white")

    steps = []
    for index, risk in ((0, "reversible"), (1, "reversible"), (3, "irreversible")):
        template = f"templates/step_{index:03d}.png"
        identifier = f"templates/identifiers/step_{index:03d}.png"
        image.save(bundle / template)
        image.save(bundle / identifier)
        steps.append(
            Step(
                id=f"step_{index:03d}",
                intent="click 'Save Note'" if index == 3 else "click fixture",
                action=ActionKind.CLICK,
                risk=risk,
                anchor=Anchor(
                    template=template,
                    region=(0, 0, 20, 20),
                    click_point=(10, 10),
                    ocr_text="Save Note" if index == 3 else "Fixture",
                    identifier_crop=identifier,
                    identifier_region=(0, 0, 20, 20),
                ),
                identity_armed=True,
            )
        )
    # Preserve the real action order: select, focus, type, save.
    steps.insert(
        2,
        Step(
            id="step_002",
            intent="type <note>",
            action=ActionKind.TYPE,
            param=qualification.NOTE_PARAM,
        ),
    )
    workflow = Workflow(name="headless-rdp-ladder", steps=steps)
    oracle_root = tmp_path / "oracle"
    oracle_root.mkdir()

    governed, save_step_id, verifier, report = qualification._seal_and_admit_workflow(
        workflow, bundle, oracle_root
    )

    assert report.passed, report.render()
    assert governed.encrypted is True
    assert governed.steps[0].expect == [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text="Active: Ada Lovelace",
        )
    ]
    assert save_step_id == "step_003"
    effect = governed.steps[-1].effects[0]
    assert effect.match["name"].literal == qualification.ORACLE_FILENAME
    assert effect.idempotency_key is not None
    assert effect.idempotency_key.literal == qualification.ORACLE_FILENAME
    assert effect.key_field == "name"
    assert effect.count_new_only is True
    assert report.required_identity_step_ids == [
        "step_000",
        "step_001",
        "step_003",
    ]
    project = governed.qualification
    assert project is not None and project.last_certification is None
    focus_classification = project.action_classifications["step_001"]
    assert focus_classification.classification.value == "read_only"
    assert focus_classification.operator_confirmed is True

    from openadapt_flow.deployment import (
        DeploymentConfig,
        EffectsConfig,
        PolicySection,
    )
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.run_gate import GATE_CERTIFICATION, evaluate_run_gate

    production = evaluate_run_gate(
        governed,
        bundle_dir=bundle,
        deployment=DeploymentConfig(
            effects=EffectsConfig(
                kind="document-hash",
                root=str(oracle_root),
                glob=qualification.ORACLE_FILENAME,
            ),
            policy=PolicySection(policy=str(qualification.POLICY_PATH)),
        ),
        effect_verifier=verifier,
        policy_source=str(qualification.POLICY_PATH),
        profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
        effective_durable=True,
        effective_require_settled=True,
        strict_templates=True,
        require_encryption=True,
    )
    assert production.gate(GATE_CERTIFICATION).passed is False
    assert not (bundle / "workflow.json").exists()
    assert (bundle / "workflow.json.enc").is_file()
    assert all(not path.is_file() for path in templates.rglob("*.png"))
    assert all(path.is_file() for path in templates.rglob("*.png.enc"))
