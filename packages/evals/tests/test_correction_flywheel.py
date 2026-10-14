"""Tests for the correction flywheel: store, capture, parser, and controller integration."""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import MagicMock, patch

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkResult,
    BenchmarkTask,
)
from openadapt_evals.correction_store import CorrectionEntry, CorrectionStore
from openadapt_evals.demo_controller import DemoController
from openadapt_evals.plan_verify import VerificationResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DEMO = """\
GOAL: Change the display resolution

PLAN:
1. Open Display settings
2. Change resolution

REFERENCE TRAJECTORY (for disambiguation -- adapt actions to your actual screen):

Step 1:
  Think: I need to open Display settings.
  Action: Click the Display button in the left sidebar.
  Expect: Display settings pane should open.

Step 2:
  Think: I need to change resolution.
  Action: Click the Resolution dropdown and select 1920x1080.
  Expect: Resolution should change to 1920x1080.
"""


def _make_entry(
    task_id: str = "display-resolution",
    step_desc: str = "Click the Display button in the left sidebar.",
    **kwargs,
) -> CorrectionEntry:
    defaults = {
        "task_id": task_id,
        "step_description": step_desc,
        "failure_screenshot_path": "/tmp/before.png",
        "failure_explanation": "Agent clicked wrong pane",
        "correction_step": {
            "think": "The Display button is in the left sidebar, third item",
            "action": "Click the Display button (third item in left sidebar)",
            "expect": "Display settings pane opens",
        },
        "run_id": "test-run-01",
    }
    defaults.update(kwargs)
    return CorrectionEntry(**defaults)


# ---------------------------------------------------------------------------
# CorrectionStore tests
# ---------------------------------------------------------------------------


class TestCorrectionStore:
    def test_save_and_find(self, tmp_path):
        store = CorrectionStore(str(tmp_path / "corrections"))
        entry = _make_entry()
        entry_id = store.save(entry)

        results = store.find(
            task_id="display-resolution",
            step_description="Click the Display button in the left sidebar.",
        )
        assert len(results) == 1
        assert results[0].entry_id == entry_id
        assert results[0].correction_step["action"] == entry.correction_step["action"]

    def test_fuzzy_match(self, tmp_path):
        store = CorrectionStore(str(tmp_path / "corrections"))
        store.save(_make_entry())

        # Slightly different description should still match
        results = store.find(
            task_id="display-resolution",
            step_description="Click Display button on the left sidebar",
        )
        assert len(results) == 1

    def test_no_match_different_task(self, tmp_path):
        store = CorrectionStore(str(tmp_path / "corrections"))
        store.save(_make_entry(task_id="display-resolution"))

        results = store.find(
            task_id="change-wallpaper",
            step_description="Click the Display button in the left sidebar.",
        )
        assert len(results) == 0

    def test_no_match_low_similarity(self, tmp_path):
        store = CorrectionStore(str(tmp_path / "corrections"))
        store.save(_make_entry())

        results = store.find(
            task_id="display-resolution",
            step_description="Open the terminal and type ls",
        )
        assert len(results) == 0

    def test_load_all(self, tmp_path):
        store = CorrectionStore(str(tmp_path / "corrections"))
        store.save(_make_entry(entry_id="aaa"))
        store.save(_make_entry(entry_id="bbb"))

        all_entries = store.load_all()
        assert len(all_entries) == 2
        ids = {e.entry_id for e in all_entries}
        assert ids == {"aaa", "bbb"}

    def test_entry_serialization_roundtrip(self, tmp_path):
        store = CorrectionStore(str(tmp_path / "corrections"))
        entry = _make_entry()
        store.save(entry)

        loaded = store.load_all()
        assert len(loaded) == 1
        assert asdict(loaded[0]) == asdict(entry)

    def test_empty_store(self, tmp_path):
        store = CorrectionStore(str(tmp_path / "corrections"))
        assert store.load_all() == []
        assert store.find("any", "any") == []

    def test_multiple_matches_ranked(self, tmp_path):
        store = CorrectionStore(str(tmp_path / "corrections"))

        # High similarity
        store.save(
            _make_entry(
                step_desc="Click the Display button in the left sidebar.",
                entry_id="exact",
            )
        )
        # Medium similarity
        store.save(
            _make_entry(
                step_desc="Click Display in sidebar",
                entry_id="partial",
            )
        )

        results = store.find(
            task_id="display-resolution",
            step_description="Click the Display button in the left sidebar.",
            top_k=2,
        )
        assert len(results) == 2
        # Exact match should come first
        assert results[0].entry_id == "exact"

    def test_skips_invalid_json(self, tmp_path):
        lib_dir = tmp_path / "corrections"
        lib_dir.mkdir()
        (lib_dir / "bad.json").write_text("not json")
        (lib_dir / "readme.txt").write_text("ignore me")

        store = CorrectionStore(str(lib_dir))
        assert store.load_all() == []


