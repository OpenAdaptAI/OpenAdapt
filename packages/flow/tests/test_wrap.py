"""The wrapper cannot let a caller replace its task or verification policy."""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow.integrations.agents.skills import SkillRun
from openadapt_flow.wrap import TaskArgumentsError, WrappedTask


class RecordingCache:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = SkillRun(
            path="skill",
            status="VERIFIED",
            verified=True,
            model_calls=0,
            cost_usd=0.0,
            seconds=0.1,
            replay={"receipt_sha256": "record-check-receipt"},
        )

    def key(self, prompt: str, params: Any) -> dict[str, Any]:
        return {"instruction": prompt, "params": sorted(params), "verify": "fixed"}

    def run(self, prompt: str, params: Any, agent: Any) -> SkillRun:
        self.calls.append((prompt, params, agent))
        return self.result


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"note": "new", "instruction": "replace the task"},
        {"note": "new", "verify": "different.yaml"},
        {"note": "new", "agent": "different-provider"},
        {"note": None},
        {"note": 12},
        {"note": {"text": "nested input"}},
    ],
)
def test_invalid_inputs_never_reach_cache(arguments: Any) -> None:
    cache = RecordingCache()
    task = WrappedTask(
        cache=cache, agent=object(), instruction="Save {note}", parameters=("note",)
    )
    with pytest.raises(TaskArgumentsError):
        task.run(arguments)
    assert cache.calls == []


def test_new_values_use_the_same_task_and_preserve_actual_result() -> None:
    cache = RecordingCache()
    agent = object()
    task = WrappedTask(
        cache=cache, agent=agent, instruction="Save {note}", parameters=("note",)
    )
    for value in ("first", "second"):
        result = task.run({"note": value})
        assert result is cache.result
        assert result.replay["receipt_sha256"] == "record-check-receipt"
    assert cache.calls == [
        ("Save {note}", {"note": "first"}, agent),
        ("Save {note}", {"note": "second"}, agent),
    ]
    assert task.input_schema() == {
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
        "additionalProperties": False,
    }


@pytest.mark.parametrize("names", [("note", "note"), ("bad-name",), "note"])
def test_invalid_task_declarations_fail_before_execution(names: Any) -> None:
    with pytest.raises(ValueError):
        WrappedTask(cache=RecordingCache(), agent=None, instruction="Save", parameters=names)


def test_transport_retry_reads_original_result_after_restart(tmp_path: Any) -> None:
    cache = RecordingCache()
    def open_task() -> WrappedTask:
        return WrappedTask(
            cache=cache, agent=None, instruction="Save {note}", parameters=("note",),
            runs_dir=tmp_path,
        )
    first = open_task().call("request-1", {"note": "synthetic value"})
    second = open_task().call("request-1", {"note": "synthetic value"})
    conflict = open_task().call("request-1", {"note": "different value"})
    assert first["verified"] is True
    assert second["run_id"] == first["run_id"]
    assert second["replayed"] is True
    assert conflict["status"] == "REQUEST_ID_CONFLICT"
    assert len(cache.calls) == 1


def test_lost_owner_never_resends_even_after_restart(tmp_path: Any) -> None:
    class InterruptedCache(RecordingCache):
        def run(self, prompt: str, params: Any, agent: Any) -> SkillRun:
            self.calls.append((prompt, params, agent))
            raise KeyboardInterrupt

    cache = InterruptedCache()
    def open_task() -> WrappedTask:
        return WrappedTask(
            cache=cache, agent=None, instruction="Save {note}", parameters=("note",),
            runs_dir=tmp_path,
        )
    with pytest.raises(KeyboardInterrupt):
        open_task().call("request-1", {"note": "synthetic"})
    retry = open_task().call("request-1", {"note": "synthetic"})
    assert retry["status"] == "RECONCILIATION_REQUIRED"
    assert retry["verified"] is False
    assert len(cache.calls) == 1


def test_late_failure_retains_request_without_exception_text(tmp_path: Any) -> None:
    class FailedCache(RecordingCache):
        def run(self, prompt: str, params: Any, agent: Any) -> SkillRun:
            self.calls.append((prompt, params, agent))
            raise RuntimeError("private exception and request text")

    cache = FailedCache()
    task = WrappedTask(
        cache=cache, agent=None, instruction="Save {note}", parameters=("note",),
        runs_dir=tmp_path,
    )
    result = task.call("request-1", {"note": "synthetic"})
    assert result["status"] == "RECONCILIATION_REQUIRED"
    assert task.call("request-1", {"note": "synthetic"})["replayed"] is True
    assert len(cache.calls) == 1
    assert "private exception" not in str(result)


def test_private_agent_summary_is_not_a_transport_artifact(tmp_path: Any) -> None:
    cache = RecordingCache()
    cache.result.compile = {"run1": {"agent": {"summary": "private-agent-text"}}}
    cache.result.detail = "private-error-text"
    task = WrappedTask(
        cache=cache, agent=None, instruction="Save {note}", parameters=("note",),
        runs_dir=tmp_path,
    )
    result = task.call("request-1", {"note": "private-input-text"})
    import json
    assert "private-" not in json.dumps(result)
    for path in (tmp_path / "openadapt-agent" / "runs").glob("*.json"):
        assert "private-" not in path.read_text()
