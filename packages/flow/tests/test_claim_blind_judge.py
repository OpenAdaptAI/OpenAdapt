"""Judge information-flow checks over synthetic evidence; no model calls."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from benchmark.verifier_baselines import medplum_views, run_judge, uv_judge
from benchmark.verifier_baselines.smoke_uv_practice import transcript_to_steps
from benchmark.verifier_baselines.test_uv_judge import FakeClient, _png

FINAL = "AGENT_FINAL_NARRATIVE_1982"
NARRATION = "AGENT_TURN_NARRATIVE_1982"
INTENT = "USER_INTENT_1982"
ACTION = "STRUCTURED_ACTION_1982"
RECORD = "OBSERVED_RECORD_1982 claim saved success"
HTTP = "MEASURED_HTTP_1982"


def _view(tmp_path, monkeypatch):
    task_file = tmp_path / "tasks.json"
    task_file.write_text(json.dumps({"synthetic-task": INTENT}))
    manifest = tmp_path / "manifest"
    row_path = "synthetic/agent/rows/run.json"
    manifest.write_text(f"unused-hash  {row_path}\n")
    transcript = [{"text": NARRATION, "actions": [{"text": ACTION}]}]
    blobs = {
        row_path: json.dumps(
            {
                "id": "synthetic-task",
                "claim": {"status": "done", "final_text": FINAL, "note": FINAL},
            }
        ).encode(),
        "synthetic/agent/rows/run/transcript.json": json.dumps(
            {
                "transcript": transcript,
            }
        ).encode(),
        "synthetic/agent/rows/run/start.png": _png(),
        "synthetic/agent/rows/run/end.png": _png((0, 255, 0)),
    }
    monkeypatch.setattr(medplum_views, "TASKS", task_file)
    monkeypatch.setattr(medplum_views, "MANIFEST", manifest)
    # No historical cases, files or git objects are read by this test.
    monkeypatch.setattr(medplum_views, "_show", blobs.__getitem__)
    view = next(medplum_views.runs())["view"]
    assert "predicted_output" not in view
    assert FINAL not in json.dumps(view, default=str)
    assert NARRATION not in json.dumps(view, default=str)
    smoke_steps = transcript_to_steps(transcript, "")
    assert NARRATION not in json.dumps(smoke_steps)
    assert ACTION in json.dumps(smoke_steps)
    return view


class CapturingClient(FakeClient):
    def __init__(self, fallback):
        super().__init__()
        self.fallback = fallback

    async def create(self, messages, json_output=False):
        from benchmark.verifier_baselines.test_uv_judge import STEP_HEADS

        if self.fallback and STEP_HEADS["4"] in self._text(messages):
            self.log.append(("4", copy.deepcopy(messages)))
            return SimpleNamespace(content="{}", usage={})
        return await super().create(messages, json_output)


@pytest.mark.parametrize("fallback", [False, True])
def test_every_model_stage_is_claim_blind(tmp_path, monkeypatch, fallback):
    view = _view(tmp_path, monkeypatch)
    # A legacy cached view must not reintroduce either narrative channel.
    view["predicted_output"] = FINAL
    step = view["steps"][0]
    step["state_description"] = NARRATION
    step["error"] = NARRATION
    step["tool_output"] = NARRATION
    # Only the harness supplies these observations; agent text is never relabelled.
    step["observations"] = [
        {"source": "system_of_record", "content": {"note": RECORD}},
        {"source": "application", "content": {"display": RECORD}},
        {"source": "http", "content": {"status_code": 201, "receipt": HTTP}},
        {"source": "agent", "content": NARRATION},
        {"content": NARRATION},
    ]
    monkeypatch.setattr(run_judge, "RES", tmp_path / "results")
    monkeypatch.setattr(run_judge, "DATA", tmp_path / "data")
    client = CapturingClient(fallback)
    output = tmp_path / "judge.json"
    asyncio.run(
        run_judge.judge_one(
            view,
            view.pop("screenshots"),
            output,
            "synthetic",
            client,
            asyncio.Semaphore(1),
        )
    )
    result = json.loads(output.read_text())
    assert "error" not in result
    assert result["output_success"] is True
    stages = {stage for stage, _ in client.log}
    expected = {"0a", "0b", "0c", "4", "6", "7", "8"}
    if fallback:
        expected.add("4single")
    assert stages == expected
    for stage, messages in client.log:
        serialized = json.dumps(messages)
        assert FINAL not in serialized, stage
        assert NARRATION not in serialized, stage
        assert INTENT in serialized, stage
        if stage not in {"0a", "0b"}:
            assert ACTION in serialized, stage
            assert RECORD in serialized, stage
            assert HTTP in serialized, stage
        if stage in {"4", "4single"}:
            images = [
                part
                for message in messages
                if isinstance(message["content"], list)
                for part in message["content"]
                if part["type"] == "image_url"
            ]
            assert images and images[0]["image_url"]["url"].startswith(
                "data:image/jpeg;base64,"
            )


def test_judge_rejects_final_answer_argument():
    client = FakeClient()
    with pytest.raises(TypeError, match="predicted_output"):
        asyncio.run(
            uv_judge.judge(
                INTENT,
                "",
                [],
                [],
                client,
                predicted_output=FINAL,
            )
        )
    assert client.log == []


@pytest.mark.parametrize("status", [201, 500, None])
def test_transport_receipt_stays_observed(status):
    from benchmark.verifier_baselines.effect_e2e_arms import ACTUATOR_TIMEOUT_S, steps

    observed_steps = steps(status)
    assert observed_steps[-1]["observations"] == [
        {
            "source": "http",
            "content": {"status_code": status, "timeout_s": ACTUATOR_TIMEOUT_S},
        }
    ]
    history = uv_judge.format_action_history(observed_steps)
    assert f'"status_code": {json.dumps(status)}' in history
    assert '"target": "Save button"' in history