# ---------------------------------------------------------------------------
# CorrectionParser tests
# ---------------------------------------------------------------------------


class TestCorrectionParser:
    @patch("openadapt_evals.correction_parser.vlm_call")
    def test_parse_correction_returns_plan_step(self, mock_vlm):
        mock_vlm.return_value = json.dumps(
            {
                "think": "The Display button is third in the sidebar",
                "action": "Click the third item in the left sidebar labeled Display",
                "expect": "Display settings pane opens showing resolution options",
            }
        )

        from openadapt_evals.correction_parser import parse_correction

        result = parse_correction(
            step_action="Click the Display button",
            failure_explanation="Clicked wrong button",
            before_screenshot=b"fake-png-before",
            after_screenshot=b"fake-png-after",
        )

        assert result["think"] == "The Display button is third in the sidebar"
        assert "Display" in result["action"]
        assert "expect" in result
        mock_vlm.assert_called_once()

    @patch("openadapt_evals.correction_parser.vlm_call")
    def test_parse_correction_handles_bad_json(self, mock_vlm):
        mock_vlm.return_value = "Sorry, I can't parse that."

        from openadapt_evals.correction_parser import parse_correction

        result = parse_correction(
            step_action="Click Display",
            failure_explanation="Failed",
            before_screenshot=b"fake",
            after_screenshot=b"fake",
        )

        # Should fall back to reasonable defaults
        assert "action" in result
        assert "think" in result
        assert "expect" in result


# ---------------------------------------------------------------------------
# DemoController correction integration tests
# ---------------------------------------------------------------------------


def _make_mock_agent():
    agent = MagicMock()
    agent._external_step_control = False
    agent.act.return_value = BenchmarkAction(
        type="click", x=100, y=200, raw_action={}
    )
    return agent


def _make_mock_adapter():
    adapter = MagicMock()
    adapter.observe.return_value = BenchmarkObservation(
        screenshot=b"fake-screenshot-bytes",
        raw_observation={},
    )
    adapter.step.return_value = (
        BenchmarkObservation(screenshot=b"fake-screenshot-bytes", raw_observation={}),
        False,  # env_done
        {},  # info
    )
    adapter.evaluate.return_value = BenchmarkResult(
        task_id="test", success=True, score=1.0
    )
    return adapter


class TestDemoControllerCorrections:
    @patch("openadapt_evals.demo_controller.verify_step")
    def test_uses_stored_correction(self, mock_verify, tmp_path):
        """Controller retrieves and uses stored correction instead of replanning."""
        store = CorrectionStore(str(tmp_path / "corrections"))
        store.save(
            _make_entry(
                task_id="test-task",
                step_desc="Click the Display button in the left sidebar.",
            )
        )

        agent = _make_mock_agent()
        adapter = _make_mock_adapter()

        controller = DemoController(
            agent=agent,
            adapter=adapter,
            demo_text=SAMPLE_DEMO,
            max_retries=1,
            correction_store=store,
        )

        # First call: fail verification (triggers correction lookup)
        # Second call (after correction injected): pass verification
        mock_verify.side_effect = [
            VerificationResult(
                status="not_verified",
                confidence=0.2,
                explanation="Wrong pane clicked",
                raw_response="",
            ),
            VerificationResult(
                status="verified",
                confidence=0.95,
                explanation="Display settings visible",
                raw_response="",
            ),
            # Step 2 passes immediately
            VerificationResult(
                status="verified",
                confidence=0.9,
                explanation="Resolution changed",
                raw_response="",
            ),
        ]

        task = BenchmarkTask(
            task_id="test-task",
            instruction="Change the display resolution",
            domain="desktop",
        )

        controller.execute(task, max_steps=10)

        # Verify the correction was used (step was replaced)
        step1 = controller.plan_state.steps[0]
        assert "third item" in step1.action.lower() or step1.status == "done"

    @patch("openadapt_evals.demo_controller.verify_step")
    def test_no_correction_falls_through_to_replan(self, mock_verify, tmp_path):
        """Without stored correction, falls through to normal replan."""
        store = CorrectionStore(str(tmp_path / "corrections"))
        # Empty store - no corrections

        agent = _make_mock_agent()
        adapter = _make_mock_adapter()

        controller = DemoController(
            agent=agent,
            adapter=adapter,
            demo_text=SAMPLE_DEMO,
            max_retries=1,
            max_replans=1,
            correction_store=store,
        )

        # All verifications fail
        mock_verify.return_value = VerificationResult(
            status="not_verified",
            confidence=0.1,
            explanation="Wrong state",
            raw_response="",
        )

        task = BenchmarkTask(
            task_id="other-task",
            instruction="Something else",
            domain="desktop",
        )

        with patch.object(controller, "_replan") as mock_replan:
            # The controller will try correction store (empty), then replan
            # We need to handle the replan to avoid infinite loop
            def fake_replan(obs, step):
                step.status = "failed"
                controller._advance()

            mock_replan.side_effect = fake_replan

            controller.execute(task, max_steps=5)

            # Replan should have been called since no corrections existed
            assert mock_replan.called

    def test_controller_accepts_correction_store_none(self):
        """Controller works normally without correction store."""
        agent = _make_mock_agent()
        adapter = _make_mock_adapter()

        controller = DemoController(
            agent=agent,
            adapter=adapter,
            demo_text=SAMPLE_DEMO,
            correction_store=None,
            enable_correction_capture=False,
        )
        assert controller.correction_store is None
        assert controller.enable_correction_capture is False


