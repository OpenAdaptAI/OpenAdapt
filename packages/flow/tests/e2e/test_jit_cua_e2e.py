"""openadapt_flow.jit end to end, with recorded CUA trajectories and no model.

Each test replays a recorded computer-use trajectory (tests/fixtures/jit/) as
the agent, against MockMed served through its transactional system of record
(``GET /api/db``). It asserts:

* a cache miss runs the agent once and returns a candidate that waits for a
  person; the dead end in the trajectory is pruned and the typed input is
  proposed as a parameter;
* the candidate never admits itself, and a second run does not re-run the
  agent;
* after a person admits it, a run with a NEW value replays with zero model
  calls and ends VERIFIED by an independent read of the system of record;
* a run that halts goes to the exception inbox and never calls the agent;
* a trajectory the recording cannot hold produces no candidate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.jit import JitResult, TaskSignature, WebApp, compile_agent
from openadapt_flow.jit.recorded import RecordedTrajectoryAgent
from openadapt_flow.mockmed.fault_server import serve
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.tutorial import _http_json, _records

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jit"
TASK = TaskSignature(
    "mockmed.add_note",
    "Open the first referral, start a Triage encounter, and save this note: {note}",
    {"note": str},
)
DEMO_NOTE = "Synthetic follow-up in two weeks"
NEW_NOTE = "Recheck knee in ten days"


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


def _app(base: str, fault: str = "ok") -> WebApp:
    return WebApp(
        url=f"{base}/?fault={fault}&idempotency=demo#tasks",
        record_reader=lambda: _records(base),
        reset=lambda: _http_json(f"{base}/api/reset", method="POST", body={}),
    )


def _verifier(base: str) -> RestRecordVerifier:
    return RestRecordVerifier(
        base,
        records_path="/api/db",
        records_key="records",
        timeout_s=2.0,
        poll_interval_s=0.05,
    )


def _jit(base: str, agent: Any, cache: Path, *, fault: str = "ok", on_halt: Any = None):
    return compile_agent(
        agent,
        task=TASK,
        app=_app(base, fault),
        verifier=_verifier(base),
        policy="clinical-write",
        cache_dir=cache,
        on_halt=on_halt,
    )


def test_anthropic_trajectory_miss_admit_hit_and_halt(
    mockmed: str, tmp_path: Path
) -> None:
    agent = RecordedTrajectoryAgent.from_file(FIXTURES / "anthropic_add_note.json")
    jit = _jit(mockmed, agent, tmp_path / "cache")

    # Cache miss: the agent explores once; the result is a candidate.
    miss = jit.run(note=DEMO_NOTE)
    assert miss.status == "needs_human_admit", miss.detail
    assert miss.cache == "miss" and miss.agent_called and agent.calls == 1
    assert not jit.admitted
    candidate = miss.candidate
    assert candidate is not None
    dropped = candidate["distilled"]["dropped"]
    assert [d["rule"] for d in dropped] == ["dead_end", "dead_end"]
    assert [p["name"] for p in candidate["params"]["lifted"]] == ["note"]
    assert candidate["effects"] and not any(
        e["needs_operator_confirmation"] for e in candidate["effects"]
    )

    # The candidate does not admit itself, and the agent is not run again.
    pending = jit.run(note=DEMO_NOTE)
    assert pending.status == "needs_human_admit" and pending.cache == "pending"
    assert agent.calls == 1 and not jit.admitted

    record = jit.admit(accept_params=["note"], confirm="ok", operator="e2e-reviewer")
    assert record["status"] == "accepted" and len(record["digest"]) == 64
    assert {e["kind"] for e in record["effects"]} == {"record_written", "field_equals"}

    # Cache hit with a new value: the governed program, no model calls, VERIFIED.
    hit = jit.run(note=NEW_NOTE)
    assert hit.status == "VERIFIED", hit.halt_reason or hit.detail
    assert hit.cache == "hit" and not hit.agent_called and hit.model_calls == 0
    assert agent.calls == 1
    rows = _records(mockmed)
    assert [r["note"] for r in rows] == [NEW_NOTE]
    tiers = {
        evidence.verification_tier
        for step in hit.report.results
        for evidence in step.effect_evidence
        if evidence.final_verdict == "confirmed"
    }
    assert tiers and all(t is not None for t in tiers)

    # A halt goes to the inbox and back to the caller. The agent is not called.
    halts: list[JitResult] = []
    broken = _jit(
        mockmed, agent, tmp_path / "cache", fault="optimistic", on_halt=halts.append
    )
    halted = broken.run(note=NEW_NOTE)
    assert halted.status == "HALTED"
    assert not halted.agent_called and agent.calls == 1
    assert halted.model_calls == 0
    assert halted.inbox_item is not None and halted.inbox_item.is_file()
    assert halts == [halted]
    assert _records(mockmed) == []  # the screen said saved; the record says no


@pytest.mark.parametrize("fixture", ["openai_add_note", "playwright_mcp_add_note"])
def test_other_dialects_produce_a_candidate(
    mockmed: str, tmp_path: Path, fixture: str
) -> None:
    agent = RecordedTrajectoryAgent.from_file(FIXTURES / f"{fixture}.json")
    jit = _jit(mockmed, agent, tmp_path / "cache")
    miss = jit.run(note=DEMO_NOTE)
    assert miss.status == "needs_human_admit", miss.detail
    assert miss.candidate is not None
    assert [p["name"] for p in miss.candidate["params"]["lifted"]] == ["note"]
    assert {e["kind"] for e in miss.candidate["effects"]} >= {"record_written"}
    assert not jit.admitted


def test_snapshot_refs_produce_no_candidate(mockmed: str, tmp_path: Path) -> None:
    agent = RecordedTrajectoryAgent.from_file(
        FIXTURES / "playwright_mcp_snapshot_refs.json"
    )
    jit = _jit(mockmed, agent, tmp_path / "cache")
    result = jit.run(note=DEMO_NOTE)
    assert result.status == "unsupported"
    assert "snapshot ref" in (result.detail or "")
    assert jit.candidate() is None and not jit.admitted
    assert list(jit.task_dir.glob("refused-*"))
