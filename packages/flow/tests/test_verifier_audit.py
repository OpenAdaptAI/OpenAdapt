"""Verifier audit kit: families, classification and intervals on a local target."""

from __future__ import annotations

import json

import pytest

from openadapt_flow.verifier_audit import (
    DEFAULT_FAMILIES,
    AuditConfig,
    Call,
    CallKind,
    Episode,
    Verdict,
    families_spec,
    run_audit,
    wilson_interval,
)
from openadapt_flow.verifier_audit.families import (
    FamilyContext,
    Refusal,
    duplicate,
    late_write,
    wrong_record,
)
from openadapt_flow.verifier_audit.targets.toy import ToyPayments


def _by_family(report):
    return {row["family"]: row for row in report["summary"]["families"]}


def test_field_only_scorer_accepts_harmful_end_states():
    report = run_audit(ToyPayments(strict=False))
    rows = _by_family(report)
    assert rows["duplicate"]["hits"] == rows["duplicate"]["n"] == 3
    assert rows["unauthorized_side_effect"]["hits"] == 3
    assert rows["late_write"]["hits"] == 3
    assert rows["optimistic_success"]["hits"] == 0
    assert rows["incidental_change"]["hits"] == 0
    examples = [c for c in report["cells"] if c["status"] == "FALSE_ACCEPT"]
    assert all(
        c["effect_diff"]["missing"] or c["effect_diff"]["extra"] for c in examples
    )


def test_strict_scorer_catches_every_harmful_family_and_keeps_the_control():
    rows = _by_family(run_audit(ToyPayments(strict=True)))
    for fam in DEFAULT_FAMILIES:
        if fam.polarity.value == "harmful":
            assert rows[fam.id]["hits"] == 0, fam.id
    assert rows["incidental_change"]["statuses"] == {"ACCEPTED": 3, "FALSE_REJECT": 0}


def test_single_write_task_refuses_partial_write():
    report = run_audit(ToyPayments(), AuditConfig(tasks=["pay-bob"]))
    cell = next(c for c in report["cells"] if c["family"] == "partial_write")
    assert cell["status"] == "REFUSED" and cell["reason"] == "FEWER_THAN_TWO_WRITES"


def test_retarget_skips_records_the_task_already_touches():
    ref = ToyPayments().reference("pay-carol-and-dave")
    pool = {"recipient": ["carol", "dave", "bob"]}
    candidates = wrong_record(FamilyContext("t", ref, pool))
    assert not isinstance(candidates, Refusal)
    targets = {
        c.calls[i].args["recipient"]
        for c in candidates
        for i in range(len(c.calls))
        if c.calls[i].kind is CallKind.WRITE
    }
    assert "bob" in targets
    for cand in candidates:
        assert sorted(
            x.args["recipient"] for x in cand.calls if x.kind is CallKind.WRITE
        ) != ["carol", "dave"]


def test_late_write_moves_the_write_past_the_claim():
    ref = [
        Call("r", {}, CallKind.READ),
        Call("w", {"a": 1}, CallKind.WRITE),
        Call("done", {}, CallKind.CLAIM),
    ]
    (cand,) = late_write(FamilyContext("t", ref))
    assert [c.name for c in cand.calls] == ["r", "done", "w"]
    assert cand.must_succeed == (2,) and cand.ordering_only


def test_duplicate_without_a_write_refuses():
    assert duplicate(FamilyContext("t", [Call("r", {}, CallKind.READ)])) == Refusal(
        "NO_WRITE"
    )


def test_episode_that_cannot_continue_past_the_claim_refuses_late_write():
    target = ToyPayments()
    target.post_claim_steps = False  # type: ignore[attr-defined]
    report = run_audit(target, AuditConfig(tasks=["pay-bob"]))
    cell = next(c for c in report["cells"] if c["family"] == "late_write")
    assert cell["reason"] == "EPISODE_ENDS_AT_CLAIM"