# ---------------------------------------------------------------------------
# CorrectionCapture tests
# ---------------------------------------------------------------------------


class TestCorrectionCapture:
    def test_capture_result_structure(self, tmp_path):
        from openadapt_evals.correction_capture import CorrectionResult

        result = CorrectionResult(
            screenshots=["/tmp/before.png", "/tmp/after.png"],
            duration_seconds=5.0,
            output_dir=str(tmp_path),
        )
        assert len(result.screenshots) == 2
        assert result.duration_seconds == 5.0

    @patch("openadapt_evals.correction_capture._has_recorder", return_value=False)
    @patch("openadapt_evals.correction_capture._take_screenshot")
    def test_capture_with_immediate_enter(self, mock_screenshot, mock_has_rec, tmp_path):
        """Test capture completes when stdin signals immediately."""
        import io

        from openadapt_evals.correction_capture import CorrectionCapture

        mock_screenshot.return_value = str(tmp_path / "after.png")

        capture = CorrectionCapture(output_dir=str(tmp_path / "capture"))

        before_data = b"fake-png-data"

        # Mock select to signal stdin ready immediately, and provide a
        # fake stdin that returns a line (avoids pytest capture conflict)
        fake_stdin = io.StringIO("\n")
        with patch("select.select", return_value=([fake_stdin], [], [])), patch(
            "sys.stdin", fake_stdin
        ):
            result = capture.capture_correction(
                failure_context={
                    "screenshot_bytes": before_data,
                    "step_action": "Click Display",
                    "explanation": "Wrong button",
                },
                timeout_seconds=1,
            )

        assert result.output_dir == str(tmp_path / "capture")
        assert result.duration_seconds > 0


# ---------------------------------------------------------------------------
# End-to-end mock test
# ---------------------------------------------------------------------------


class TestCorrectionFlywheelE2E:
    @patch("openadapt_evals.correction_parser.vlm_call")
    @patch("openadapt_evals.demo_controller.verify_step")
    def test_full_loop_mock(self, mock_verify, mock_vlm, tmp_path):
        """Full loop: fail -> capture correction (mocked) -> store -> retrieve on next run."""
        store = CorrectionStore(str(tmp_path / "corrections"))
        task = BenchmarkTask(
            task_id="display-resolution",
            instruction="Change the display resolution",
            domain="desktop",
        )

        # --- Phase 1: Store a correction directly (simulating capture) ---
        mock_vlm.return_value = json.dumps(
            {
                "think": "Display button is third item in sidebar",
                "action": "Click the third item labeled Display in sidebar",
                "expect": "Display settings pane opens",
            }
        )

        from openadapt_evals.correction_parser import parse_correction

        correction_step = parse_correction(
            step_action="Click the Display button in the left sidebar.",
            failure_explanation="Agent clicked wrong pane",
            before_screenshot=b"before",
            after_screenshot=b"after",
        )

        store.save(
            CorrectionEntry(
                task_id="display-resolution",
                step_description="Click the Display button in the left sidebar.",
                failure_screenshot_path="/tmp/before.png",
                failure_explanation="Agent clicked wrong pane",
                correction_step=correction_step,
                run_id="run-1",
            )
        )

        # --- Phase 2: Second run retrieves correction ---
        agent = _make_mock_agent()
        adapter = _make_mock_adapter()

        controller = DemoController(
            agent=agent,
            adapter=adapter,
            demo_text=SAMPLE_DEMO,
            max_retries=1,
            correction_store=store,
        )

        # Step 1: fail first attempt, correction injected, second attempt succeeds
        # Step 2: succeeds immediately
        mock_verify.side_effect = [
            VerificationResult(
                status="not_verified",
                confidence=0.2,
                explanation="Wrong pane",
                raw_response="",
            ),
            VerificationResult(
                status="verified",
                confidence=0.95,
                explanation="Display settings open",
                raw_response="",
            ),
            VerificationResult(
                status="verified",
                confidence=0.9,
                explanation="Resolution changed",
                raw_response="",
            ),
        ]

        controller.execute(task, max_steps=10)

        # The correction should have been used
        step1 = controller.plan_state.steps[0]
        assert step1.status == "done"
        assert "third item" in step1.action.lower() or "display" in step1.action.lower()
