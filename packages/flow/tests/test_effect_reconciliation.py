"""Typed ReconciliationTask emission (kit; interface-level, no engine).

Every ESCALATED `reconcile_or_escalate` outcome must carry a self-contained
`ReconciliationTask` (halt + evidence): kind, one-way contract hash (never the
resolved values), verdict evidence, and a suggested action. RECONCILED / NOOP
outcomes carry none. The duplicate-write guard's `count_new_only` is included
in the hash only when set, so pre-kit contract hashes are unchanged.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime.effects import (
    CompensationOutcome,
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
    build_reconciliation_task,
    reconcile_or_escalate,
)


def _effect(**kwargs) -> Effect:
    defaults = dict(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": "p1"},
        expected_count=1,
    )
    defaults.update(kwargs)
    return Effect(**defaults)


def _verdict(verdict: Verdict, *, observed: int | None = None) -> EffectVerdict:
    return EffectVerdict(
        verdict=verdict,
        kind=EffectKind.RECORD_WRITTEN,
        substrate="sql",
        reason="test verdict",
        observed_count=observed,
        expected_count=1,
    )


class _StubVerifier:
    substrate = "sql"

    def capture_pre_state(self, context=None):
        return EffectState(substrate="sql", reachable=True)

    def verify(self, expected, before, context=None):
        return _verdict(Verdict.REFUTED, observed=0)


_BEFORE = EffectState(substrate="sql", reachable=True)


class TestReconciliationTask:
    def test_indeterminate_escalation_emits_task(self):
        effect = _effect()
        result = reconcile_or_escalate(
            effect,
            _verdict(Verdict.INDETERMINATE),
            verifier=_StubVerifier(),
            before=_BEFORE,
        )
        assert result.outcome is CompensationOutcome.ESCALATED
        task = result.task
        assert task is not None
        assert task.kind == "effect_indeterminate"
        assert task.contract_hash == effect.contract_hash()
        assert task.verdict == "indeterminate"
        assert task.suggested_action
        assert task.task_id.startswith("recon-")

    def test_refuted_missing_write_task(self):
        result = reconcile_or_escalate(
            _effect(risk="irreversible"),
            _verdict(Verdict.REFUTED, observed=0),
            verifier=_StubVerifier(),
            before=_BEFORE,
            compensator=None,
        )
        task = result.task
        assert task is not None
        assert task.kind == "effect_refuted"
        assert task.evidence["observed_count"] == 0
        assert task.evidence["expected_count"] == 1
        assert "repair" in task.suggested_action

    def test_duplicate_task_suggests_duplicate_removal(self):
        result = reconcile_or_escalate(
            _effect(risk="irreversible"),
            _verdict(Verdict.REFUTED, observed=2),
            verifier=_StubVerifier(),
            before=_BEFORE,
            compensator=None,  # no compensator -> escalate with advice
        )
        task = result.task
        assert task is not None
        assert "duplicate" in task.suggested_action

    def test_confirmed_noop_has_no_task(self):
        result = reconcile_or_escalate(
            _effect(),
            _verdict(Verdict.CONFIRMED, observed=1),
            verifier=_StubVerifier(),
            before=_BEFORE,
        )
        assert result.outcome is CompensationOutcome.NOOP
        assert result.task is None

    def test_task_carries_no_resolved_values(self):
        """The FULL task dump — evidence included — carries digests and
        counts only: never the selector values, never matched-record field
        contents, never field read-back values (all can be PHI)."""
        effect = _effect(match={"patient_id": "SENSITIVE-123"})
        verdict = EffectVerdict(
            verdict=Verdict.REFUTED,
            kind=EffectKind.RECORD_WRITTEN,
            substrate="sql",
            reason="duplicate write",
            observed_count=2,
            expected_count=1,
            observed_value="SENSITIVE-NOTE-OBSERVED",
            expected_value="SENSITIVE-NOTE-EXPECTED",
            matched_records=[
                {"id": 1, "patient_id": "SENSITIVE-123", "ssn": "999-99-9999"},
                {"id": 2, "patient_id": "SENSITIVE-123", "ssn": "999-99-9999"},
            ],
        )
        result = reconcile_or_escalate(
            effect,
            verdict,
            verifier=_StubVerifier(),
            before=_BEFORE,
        )
        task = result.task
        assert task is not None
        dumped = task.model_dump_json()  # FULL dump, evidence included
        for sensitive in (
            "SENSITIVE-123",
            "999-99-9999",
            "SENSITIVE-NOTE-OBSERVED",
            "SENSITIVE-NOTE-EXPECTED",
        ):
            assert sensitive not in dumped
        # ...while still giving the operator joinable evidence.
        assert task.evidence["observed_count"] == 2
        assert task.evidence["values_match"] is False
        digests = task.evidence["matched_record_digests"]
        assert len(digests) == 2
        assert all(d.startswith("sha256:") for d in digests)

    def test_build_helper_directly(self):
        effect = _effect()
        task = build_reconciliation_task(effect, _verdict(Verdict.REFUTED, observed=0))
        assert task.kind == "effect_refuted"
        assert task.effect_kind == "record_written"
        assert task.substrate == "sql"


class TestCountNewOnlyScope:
    def test_field_equals_refuses_count_new_only(self):
        """Fail loud, never silently ignore, on the invalid combination."""
        with pytest.raises(ValueError, match="record_written"):
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match={"patient_id": "p1"},
                field="note",
                value="x",
                count_new_only=True,
            )

    def test_record_written_accepts_count_new_only(self):
        assert _effect(count_new_only=True).count_new_only is True


class TestResumeCliParams:
    def test_resume_parser_accepts_param_bindings(self):
        """A param-bound effect-verifier config must be re-bindable on
        resume (adversarial-review finding: the flags were replay/run-only,
        making a durable param-bound run un-resumable)."""
        from openadapt_flow.__main__ import build_parser

        args = build_parser().parse_args(
            ["resume", "runs/x", "--param", "k=v", "--params-file", "p.json"]
        )
        assert args.param == ["k=v"]
        assert args.params_file == "p.json"


class TestContractHashStability:
    def test_count_new_only_false_hash_unchanged(self):
        """Pre-kit contracts keep their exact digests (governed-run / resume
        ledgers bind them)."""
        base = _effect()
        with_flag_default = _effect(count_new_only=False)
        assert base.contract_hash() == with_flag_default.contract_hash()

    def test_count_new_only_true_changes_hash(self):
        assert _effect().contract_hash() != _effect(count_new_only=True).contract_hash()
