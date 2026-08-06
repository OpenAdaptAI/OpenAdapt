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
import sqlite3
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


def _wrong_record(condition_trial: int) -> dict:
    return {
        "condition_trial": condition_trial,
        "passed": True,
        "model_calls": 0,
        "halted": True,
        "identity_mismatch": True,
        "silent_write": False,
        "false_completion": False,
        "policy_bound": True,
    }


def test_acceptance_requires_exact_fail_closed_three_by_three_matrix() -> None:
    healthy = [_healthy(i) for i in range(1, 4)]
    drift = [_drift(i) for i in range(1, 4)]
    wrong_record = [_wrong_record(i) for i in range(1, 4)]
    assert qualification._accepted_contract(healthy, drift, wrong_record)

    for collection, index, key, value in (
        (healthy, 0, "runtime_effect_verified", False),
        (healthy, 1, "identity_verified", False),
        (healthy, 2, "silent_incorrect_success", True),
        (healthy, 2, "visual_rungs_used", {}),
        (drift, 0, "silent_write", True),
        (drift, 1, "false_completion", True),
        (drift, 2, "policy_bound", False),
        (wrong_record, 0, "identity_mismatch", False),
        (wrong_record, 1, "silent_write", True),
        (wrong_record, 2, "false_completion", True),
    ):
        broken_healthy = copy.deepcopy(healthy)
        broken_drift = copy.deepcopy(drift)
        broken_wrong = copy.deepcopy(wrong_record)
        target = (
            broken_healthy
            if collection is healthy
            else broken_drift
            if collection is drift
            else broken_wrong
        )
        target[index][key] = value
        assert not qualification._accepted_contract(
            broken_healthy,
            broken_drift,
            broken_wrong,
        )

    assert not qualification._accepted_contract(healthy[:2], drift, wrong_record)
    assert not qualification._accepted_contract(healthy, drift[:2], wrong_record)
    assert not qualification._accepted_contract(healthy, drift, wrong_record[:2])


def test_source_provenance_requires_full_lowercase_shas() -> None:
    with pytest.raises(RuntimeError, match="candidate commit must be a full"):
        qualification._validate_source_provenance("abc123", "0" * 40)
    with pytest.raises(RuntimeError, match="base commit must be a full"):
        qualification._validate_source_provenance("0" * 40, "A" * 40)


