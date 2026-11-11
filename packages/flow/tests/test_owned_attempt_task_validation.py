"""Task validation preserves observed writes while refusing owned credit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verify_run as verifier_module
from openadapt_flow.taskgen import validate
from tests._owned_attempt_consumer_support import (
    PARAMS,
    check_matching_read,
    exercise_external_write,
    observe_receipts,
    setup_store,
)


class SyntheticEpisode:
    """Deterministic stand-in for the workbench and UI around a real SQL read."""

    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label

    def reset(self, setup: dict[str, Any]) -> None:
        assert setup == {}

    def open(self, url: str) -> None:
        assert url == "http://synthetic.invalid/login"

    def run(self, solution: list[dict[str, Any]]) -> bool:
        assert solution == [{"op": "synthetic-noop"}]
        exercise_external_write(self.path, label=self.label)
        return True


def _episode(
    path: Path, label: str, monkeypatch: pytest.MonkeyPatch, *, owned: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = setup_store(path)
    episode = SyntheticEpisode(path, label)
    with monkeypatch.context() as scope:
        receipts = observe_receipts(
            scope, verifier_module, verifier_module, owned=owned
        )
        result = validate.judge_episode(
            episode,
            episode,
            "http://synthetic.invalid",
            cfg,
            dict(PARAMS),
            {},
            [{"op": "synthetic-noop"}],
        )
    (readback,) = receipts
    assert result["ui"] == "reached_done"
    assert "error" not in result
    assert result["nothing_written"] is False
    return result, readback


@pytest.mark.parametrize("owned", [False, True], ids=["observation", "owned"])
def test_matching_external_write_cannot_validate_an_owned_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owned: bool
) -> None:
    positive, readback = _episode(
        tmp_path / "positive.sqlite", PARAMS["label"], monkeypatch, owned=owned
    )
    check_matching_read(readback, owned=owned)
    assert positive["verdict"] == readback["receipt"]["effect_verdict"]
    assert positive["refuted_by"] == []

    planted, readback = _episode(
        tmp_path / "planted.sqlite", "wrong-value", monkeypatch, owned=owned
    )
    assert readback["receipt"]["effect_verdict"] == "REFUTED"
    assert planted["verdict"] == "REFUTED"
    assert planted["refuted_by"]

    pack = tmp_path / "pack"
    plans = pack / "validation"
    plans.mkdir(parents=True)
    (pack / "tasks.jsonl").write_text(
        json.dumps({"id": "synthetic-task"}) + "\n", encoding="utf-8"
    )
    (plans / "synthetic-task.json").write_text(
        json.dumps(
            {
                "app": "synthetic",
                "schema": "synthetic",
                "kind": "insert",
                "family": "attribution",
                "positive": {},
                "name": "Synthetic validation",
                "planted": [{"name": "wrong-value"}],
            }
        ),
        encoding="utf-8",
    )
    summary = validate.summarize(
        pack,
        [
            {**positive, "task_id": "synthetic-task", "episode": "positive"},
            {**planted, "task_id": "synthetic-task", "episode": "wrong-value"},
        ],
    )
    assert summary["valid_ids"] == ([] if owned else ["synthetic-task"])
    assert summary["tasks_with_a_substantive_refutation"] == 1
    if owned:
        assert summary["rejected"]["synthetic-task"][0].startswith(
            "positive INDETERMINATE:"
        )
    else:
        assert summary["rejected"] == {}
