"""Unit tests for the DOM-selector benchmark arm.

No network, no real browser in the harness tests: the Playwright page is
faked with a call-recording double, and aggregation/rendering are tested on
fabricated rows. ``verify_final_state`` runs real OCR on synthetic
cv2-rendered screenshots, matching the other benchmarks' test style.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from openadapt_flow.benchmark.dom_arm import (
    PERTURBATIONS,
    SCHEDULE,
    aggregate_dom_results,
    classify_outcome,
    condition_url,
    dom_arm_aggregate,
    dom_script,
    needs_maintenance,
    note_for_slot,
    render_dom_markdown,
    run_dom_script,
    verify_final_state,
    write_dom_outputs,
)

NOTE = "Vitals stable; recheck in two weeks. [S00]"


# -- fakes ---------------------------------------------------------------------


class FakeLocator:
    """Locator double that records every chained call on the page's log."""

    def __init__(self, page: "FakePage", desc: tuple[Any, ...]) -> None:
        self.page = page
        self.desc = desc

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self.page, (*self.desc, "first"))

    def get_by_role(self, role: str, *, name: str) -> "FakeLocator":
        return FakeLocator(self.page, (*self.desc, "role", role, name))

    def _act(self, action: str, *args: Any) -> None:
        call = (*self.desc, action, *args)
        self.page.calls.append(call)
        if self.page.fail_on is not None and self.page.fail_on(call):
            raise TimeoutError(f"Timeout on {call}")

    def click(self) -> None:
        self._act("click")

    def fill(self, value: str) -> None:
        self._act("fill", value)

    def wait_for(self, state: str = "visible") -> None:
        self._act("wait_for", state)


