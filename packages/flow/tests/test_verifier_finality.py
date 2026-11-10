"""Finality is a settled verdict, independently of verified success.

These are result-projection controls. The public SQLite and MCP cases cover
actual reads and transport projections separately.
"""

from __future__ import annotations

import json
from typing import Literal

import pytest

from openadapt_flow.verifier import Outcome, RecheckResult, Scope, Window, WriteResult
from openadapt_flow.verify import result_json

DUE = "2026-11-05T00:02:00Z"
READ = "2026-11-05T00:03:00Z"


@pytest.mark.parametrize(
    "outcome,due,verified,provisional,final",
    [
        (Outcome.CONFIRMED, None, True, False, True),
        (Outcome.CONFIRMED, DUE, True, True, False),
        (Outcome.REFUTED, None, False, False, True),
        (Outcome.REFUTED, DUE, False, False, True),
        (Outcome.UNCERTAIN, None, False, False, False),
        (Outcome.UNCERTAIN, DUE, False, False, False),
    ],
)
def test_write_finality_preserves_success_and_negative_verdicts(
    outcome: Outcome,
    due: str | None,
    verified: bool,
    provisional: bool,
    final: bool,
) -> None:
    scope = Scope((), (), (), Window(None, None, None), "unchecked", ())
    result = WriteResult(outcome=outcome, scope=scope, recheck_due_at=due)
    assert result.verified is verified
    assert result.provisional is provisional
    assert result.final is final
    for projected in (result.to_dict(), json.loads(result_json(result))):
        assert projected["outcome"] == outcome.value
        assert projected["verified"] is verified
        assert projected["provisional"] is provisional
        assert projected["final"] is final
        assert projected["retry_safe"] is False


@pytest.mark.parametrize(
    "outcome,frame,read_at,verified,final",
    [
        ("CONFIRMED", "held", READ, True, True),
        ("REFUTED", "refuted", READ, False, True),
        ("UNCERTAIN", "held", READ, False, False),
        ("UNCERTAIN", "refuted", READ, False, False),
        ("UNCERTAIN", "not_checked", READ, False, False),
        ("CONFIRMED", "not_checked", READ, True, False),
        ("REFUTED", "not_checked", READ, False, False),
        ("CONFIRMED", "held", "", True, False),
        ("REFUTED", "refuted", "", False, False),
    ],
)
def test_recheck_requires_a_resolved_outcome_and_recorded_proof(
    outcome: Literal["CONFIRMED", "REFUTED", "UNCERTAIN"],
    frame: str,
    read_at: str,
    verified: bool,
    final: bool,
) -> None:
    result = RecheckResult(outcome=outcome, frame=frame, read_at=read_at)
    assert result.verified is verified
    assert result.final is final
    assert result.late is (outcome == "REFUTED")