def test_presentation_is_hash_bound_and_renders_without_staged_video_frames(
    tmp_path: Path,
) -> None:
    workflow = Workflow(
        name="test-rdp-workflow",
        steps=[],
        params=dict(qualification.DEMO_PARAMS),
    )
    from openadapt_flow.visualize import build_program_graph

    graph_dir = tmp_path / "02-compiled-workflow"
    graph_dir.mkdir()
    graph_path = graph_dir / "program-graph.json"
    graph_path.write_text(
        build_program_graph(workflow).model_dump_json(indent=2),
        encoding="utf-8",
    )
    (tmp_path / "execution-request.json").write_text(
        json.dumps(
            {
                "schema_version": "openadapt.rdp-presentation-request.v1",
                "workflow": workflow.name,
                "workflow_digest": "a" * 64,
                "gate_passed": True,
                "demonstration": {
                    "patient_mrn": qualification.EXPECTED_MRN,
                    **qualification.DEMO_PARAMS,
                },
                "execution": {
                    "patient_mrn": qualification.EXPECTED_MRN,
                    **qualification.REPLAY_PARAMS,
                    qualification.REQUEST_ID_PARAM: "REQ-LIVE-2048-1",
                },
                "program_graph": "02-compiled-workflow/program-graph.json",
            }
        ),
        encoding="utf-8",
    )
    for index, (phase, outcome) in enumerate(
        (
            ("Demonstration", "RECORDED"),
            ("Governed replay", "VERIFIED"),
            ("Wrong-record refusal", "HALTED"),
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
        if index == 1:
            capture.input_event("pointer", x=100, y=120, button="left")
        capture.observe_image(image, source="duplicate-test-frame")
        summary: dict = {"model_calls": 0}
        if index == 2:
            summary["verifier"] = {
                "kind": "read-only SQL",
                "query": "SELECT * FROM appointments",
                "rows": [
                    {
                        "request_id": "REQ-LIVE-2048-1",
                        "patient_mrn": qualification.EXPECTED_MRN,
                        "appointment_slot": qualification.REPLAY_PARAMS[
                            qualification.SLOT_PARAM
                        ],
                        "visit_type": qualification.REPLAY_PARAMS[
                            qualification.VISIT_TYPE_PARAM
                        ],
                        "status": "scheduled",
                    }
                ],
            }
        if index == 3:
            summary.update(
                {
                    "expected_record": "Ada Lovelace · MRN A1001",
                    "observed_record": "Grace Hopper · MRN B2002",
                    "identity_region": list(
                        qualification.ACTIVE_PATIENT_IDENTIFIER_REGION
                    ),
                    "verifier": {"kind": "read-only SQL", "rows": []},
                }
            )
        capture.finalize(outcome=outcome, summary=summary)

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
    timeline_path = output.with_suffix(".timeline.json")
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    assert timeline["schema_version"] == "openadapt.rdp-hybrid-presentation.v1"
    assert timeline["derivative"]["video_sha256"] == result["video_sha256"]
    assert timeline["derivative"]["frame_count"] > 0
    assert (
        result["hybrid_timeline_sha256"]
        == hashlib.sha256(timeline_path.read_bytes()).hexdigest()
    )
    source_entries = [
        entry for entry in timeline["timeline"] if "source_frame" in entry
    ]
    assert source_entries
    assert all(
        entry["end_frame_exclusive"] > entry["start_frame"] for entry in source_entries
    )
    assert all("sha256" in entry["source_frame"] for entry in source_entries)
    assert all("target_geometry" not in entry for entry in timeline["timeline"])
    assert "compiled_workflow" not in {entry["phase"] for entry in timeline["timeline"]}
    assert not any("compiled_graph" in entry for entry in timeline["timeline"])
    source_manifests = {
        name: json.loads(
            (tmp_path / name / "manifest.json").read_text(encoding="utf-8")
        )
        for name in renderer.PHASE_DIRS
    }
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    invalid_timelines = []
    invalid = copy.deepcopy(timeline)
    invalid["timeline"][1]["start_frame"] = 0
    invalid_timelines.append((invalid, "incomplete or overlapping"))
    invalid = copy.deepcopy(timeline)
    invalid["timeline"][0]["end_pts_s"] = 0.0
    invalid_timelines.append((invalid, "PTS does not match"))
    invalid = copy.deepcopy(timeline)
    next(entry for entry in invalid["timeline"] if "source_frame" in entry)[
        "source_frame"
    ]["sha256"] = "0" * 64
    invalid_timelines.append((invalid, "does not match its manifest"))
    invalid = copy.deepcopy(timeline)
    invalid["timeline"][0]["compiled_graph"] = {
        "node_index": 0,
        "node_id": "different-step",
    }
    invalid_timelines.append((invalid, "does not match the graph"))
    invalid = copy.deepcopy(timeline)
    invalid["timeline"][0]["facts"]["record_value"] = "not allowed"
    invalid_timelines.append((invalid, "non-public fact"))
    for invalid, message in invalid_timelines:
        with pytest.raises(RuntimeError, match=message):
            renderer.validate_hybrid_timeline(
                invalid,
                manifests=source_manifests,
                graph=graph,
            )
    assert list(tmp_path.rglob("*.png")) == [
        tmp_path / "01-demonstration" / "frames" / "0000.png",
        tmp_path / "02-verified-replay" / "frames" / "0000.png",
        tmp_path / "03-safe-halt" / "frames" / "0000.png",
    ]


@pytest.mark.parametrize(
    ("character", "keysym"),
    [("-", "minus"), ("/", "slash"), (":", "colon")],
)
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


def test_rdp_fixture_transport_types_parameter_in_one_input_operation() -> None:
    transport = qualification.DockerX11RdpTransport("synthetic-fixture")
    commands: list[list[str]] = []
    transport._exec = lambda args, **_kwargs: commands.append(args)  # type: ignore[method-assign]

    value = "2026-08-14 10:45"
    assert transport.supports_bulk_text(value)
    transport.bulk_type_text(value)

    assert commands == [
        [
            "xdotool",
            "type",
            "--clearmodifiers",
            "--delay",
            "35",
            "--",
            value,
        ]
    ]


def test_rdp_fixture_does_not_refocus_an_already_active_client() -> None:
    transport = qualification.DockerX11RdpTransport("synthetic-fixture")
    commands: list[list[str]] = []

    def execute(args, **_kwargs):
        commands.append(args)
        if args[1] == "search":
            return b"123\n"
        if args[1] == "getactivewindow":
            return b"123\n"
        raise AssertionError(f"unexpected input-changing command: {args!r}")

    transport._exec = execute  # type: ignore[method-assign]

    transport.focus_input_surface()

    assert [command[1] for command in commands] == ["search", "getactivewindow"]


def test_recorded_identity_regions_cover_every_pointer_action(tmp_path: Path) -> None:
    recording = tmp_path / "recording"
    recording.mkdir()
    events = [
        {"kind": "click", "x": 1, "y": 2},
        {"kind": "click", "x": 3, "y": 4},
        {"kind": "type", "text": "example"},
        {"kind": "click", "x": 5, "y": 6},
        {"kind": "type", "text": "example"},
        {"kind": "click", "x": 7, "y": 8},
        {"kind": "type", "text": "example"},
        {"kind": "click", "x": 9, "y": 10},
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
    assert updated[5]["identifier_region"] == list(
        qualification.ACTIVE_PATIENT_IDENTIFIER_REGION
    )
    assert updated[7]["identifier_region"] == list(
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


def test_committed_result_retains_the_prior_accepted_v2_evidence() -> None:
    result = json.loads(
        (REPO / "benchmark" / "rdp_ladder" / "results.json").read_text()
    )
    assert result["schema_version"] == "openadapt.rdp-ladder-qualification.v2"
    assert result["run_count"] == 6
    assert result["successes"] == 6
    assert result["accepted"] is True
    assert result["model_calls"] == 0
    assert not any(
        trial.get("silent_incorrect_success") or trial.get("false_completion")
        for trial in result["trials"]
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
    for index, risk in (
        (0, "reversible"),
        (1, "reversible"),
        (3, "reversible"),
        (5, "reversible"),
        (7, "irreversible"),
    ):
        template = f"templates/step_{index:03d}.png"
        identifier = f"templates/identifiers/step_{index:03d}.png"
        image.save(bundle / template)
        image.save(bundle / identifier)
        steps.append(
            Step(
                id=f"step_{index:03d}",
                intent=("click 'Save appointment'" if index == 7 else "click fixture"),
                action=ActionKind.CLICK,
                risk=risk,
                anchor=Anchor(
                    template=template,
                    region=(0, 0, 20, 20),
                    click_point=(10, 10),
                    ocr_text="Save appointment" if index == 7 else "Fixture",
                    identifier_crop=identifier,
                    identifier_region=(0, 0, 20, 20),
                ),
                identity_armed=True,
            )
        )
    for index, param in (
        (2, qualification.SLOT_PARAM),
        (4, qualification.VISIT_TYPE_PARAM),
        (6, qualification.REQUEST_ID_PARAM),
    ):
        steps.insert(
            index,
            Step(
                id=f"step_{index:03d}",
                intent=f"type <{param}>",
                action=ActionKind.TYPE,
                param=param,
            ),
        )
    workflow = Workflow(
        name="headless-rdp-ladder",
        steps=steps,
        params=dict(qualification.DEMO_PARAMS),
    )
    oracle_root = tmp_path / "oracle"
    oracle_root.mkdir()
    database = oracle_root / qualification.DATABASE_FILENAME
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE appointments (
            appointment_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            patient_mrn TEXT NOT NULL,
            patient_name TEXT NOT NULL,
            appointment_slot TEXT NOT NULL,
            visit_type TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    governed, save_step_id, verifier, report = qualification._seal_and_admit_workflow(
        workflow, bundle, oracle_root
    )

    assert report.passed, report.render()
    assert governed.encrypted is True
    assert governed.steps[0].expect == [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT,
            text="Active record: Ada Lovelace",
        )
    ]
    assert save_step_id == "step_007"
    effect = governed.steps[-1].effects[0]
    assert effect.match["request_id"].param == qualification.REQUEST_ID_PARAM
    assert effect.idempotency_key is not None
    assert effect.idempotency_key.param == qualification.REQUEST_ID_PARAM
    assert effect.key_field == "request_id"
    assert effect.count_new_only is True
    assert report.required_identity_step_ids == [
        "step_000",
        "step_001",
        "step_003",
        "step_005",
        "step_007",
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
                kind="sql",
                sqlite_database=str(database),
                sql_query=(
                    "SELECT appointment_id, request_id, patient_mrn, "
                    "patient_name, appointment_slot, visit_type, status "
                    "FROM appointments"
                ),
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
