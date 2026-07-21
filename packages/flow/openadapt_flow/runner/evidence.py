"""Build the PHI-free ``openadapt.run-evidence/v1`` stream from local results.

Mirrors the FAIL-CLOSED server validator in ``openadapt-cloud``
``src/lib/runEvidence.ts``: whitelisted fields only, counts and digests and
step ids — never free text, values, frames, or report bodies. The
full-fidelity evidence (resolved values, matched records, screenshots) stays
in the LOCAL run directory; the deep link / operator console is how a human
sees it.

Deliberate omissions (schema-minimal discipline, same as
``hosted._build_break_payload``):

* ``HaltObservation.reason`` / ``observed_texts`` / ``intent`` free text is
  NEVER forwarded — the halt event carries only structural identifiers, a
  one-way drift signature, and ``*_count`` rollups.
* Step events are emitted only for steps that actually resolved (the rung is
  a required field server-side; inventing one would be dishonest).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Optional

from openadapt_flow.runner.verify import Refusal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openadapt_flow.ir import RunReport, StepResult

SCHEMA = "openadapt.run-evidence/v1"

#: Server batch ceiling (runEvidence.ts MAX_EVIDENCE_BATCH).
MAX_EVIDENCE_BATCH = 50

_RUNGS = frozenset({"structural", "template", "ocr", "geometry"})
_MAX_ID = 64


def _event(
    run_id: str, authorization_id: str, seq: int, kind: str, body: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "authorization_id": authorization_id[:_MAX_ID],
        "seq": seq,
        "kind": kind,
        kind: body,
    }


def state_event(
    run_id: str, authorization_id: str, seq: int, state: str
) -> dict[str, Any]:
    """One run-state event (``started`` / ``resumed`` / ``finished``)."""
    if state not in {"started", "resumed", "finished"}:
        raise ValueError(f"invalid run state {state!r}")
    return _event(run_id, authorization_id, seq, "state", {"state": state})


def drift_signature(workflow_id: str, rung: Optional[str], steps: int) -> str:
    """The same one-way structural fingerprint ``report-break`` emits."""
    digest = hashlib.sha256(f"{workflow_id}|{rung}|{steps}".encode()).hexdigest()
    return digest[:16]


def _last_failed_result(report: "RunReport") -> Optional["StepResult"]:
    for result in reversed(report.results):
        if not result.ok:
            return result
    return None


def _halt_kind(report: "RunReport") -> str:
    """Classify the halt from structural verdict fields only."""
    failed = _last_failed_result(report)
    if failed is not None:
        if failed.identity is not None and failed.identity.status == "mismatch":
            return "identity_halt"
        if failed.effect_verified is False:
            return "effect_refuted"
    return "resolver_halt"


def _step_events(
    report: "RunReport", run_id: str, authorization_id: str, start_seq: int
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seq = start_seq
    for result in report.results:
        if result.resolution is None or result.resolution.rung not in _RUNGS:
            continue
        body: dict[str, Any] = {
            "step_id": str(result.step_id)[:_MAX_ID],
            "rung": result.resolution.rung,
            "elapsed_ms": max(0, int(result.elapsed_ms)),
        }
        hashes = [h for h in result.effect_contract_hashes if h.startswith("sha256:")]
        if hashes:
            body["effect_contract_hashes"] = hashes
        # Mutually exclusive server-side; StepResult already guarantees that.
        if result.effect_verified is True:
            body["effect_verified"] = True
        elif result.effect_approved_unverified:
            body["effect_approved_unverified"] = True
        if result.identity is not None:
            if result.identity.status == "verified":
                body["identity_verified"] = True
            elif result.identity.status == "mismatch":
                body["identity_verified"] = False
        events.append(_event(run_id, authorization_id, seq, "step", body))
        seq += 1
    return events


def _last_failed_rung(report: "RunReport") -> Optional[str]:
    failed = _last_failed_result(report)
    if failed is not None and failed.resolution is not None:
        rung = failed.resolution.rung
        return rung if rung in _RUNGS else None
    return None


def _halt_event(
    report: "RunReport",
    *,
    run_id: str,
    workflow_id: str,
    authorization_id: str,
    seq: int,
) -> dict[str, Any]:
    assert report.halt is not None
    rung = _last_failed_rung(report)
    step_id = (report.halt.state_id or "").strip()[:_MAX_ID]
    body: dict[str, Any] = {
        "task_id": f"halt-{run_id}"[:_MAX_ID],
        "kind": _halt_kind(report),
        # Structural only: never the HaltObservation free-text reason.
        "reason": (
            f"halt at step {step_id}" if step_id else "halt at unidentified step"
        ),
        "drift_signature": drift_signature(workflow_id, rung, len(report.results)),
        "evidence_digest": {
            "observed_texts_count": len(report.halt.observed_texts),
            "completed_steps_count": len(report.halt.completed_intents),
        },
    }
    if step_id:
        body["step_id"] = step_id
    if rung is not None:
        body["rung"] = rung
    return _event(run_id, authorization_id, seq, "halt", body)


def summary_status(report: "RunReport") -> str:
    if report.success:
        return "confirmed"
    if report.halt is not None:
        return "halted-needs-attention"
    return "failed"


def _summary_body(
    *,
    bundle_digest: str,
    authorization_id: str,
    status: str,
    steps_total: int = 0,
    consequential_steps: int = 0,
    effect_covered_consequential_steps: int = 0,
    effects_confirmed: int = 0,
    effects_approved_unverified: int = 0,
    identity_steps_required: int = 0,
    identity_steps_verified: int = 0,
    duration_ms: int = 0,
) -> dict[str, Any]:
    return {
        "bundle_digest": bundle_digest,
        "authorization_id": authorization_id[:_MAX_ID],
        "status": status,
        "steps_total": steps_total,
        "consequential_steps": consequential_steps,
        "effect_covered_consequential_steps": effect_covered_consequential_steps,
        "effects_confirmed": effects_confirmed,
        "effects_approved_unverified": effects_approved_unverified,
        "identity_steps_required": identity_steps_required,
        "identity_steps_verified": identity_steps_verified,
        "duration_ms": duration_ms,
        # Assertion, not a toggle: the server refuses anything but literal
        # false. Callers must not emit a summary for an egress-enabled run.
        "screenshots_may_leave_box": False,
    }


def report_events(
    report: "RunReport",
    *,
    run_id: str,
    workflow_id: str,
    bundle_digest: str,
    authorization_id: str,
    consequential_steps: int,
    effect_covered_consequential_steps: int,
    start_seq: int = 1,
) -> list[dict[str, Any]]:
    """The ordered evidence stream for a completed local run.

    Raises:
        ValueError: when the report records that a screenshot could have left
            the box — the evidence contract's ``screenshots_may_leave_box``
            assertion would be false, so no summary is fabricated. (The
            pre-execution profile check makes this unreachable in the daemon;
            this is defense in depth for direct callers.)
    """
    if report.screenshots_may_leave_box:
        raise ValueError(
            "run had an egress-capable component wired; refusing to assert "
            "screenshots_may_leave_box=false in the evidence stream"
        )
    events = _step_events(report, run_id, authorization_id, start_seq)
    seq = start_seq + len(events)
    if report.halt is not None:
        events.append(
            _halt_event(
                report,
                run_id=run_id,
                workflow_id=workflow_id,
                authorization_id=authorization_id,
                seq=seq,
            )
        )
        seq += 1
    identity_verified = sum(
        1
        for result in report.results
        if result.identity is not None and result.identity.status == "verified"
    )
    body = _summary_body(
        bundle_digest=bundle_digest,
        authorization_id=authorization_id,
        status=summary_status(report),
        steps_total=len(report.results),
        consequential_steps=consequential_steps,
        effect_covered_consequential_steps=effect_covered_consequential_steps,
        effects_confirmed=sum(
            1 for result in report.results if result.effect_verified is True
        ),
        effects_approved_unverified=len(report.approved_unverified_effect_step_ids),
        identity_steps_required=len(report.required_identity_step_ids),
        identity_steps_verified=identity_verified,
        duration_ms=max(0, int(report.total_ms)),
    )
    events.append(_event(run_id, authorization_id, seq, "run_summary", body))
    return events


def refusal_events(
    refusal: Refusal,
    *,
    run_id: str,
    workflow_id: str,
    bundle_digest: str,
    authorization_id: str,
    start_seq: int = 0,
) -> list[dict[str, Any]]:
    """Report a refused dispatch: an ``authorization_refused`` halt plus a
    terminal ``failed`` summary that acks (closes) the lease honestly."""
    halt_body = {
        "task_id": f"refusal-{run_id}"[:_MAX_ID],
        "kind": "authorization_refused",
        "reason": refusal.reason(),
        "drift_signature": drift_signature(workflow_id, refusal.code.value, 0),
    }
    return [
        _event(run_id, authorization_id, start_seq, "halt", halt_body),
        _event(
            run_id,
            authorization_id,
            start_seq + 1,
            "run_summary",
            _summary_body(
                bundle_digest=bundle_digest,
                authorization_id=authorization_id,
                status="failed",
            ),
        ),
    ]


def failure_events(
    *,
    run_id: str,
    bundle_digest: str,
    authorization_id: str,
    duration_ms: int = 0,
    start_seq: int = 1,
) -> list[dict[str, Any]]:
    """Terminal evidence when the engine produced no readable report
    (crash / missing report.json): an honest bare ``failed`` summary."""
    return [
        _event(
            run_id,
            authorization_id,
            start_seq,
            "run_summary",
            _summary_body(
                bundle_digest=bundle_digest,
                authorization_id=authorization_id,
                status="failed",
                duration_ms=max(0, duration_ms),
            ),
        )
    ]
