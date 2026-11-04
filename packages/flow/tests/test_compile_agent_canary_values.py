"""A canary must not repeat run 1's input values.

A program that types the demonstrated text as a constant passes a canary that
uses run 1's values, so such a canary proves nothing about its parameters.
Run 1's values are kept only as salted digests.
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.compile_agent.pipeline import _repeats_run1, _write_run1_values


class _Task:
    params = {"patient": "str", "note": "str"}

    @staticmethod
    def validate(row):
        return {name: str(value) for name, value in row.items()}


def test_run1_values_are_stored_only_as_digests(tmp_path: Path) -> None:
    path = tmp_path / "run1_values.json"
    _write_run1_values(path, {"patient": "Jane Doe", "note": "Follow up"})
    text = path.read_text()
    assert "Jane Doe" not in text and "Follow up" not in text
    assert set(json.loads(text)["digests"]) == {"patient", "note"}


def test_a_canary_with_run1_values_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "run1_values.json"
    _write_run1_values(path, {"patient": "Jane Doe", "note": "Follow up"})
    why = _repeats_run1(
        path,
        _Task,
        [
            {"patient": "Ada Byron", "note": "Canary"},
            {"patient": "Jane Doe", "note": "Canary 2"},
        ],
    )
    assert why is not None
    assert "canary 2" in why and "patient" in why
    assert "Jane Doe" not in why


def test_canaries_with_new_values_pass(tmp_path: Path) -> None:
    path = tmp_path / "run1_values.json"
    _write_run1_values(path, {"patient": "Jane Doe", "note": "Follow up"})
    assert (
        _repeats_run1(path, _Task, [{"patient": "Ada Byron", "note": "Canary"}]) is None
    )


def test_missing_run1_digests_fail_closed(tmp_path: Path) -> None:
    why = _repeats_run1(tmp_path / "absent.json", _Task, [{"patient": "x"}])
    assert why is not None and "record run 1 again" in why
