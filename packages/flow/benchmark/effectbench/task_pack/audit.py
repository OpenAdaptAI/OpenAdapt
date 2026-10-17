"""Adversarial non-gameability audit for the MockMed oracle (LIVE red-team).

The design doc (section 3.3) makes the adversarial oracle audit the
non-compressible human-in-loop gate: before a task's ``adversarially_audited``
flag may be flipped True, a red-team pass must try — and fail — to satisfy its
oracle WITHOUT the true business effect. This module IS that pass for the
MockMed anchor's shared oracle (the ``RestRecordVerifier`` over ``GET /api/db``
plus the trial-unique ``record_written`` / ``field_equals`` contracts every
MockMed task uses). It runs live against the real fault server and asserts the
oracle never CONFIRMS an attack.

Each attack establishes a system-of-record state that a green screen would
report as success but that is NOT this trial's exact effect, then checks the
oracle REFUSES (REFUTED / INDETERMINATE, never CONFIRMED):

- ``phantom``        — banner-only: nothing written.
- ``decoy_patient``  — the write went to a same-name decoy record.
- ``wrong_note``     — the row exists but carries a different note (payload).
- ``wrong_type``     — right patient, wrong encounter type (wrong target).
- ``duplicate``      — two rows behind one banner (not at-most-once).
- ``cross_trial``    — a PRIOR trial's row must not satisfy THIS trial's oracle
                       (proves the trial-unique payload actually isolates trials).

A positive control confirms the oracle DOES confirm the exact correct effect
(so it is not a trivial always-refuse). Only if every attack is refused AND the
control confirms does :func:`audit_mockmed_oracle` report ``passed=True`` — the
evidence that flips the MockMed tasks' ``adversarially_audited`` truthfully.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from benchmark.effectbench.task_pack._authoring import PARAM_NOTE
from benchmark.effectbench.task_pack.driver import serve_mockmed
from benchmark.effectbench.task_pack.mockmed_tasks import (
    TARGET_PATIENT,
    TARGET_TYPE,
    _note_effect,
    _record_effect,
)
from openadapt_flow.benchmark.effectbench import RestRecordVerifier, Verdict

_HTTP_TIMEOUT_S = 5.0
TRIAL_NOTE = "triage note trial-unique-AUDIT"
OTHER_NOTE = "attacker-substituted note"


@dataclass(frozen=True)
class AttackResult:
    name: str
    gamed: bool  # True == the oracle was fooled (CONFIRMED without the effect)
    verdict: str
    detail: str


@dataclass(frozen=True)
class AuditReport:
    passed: bool
    control_confirmed: bool
    attacks: tuple[AttackResult, ...]

    def summary(self) -> str:
        lines = [
            f"MockMed oracle adversarial audit: "
            f"{'PASS' if self.passed else 'FAIL'} "
            f"(control_confirmed={self.control_confirmed})"
        ]
        for a in self.attacks:
            lines.append(
                f"  attack {a.name:14s} -> {a.verdict:14s} "
                f"{'GAMED!' if a.gamed else 'refused'} — {a.detail}"
            )
        return "\n".join(lines)


def _reset(base: str, *, seed_concurrent: bool = False) -> None:
    requests.post(
        base + "/api/reset",
        json={"seed_concurrent": seed_concurrent},
        timeout=_HTTP_TIMEOUT_S,
    )


def _write(base: str, *, patient: str, type_: str, note: str) -> None:
    requests.post(
        base + "/api/encounter",
        json={"patient_id": patient, "type": type_, "note": note},
        timeout=_HTTP_TIMEOUT_S,
    )


def audit_mockmed_oracle() -> AuditReport:
    """Run the live red-team pass; return the pass/fail evidence.

    Each attack faithfully emulates one trial: reset, snapshot the pre-state,
    let the ATTACKER act (the writes that do NOT constitute this trial's exact
    effect), then verify. An attack that leaves a prior row in the pre-state
    (``cross_trial``) plants it BEFORE the snapshot, so it is correctly treated
    as pre-existing rather than attributable to this action.
    """
    params = {PARAM_NOTE: TRIAL_NOTE}
    record_eff = _record_effect().resolve(params)
    note_eff = _note_effect().resolve(params)
    attacks: list[AttackResult] = []

    with serve_mockmed() as (base, _db):
        oracle = RestRecordVerifier(base, records_path="/api/db", records_key="records")

        def attack(name, effect, action, *, preseed=None, seed_concurrent=False):
            _reset(base, seed_concurrent=seed_concurrent)
            if preseed is not None:
                preseed()  # pre-existing rows land BEFORE the snapshot
            before = oracle.capture_pre_state()
            action()  # the attacker's writes ARE this trial's action
            v = oracle.verify(effect, before)
            attacks.append(
                AttackResult(
                    name, v.verdict is Verdict.CONFIRMED, v.verdict.value, v.reason[:70]
                )
            )

        # banner only, nothing written -> phantom.
        attack("phantom", record_eff, lambda: None)
        # write went to the same-name decoy p2 -> intended chart absent.
        attack(
            "decoy_patient",
            record_eff,
            lambda: _write(base, patient="p2", type_=TARGET_TYPE, note=TRIAL_NOTE),
        )
        # row exists but the note was substituted -> partial/wrong read-back.
        attack(
            "wrong_note",
            note_eff,
            lambda: _write(
                base, patient=TARGET_PATIENT, type_=TARGET_TYPE, note=OTHER_NOTE
            ),
        )
        # right patient, wrong encounter type -> intended type absent.
        attack(
            "wrong_type",
            record_eff,
            lambda: _write(
                base, patient=TARGET_PATIENT, type_="Consult", note=TRIAL_NOTE
            ),
        )

        # two rows behind one banner -> not at-most-once.
        def _double() -> None:
            _write(base, patient=TARGET_PATIENT, type_=TARGET_TYPE, note=TRIAL_NOTE)
            _write(base, patient=TARGET_PATIENT, type_=TARGET_TYPE, note=TRIAL_NOTE)

        attack("duplicate", record_eff, _double)
        # a PRIOR trial's row (present before this trial's snapshot) must not
        # satisfy THIS trial's note oracle.
        attack(
            "cross_trial",
            note_eff,
            lambda: None,
            preseed=lambda: _write(
                base, patient=TARGET_PATIENT, type_=TARGET_TYPE, note="prior trial note"
            ),
        )

        # positive control: the exact correct effect DOES confirm.
        _reset(base)
        before = oracle.capture_pre_state()  # empty baseline
        _write(base, patient=TARGET_PATIENT, type_=TARGET_TYPE, note=TRIAL_NOTE)
        control = oracle.verify(record_eff, before)
        control_note = oracle.verify(note_eff, before)
        control_confirmed = (
            control.verdict is Verdict.CONFIRMED
            and control_note.verdict is Verdict.CONFIRMED
        )

    passed = control_confirmed and not any(a.gamed for a in attacks)
    return AuditReport(
        passed=passed,
        control_confirmed=control_confirmed,
        attacks=tuple(attacks),
    )


def main() -> None:
    report = audit_mockmed_oracle()
    print(report.summary())
    if not report.passed:
        raise SystemExit("adversarial audit FAILED — do not flip adversarially_audited")


if __name__ == "__main__":
    main()