class FakePage:
    """Page double exposing the get_by_* surface the DOM script uses."""

    def __init__(self, fail_on: Any = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail_on = fail_on

    def get_by_label(self, text: str) -> FakeLocator:
        return FakeLocator(self, ("label", text))

    def get_by_role(self, role: str, *, name: str) -> FakeLocator:
        return FakeLocator(self, ("role", role, name))

    def get_by_text(self, text: str) -> FakeLocator:
        return FakeLocator(self, ("text", text))


# -- the script (mocked page) ----------------------------------------------------


class TestDomScript:
    def test_happy_path_executes_all_steps_in_order(self) -> None:
        page = FakePage()
        done, failed, error = run_dom_script(page, NOTE)
        assert (done, failed, error) == (9, None, None)
        assert page.calls == [
            ("label", "Username", "fill", "nurse.demo"),
            ("label", "Password", "fill", "mockmed-demo-pass"),
            ("role", "button", "Sign In", "click"),
            # First-row selection: .first is part of the recorded call —
            # the wrong-target risk the benchmark measures lives here.
            ("role", "button", "Open", "first", "click"),
            ("role", "button", "New Encounter", "click"),
            ("role", "button", "Triage", "click"),
            ("label", "Note", "fill", NOTE),
            ("role", "button", "Save Encounter", "click"),
            # Outcome assertion: the script never assumes the save worked.
            ("text", "Encounter saved", "wait_for", "visible"),
        ]

    def test_step_names_match_selector_plan(self) -> None:
        names = [name for name, _ in dom_script(FakePage(), NOTE)]
        assert names == [
            "fill username",
            "fill password",
            "click Sign In",
            "open first referral",
            "click New Encounter",
            "select Triage type",
            "fill note",
            "click Save Encounter",
            "confirm saved banner",
        ]

    def test_named_variant_differs_in_exactly_one_step(self) -> None:
        positional = [n for n, _ in dom_script(FakePage(), NOTE)]
        named = [
            n for n, _ in dom_script(FakePage(), NOTE, target_patient="Jane Sample")
        ]
        assert positional[3] == "open first referral"
        assert named[3] == "open referral by patient name"
        assert positional[:3] == named[:3]
        assert positional[4:] == named[4:]

    def test_named_variant_scopes_open_to_the_patient_row(self) -> None:
        page = FakePage()
        done, failed, error = run_dom_script(page, NOTE, target_patient="Jane Sample")
        assert (done, failed, error) == (9, None, None)
        # Row filtered by the demonstrated patient's accessible name,
        # then the Open button inside it — no .first anywhere.
        assert page.calls[3] == (
            "role",
            "row",
            "Jane Sample",
            "role",
            "button",
            "Open",
            "click",
        )

    def test_failure_stops_the_run_and_is_captured_not_raised(self) -> None:
        # A renamed Save button times out; nothing after it may execute.
        def fail(call: tuple[Any, ...]) -> bool:
            return call[:3] == ("role", "button", "Save Encounter")

        page = FakePage(fail_on=fail)
        done, failed, error = run_dom_script(page, NOTE)
        assert done == 7
        assert failed == "click Save Encounter"
        assert error is not None and "Timeout" in error
        assert page.calls[-1][:3] == ("role", "button", "Save Encounter")
        assert not any(c[0] == "text" for c in page.calls)  # no confirm step

    def test_first_step_failure_reports_zero_completed(self) -> None:
        page = FakePage(fail_on=lambda call: True)
        done, failed, error = run_dom_script(page, NOTE)
        assert done == 0
        assert failed == "fill username"
        assert error is not None


# -- schedule / notes --------------------------------------------------------------


class TestScheduleAndNotes:
    def test_schedule_matches_hybrid_benchmark_freeze(self) -> None:
        assert len(SCHEDULE) == 20
        assert SCHEDULE.count("clean") == 14
        for drift in ("notice", "reqfield", "modal-once"):
            assert SCHEDULE.count(drift) == 2

    def test_perturbation_menu(self) -> None:
        assert PERTURBATIONS == (
            "lookalike",
            "missing",
            "grow",
            "sort",
            "theme",
            "rename",
            "move",
            "typelabel",
        )

    def test_notes_distinct_per_arm_and_slot(self) -> None:
        from openadapt_flow.benchmark.dom_arm import ARMS

        notes = {
            note_for_slot(arm, slot)
            for arm in ARMS
            for slot in range(len(SCHEDULE) + len(PERTURBATIONS))
        }
        assert len(notes) == len(ARMS) * (len(SCHEDULE) + len(PERTURBATIONS))

    def test_condition_url(self) -> None:
        assert condition_url("http://x/", "clean") == "http://x/"
        assert condition_url("http://x/", "sort") == "http://x/?drift=sort"


# -- outcome classification --------------------------------------------------------


class TestClassification:
    def test_wrong_action_outranks_success_flag(self) -> None:
        assert (
            classify_outcome({"success": False, "wrong_action": True}) == "wrong-action"
        )

    def test_success(self) -> None:
        assert classify_outcome({"success": True, "wrong_action": False}) == "success"

    def test_everything_else_is_halt_or_error(self) -> None:
        assert classify_outcome({"success": False}) == "halt-or-error"

    def test_maintenance_is_dom_loud_break_on_drift_only(self) -> None:
        loud_dom = {
            "arm": "dom",
            "condition": "rename",
            "success": False,
            "failed_step": "open first referral",
        }
        assert needs_maintenance(loud_dom)
        # Wrong actions are counted separately, not as maintenance.
        silent_dom = {
            "arm": "dom",
            "condition": "sort",
            "success": False,
            "wrong_action": True,
        }
        assert not needs_maintenance(silent_dom)
        # Compiled halts are safe halts, never hand-edited.
        compiled = {
            "arm": "compiled",
            "condition": "rename",
            "success": False,
        }
        assert not needs_maintenance(compiled)
        # A clean-slot DOM failure is a bug, not drift maintenance.
        clean_dom = {
            "arm": "dom",
            "condition": "clean",
            "success": False,
            "failed_step": "confirm saved banner",
        }
        assert not needs_maintenance(clean_dom)
        # A completed run the judge failed has nothing to edit.
        disputed = {
            "arm": "dom",
            "condition": "theme",
            "success": False,
            "failed_step": None,
            "error": None,
        }
        assert not needs_maintenance(disputed)

    def test_verification_dispute(self) -> None:
        from openadapt_flow.benchmark.dom_arm import verification_dispute

        # DOM: every step ran, no error, judge said no -> dispute.
        assert verification_dispute(
            {
                "arm": "dom",
                "condition": "theme",
                "success": False,
                "failed_step": None,
                "error": None,
            }
        )
        # Compiled: replayer green, judge said no -> dispute.
        assert verification_dispute(
            {
                "arm": "compiled",
                "condition": "theme",
                "success": False,
                "replayer_success": True,
            }
        )
        # A loud break is a real failure, not a dispute.
        assert not verification_dispute(
            {
                "arm": "dom",
                "condition": "rename",
                "success": False,
                "failed_step": "open first referral",
            }
        )
        # Successes and wrong actions are never disputes.
        assert not verification_dispute(
            {"arm": "dom", "success": True, "failed_step": None}
        )
        assert not verification_dispute(
            {"arm": "dom", "success": False, "wrong_action": True, "failed_step": None}
        )


# -- fabricated rows -----------------------------------------------------------------


def make_row(
    arm: str,
    condition: str,
    *,
    success: bool = True,
    wrong_action: bool = False,
    wall: float = 2.0,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "arm": arm,
        "condition": condition,
        "wall_s": wall,
        "success": success,
        "wrong_action": wrong_action,
        "right_patient": not wrong_action,
        "wrong_type_row": False,
        "actions": 9,
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "error": None,
    }
    row.update(extra)
    return row


def fabricated_results() -> dict[str, Any]:
    """Rows shaped like a real run: positional-DOM wrong-actions on the
    data drifts, name-filtered DOM absorbing them / failing closed, a
    loud DOM break on rename, and one compiled judge-dispute on theme."""

    def dom_schedule(arm: str) -> list[dict[str, Any]]:
        return [
            make_row(
                arm,
                c,
                success=(c == "clean"),
                wall=1.0,
                failed_step=None if c == "clean" else "confirm saved banner",
                slot=i,
                phase="schedule",
            )
            for i, c in enumerate(SCHEDULE)
        ]

    schedule_runs = {
        "compiled": [
            make_row(
                "compiled",
                c,
                success=(c == "clean"),
                heal_count=0,
                slot=i,
                phase="schedule",
            )
            for i, c in enumerate(SCHEDULE)
        ],
        "dom": dom_schedule("dom"),
        "dom_named": dom_schedule("dom_named"),
    }
    perturbation_runs = {
        "compiled": [
            make_row(
                "compiled",
                c,
                success=(c != "theme"),
                heal_count=1,
                # One completed-but-failed-verification row (dark palette
                # OCR false negative) to exercise dispute reporting.
                replayer_success=True,
                slot=20 + i,
                phase="perturbation",
            )
            for i, c in enumerate(PERTURBATIONS)
        ],
        "dom": [
            make_row(
                "dom",
                c,
                success=(c in ("theme", "move", "typelabel")),
                wrong_action=(c in ("lookalike", "missing", "grow", "sort")),
                failed_step=("open first referral" if c == "rename" else None),
                final_hash="#patient/p0" if c == "lookalike" else "",
                slot=20 + i,
                phase="perturbation",
            )
            for i, c in enumerate(PERTURBATIONS)
        ],
        "dom_named": [
            make_row(
                "dom_named",
                c,
                success=(c not in ("missing", "rename")),
                failed_step=(
                    "open referral by patient name"
                    if c in ("missing", "rename")
                    else None
                ),
                final_hash=("" if c in ("missing", "rename") else "#patient/p1"),
                slot=20 + i,
                phase="perturbation",
            )
            for i, c in enumerate(PERTURBATIONS)
        ],
    }
    return aggregate_dom_results(schedule_runs, perturbation_runs)


class TestAggregation:
    def test_arm_aggregate_counts(self) -> None:
        rows = [
            make_row("dom", "clean", success=True),
            make_row(
                "dom",
                "notice",
                success=False,
                failed_step="open first referral",
            ),
            make_row("dom", "sort", success=False, wrong_action=True),
        ]
        agg = dom_arm_aggregate(rows)
        assert agg["n"] == 3
        assert agg["success_count"] == 1
        assert agg["clean"] == {
            "n": 1,
            "success_count": 1,
            "success_rate": 1.0,
        }
        assert agg["drift"]["n"] == 2
        assert agg["wrong_action_count"] == 1
        assert agg["halt_or_error_count"] == 1
        assert agg["maintenance_count"] == 1  # the notice loud break only

    def test_heal_totals_only_when_present(self) -> None:
        compiled = dom_arm_aggregate([make_row("compiled", "clean", heal_count=2)])
        assert compiled["heal_count_total"] == 2
        dom = dom_arm_aggregate([make_row("dom", "clean")])
        assert "heal_count_total" not in dom

    def test_results_document_structure(self) -> None:
        results = fabricated_results()
        assert set(results["arms"]) == {"compiled", "dom", "dom_named"}
        assert results["schedule"]["conditions"] == list(SCHEDULE)
        assert results["schedule"]["drift_fraction"] == pytest.approx(0.3)
        matrix = results["perturbation_matrix"]
        assert set(matrix) == set(PERTURBATIONS)
        assert matrix["sort"]["dom"][0]["outcome"] == "wrong-action"
        assert matrix["rename"]["dom"][0]["outcome"] == "halt-or-error"
        assert matrix["rename"]["dom"][0]["maintenance"] is True
        assert matrix["rename"]["compiled"][0]["outcome"] == "success"
        # The compiled theme row completed but failed the judge.
        assert matrix["theme"]["compiled"][0]["outcome"] == "halt-or-error"
        assert matrix["theme"]["compiled"][0]["dispute"] is True
        assert results["totals"]["dom"]["wrong_action_count"] == 4
        assert results["totals"]["compiled"]["wrong_action_count"] == 0
        # The identity reading of the same script: zero wrong actions,
        # fail-closed on missing/rename.
        assert results["totals"]["dom_named"]["wrong_action_count"] == 0
        assert matrix["sort"]["dom_named"][0]["outcome"] == "success"
        assert matrix["missing"]["dom_named"][0]["outcome"] == "halt-or-error"
        # Maintenance: rename (loud) + 6 schedule drifts; wrong actions
        # and judge disputes are NOT maintenance.
        assert results["totals"]["dom"]["maintenance_count"] == 7
        # Name-filtered: 6 schedule + missing + rename.
        assert results["totals"]["dom_named"]["maintenance_count"] == 8
        assert results["totals"]["compiled"]["maintenance_count"] == 0
        assert results["totals"]["compiled"]["verification_dispute_count"] == 1
        disputes = results["verification_disputes"]
        assert len(disputes) == 1
        assert disputes[0]["arm"] == "compiled"
        assert disputes[0]["condition"] == "theme"


class TestRenderer:
    def test_markdown_reports_both_arms_per_condition(self) -> None:
        md = render_dom_markdown(fabricated_results())
        # Per-condition rows for schedule drift types and perturbations.
        for cond in ("notice", "reqfield", "modal-once", *PERTURBATIONS):
            assert f"`{cond}`" in md
        assert "WRONG ACTION" in md
        assert "## Verdict" in md
        assert "## Maintenance asymmetry" in md
        # The honest boundary of the comparison must always be stated.
        assert "browser backend" in md
        assert "$0" in md

    def test_markdown_wrong_action_totals(self) -> None:
        md = render_dom_markdown(fabricated_results())
        assert (
            "Totals: compiled replay 0, DOM (positional) 4, DOM (name-filtered) 0" in md
        )

    def test_markdown_reports_verification_disputes(self) -> None:
        md = render_dom_markdown(fabricated_results())
        assert "## Measurement validity" in md
        # The fabricated compiled theme row completed but failed the OCR
        # judge; the section must name it and point at the saved final.
        assert "compiled on `theme`" in md
        assert "finals/perturbation_compiled_024.png" in md

    def test_write_outputs(self, tmp_path: Path) -> None:
        out = tmp_path / "dom"
        write_dom_outputs(fabricated_results(), out)
        assert (out / "results.json").exists()
        assert (out / "BENCHMARK.md").exists()
        assert (out / "outcome_matrix.png").stat().st_size > 0


# -- verify (real OCR on synthetic frames) ------------------------------------------


def to_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def make_screen(*lines: str) -> bytes:
    img = np.full((800, 1280, 3), 245, dtype=np.uint8)
    for i, line in enumerate(lines):
        cv2.putText(
            img,
            line,
            (40, 200 + i * 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
    return to_png(img)


class TestVerifyFinalState:
    """The check itself is verify_hybrid_final, fully covered in
    test_hybrid_benchmark.py; this pins that the DOM benchmark's exported
    check is that function and flags the failure mode this benchmark
    exists to measure."""

    def test_dom_check_is_the_hybrid_check(self) -> None:
        from openadapt_flow.benchmark.hybrid_benchmark import (
            verify_hybrid_final,
        )

        assert verify_final_state is verify_hybrid_final

    def test_saved_on_wrong_patient_is_wrong_action_not_success(
        self,
    ) -> None:
        screen = make_screen(
            "Taylor Duplicate - MRN P0 - DOB 1982-12-12",
            f"Encounter saved - {NOTE[:40]}",
            f"Triage - {NOTE[:60]}",
        )
        verdict = verify_final_state(screen, NOTE)
        assert verdict.banner_found and verdict.note_found
        assert not verdict.right_patient
        assert verdict.wrong_action
        assert not verdict.success
