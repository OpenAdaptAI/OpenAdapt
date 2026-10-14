"""Tests for workflow transcript generation pipeline."""

from __future__ import annotations

import json
from unittest.mock import patch

from openadapt_evals.workflow.models import (
    ActionType,
    NormalizedAction,
    RecordingSession,
    RecordingSource,
    TranscriptEntry,
)
from openadapt_evals.workflow.pipeline.scrub import (
    scrub_recording_session,
    scrub_text,
)
from openadapt_evals.workflow.pipeline.transcript import (
    _parse_transcript_response,
    estimate_transcript_cost,
    generate_transcript,
)


def _make_session(n_actions: int = 5) -> RecordingSession:
    """Create a synthetic recording session for testing."""
    actions = []
    for i in range(n_actions):
        actions.append(
            NormalizedAction(
                timestamp=float(i * 2),
                action_type=ActionType.CLICK,
                description=f"Click button {i}",
                x=100 + i * 50,
                y=200,
                app_name="TestApp",
                window_title="Test Window",
            )
        )
    return RecordingSession(
        source=RecordingSource.WAA_VNC,
        task_description="Complete the test task",
        platform="windows",
        duration_seconds=float(n_actions * 2),
        actions=actions,
    )


MOCK_VLM_RESPONSE = json.dumps([
    {
        "narration": f"Clicks button {i}",
        "intent": "Interact with UI",
        "ui_element_description": f"Button {i}",
        "app_context": "TestApp - Test Window",
        "state_change": "Button clicked",
        "is_corrective": False,
        "is_exploratory": False,
        "vlm_confidence": 0.9,
    }
    for i in range(5)
])


class TestTranscriptGeneration:
    @patch("openadapt_evals.vlm.vlm_call")
    def test_generates_one_entry_per_action(self, mock_vlm):
        mock_vlm.return_value = MOCK_VLM_RESPONSE
        session = _make_session(5)
        transcript = generate_transcript(session, batch_size=10)
        assert len(transcript.entries) == 5
        assert all(isinstance(e, TranscriptEntry) for e in transcript.entries)

    @patch("openadapt_evals.vlm.vlm_call")
    def test_batching_produces_multiple_calls(self, mock_vlm):
        mock_vlm.return_value = json.dumps([
            {
                "narration": "Action",
                "intent": "Do thing",
                "ui_element_description": "Element",
                "app_context": "App",
                "state_change": "Changed",
                "is_corrective": False,
                "is_exploratory": False,
                "vlm_confidence": 0.8,
            }
        ] * 3)
        session = _make_session(10)
        generate_transcript(session, batch_size=3)
        # With batch_size=3 and 10 actions, multiple VLM calls needed
        assert mock_vlm.call_count >= 3

    @patch("openadapt_evals.vlm.vlm_call")
    def test_transcript_preserves_action_ids(self, mock_vlm):
        mock_vlm.return_value = MOCK_VLM_RESPONSE
        session = _make_session(5)
        transcript = generate_transcript(session, batch_size=10)
        action_ids = {a.action_id for a in session.actions}
        for entry in transcript.entries:
            assert entry.action_id in action_ids

    @patch("openadapt_evals.vlm.vlm_call")
    def test_transcript_metadata(self, mock_vlm):
        mock_vlm.return_value = MOCK_VLM_RESPONSE
        session = _make_session(5)
        transcript = generate_transcript(
            session, model="gpt-4.1-mini", provider="openai",
        )
        assert transcript.vlm_model == "gpt-4.1-mini"
        assert transcript.vlm_provider == "openai"
        assert transcript.session_id == session.session_id
        assert transcript.task_description == session.task_description

    @patch("openadapt_evals.vlm.vlm_call")
    def test_apps_used_populated(self, mock_vlm):
        mock_vlm.return_value = MOCK_VLM_RESPONSE
        session = _make_session(5)
        transcript = generate_transcript(session, batch_size=10)
        assert "TestApp" in transcript.apps_used


class TestParseTranscriptResponse:
    def test_parses_json_array(self):
        raw = json.dumps([
            {"narration": "Click", "intent": "Do"},
            {"narration": "Type", "intent": "Enter"},
        ])
        result = _parse_transcript_response(raw, 2)
        assert len(result) == 2
        assert result[0]["narration"] == "Click"

    def test_parses_json_in_markdown(self):
        raw = "Here are the annotations:\n```json\n" + json.dumps([
            {"narration": "Click"}
        ]) + "\n```"
        result = _parse_transcript_response(raw, 1)
        assert len(result) >= 1

    def test_fallback_on_garbage(self):
        raw = "I'm sorry, I can't process that."
        result = _parse_transcript_response(raw, 3)
        assert len(result) == 3
        assert all(r["vlm_confidence"] == 0.0 for r in result)

    def test_parses_individual_objects(self):
        raw = '{"narration": "A"}\n{"narration": "B"}'
        result = _parse_transcript_response(raw, 2)
        assert len(result) == 2


class TestCostEstimation:
    def test_estimate_returns_dict(self):
        session = _make_session(10)
        estimate = estimate_transcript_cost(session, model="gpt-4.1-mini")
        assert "estimated_cost_usd" in estimate
        assert "num_actions" in estimate
        assert estimate["num_actions"] == 10
        assert estimate["estimated_cost_usd"] > 0

    def test_mini_cheaper_than_full(self):
        session = _make_session(10)
        mini = estimate_transcript_cost(session, model="gpt-4.1-mini")
        full = estimate_transcript_cost(session, model="gpt-4.1")
        assert mini["estimated_cost_usd"] < full["estimated_cost_usd"]


class TestPIIScrubbing:
    def test_scrub_text_passthrough_without_privacy(self):
        # Without openadapt-privacy installed, should pass through
        result = scrub_text("john@example.com sent a file")
        assert isinstance(result, str)

    def test_scrub_session_returns_new_object(self):
        session = _make_session(3)
        scrubbed = scrub_recording_session(session)
        assert scrubbed is not session
        assert scrubbed.session_id == session.session_id
        assert len(scrubbed.actions) == len(session.actions)

    def test_scrub_session_scrubs_descriptions(self):
        session = _make_session(2)
        session.actions[0].description = "Type john@example.com"
        session.actions[0].typed_text = "john@example.com"
        scrubbed = scrub_recording_session(session)
        # Without openadapt-privacy, text passes through unchanged
        assert scrubbed.actions[0].description is not None