def test_ledger_resumes_without_rescoring(tmp_path):
    first = run_audit(ToyPayments(), AuditConfig(out_dir=tmp_path))
    calls = []

    class Counting(ToyPayments):
        def run(self, task, calls_, *, score):
            calls.append(task)
            return super().run(task, calls_, score=score)

    second = run_audit(Counting(), AuditConfig(out_dir=tmp_path))
    assert calls == []
    assert second["summary"] == first["summary"]
    assert (
        (tmp_path / "REPORT.md")
        .read_text()
        .startswith("# Verifier audit: toy-payments")
    )


def test_single_replay_halves_scored_replays_and_keeps_verdicts():
    scored = []

    class Counting(ToyPayments):
        def run(self, task, calls_, *, score):
            scored.append(score)
            return super().run(task, calls_, score=score)

    once = run_audit(Counting(), AuditConfig(single_replay=True))
    assert once["summary"] == run_audit(ToyPayments())["summary"]
    assert scored.count(False) == 3  # only the reference reproducibility replays


def test_redaction_digests_arguments_effects_and_edit_text(tmp_path):
    report = run_audit(ToyPayments(), AuditConfig(redact=True, out_dir=tmp_path))
    text = json.dumps(report) + (tmp_path / "REPORT.md").read_text()
    text += (tmp_path / "cells.jsonl").read_text()
    for task in ToyPayments().tasks():  # task ids are kept on purpose
        text = text.replace(task, "<task>")
    for value in ("lunch", "alice", "bob", "carol", "dave"):
        assert value not in text, value
    assert "sha256:" in text


def test_ledger_refuses_another_target_or_redaction_mode(tmp_path):
    run_audit(ToyPayments(), AuditConfig(out_dir=tmp_path, tasks=["pay-bob"]))
    with pytest.raises(ValueError):
        run_audit(ToyPayments(), AuditConfig(out_dir=tmp_path, redact=True))

    class Other(ToyPayments):
        name = "other"

    with pytest.raises(ValueError):
        run_audit(Other(), AuditConfig(out_dir=tmp_path))


def test_crashed_replay_is_skipped_not_taken_as_an_effect(tmp_path):
    class Crashing(ToyPayments):
        crashes = 0

        def run(self, task, calls_, *, score):
            if list(calls_) != self.reference(task) and self.crashes < 1:
                self.crashes += 1
                return Episode(
                    final_effect=(), verdict=Verdict.ERROR, scorer_detail="boom"
                )
            return super().run(task, calls_, score=score)

    target = Crashing()
    report = run_audit(target, AuditConfig(tasks=["pay-bob"], out_dir=tmp_path))
    first = next(c for c in report["cells"] if c.get("census_attempts"))
    assert first["census_attempts"][0] == {
        "edit": first["census_attempts"][0]["edit"],
        "harness_error": True,
    }
    assert first["status"] != "EFFECT_NOT_REPRODUCIBLE"


def test_resume_retries_harness_errors(tmp_path):
    run_audit(ToyPayments(), AuditConfig(out_dir=tmp_path, tasks=["pay-bob"]))
    ledger = tmp_path / "cells.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    rows[0]["status"] = "HARNESS_ERROR"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    again = run_audit(ToyPayments(), AuditConfig(out_dir=tmp_path, tasks=["pay-bob"]))
    assert all(c["status"] != "HARNESS_ERROR" for c in again["cells"])


@pytest.mark.parametrize(
    ("hits", "n", "lo", "hi"),
    [(0, 10, 0.0, 0.2775), (5, 10, 0.2366, 0.7634), (10, 10, 0.7225, 1.0)],
)
def test_wilson_interval(hits, n, lo, hi):
    got = wilson_interval(hits, n)
    assert got[0] == pytest.approx(lo, abs=1e-4) and got[1] == pytest.approx(
        hi, abs=1e-4
    )


def test_family_spec_is_shareable_json():
    spec = families_spec()
    assert spec["format"] == "openadapt.verifier-audit.families/v1"
    assert {f["id"] for f in spec["families"]} >= {
        "duplicate",
        "late_write",
        "incidental_change",
    }
    ids = {f["id"] for f in spec["families"]}
    assert set(spec["server_fault_modes"].values()) <= ids
    json.dumps(spec)
