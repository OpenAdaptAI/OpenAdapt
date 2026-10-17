"""Unit tests for the hybrid benchmark (no network, no real API spend).

The Anthropic client, backend, and replayer are faked; the final-state
check runs real OCR on synthetic cv2-rendered screenshots — the same
testing style as ``test_benchmark.py`` / ``test_openemr_benchmark.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from openadapt_flow.benchmark import agent_baseline, hybrid_benchmark
from openadapt_flow.benchmark.hybrid_benchmark import (
    AGENT_SLOTS,
    DRIFT_TYPES,
    FALLBACK_MAX_ACTIONS,
    SCHEDULE,
    SpendLedger,
    _hybrid_run,
    aggregate_hybrid_results,
    condition_url,
    demo_conditioned_task_prompt,
    handoff_task_prompt,
    hybrid_arm_aggregate,
    note_for_slot,
    render_hybrid_markdown,
    run_hybrid_benchmark,
    serialize_demo,
    verify_hybrid_final,
    write_hybrid_outputs,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Landmark,
    Step,
    Workflow,
)

NOTE = "Walking program started this week. [D09]"


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


BLANK_PNG = to_png(np.full((800, 1280, 3), 245, dtype=np.uint8))


def make_workflow() -> Workflow:
    """A small workflow exercising every serialization branch."""
    return Workflow(
        name="t",
        steps=[
            Step(
                id="step_000",
                intent="click 'Username'",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/step_000.png",
                    region=(10, 10, 160, 64),
                    click_point=(90, 42),
                    ocr_text="Username",
                ),
            ),
            Step(
                id="step_001",
                intent="type 'nurse.demo'",
                action=ActionKind.TYPE,
                text="nurse.demo",
            ),
            Step(
                id="step_002",
                intent="click at (300, 400)",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/step_002.png",
                    region=(240, 360, 160, 64),
                    click_point=(300, 400),
                    ocr_text=None,
                    landmarks=[
                        Landmark(
                            relation="above",
                            ocr_text="Note",
                            distance_px=40,
                            dx_px=0,
                            dy_px=40,
                        )
                    ],
                ),
            ),
            Step(
                id="step_003",
                intent="type <note>",
                action=ActionKind.TYPE,
                param="note",
            ),
            Step(
                id="step_004",
                intent="press Enter",
                action=ActionKind.KEY,
                key="Enter",
            ),
            Step(
                id="step_005",
                intent="scroll by (0, 240)",
                action=ActionKind.SCROLL,
                scroll_dx=0,
                scroll_dy=240,
            ),
        ],
    )


# -- demo serialization ---------------------------------------------------------


class TestSerializeDemo:
    def test_labeled_click_uses_label(self) -> None:
        text = serialize_demo(make_workflow())
        assert '1. click the element labeled "Username"' in text

    def test_literal_type_included(self) -> None:
        assert '2. type "nurse.demo"' in serialize_demo(make_workflow())

    def test_unlabeled_click_described_via_landmark_not_coordinates(
        self,
    ) -> None:
        text = serialize_demo(make_workflow())
        assert '3. click the unlabeled control below the text "Note"' in text
        assert "(300, 400)" not in text

    def test_param_type_is_placeholder_not_value(self) -> None:
        text = serialize_demo(make_workflow())
        assert "4. type <note>" in text

    def test_key_and_scroll(self) -> None:
        text = serialize_demo(make_workflow())
        assert "5. press the Enter key" in text
        assert "6. scroll by (0, 240)" in text

    def test_one_numbered_line_per_step(self) -> None:
        wf = make_workflow()
        lines = serialize_demo(wf).splitlines()
        assert len(lines) == len(wf.steps)
        assert all(line.startswith(f"{i}.") for i, line in enumerate(lines, 1))


# -- prompts ----------------------------------------------------------------------


class TestPrompts:
    def test_demo_conditioned_prompt_embeds_task_and_demo(self) -> None:
        demo = serialize_demo(make_workflow())
        prompt = demo_conditioned_task_prompt(NOTE, demo)
        base = agent_baseline.triage_task_prompt(NOTE)
        assert base in prompt
        assert demo in prompt
        assert "guide, not a script" in prompt

    def test_handoff_prompt_reports_progress_and_reason(self) -> None:
        demo = serialize_demo(make_workflow())
        prompt = handoff_task_prompt(
            NOTE,
            demo,
            completed_steps=7,
            total_steps=11,
            halted_step_intent="click 'Save Encounter'",
            halt_reason="Postconditions failed: banner never appeared",
        )
        assert "Steps 1..7 of 11 reported complete" in prompt
        assert "halted at step 8" in prompt
        assert "click 'Save Encounter'" in prompt
        assert "banner never appeared" in prompt
        assert NOTE in prompt
        assert demo in prompt
        assert "do NOT start over" in prompt

    def test_handoff_prompt_zero_completed(self) -> None:
        prompt = handoff_task_prompt(
            NOTE,
            "1. click",
            completed_steps=0,
            total_steps=11,
            halted_step_intent="click 'Sign In'",
            halt_reason="drift",
        )
        assert "No steps completed" in prompt
        assert "halted at step 1 of 11" in prompt


# -- final-state verification -------------------------------------------------------


class TestVerifyHybridFinal:
    def test_correct_save_passes(self) -> None:
        screen = make_screen(
            "Jane Sample - MRN P1 - DOB 1980-01-01",
            f"Encounter saved - {NOTE[:40]}",
            f"Triage - {NOTE[:60]}",
        )
        verdict = verify_hybrid_final(screen, NOTE)
        assert verdict.success
        assert verdict.right_patient
        assert not verdict.wrong_action

    def test_wrong_patient_is_wrong_action_not_success(self) -> None:
        screen = make_screen(
            "Alex Testcase - MRN P2 - DOB 1975-05-05",
            f"Encounter saved - {NOTE[:40]}",
            f"Triage - {NOTE[:60]}",
        )
        verdict = verify_hybrid_final(screen, NOTE)
        assert not verdict.success
        assert not verdict.right_patient
        assert verdict.wrong_action

    def test_wrong_type_row_is_wrong_action(self) -> None:
        screen = make_screen(
            "Jane Sample - MRN P1 - DOB 1980-01-01",
            f"Encounter saved - {NOTE[:40]}",
            f"Consult - {NOTE[:60]}",
        )
        verdict = verify_hybrid_final(screen, NOTE)
        assert verdict.wrong_type_row
        assert verdict.wrong_action
        assert not verdict.success

    def test_halted_form_fails_without_wrong_action(self) -> None:
        screen = make_screen("New Encounter", "Encounter Type", "Note")
        verdict = verify_hybrid_final(screen, NOTE)
        assert not verdict.success
        assert not verdict.wrong_action


# -- schedule fairness ----------------------------------------------------------------


class TestSchedule:
    def test_twenty_slots_thirty_percent_drift(self) -> None:
        assert len(SCHEDULE) == 20
        drifted = [c for c in SCHEDULE if c != "clean"]
        assert len(drifted) == 6
        for drift in DRIFT_TYPES:
            assert drifted.count(drift) == 2

    def test_agent_subsample_is_proportional_same_conditions(self) -> None:
        assert len(AGENT_SLOTS) == 8
        assert all(0 <= s < len(SCHEDULE) for s in AGENT_SLOTS)
        assert len(set(AGENT_SLOTS)) == len(AGENT_SLOTS)
        conditions = [SCHEDULE[s] for s in AGENT_SLOTS]
        assert conditions.count("clean") == 5
        for drift in DRIFT_TYPES:
            assert conditions.count(drift) == 1

    def test_notes_distinct_per_arm_slot(self) -> None:
        notes = {
            note_for_slot(arm, slot)
            for arm in ("compiled", "agent", "demo_agent", "hybrid")
            for slot in range(len(SCHEDULE))
        }
        assert len(notes) == 4 * len(SCHEDULE)

    def test_condition_url(self) -> None:
        assert condition_url("http://h:1/", "clean") == "http://h:1/"
        assert (
            condition_url("http://h:1/", "modal-once") == "http://h:1/?drift=modal-once"
        )


# -- spend ledger ------------------------------------------------------------------


class TestSpendLedger:
    def test_blocks_when_next_cap_could_exceed_ceiling(self) -> None:
        ledger = SpendLedger(per_run_cap=1.5, total_cap=4.0)
        assert ledger.can_start()
        ledger.record(1.4)
        assert ledger.can_start()  # 1.4 + 1.5 <= 4.0
        ledger.record(1.4)
        assert not ledger.can_start()  # 2.8 + 1.5 > 4.0
        assert "ceiling" in ledger.blocked_reason()

    def test_two_consecutive_billing_errors_abort(self) -> None:
        ledger = SpendLedger(per_run_cap=1.5, total_cap=100.0)
        ledger.record(0.0, error="401 authentication_error")
        assert ledger.can_start()
        ledger.record(0.0, error="credit balance too low")
        assert not ledger.can_start()
        assert "billing" in ledger.blocked_reason()

    def test_success_resets_billing_streak(self) -> None:
        ledger = SpendLedger(per_run_cap=1.5, total_cap=100.0)
        ledger.record(0.0, error="401 authentication_error")
        ledger.record(0.2)  # clean run resets the streak
        ledger.record(0.0, error="401 authentication_error")
        assert ledger.can_start()


# -- hybrid run (halt -> handoff) ---------------------------------------------------


class FakeStepResult:
    def __init__(self, step_id: str, intent: str, ok: bool, error=None):
        self.step_id = step_id
        self.intent = intent
        self.ok = ok
        self.error = error


class FakeReport:
    def __init__(self, results, success: bool, heal_count: int = 0):
        self.results = results
        self.success = success
        self.heal_count = heal_count


class FakeBackend:
    viewport = (1280, 800)

    def __init__(self, png: bytes = BLANK_PNG) -> None:
        self.png = png

    def screenshot(self) -> bytes:
        return self.png


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    bundle_dir = tmp_path / "bundle"
    make_workflow().save(bundle_dir)
    return bundle_dir


@pytest.fixture()
def fake_launch(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    backend = FakeBackend()
    from openadapt_flow.backends import playwright_backend

    monkeypatch.setattr(
        playwright_backend.PlaywrightBackend,
        "launch",
        staticmethod(lambda url, headless=True: (backend, lambda: None)),
    )
    return backend


def install_fake_replayer(
    monkeypatch: pytest.MonkeyPatch, report: FakeReport
) -> list[dict[str, Any]]:
    """Replace the runtime Replayer with one returning ``report``."""
    calls: list[dict[str, Any]] = []

    class FakeReplayer:
        def __init__(self, backend, **kwargs):
            pass

        def run(self, workflow, **kwargs):
            calls.append(kwargs)
            return report

    import openadapt_flow.runtime as runtime

    monkeypatch.setattr(runtime, "Replayer", FakeReplayer)
    return calls


SUCCESS_SCREEN = make_screen(
    "Jane Sample - MRN P1 - DOB 1980-01-01",
    f"Encounter saved - {NOTE[:40]}",
    f"Triage - {NOTE[:60]}",
)


class TestHybridRun:
    def test_completed_replay_costs_zero_and_skips_fallback(
        self,
        bundle: Path,
        fake_launch: FakeBackend,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        fake_launch.png = SUCCESS_SCREEN
        install_fake_replayer(
            monkeypatch,
            FakeReport([FakeStepResult("step_000", "click", True)], success=True),
        )

        def boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("fallback must not fire on success")

        monkeypatch.setattr(agent_baseline, "run_agent", boom)
        ledger = SpendLedger(1.5, 8.0)
        row = _hybrid_run(
            bundle,
            "http://x/",
            tmp_path / "run",
            NOTE,
            demo_text="1. click",
            ledger=ledger,
        )
        assert row["success"]
        assert row["cost_usd"] == 0.0
        assert not row["fallback_used"]
        assert not row["halted"]
        assert ledger.spent == 0.0

    def test_halt_hands_off_with_budget_prompt_and_cost_attribution(
        self,
        bundle: Path,
        fake_launch: FakeBackend,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        fake_launch.png = SUCCESS_SCREEN
        install_fake_replayer(
            monkeypatch,
            FakeReport(
                [
                    FakeStepResult("step_000", "click 'Username'", True),
                    FakeStepResult("step_001", "type 'nurse.demo'", True),
                    FakeStepResult(
                        "step_002",
                        "click 'Sign In'",
                        False,
                        error="Postconditions failed: semantic drift",
                    ),
                ],
                success=False,
            ),
        )
        seen: dict[str, Any] = {}

        def fake_run_agent(backend, task, **kwargs):
            seen["task"] = task
            seen["max_actions"] = kwargs.get("max_actions")
            seen["max_cost_usd"] = kwargs.get("max_cost_usd")
            return agent_baseline.AgentRunResult(
                actions=6,
                api_calls=7,
                input_tokens=1000,
                output_tokens=200,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=3000,
                cost_usd=0.21,
                wall_s=33.0,
                stopped="model_done",
                model_stop_reason="end_turn",
                final_screenshot=SUCCESS_SCREEN,
            )

        monkeypatch.setattr(agent_baseline, "run_agent", fake_run_agent)
        ledger = SpendLedger(1.5, 8.0)
        row = _hybrid_run(
            bundle,
            "http://x/",
            tmp_path / "run",
            NOTE,
            demo_text='1. click the element labeled "Username"',
            ledger=ledger,
        )
        # Handoff prompt construction.
        assert "Steps 1..2 of 6 reported complete" in seen["task"]
        assert "halted at step 3" in seen["task"]
        assert "click 'Sign In'" in seen["task"]
        assert "semantic drift" in seen["task"]
        assert NOTE in seen["task"]
        # Fallback budget.
        assert seen["max_actions"] == FALLBACK_MAX_ACTIONS
        assert seen["max_cost_usd"] == 1.5
        # Cost attribution: the row's cost is the fallback's cost only,
        # recorded against the shared ledger.
        assert row["halted"] and row["fallback_used"]
        assert row["halt_step"] == "step_002"
        assert row["cost_usd"] == pytest.approx(0.21)
        assert row["fallback_cost_usd"] == pytest.approx(0.21)
        assert row["fallback_actions"] == 6
        assert ledger.spent == pytest.approx(0.21)
        assert row["success"]  # final screen verified independently

    def test_fallback_mid_run_crash_records_partial_spend(
        self,
        bundle: Path,
        fake_launch: FakeBackend,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """ADDED 2026-07-09 (review): a fallback that crashes mid-run
        previously recorded $0 to the SpendLedger and a zero-cost row —
        real paid calls vanished from the ceiling. The partial usage must
        reach the row AND the shared ledger."""
        from types import SimpleNamespace

        install_fake_replayer(
            monkeypatch,
            FakeReport(
                [FakeStepResult("step_000", "click", False, error="drift")],
                success=False,
            ),
        )

        def crashing_run_agent(backend, task, **kwargs):
            usage = kwargs["ledger"]
            for _ in range(2):  # two paid calls, then the API dies
                usage.record(
                    SimpleNamespace(
                        input_tokens=200_000,
                        output_tokens=50,
                        cache_creation_input_tokens=0,
                        cache_read_input_tokens=0,
                    )
                )
            raise RuntimeError("Error code: 529 - overloaded_error")

        monkeypatch.setattr(agent_baseline, "run_agent", crashing_run_agent)
        ledger = SpendLedger(1.5, 8.0)
        row = _hybrid_run(
            bundle,
            "http://x/",
            tmp_path / "run",
            NOTE,
            demo_text="1. click",
            ledger=ledger,
        )
        expected = agent_baseline.compute_cost(400_000, 100)
        assert row["error"] == "RuntimeError: Error code: 529 - overloaded_error"
        assert row["fallback_used"]
        assert row["api_calls"] == 2
        assert row["input_tokens"] == 400_000
        assert row["cost_usd"] == pytest.approx(expected)
        assert row["fallback_cost_usd"] == pytest.approx(expected)
        assert ledger.spent == pytest.approx(expected)  # counts to ceiling

    def test_failed_report_without_failing_step_counts_all_completed(
        self,
        bundle: Path,
        fake_launch: FakeBackend,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """ADDED 2026-07-09 (review): success=False with every step result
        ok used to blame the LAST COMPLETED step and undercount
        completed_steps by one in the handoff prompt."""
        install_fake_replayer(
            monkeypatch,
            FakeReport(
                [
                    FakeStepResult("step_000", "click 'Username'", True),
                    FakeStepResult("step_001", "type 'nurse.demo'", True),
                ],
                success=False,
            ),
        )
        seen: dict[str, Any] = {}

        def fake_run_agent(backend, task, **kwargs):
            seen["task"] = task
            return agent_baseline.AgentRunResult(
                actions=1,
                api_calls=1,
                input_tokens=100,
                output_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                cost_usd=0.001,
                wall_s=1.0,
                stopped="model_done",
                model_stop_reason="end_turn",
                final_screenshot=SUCCESS_SCREEN,
            )

        monkeypatch.setattr(agent_baseline, "run_agent", fake_run_agent)
        ledger = SpendLedger(1.5, 8.0)
        row = _hybrid_run(
            bundle,
            "http://x/",
            tmp_path / "run",
            NOTE,
            demo_text="1. click",
            ledger=ledger,
        )
        assert row["halted"]
        assert row["halt_step"] is None  # no failing step to name
        # Both completed steps count; not len - 1.
        assert "Steps 1..2 of" in seen["task"]
        assert "no failing step recorded" in seen["task"]

    def test_failed_result_index_edge_cases(self) -> None:
        from openadapt_flow.benchmark.hybrid_benchmark import (
            _failed_result_index,
        )

        ok = FakeStepResult("a", "x", True)
        bad = FakeStepResult("b", "y", False, error="boom")
        assert _failed_result_index(FakeReport([ok, bad, ok], False)) == 1
        assert _failed_result_index(FakeReport([bad], False)) == 0
        assert _failed_result_index(FakeReport([ok, ok], False)) is None
        assert _failed_result_index(FakeReport([], False)) is None

    def test_halt_with_exhausted_budget_skips_fallback(
        self,
        bundle: Path,
        fake_launch: FakeBackend,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        install_fake_replayer(
            monkeypatch,
            FakeReport(
                [FakeStepResult("step_000", "click", False, error="drift")],
                success=False,
            ),
        )

        def boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("fallback must not fire over budget")

        monkeypatch.setattr(agent_baseline, "run_agent", boom)
        ledger = SpendLedger(1.5, 8.0)
        ledger.spent = 7.0  # 7.0 + 1.5 > 8.0
        row = _hybrid_run(
            bundle,
            "http://x/",
            tmp_path / "run",
            NOTE,
            demo_text="1. click",
            ledger=ledger,
        )
        assert row["halted"]
        assert not row["fallback_used"]
        assert "ceiling" in row["fallback_skipped_reason"]
        assert row["cost_usd"] == 0.0
        assert not row["success"]


# -- aggregation --------------------------------------------------------------------


def _row(
    arm: str,
    slot: int,
    condition: str,
    success: bool,
    cost: float = 0.0,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "arm": arm,
        "i": slot,
        "slot": slot,
        "condition": condition,
        "note": note_for_slot(arm, slot),
        "wall_s": 5.0,
        "success": success,
        "actions": 11,
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": cost,
        "error": None,
    }
    row.update(extra)
    return row


def make_results() -> dict[str, Any]:
    compiled = [
        _row("compiled", s, c, success=(c == "clean")) for s, c in enumerate(SCHEDULE)
    ]
    hybrid = []
    for s, c in enumerate(SCHEDULE):
        if c == "clean":
            hybrid.append(
                _row(
                    "hybrid",
                    s,
                    c,
                    success=True,
                    fallback_used=False,
                    halted=False,
                    fallback_actions=0,
                    fallback_cost_usd=0.0,
                    fallback_skipped_reason=None,
                )
            )
        else:
            hybrid.append(
                _row(
                    "hybrid",
                    s,
                    c,
                    success=True,
                    cost=0.2,
                    fallback_used=True,
                    halted=True,
                    halt_step="step_004",
                    fallback_actions=6,
                    fallback_cost_usd=0.2,
                    fallback_skipped_reason=None,
                )
            )
    agent = [
        _row("agent", s, SCHEDULE[s], success=True, cost=0.27, api_calls=13)
        for s in AGENT_SLOTS
    ]
    demo_agent = [
        _row("demo_agent", s, SCHEDULE[s], success=(i != 0), cost=0.30, api_calls=12)
        for i, s in enumerate(AGENT_SLOTS)
    ]
    return aggregate_hybrid_results(
        {
            "compiled": compiled,
            "agent": agent,
            "demo_agent": demo_agent,
            "hybrid": hybrid,
        },
        arm_notes={
            "compiled": None,
            "agent": None,
            "demo_agent": None,
            "hybrid": None,
        },
    )


class TestAggregation:
    def test_cost_per_success_headline(self) -> None:
        results = make_results()
        arms = results["arms"]
        assert arms["compiled"]["cost_per_success_usd"] == 0.0
        assert arms["agent"]["cost_per_success_usd"] == pytest.approx(0.27)
        # Hybrid: 6 fallbacks x $0.20 over 20 successes.
        assert arms["hybrid"]["cost_per_success_usd"] == pytest.approx(6 * 0.2 / 20)

    def test_hybrid_fallback_stats(self) -> None:
        h = make_results()["arms"]["hybrid"]
        assert h["halt_count"] == 6
        assert h["fallback_count"] == 6
        assert h["fallback_rate"] == pytest.approx(0.3)
        assert h["fallback_success_rate"] == 1.0
        assert h["fallback_cost_usd_mean"] == pytest.approx(0.2)
        assert h["fallback_skipped_count"] == 0

    def test_clean_drift_split(self) -> None:
        c = make_results()["arms"]["compiled"]
        assert c["clean"] == {"n": 14, "success_count": 14, "success_rate": 1.0}
        assert c["drift"]["n"] == 6
        assert c["drift"]["success_count"] == 0

    def test_wrong_action_count(self) -> None:
        rows = [
            _row("agent", 0, "clean", success=False, wrong_action=True),
            _row("agent", 1, "clean", success=True),
        ]
        assert hybrid_arm_aggregate(rows)["wrong_action_count"] == 1

    def test_total_spend_recorded(self) -> None:
        results = make_results()
        expected = 6 * 0.2 + 8 * 0.27 + 8 * 0.30
        assert results["cost_caps_usd"]["total_spent_list"] == pytest.approx(expected)


# -- markdown + outputs ----------------------------------------------------------------


class TestMarkdownAndOutputs:
    def test_markdown_states_verdict_and_pr12_caveat(self) -> None:
        md = render_hybrid_markdown(make_results())
        assert "## Verdict" in md
        assert "**Supported**" in md
        assert "PR #12" in md
        assert "halt-DETECTION reliability" in md
        assert "Selection bias" in md
        assert "break-even" in md.lower()
        assert "cost / successful run" in md

    def test_markdown_reports_all_four_arms(self) -> None:
        md = render_hybrid_markdown(make_results())
        for arm in ("compiled (A)", "agent (B)", "demo agent (C)", "hybrid (D)"):
            assert arm in md

    def test_verdict_refuted_when_hybrid_worse_and_dearer(self) -> None:
        results = make_results()
        results["arms"]["hybrid"]["success_rate"] = 0.5
        results["arms"]["hybrid"]["cost_per_success_usd"] = 9.0
        md = render_hybrid_markdown(results)
        assert "**Refuted**" in md

    def test_write_outputs(self, tmp_path: Path) -> None:
        write_hybrid_outputs(make_results(), tmp_path)
        assert (tmp_path / "results.json").is_file()
        assert (tmp_path / "BENCHMARK.md").is_file()
        assert (tmp_path / "success_cost.png").is_file()
        loaded = json.loads((tmp_path / "results.json").read_text())
        assert set(loaded["arms"]) == {"compiled", "agent", "demo_agent", "hybrid"}


# -- orchestrator guardrails --------------------------------------------------------


def install_fake_arms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    agent_cost: float = 0.27,
    url_log: list | None = None,
) -> None:
    """Fake recording/compiling/serving and the three run helpers.

    When ``url_log`` is given, every run helper appends ``(arm, url)`` in
    call order — the URLs the arms ACTUALLY received, so tests can assert
    on the real per-slot conditions rather than orchestrator-stamped
    labels.
    """

    def log_url(arm: str, url: str) -> None:
        if url_log is not None:
            url_log.append((arm, url))

    import openadapt_flow.compiler as compiler
    import openadapt_flow.demo_driver as demo_driver
    import openadapt_flow.mockmed.server as server

    monkeypatch.setattr(server, "serve", lambda port=0: ("http://x:1/", lambda: None))
    monkeypatch.setattr(
        demo_driver,
        "record_triage_demo",
        lambda url, out, note_text, headed=False: Path(out),
    )

    def fake_compile(recording, bundle, name):
        wf = make_workflow()
        wf.save(bundle)
        return wf

    monkeypatch.setattr(compiler, "compile_recording", fake_compile)

    def fake_compiled_run(bundle, url, run_dir, note, **kw):
        log_url("compiled", url)
        return _row("compiled", 0, "x", success="drift" not in url)

    monkeypatch.setattr(hybrid_benchmark, "_compiled_run", fake_compiled_run)

    def fake_hybrid_run(bundle, url, run_dir, note, *, ledger, **kw):
        log_url("hybrid", url)
        if "drift" not in url:
            return _row(
                "hybrid",
                0,
                "x",
                success=True,
                fallback_used=False,
                halted=False,
                fallback_actions=0,
                fallback_cost_usd=0.0,
                fallback_skipped_reason=None,
            )
        if not ledger.can_start():
            return _row(
                "hybrid",
                0,
                "x",
                success=False,
                fallback_used=False,
                halted=True,
                fallback_actions=0,
                fallback_cost_usd=0.0,
                fallback_skipped_reason=ledger.blocked_reason(),
            )
        ledger.record(0.2)
        return _row(
            "hybrid",
            0,
            "x",
            success=True,
            cost=0.2,
            fallback_used=True,
            halted=True,
            halt_step="step_004",
            fallback_actions=6,
            fallback_cost_usd=0.2,
            fallback_skipped_reason=None,
        )

    monkeypatch.setattr(hybrid_benchmark, "_hybrid_run", fake_hybrid_run)

    def fake_agent_run(url, note, **kw):
        log_url("agent", url)
        return _row("agent", 0, "x", success=True, cost=agent_cost, api_calls=13)

    monkeypatch.setattr(hybrid_benchmark, "_agent_run", fake_agent_run)


class TestOrchestratorGuardrails:
    def test_budget_ceiling_truncates_paid_arms_and_discloses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_fake_arms(monkeypatch, tmp_path, agent_cost=1.4)
        results = run_hybrid_benchmark(
            tmp_path / "out",
            max_cost_per_run_usd=1.5,
            max_total_cost_usd=4.0,
            preflight=lambda: (True, None),
            log=lambda s: None,
        )
        # 6 hybrid fallbacks x $0.2 = $1.2 spent; then agent runs at
        # $1.4: 1.2+1.5<=4 ok (2.6), 2.6+1.5>4 -> stop after 1 run.
        assert len(results["runs"]["agent"]) == 1
        assert len(results["runs"]["demo_agent"]) == 0
        assert "truncated" in results["arm_notes"]["agent"]
        assert "truncated" in results["arm_notes"]["demo_agent"]
        spent = results["cost_caps_usd"]["total_spent_list"]
        assert spent <= 4.0

    def test_failed_preflight_skips_paid_but_runs_free_arms(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_fake_arms(monkeypatch, tmp_path)
        results = run_hybrid_benchmark(
            tmp_path / "out",
            preflight=lambda: (False, "401 authentication_error"),
            log=lambda s: None,
        )
        assert len(results["runs"]["compiled"]) == len(SCHEDULE)
        assert len(results["runs"]["hybrid"]) == len(SCHEDULE)
        assert len(results["runs"]["agent"]) == 0
        assert len(results["runs"]["demo_agent"]) == 0
        assert "preflight" in results["arm_notes"]["agent"]
        assert "preflight" in results["arm_notes"]["hybrid"]
        # No fallback fired anywhere.
        assert all(not r["fallback_used"] for r in results["runs"]["hybrid"])

    def test_rows_jsonl_appended_for_every_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_fake_arms(monkeypatch, tmp_path)
        out = tmp_path / "out"
        results = run_hybrid_benchmark(
            out,
            preflight=lambda: (True, None),
            log=lambda s: None,
        )
        lines = (out / "rows.jsonl").read_text().strip().splitlines()
        total_runs = sum(len(rows) for rows in results["runs"].values())
        assert len(lines) == total_runs
        assert all(json.loads(line)["arm"] for line in lines)

    def test_arms_see_identical_conditions_per_slot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Asserted on the URLs each arm ACTUALLY received (captured from
        the run helpers), not on the orchestrator-stamped condition labels
        — the labels come from the same schedule lookup the orchestrator
        uses to build the URLs, so asserting on them alone is circular."""
        from openadapt_flow.benchmark.hybrid_benchmark import (
            AGENT_SLOTS,
            condition_url,
        )

        url_log: list = []
        install_fake_arms(monkeypatch, tmp_path, url_log=url_log)
        results = run_hybrid_benchmark(
            tmp_path / "out",
            preflight=lambda: (True, None),
            log=lambda s: None,
        )
        base = "http://x:1/"
        n = len(SCHEDULE)
        # Call order is deterministic: compiled over the full schedule,
        # hybrid over the full schedule, then agent and demo_agent over
        # the subsample (both through the same _agent_run helper).
        expected = (
            [("compiled", s) for s in range(n)]
            + [("hybrid", s) for s in range(n)]
            + [("agent", s) for s in AGENT_SLOTS]
            + [("agent", s) for s in AGENT_SLOTS]
        )
        assert len(url_log) == len(expected)
        for (arm, url), (want_arm, slot) in zip(url_log, expected):
            assert arm == want_arm
            assert url == condition_url(base, SCHEDULE[slot]), (arm, slot, url)
        # And the stamped labels agree with the URLs actually used.
        by_slot: dict[int, set[str]] = {}
        for rows in results["runs"].values():
            for r in rows:
                by_slot.setdefault(r["slot"], set()).add(r["condition"])
        assert all(len(conds) == 1 for conds in by_slot.values())
        for slot, conds in by_slot.items():
            assert conds == {SCHEDULE[slot]}
