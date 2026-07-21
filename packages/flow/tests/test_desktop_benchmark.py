"""Orchestrator tests for the desktop benchmark (no live VM).

A ``FakeHarness`` implements the small surface ``run_desktop_benchmark`` drives,
scripted per (arm, condition) to exercise the DB-verdict classification,
aggregation, and output writers. Mirrors tests/test_openemr_benchmark.py.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow.benchmark import desktop_benchmark as db
from openadapt_flow.benchmark.desktop_benchmark import (
    _classify,
    run_desktop_benchmark,
)


# --- _classify unit table ---------------------------------------------------


def test_classify_success():
    j = {"wrong_action": False, "target_note_ok": True, "wrong_patient_id": None}
    assert _classify(j, completed=True, cosmetic=True) == ("success", False)


def test_classify_wrong_action_beats_everything():
    j = {"wrong_action": True, "target_note_ok": False, "wrong_patient_id": 11}
    assert _classify(j, completed=True, cosmetic=False) == ("wrong_action", False)


def test_classify_safe_halt_on_cosmetic_is_false_abort():
    j = {"wrong_action": False, "target_note_ok": False, "wrong_patient_id": None}
    assert _classify(j, completed=False, cosmetic=True) == ("safe_halt", True)


def test_classify_safe_halt_on_data_drift_is_caution_not_false_abort():
    j = {"wrong_action": False, "target_note_ok": False, "wrong_patient_id": None}
    assert _classify(j, completed=False, cosmetic=False) == ("safe_halt", False)


def test_classify_completed_but_no_write_is_miss():
    j = {"wrong_action": False, "target_note_ok": False, "wrong_patient_id": None}
    assert _classify(j, completed=True, cosmetic=True) == ("miss", False)


# --- fake harness -----------------------------------------------------------


class FakeHarness:
    """Scripts a DB outcome per (arm, condition)."""

    def __init__(self, script):
        self.script = script  # {(arm, condition): "success"|"wrong"|"halt"}
        self._last = None
        self._completed = False

    def prepare_condition(self, condition):
        self._cond = condition

    def record_and_compile(self, work_dir):
        (work_dir / "bundle").mkdir(parents=True, exist_ok=True)
        return work_dir / "bundle"

    def uia_tree_quality(self):
        return {
            "n_usable_id": 5,
            "n_targets": 6,
            "usable_fraction": 0.833,
            "identity_target_has_id": False,
        }

    def _apply(self, arm):
        outcome = self.script.get((arm, self._cond), "success")
        self._last = outcome
        self._completed = outcome != "halt"

    def compiled_run(self, bundle, note, run_dir):
        self._apply("compiled")
        return {
            "replay_success": self._completed,
            "rungs": {"template": 4},
            "identity": {"verified": 1, "mismatch": 0, "unreadable": 0},
            "halt_step": None if self._completed else "step_003",
            "halt_reason": None if self._completed else "mismatch",
        }

    def uia_run(self, mode, note):
        arm = "uia_identity" if mode == "identity" else "uia_positional"
        self._apply(arm)
        return {
            "status": "ok" if self._completed else "no_row_selected",
            "selected_index": 0,
            "selected_name": "Neil Sorenson",
        }

    def judge(self, note):
        if self._last == "success":
            return {
                "target_note_ok": True,
                "wrong_patient_id": None,
                "wrong_action": False,
            }
        if self._last == "wrong":
            return {
                "target_note_ok": False,
                "wrong_patient_id": 11,
                "wrong_action": True,
            }
        return {
            "target_note_ok": False,
            "wrong_patient_id": None,
            "wrong_action": False,
        }


def test_full_matrix_aggregates_and_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "_armed_coverage",
        lambda bundle: {"click_steps": 4, "armed_clicks": 2, "armed_coverage": 0.5},
    )
    # Positional arm mis-writes under sibling drift; identity + compiled hold.
    script = {
        ("uia_positional", "data_siblings"): "wrong",
        ("uia_positional", "data_reorder"): "wrong",
    }
    results = run_desktop_benchmark(
        tmp_path / "out",
        conditions=["clean", "data_siblings", "data_reorder"],
        arms=("compiled", "uia_identity", "uia_positional"),
        n_per=2,
        harness=FakeHarness(script),
        log=lambda *a: None,
    )
    arms = results["arms"]
    # 3 conditions x 2 = 6 runs per arm.
    assert arms["compiled"]["n"] == 6
    assert arms["compiled"]["success"] == 6
    assert arms["uia_identity"]["wrong_action"] == 0
    # positional mis-writes on the two data-drift conditions (2 runs each).
    assert arms["uia_positional"]["wrong_action"] == 4
    assert arms["uia_positional"]["success"] == 2  # only the clean pair

    # matrix records the wrong-actions in the right cells.
    assert results["matrix"]["uia_positional"]["data_siblings"]["wrong_action"] == 2
    assert results["matrix"]["uia_positional"]["clean"]["success"] == 2

    # outputs written.
    out = tmp_path / "out"
    assert (out / "results.json").exists()
    assert (out / "BENCHMARK.md").exists()
    loaded = json.loads((out / "results.json").read_text())
    assert loaded["uia_tree_quality"]["identity_target_has_id"] is False
    assert loaded["identity_armed_coverage"]["armed_coverage"] == 0.5


def test_error_in_arm_becomes_error_row(tmp_path, monkeypatch):
    monkeypatch.setattr(
        db,
        "_armed_coverage",
        lambda bundle: {"click_steps": 4, "armed_clicks": 2, "armed_coverage": 0.5},
    )

    class Boom(FakeHarness):
        def compiled_run(self, *a, **k):
            raise RuntimeError("vm gone")

    results = run_desktop_benchmark(
        tmp_path / "out",
        conditions=["clean"],
        arms=("compiled",),
        n_per=1,
        harness=Boom({}),
        log=lambda *a: None,
    )
    assert results["arms"]["compiled"]["error"] == 1
    assert results["runs"][0]["outcome"] == "error"


def test_markdown_renders_headline_and_matrix(tmp_path):
    results = {
        "generated_at": "2026-07-10T00:00:00+00:00",
        "task": "t",
        "substrate": "s",
        "target_app_note": "n",
        "identity_armed_coverage": {
            "armed_clicks": 2,
            "click_steps": 4,
            "armed_coverage": 0.5,
        },
        "uia_tree_quality": {
            "n_usable_id": 5,
            "n_targets": 6,
            "usable_fraction": 0.833,
            "identity_target_has_id": False,
        },
        "arms": {
            "compiled": {
                "n": 2,
                "success": 2,
                "wrong_action": 0,
                "safe_halt": 0,
                "false_abort": 0,
                "miss": 0,
                "error": 0,
                "success_rate": 1.0,
                "wrong_action_rate": 0.0,
                "wall_s_mean": 10.0,
            }
        },
        "matrix": {
            "compiled": {
                "clean": {
                    "n": 2,
                    "success": 2,
                    "wrong_action": 0,
                    "safe_halt": 0,
                    "false_abort": 0,
                }
            }
        },
        "conditions": ["clean"],
        "runs": [],
    }
    md = db.render_markdown(results)
    assert "Desktop Benchmark" in md
    assert "`compiled`" in md
    assert "vision is necessary" in md


def test_harness_threads_auth_token_to_backend():
    # The auth_token plumbing lets the harness drive an authenticated in-guest
    # agent; the WindowsBackend must carry the token.
    harness = db.DesktopHarness(object(), "http://10.211.55.3:5000", auth_token="tok")
    assert harness.backend._auth_token == "tok"
    plain = db.DesktopHarness(object(), "http://10.211.55.3:5000")
    assert plain.backend._auth_token is None
