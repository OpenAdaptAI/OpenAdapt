"""The refinery demo (demo/refinery): recorded workflow -> environment -> grader audit.

Runs the pipeline from the committed capture against an in-process MockMed
(no browser, synthetic data, about a second) and checks the committed
results are what it produces.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip(
    "openadapt_flow.verifier_audit", reason="openadapt-flow not installed"
)
pytest.importorskip(
    "openadapt_flow.mockmed.fault_server", reason="openadapt-flow not installed"
)

DEMO = Path(__file__).resolve().parents[1] / "demo" / "refinery"


def _load():
    spec = importlib.util.spec_from_file_location(
        "refinery_pipeline", DEMO / "pipeline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pipeline():
    return _load()


@pytest.fixture(scope="module")
def capture():
    return json.loads((DEMO / "results" / "capture.json").read_text())


@pytest.fixture(scope="module")
def built(pipeline, capture):
    return pipeline.build(capture)


def test_committed_results_are_reproduced(built):
    doc, markdown = built
    committed = json.loads((DEMO / "results" / "refinery.json").read_text())
    assert json.loads(json.dumps(doc, sort_keys=True)) == committed
    for name, text in markdown.items():
        assert (DEMO / "results" / "audit" / f"{name}.md").read_text() == text


def test_capture_matches_the_recording(capture):
    recording = Path(__file__).resolve().parents[1] / capture["recording"]
    assert (
        hashlib.sha256(recording.read_bytes()).hexdigest()
        == capture["recording_sha256"]
    )


def test_lift_is_checked_against_the_store(pipeline, capture):
    lifted = pipeline.lift(capture)
    assert lifted["store_diff_equals_write_bodies"]
    assert set(lifted["parameters"]) == {"note", "patient_id"}
    assert lifted["server_assigned_fields"] == ["id", "key", "source"]
    # A capture whose store diff disagrees with the wire is refused.
    tampered = json.loads(json.dumps(capture))
    tampered["store_after"][0]["patient_id"] = "p2"
    assert not pipeline.lift(tampered)["store_diff_equals_write_bodies"]


def test_seeded_side_effect_loophole_is_caught(built):
    audit = built[0]["audit"]["mined_postcondition"]
    families = {(c["family"]) for c in audit["false_accepts"]}
    assert "unauthorized_side_effect" in families
    case = next(
        c for c in audit["false_accepts"] if c["family"] == "unauthorized_side_effect"
    )
    assert case["verdict"] == "PASS" and case["effect_diff"]["extra"]


def test_false_reject_is_reported(built):
    audit = built[0]["audit"]["trajectory_replay"]
    assert {c["family"] for c in audit["false_rejects"]} == {"incidental_change"}


def test_only_a_clean_grader_ships(built):
    doc = built[0]
    shipped = doc["audit"][doc["shipped_grader"]]
    assert doc["shipped_grader"] == "effect_at_claim"
    assert not shipped["false_accepts"] and not shipped["false_rejects"]
    assert shipped["statuses"].get("CAUGHT", 0) > 0


def test_audit_flags_a_weakened_grader(pipeline, capture, monkeypatch):
    # Mutation check on the audit itself: drop the claim-time clause from the
    # shipped grader and the late write must come back as a false accept.
    def final_only(task, obs):
        want = pipeline.canonical_effect(task["expected_effect"])
        return (
            pipeline.store_effect(obs["initial"], obs["final"], obs["fields"]) == want
        )

    graders = {"effect_at_claim": (final_only, "final effect only")}
    monkeypatch.setattr(pipeline, "GRADERS", graders)
    doc, _ = pipeline.build(capture)
    assert {c["family"] for c in doc["audit"]["effect_at_claim"]["false_accepts"]} == {
        "late_write"
    }
    assert doc["shipped_grader"] is None
