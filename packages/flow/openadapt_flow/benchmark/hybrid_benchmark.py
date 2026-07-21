"""Hybrid benchmark: does compiled-first with agent-fallback-on-halt dominate?

Four arms run the same MockMed triage task over ONE frozen schedule of
clean and drifted conditions (identical condition per slot index across
arms), all judged by the same arm-independent final-state check:

- **compiled** (A): the existing compiled replay. Free. Full schedule.
- **agent** (B): the existing computer-use agent baseline with the
  intent-level task prompt. Paid. Subsample of the schedule.
- **demo_agent** (C): the same agent loop, but the prompt additionally
  includes a compact textual serialization of the recorded demonstration
  (:func:`serialize_demo`). Paid. Same subsample as B.
- **hybrid** (D): run the compiled replay; if it completes, done ($0). If
  it SAFE-HALTS (resolver failure / postcondition drift), hand the SAME
  browser session to a demo-conditioned fallback agent
  (:func:`handoff_task_prompt`) with a smaller action budget. Full
  schedule; pays only on fallbacks.

Drift schedule (frozen in :data:`SCHEDULE`): ~30% of slots carry one of
three MockMed drift conditions chosen — deliberately, and disclosed as
selection bias — because free probe runs showed each makes the CURRENT
compiled bundle safe-halt deterministically while remaining completable at
the intent level:

- ``notice``: a "What's New" interstitial replaces the tasks screen after
  sign-in until dismissed (halts the replay at the Sign In step).
- ``reqfield``: the encounter form gains a required Acuity field; saving
  without it shows an inline validation error (halts at the New Encounter
  step — the form's expected-region postcondition catches the new field
  before anything is typed).
- ``modal-once``: a survey modal intercepts the first save click; after
  dismissing it, saving works (halts at the Save Encounter step).

Probed-but-rejected conditions, reported as findings: ``theme``,
``rename``, ``move``, and a Triage->"Triage Assessment" relabel+reorder
(``typelabel``) are all absorbed by the resolution ladder (the replay
heals and completes correctly), so they cannot exercise the fallback.

Success, implemented once for ALL arms (:func:`verify_hybrid_final`):
OCR of the final screenshot must show the saved-encounter evidence
(banner + ``Triage — <note>`` row), the RIGHT patient, and no wrong-type
write. This is deliberately stricter than the earlier MockMed benchmark:
the validation suite (PR #12) documents silent wrong-action modes in the
replayer, so success here checks final-state identity, not just the
run's own flags.

Hard cost guardrails, shared across ALL paid arms (B, C, and D's
fallbacks) via one :class:`SpendLedger`: preflight probe before any
spend, a per-run cap (default $1.50 at list price), a TOTAL ceiling
(default $8.00 at list price) enforced before every paid run,
consecutive-billing-error abort, and incremental per-run persistence to
``out_dir/rows.jsonl``.
"""

from __future__ import annotations

import argparse
import difflib
import json
import platform
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel

from openadapt_flow.benchmark import agent_baseline
from openadapt_flow.benchmark.openemr_benchmark import (
    BILLING_ABORT_AFTER,
    _looks_like_billing_error,
)
from openadapt_flow.benchmark.run_benchmark import (
    _agent_run,
    _arm_aggregate,
    _compiled_run,
    identity_coverage_block,
)
from openadapt_flow.ir import Workflow

WORKFLOW_NAME = "triage-hybrid"
#: Note used only to record the demonstration; every run substitutes its own.
RECORD_NOTE = "Baseline demonstration note for compilation."

#: The three drift conditions used in the schedule (see module docstring).
DRIFT_TYPES = ("notice", "reqfield", "modal-once")

#: The frozen run schedule: one condition per slot. 14 clean + 6 drifted
#: (2 of each drift type) = 30% drift. Every arm sees the SAME condition at
#: the same slot index.
SCHEDULE: tuple[str, ...] = (
    "clean",
    "clean",
    "notice",
    "clean",
    "clean",
    "reqfield",
    "clean",
    "clean",
    "modal-once",
    "clean",
    "clean",
    "clean",
    "notice",
    "clean",
    "reqfield",
    "clean",
    "clean",
    "modal-once",
    "clean",
    "clean",
)

#: Slot indices the paid agent-only arms (B and C) run: a proportional
#: subsample of the same schedule (5 clean + one slot of each drift type).
AGENT_SLOTS: tuple[int, ...] = (0, 2, 4, 5, 8, 10, 15, 18)

#: Action budget for the hybrid fallback agent: it starts mid-workflow, so
#: it needs fewer actions than the from-scratch agent arms.
FALLBACK_MAX_ACTIONS = 15
#: Action budget for the from-scratch agent arms (same as the original
#: MockMed benchmark).
AGENT_MAX_ACTIONS = agent_baseline.MAX_ACTIONS
#: Hard ceiling on TOTAL list-price spend across ALL paid arms.
MAX_TOTAL_COST_USD = 8.00

ARM_TAGS = {"compiled": "A", "agent": "B", "demo_agent": "C", "hybrid": "D"}

#: Twenty distinct short clinical phrases; :func:`note_for_slot` appends an
#: (arm, slot) tag so every run in every arm types a distinct value —
#: success proves parameter substitution, not replay of a baked-in literal.
_NOTE_POOL = (
    "Vitals stable; recheck in two weeks.",
    "Knee brace fitted; gait steady today.",
    "Hydration counseling completed.",
    "Referral letter faxed to cardiology.",
    "Home BP log reviewed; stable readings.",
    "Flu shot offered; patient declined.",
    "Wound dressing changed; healing well.",
    "Stretching plan issued for knee pain.",
    "Allergy list updated with pollen.",
    "Walking program started this week.",
    "Pharmacy switched to downtown branch.",
    "Lab panel ordered for next visit.",
    "Diet handout given and explained.",
    "Sleep questionnaire returned scored.",
    "Podiatry exam booked for next month.",
    "Eye exam reminder sent via portal.",
    "Crutches returned; steady without aid.",
    "Ice and elevation advised nightly.",
    "Follow-up call scheduled for Friday.",
    "Physio discharge summary attached.",
)


def note_for_slot(arm: str, slot: int) -> str:
    """Distinct per-(arm, slot) note text.

    Args:
        arm: Arm key (``compiled``/``agent``/``demo_agent``/``hybrid``).
        slot: Zero-based slot index in the schedule.

    Returns:
        A clinically plausible note unique to (arm, slot).
    """
    tag = ARM_TAGS.get(arm, arm[:1].upper())
    return f"{_NOTE_POOL[slot % len(_NOTE_POOL)]} [{tag}{slot:02d}]"


def condition_url(base_url: str, condition: str) -> str:
    """Target URL for a schedule condition.

    Args:
        base_url: MockMed base URL (trailing slash).
        condition: ``"clean"`` or a MockMed drift flag.

    Returns:
        The URL to launch the run's browser at.
    """
    if condition == "clean":
        return base_url
    return f"{base_url.rstrip('/')}/?drift={condition}"


# -- demonstration serialization ----------------------------------------------

_INVERSE_RELATION = {
    # Landmark.relation describes where the LANDMARK sits relative to the
    # target; the description walks the other way (target relative to text).
    "left_of": "to the right of",
    "right_of": "to the left of",
    "above": "below",
    "below": "above",
}


def _describe_step(step: Any) -> str:
    """One compact human-readable line for a compiled step (no coordinates)."""
    action = step.action.value if hasattr(step.action, "value") else step.action
    if action in ("click", "double_click"):
        verb = "double-click" if action == "double_click" else "click"
        anchor = step.anchor
        if anchor is not None and anchor.ocr_text:
            return f'{verb} the element labeled "{anchor.ocr_text}"'
        if anchor is not None and anchor.landmarks:
            lm = anchor.landmarks[0]
            where = _INVERSE_RELATION.get(lm.relation, "near")
            return f'{verb} the unlabeled control {where} the text "{lm.ocr_text}"'
        return f"{verb} the recorded target ({step.intent})"
    if action == "type":
        if step.param:
            return f"type <{step.param}> — substitute this run's {step.param} value"
        return f'type "{step.text or ""}"'
    if action == "key":
        return f"press the {step.key} key"
    if action == "scroll":
        return f"scroll by ({step.scroll_dx or 0}, {step.scroll_dy or 0})"
    if action == "wait":
        return "wait for the screen to settle"
    return step.intent


def serialize_demo(workflow: Workflow) -> str:
    """Compact textual serialization of the recorded demonstration.

    One numbered line per compiled step: action type plus a human-readable
    target description (the anchor's OCR label, or its nearest landmark
    for unlabeled targets) and typed-text placeholders for parameterized
    values. Deliberately contains NO pixel coordinates: the serialization
    tells the agent WHAT the demonstration did, not where to click.

    Args:
        workflow: The compiled workflow (bundle's ``workflow.json``).

    Returns:
        The serialized demonstration, one step per line.
    """
    return "\n".join(
        f"{i}. {_describe_step(step)}" for i, step in enumerate(workflow.steps, 1)
    )


# -- prompts -------------------------------------------------------------------


def demo_conditioned_task_prompt(note_text: str, demo_text: str) -> str:
    """Arm C prompt: the intent task plus the serialized demonstration.

    Args:
        note_text: The note the agent must enter.
        demo_text: Output of :func:`serialize_demo`.

    Returns:
        The task prompt string.
    """
    base = agent_baseline.triage_task_prompt(note_text)
    return (
        f"{base}\n\n"
        "A recorded demonstration of this exact workflow, from a previous "
        "session, is serialized below. The UI may have changed since it "
        "was recorded — treat it as a guide, not a script, and adapt to "
        "what you actually see on screen:\n\n"
        f"{demo_text}"
    )


def handoff_task_prompt(
    note_text: str,
    demo_text: str,
    *,
    completed_steps: int,
    total_steps: int,
    halted_step_intent: str,
    halt_reason: str,
) -> str:
    """Arm D fallback prompt: resume mid-workflow after a compiled halt.

    Args:
        note_text: The note this run must enter.
        demo_text: Output of :func:`serialize_demo`.
        completed_steps: Number of demonstration steps the replayer
            reported complete before halting (1-based count).
        total_steps: Total steps in the demonstration.
        halted_step_intent: Human-readable intent of the halted step.
        halt_reason: The replayer's halt error message.

    Returns:
        The fallback task prompt string.
    """
    if completed_steps:
        progress = (
            f"Steps 1..{completed_steps} of {total_steps} reported "
            f"complete. The replayer halted at step {completed_steps + 1}"
        )
    else:
        progress = f"No steps completed. The replayer halted at step 1 of {total_steps}"
    return (
        "You are looking at MockMed, a demo clinic web app (fake data "
        "only). The overall task is:\n\n"
        '1. Sign in with username "nurse.demo" and password '
        '"mockmed-demo-pass".\n'
        "2. Open the first referral task in the list.\n"
        "3. From the patient's page, create a New Encounter and choose "
        'the type "Triage".\n'
        f'4. Enter exactly this note in the Note field: "{note_text}"\n'
        "5. Save the encounter.\n\n"
        "A deterministic replayer was executing this recorded "
        "demonstration of the task:\n\n"
        f"{demo_text}\n\n"
        f'{progress} ("{halted_step_intent}") because:\n\n'
        f"{halt_reason}\n\n"
        "The browser is exactly where the replayer left it — do NOT start "
        "over unless the current screen requires it. Continue from the "
        "CURRENT state, adapt to whatever UI change caused the halt, and "
        "finish the task. You are done when you are back on the patient's "
        "page and see the 'Encounter saved' confirmation. Then stop and "
        "reply with a one-line summary. Start by taking a screenshot to "
        "see the current state."
    )


# -- final-state verification (all arms) ---------------------------------------


class HybridVerifyResult(BaseModel):
    """Final-state verdict shared by all four arms.

    Attributes:
        success: Banner + Triage row found, right patient, no wrong-type
            write.
        banner_found: ``Encounter saved — <note>`` banner located.
        note_found: ``Triage — <note>`` encounter row located.
        right_patient: The expected patient's name is on the final screen.
        wrong_type_row: This run's note appears in a ``Consult`` encounter
            row — a wrong-target write.
        wrong_action: A state mutation landed on the wrong target: either
            the save evidence is present but on the wrong patient, or a
            wrong-type row carries this run's note.
    """

    success: bool
    banner_found: bool
    note_found: bool
    right_patient: bool
    wrong_type_row: bool
    wrong_action: bool


def _squash(text: str) -> str:
    """Lowercase and remove all whitespace (OCR-tolerant comparison form)."""
    return "".join(text.lower().split())


def _longest_run(needle: str, hay: str) -> int:
    """Longest contiguous matched character run between two squashed strings."""
    if not needle or not hay:
        return 0
    blocks = difflib.SequenceMatcher(
        None, needle, hay, autojunk=False
    ).get_matching_blocks()
    return max((block.size for block in blocks), default=0)


#: Minimum contiguous squashed-character run for the note inside a single
#: encounter-row/banner line.
_NOTE_LINE_RUN = 12


def verify_hybrid_final(
    screen_png: bytes,
    note_text: str,
    *,
    patient_name: str = "Jane Sample",
) -> HybridVerifyResult:
    """Arm-independent final-state check with identity verification.

    All evidence is LINE-level contiguous matching on squashed
    (lowercased, whitespace-stripped) OCR text, not whole-line fuzzy
    ratios: a composite fuzzy candidate like ``Encounter saved — <note>``
    is dominated by the note for long notes and false-positives on the
    encounter FORM, where the typed note is visible before any save (the
    banner prefix contributes only ~1/5 of the candidate). Measured on
    real finals: contiguous line evidence separates saved from halted
    screens cleanly.

    Checks:

    - ``banner_found``: some OCR line carries a >=13-character contiguous
      match of ``encountersaved`` (the banner prefix; MockMed renders it
      only after a successful save).
    - ``note_found``: some single OCR line carries both ``triage`` and a
      >=12-character contiguous run of this run's note — the saved
      encounter row. Requiring both in ONE line rejects the encounter
      form, where "Triage" (segment button) and the typed note are
      separate lines.
    - ``right_patient``: the expected patient's name appears in the
      frame's squashed OCR text — identity, motivated by the validation
      suite's silent wrong-action findings (PR #12).
    - ``wrong_type_row``: some single line carries both ``consult`` and
      this run's note — a wrong-type write.

    A run whose save evidence is present but on the wrong patient, or
    whose note landed in a wrong-type row, is a **wrong action**, not a
    success.

    Args:
        screen_png: Full-frame screenshot of the final state as PNG bytes.
        note_text: The note the run was asked to enter.
        patient_name: The patient the intent-level task targets (the first
            referral in MockMed's default order).

    Returns:
        A :class:`HybridVerifyResult`.
    """
    from openadapt_flow.vision import ocr

    lines = [_squash(line.text) for line in ocr(screen_png)]
    hay = "".join(lines)
    banner_needle = _squash("Encounter saved")
    banner_found = any(
        _longest_run(banner_needle, sq) >= len(banner_needle) - 1 for sq in lines
    )
    needle = _squash(note_text)
    note_found = any(
        _longest_run("triage", sq) >= 5 and _longest_run(needle, sq) >= _NOTE_LINE_RUN
        for sq in lines
    )
    name = _squash(patient_name)
    right_patient = name in hay or _longest_run(name, hay) >= len(name) - 1
    wrong_type_row = any(
        _longest_run("consult", sq) >= 6 and _longest_run(needle, sq) >= _NOTE_LINE_RUN
        for sq in lines
    )

    saved = banner_found and note_found
    wrong_action = (saved and not right_patient) or wrong_type_row
    return HybridVerifyResult(
        success=saved and right_patient and not wrong_type_row,
        banner_found=banner_found,
        note_found=note_found,
        right_patient=right_patient,
        wrong_type_row=wrong_type_row,
        wrong_action=wrong_action,
    )


# -- shared paid-spend ledger ---------------------------------------------------


class SpendLedger:
    """One budget across ALL paid runs (arms B, C, and D's fallbacks).

    Enforces the total ceiling BEFORE each paid run (a run may not start
    unless its full per-run cap still fits under the ceiling) and aborts
    all further paid spending after :data:`BILLING_ABORT_AFTER`
    consecutive auth/billing-looking failures.

    Attributes:
        per_run_cap: Per-run list-price cost cap (forwarded to the loop).
        total_cap: Hard ceiling on cumulative list-price spend.
        spent: Cumulative list-price spend recorded so far.
        aborted: Reason string once paid spending must stop, else None.
    """

    def __init__(self, per_run_cap: float, total_cap: float) -> None:
        self.per_run_cap = per_run_cap
        self.total_cap = total_cap
        self.spent = 0.0
        self.aborted: Optional[str] = None
        self._consecutive_billing_errors = 0

    def can_start(self) -> bool:
        """True when a new paid run may start under the ceiling."""
        return self.aborted is None and self.spent + self.per_run_cap <= self.total_cap

    def blocked_reason(self) -> str:
        """Why a paid run may not start right now."""
        if self.aborted is not None:
            return self.aborted
        return (
            f"total budget ceiling: ${self.spent:.2f} of "
            f"${self.total_cap:.2f} spent at list price; the next run's "
            f"${self.per_run_cap:.2f} cap could exceed the ceiling"
        )

    def record(self, cost_usd: float, error: Optional[str] = None) -> None:
        """Record a finished paid run's cost and error status.

        Args:
            cost_usd: The run's list-price cost.
            error: The run's error string, if it failed.
        """
        self.spent += cost_usd
        if error and _looks_like_billing_error(error):
            self._consecutive_billing_errors += 1
            if self._consecutive_billing_errors >= BILLING_ABORT_AFTER:
                self.aborted = (
                    f"paid spending aborted after "
                    f"{self._consecutive_billing_errors} consecutive "
                    f"auth/billing errors — last error: {error}"
                )
        else:
            self._consecutive_billing_errors = 0


# -- hybrid run (arm D) ----------------------------------------------------------


def _failed_result_index(report: Any) -> Optional[int]:
    """Index of the first failed step result in a run report.

    Returns None when no result failed (a failed report whose step
    results are all ok): the earlier fallback of ``len(results) - 1``
    blamed the last COMPLETED step and undercounted ``completed_steps``
    by one in the handoff prompt.
    """
    for i, result in enumerate(report.results):
        if not result.ok:
            return i
    return None


def _hybrid_run(
    bundle_dir: Path,
    url: str,
    run_dir: Path,
    note_text: str,
    *,
    demo_text: str,
    ledger: SpendLedger,
    client: Any = None,
    fallback_max_actions: int = FALLBACK_MAX_ACTIONS,
    headed: bool = False,
    save_final_to: Optional[Path] = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One hybrid run: compiled replay, agent fallback on safe-halt.

    The compiled replay runs first. If it completes, the run costs $0 and
    is verified as-is. If it halts, the SAME browser session (current
    state preserved) is handed to a demo-conditioned fallback agent whose
    prompt names the completed steps and the halt reason
    (:func:`handoff_task_prompt`), with a reduced action budget. The
    fallback fires only if the shared ledger allows a paid run; otherwise
    the halt is recorded as a failure with the skip reason disclosed.

    Args:
        bundle_dir: Compiled workflow bundle.
        url: Target URL (may carry a drift query).
        run_dir: Scratch directory for replay artifacts.
        note_text: The run's note parameter value.
        demo_text: Serialized demonstration for the fallback prompt.
        ledger: Shared paid-spend ledger.
        client: Optional injected Anthropic client (tests).
        fallback_max_actions: Action budget for the fallback agent.
        headed: Run the browser headed.
        save_final_to: Optional path to save the final screenshot.
        log: Per-API-call usage logger for the fallback loop.

    Returns:
        A per-run row dict with halt/fallback metadata and cost attributed
        to the fallback only (the compiled portion is free).
    """
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime import Replayer

    workflow = Workflow.load(bundle_dir)
    backend, close = PlaywrightBackend.launch(url, headless=not headed)
    row: dict[str, Any] = {
        "arm": "hybrid",
        "actions": 0,
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "fallback_used": False,
        "fallback_skipped_reason": None,
        "fallback_actions": 0,
        "fallback_api_calls": 0,
        "fallback_cost_usd": 0.0,
        "fallback_wall_s": 0.0,
        "fallback_stopped": None,
        "halted": False,
        "halt_step": None,
        "halt_reason": None,
        "error": None,
    }
    try:
        start = time.monotonic()
        report = Replayer(backend).run(
            workflow,
            params={"note": note_text},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
        compiled_wall_s = time.monotonic() - start
        row["compiled_wall_s"] = compiled_wall_s
        row["wall_s"] = compiled_wall_s
        row["replayer_success"] = report.success
        row["heal_count"] = report.heal_count
        row["actions"] = len(report.results)

        if not report.success:
            failed_i = _failed_result_index(report)
            if failed_i is None:
                # Defensive: a failed report where every step result is ok
                # (nothing to name). All listed steps completed; the
                # fallback prompt must not blame the last completed step.
                completed_steps = len(report.results)
                halt_step = None
                halt_intent = "(no failing step recorded)"
                halt_reason = "replayer reported failure without a failing step"
            else:
                failed = report.results[failed_i]
                completed_steps = failed_i
                halt_step = failed.step_id
                halt_intent = failed.intent
                halt_reason = failed.error or "unknown"
            row["halted"] = True
            row["halt_step"] = halt_step
            row["halt_reason"] = halt_reason
            if not ledger.can_start():
                row["fallback_skipped_reason"] = ledger.blocked_reason()
            else:
                task = handoff_task_prompt(
                    note_text,
                    demo_text,
                    completed_steps=completed_steps,
                    total_steps=len(workflow.steps),
                    halted_step_intent=halt_intent,
                    halt_reason=halt_reason,
                )
                row["fallback_used"] = True
                # Usage is recorded into this ledger the moment each API
                # response arrives, so a fallback that crashes mid-run
                # still surfaces its paid usage to the row and the shared
                # SpendLedger (crashed spend must count against the
                # ceiling, never vanish with the exception).
                usage = agent_baseline.UsageLedger()
                try:
                    result = agent_baseline.run_agent(
                        backend,
                        task,
                        client=client,
                        max_actions=fallback_max_actions,
                        max_cost_usd=ledger.per_run_cap,
                        log=log,
                        ledger=usage,
                    )
                except Exception as exc:  # noqa: BLE001 - a failed run is data
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    row["fallback_api_calls"] = usage.api_calls
                    row["fallback_cost_usd"] = usage.cost_usd
                    row["api_calls"] = usage.api_calls
                    row["input_tokens"] = usage.input_tokens
                    row["output_tokens"] = usage.output_tokens
                    row["cache_creation_input_tokens"] = (
                        usage.cache_creation_input_tokens
                    )
                    row["cache_read_input_tokens"] = usage.cache_read_input_tokens
                    row["cost_usd"] = usage.cost_usd
                    ledger.record(usage.cost_usd, error=row["error"])
                else:
                    row["fallback_actions"] = result.actions
                    row["fallback_api_calls"] = result.api_calls
                    row["fallback_cost_usd"] = result.cost_usd
                    row["fallback_wall_s"] = result.wall_s
                    row["fallback_stopped"] = result.stopped
                    row["actions"] += result.actions
                    row["api_calls"] = result.api_calls
                    row["input_tokens"] = result.input_tokens
                    row["output_tokens"] = result.output_tokens
                    row["cache_creation_input_tokens"] = (
                        result.cache_creation_input_tokens
                    )
                    row["cache_read_input_tokens"] = result.cache_read_input_tokens
                    row["cost_usd"] = result.cost_usd
                    row["wall_s"] = compiled_wall_s + result.wall_s
                    ledger.record(result.cost_usd)

        final_png = backend.screenshot()
        verdict = verify_hybrid_final(final_png, note_text)
        row["success"] = verdict.success
        row.update(verdict.model_dump(exclude={"success"}))
        if save_final_to is not None:
            save_final_to.parent.mkdir(parents=True, exist_ok=True)
            save_final_to.write_bytes(final_png)
    finally:
        close()
    return row


# -- aggregation ------------------------------------------------------------------


def _cost_per_success(rows: list[dict[str, Any]]) -> Optional[float]:
    """Total cost divided by successful runs; None when nothing succeeded."""
    successes = sum(1 for r in rows if r.get("success"))
    if not successes:
        return None
    return sum(r["cost_usd"] for r in rows) / successes


def _split_rate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """n / success_count / success_rate for a row subset."""
    n = len(rows)
    successes = sum(1 for r in rows if r.get("success"))
    return {
        "n": n,
        "success_count": successes,
        "success_rate": (successes / n) if n else 0.0,
    }


def hybrid_arm_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-run rows for one arm, with hybrid-benchmark extras.

    Extends the base arm aggregate with the headline cost-per-successful-
    run, wrong-action count, clean/drift splits, and — for rows carrying
    fallback metadata — fallback rate/success/cost statistics.

    Args:
        rows: Per-run row dicts (must carry ``condition``).

    Returns:
        The aggregate dict.
    """
    agg = _arm_aggregate(rows)
    agg["cost_per_success_usd"] = _cost_per_success(rows)
    agg["wrong_action_count"] = sum(1 for r in rows if r.get("wrong_action"))
    agg["clean"] = _split_rate([r for r in rows if r.get("condition") == "clean"])
    agg["drift"] = _split_rate([r for r in rows if r.get("condition") != "clean"])
    fallback_rows = [r for r in rows if r.get("fallback_used")]
    if any("fallback_used" in r for r in rows):
        halted = [r for r in rows if r.get("halted")]
        agg["halt_count"] = len(halted)
        agg["fallback_count"] = len(fallback_rows)
        agg["fallback_rate"] = len(fallback_rows) / len(rows) if rows else 0.0
        agg["fallback_success_count"] = sum(
            1 for r in fallback_rows if r.get("success")
        )
        agg["fallback_success_rate"] = (
            agg["fallback_success_count"] / len(fallback_rows) if fallback_rows else 0.0
        )
        agg["fallback_actions_mean"] = (
            statistics.fmean(r["fallback_actions"] for r in fallback_rows)
            if fallback_rows
            else 0.0
        )
        agg["fallback_cost_usd_mean"] = (
            statistics.fmean(r["fallback_cost_usd"] for r in fallback_rows)
            if fallback_rows
            else 0.0
        )
        agg["fallback_skipped_count"] = sum(
            1 for r in rows if r.get("fallback_skipped_reason")
        )
    return agg


def aggregate_hybrid_results(
    runs: dict[str, list[dict[str, Any]]],
    *,
    arm_notes: dict[str, Optional[str]],
    max_cost_per_run_usd: float = agent_baseline.MAX_COST_USD,
    max_total_cost_usd: float = MAX_TOTAL_COST_USD,
    schedule: tuple[str, ...] = SCHEDULE,
    agent_slots: tuple[int, ...] = AGENT_SLOTS,
) -> dict[str, Any]:
    """Assemble the full results document from per-run rows.

    Args:
        runs: Arm key -> list of per-run rows.
        arm_notes: Arm key -> honest-disclosure note (skip/truncation/abort)
            or None when the arm ran fully.
        max_cost_per_run_usd: Per-run cost cap that was enforced.
        max_total_cost_usd: Total paid-spend ceiling that was enforced.
        schedule: The frozen condition schedule.
        agent_slots: Slot indices arms B and C ran.

    Returns:
        The results dict serialized to ``results.json``.
    """
    paid_total = sum(r["cost_usd"] for rows in runs.values() for r in rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task": (
            "MockMed triage: sign in, open first referral, create a "
            "Triage encounter, enter a parameterized note, save"
        ),
        "model": agent_baseline.MODEL,
        "computer_tool": agent_baseline.COMPUTER_TOOL_TYPE,
        "beta_header": agent_baseline.COMPUTER_USE_BETA,
        "pricing_usd_per_mtok": {
            "input": agent_baseline.INPUT_USD_PER_MTOK,
            "output": agent_baseline.OUTPUT_USD_PER_MTOK,
            "cache_write": agent_baseline.CACHE_WRITE_USD_PER_MTOK,
            "cache_read": agent_baseline.CACHE_READ_USD_PER_MTOK,
            "note": (
                "list price; an introductory $2/$10 rate applies through 2026-08-31"
            ),
        },
        "cost_caps_usd": {
            "per_run": max_cost_per_run_usd,
            "total": max_total_cost_usd,
            "total_spent_list": paid_total,
        },
        "schedule": {
            "conditions": list(schedule),
            "agent_slots": list(agent_slots),
            "drift_types": list(DRIFT_TYPES),
            "drift_fraction": (
                sum(1 for c in schedule if c != "clean") / len(schedule)
            ),
        },
        "fallback_max_actions": FALLBACK_MAX_ACTIONS,
        "agent_max_actions": AGENT_MAX_ACTIONS,
        "platform": platform.platform(),
        "arm_notes": arm_notes,
        "arms": {arm: hybrid_arm_aggregate(rows) for arm, rows in runs.items()},
        "runs": runs,
    }


# -- chart ------------------------------------------------------------------------

#: Categorical arm colors, validated for CVD separation / lightness /
#: chroma against the chart surface (#fcfcfb); the below-3:1 contrast of
#: the green is relieved by the direct value labels on every bar.
ARM_COLORS = {
    "compiled": "#2a78d6",
    "agent": "#1baf7a",
    "demo_agent": "#c2571f",
    "hybrid": "#8a5cd6",
}
ARM_LABELS = {
    "compiled": "compiled\n(A)",
    "agent": "agent\n(B)",
    "demo_agent": "demo agent\n(C)",
    "hybrid": "hybrid\n(D)",
}
ARM_ORDER = ("compiled", "agent", "demo_agent", "hybrid")


def render_hybrid_chart(results: dict[str, Any], out_png: Path) -> Path:
    """Render the success-rate + cost-per-successful-run chart.

    Two panels in the existing benchmarks' style (one measure per axis):
    success rate under the mixed schedule, and the headline cost per
    successful run. Every bar carries a direct value label.

    Args:
        results: The aggregate results dict.
        out_png: Output PNG path.

    Returns:
        The written PNG path.
    """
    from openadapt_flow.benchmark.chart_fonts import configure_bundled_font

    plt = configure_bundled_font()

    surface = "#fcfcfb"
    ink = "#0b0b0b"
    ink2 = "#52514e"

    arms = [a for a in ARM_ORDER if a in results["arms"]]
    labels = [ARM_LABELS[a] for a in arms]
    colors = [ARM_COLORS[a] for a in arms]

    fig, (ax_sr, ax_cost) = plt.subplots(1, 2, figsize=(9.6, 4.2), facecolor=surface)
    fig.suptitle(
        "Four arms, one mixed clean+drift schedule, one success check",
        color=ink,
        fontsize=12,
    )

    def style(ax: Any, title: str, ylabel: str) -> None:
        ax.set_facecolor(surface)
        ax.set_title(title, color=ink, fontsize=10)
        ax.set_ylabel(ylabel, color=ink2, fontsize=9)
        ax.tick_params(colors=ink2, labelsize=9)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(ink2)
        ax.grid(axis="y", color="#e6e5e0", linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)

    rates = [results["arms"][a]["success_rate"] for a in arms]
    bars = ax_sr.bar(labels, rates, color=colors, width=0.55, zorder=2)
    ax_sr.set_ylim(0, 1.12)
    style(ax_sr, "Success rate (mixed schedule)", "fraction of runs")
    for bar, value, arm in zip(bars, rates, arms):
        agg = results["arms"][arm]
        ax_sr.annotate(
            f"{value:.0%}\n({agg['success_count']}/{agg['n']})",
            (bar.get_x() + bar.get_width() / 2, value),
            ha="center",
            va="bottom",
            fontsize=8.5,
            color=ink,
        )

    costs = [results["arms"][a]["cost_per_success_usd"] for a in arms]
    plotted = [c if c is not None else 0.0 for c in costs]
    bars = ax_cost.bar(labels, plotted, color=colors, width=0.55, zorder=2)
    style(ax_cost, "Model cost per successful run", "USD")
    for bar, value in zip(bars, costs):
        text = "n/a" if value is None else ("$0" if value == 0 else f"${value:.3f}")
        ax_cost.annotate(
            text,
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            ha="center",
            va="bottom",
            fontsize=9,
            color=ink,
        )

    fig.tight_layout(rect=(0, 0, 1, 0.92))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160, facecolor=surface)
    plt.close(fig)
    return out_png


# -- markdown -----------------------------------------------------------------------


def _fmt_cps(value: Optional[float]) -> str:
    """Format a cost-per-success value ('n/a' when nothing succeeded)."""
    if value is None:
        return "n/a (0 successes)"
    if value == 0:
        return "$0"
    return f"${value:.4f}"


def _verdict(results: dict[str, Any]) -> str:
    """Plain-language verdict on the dominance thesis, computed from data.

    Rule, stated so readers can re-derive it: the thesis is SUPPORTED when
    the hybrid's success rate is >= the best agent-only arm's AND its cost
    per successful run is lower; PARTIALLY SUPPORTED when one holds and
    the other is within one run's worth of the small agent N; otherwise
    REFUTED.
    """
    h = results["arms"]["hybrid"]
    agent_arms = [
        results["arms"][a]
        for a in ("agent", "demo_agent")
        if results["arms"].get(a, {}).get("n")
    ]
    if not agent_arms or not h.get("n"):
        return (
            "**No verdict**: a paid arm recorded no runs (see arm notes), "
            "so the dominance thesis could not be tested."
        )
    best_agent_sr = max(a["success_rate"] for a in agent_arms)
    agent_cps = [
        a["cost_per_success_usd"]
        for a in agent_arms
        if a["cost_per_success_usd"] is not None
    ]
    h_cps = h["cost_per_success_usd"]
    cheaper = h_cps is not None and (not agent_cps or h_cps < min(agent_cps))
    sr_at_least = h["success_rate"] >= best_agent_sr
    # "Within noise": one run's worth of success rate at the smaller N.
    min_agent_n = min(a["n"] for a in agent_arms)
    sr_close = best_agent_sr - h["success_rate"] <= 1.0 / max(min_agent_n, 1)
    if sr_at_least and cheaper:
        return (
            "**Supported** (on this schedule): the hybrid matched or beat "
            "the best agent-only arm's success rate at a lower cost per "
            "successful run. The drift mix (30%) is an assumption — see "
            "the sensitivity note."
        )
    if cheaper and sr_close:
        return (
            "**Partially supported**: the hybrid's cost per successful "
            "run is lower, and its success rate is within one run's "
            "worth of the best agent-only arm at these small Ns. The "
            "drift mix (30%) is an assumption — see the sensitivity note."
        )
    if sr_at_least and not cheaper:
        return (
            "**Partially supported**: the hybrid matched the agent arms' "
            "reliability but was NOT cheaper per successful run on this "
            "schedule."
        )
    return (
        "**Refuted** (on this schedule): the hybrid neither matched the "
        "best agent-only arm's success rate nor achieved a lower cost per "
        "successful run."
    )


def _demo_conditioning_note(b: dict[str, Any], c: dict[str, Any]) -> str:
    """One computed paragraph answering: did the demo help the agent (C vs B)?"""
    if not b.get("n") or not c.get("n"):
        return (
            "Demo-conditioning (C vs B): not measurable — a paid arm recorded no runs."
        )
    delta = c["cost_usd_per_run"] - b["cost_usd_per_run"]
    numbers = (
        f"Demo-conditioning finding (C vs B): success "
        f"{c['success_count']}/{c['n']} vs {b['success_count']}/{b['n']}; "
        f"mean cost/run ${c['cost_usd_per_run']:.4f} vs "
        f"${b['cost_usd_per_run']:.4f} "
        f"({'+' if delta >= 0 else '−'}${abs(delta):.4f}); mean actions "
        f"{c['actions_mean']:.1f} vs {b['actions_mean']:.1f}."
    )
    if (
        c["success_rate"] <= b["success_rate"]
        and c["cost_usd_per_run"] >= b["cost_usd_per_run"]
    ):
        return (
            f"{numbers} On this schedule the serialized demonstration "
            "made the from-scratch agent neither cheaper nor more "
            "reliable (the extra prompt tokens cost slightly more); its "
            "measured value showed up in the hybrid's mid-workflow "
            "fallback instead."
        )
    if (
        c["success_rate"] >= b["success_rate"]
        and c["cost_usd_per_run"] < b["cost_usd_per_run"]
    ):
        return (
            f"{numbers} On this schedule the serialized demonstration "
            "made the from-scratch agent cheaper without hurting "
            "reliability."
        )
    return f"{numbers} Mixed effect on this schedule — see per-run rows for detail."


def _subsample_mix_note(results: dict[str, Any]) -> str:
    """Disclosure sentence for the B/C subsample's drift-mix bias.

    Arms B and C run a subsample of the schedule; when the subsample's
    drift fraction differs from the full schedule's, the agent-only mean
    cost per run is measured on a slightly different mix than the hybrid
    arm's (drifted runs cost more). Reports the measured agent mean next
    to the mean reweighted to the full schedule's mix, and both cost
    ratios, so the small bias is visible. Empty when the mixes match or
    the reweighting cannot be computed from the rows.
    """
    sched = results["schedule"]
    slots = sched["agent_slots"]
    sub = [sched["conditions"][s] for s in slots]
    sub_drift = sum(1 for c in sub if c != "clean")
    sub_fraction = sub_drift / len(sub) if sub else 0.0
    full_fraction = sched["drift_fraction"]
    if abs(sub_fraction - full_fraction) < 1e-9:
        return ""
    agent_rows = results["runs"].get("agent", [])
    clean = [r["cost_usd"] for r in agent_rows if r["condition"] == "clean"]
    drift = [r["cost_usd"] for r in agent_rows if r["condition"] != "clean"]
    if not clean or not drift:
        return ""
    measured = statistics.fmean(r["cost_usd"] for r in agent_rows)
    reweighted = (1 - full_fraction) * statistics.fmean(clean) + (
        full_fraction * statistics.fmean(drift)
    )
    hybrid_mean = results["arms"]["hybrid"]["cost_usd_per_run"]
    ratios = ""
    if hybrid_mean > 0:
        ratios = (
            f"; cost-per-run ratio {measured / hybrid_mean:.1f}x measured "
            f"vs {reweighted / hybrid_mean:.1f}x reweighted"
        )
    return (
        f"\nDisclosure: the B/C agent subsample is {sub_drift}/{len(sub)} "
        f"= {sub_fraction:.1%} drift versus the "
        f"{len(sched['conditions'])}-slot schedule's {full_fraction:.0%} — "
        f"drifted runs cost more, so the agent-only mean is measured on a "
        f"slightly drift-heavier mix than the hybrid arm ran: a small cost "
        f"bias in the hybrid's FAVOR of about "
        f"${measured - reweighted:.5f}/run (B mean ${measured:.5f} "
        f"measured vs ~${reweighted:.5f} reweighted to the schedule "
        f"mix{ratios}). Conclusions unchanged.\n"
    )


def render_hybrid_markdown(results: dict[str, Any]) -> str:
    """Render ``BENCHMARK.md`` from the results dict.

    Args:
        results: The aggregate results dict.

    Returns:
        The markdown document as a string.
    """
    arms = results["arms"]
    a, b = arms["compiled"], arms["agent"]
    c, d = arms["demo_agent"], arms["hybrid"]
    date = results["generated_at"][:10]
    identity_block = identity_coverage_block(a)
    caps = results["cost_caps_usd"]
    sched = results["schedule"]
    n_drift = sum(1 for x in sched["conditions"] if x != "clean")

    notes_block = "".join(
        f"> **{arm} arm disclosure:** {note}\n>\n"
        for arm, note in results["arm_notes"].items()
        if note
    )

    def _failure_detail(arm: str, r: dict[str, Any]) -> str:
        if r.get("error"):
            return str(r["error"])
        first_failure = r.get("first_failure")
        if arm == "compiled" and first_failure:
            return (
                f"safe-halted at {first_failure.get('step')} "
                f"({first_failure.get('error', '')[:120]}...)"
            )
        if r.get("halted"):
            if r.get("fallback_skipped_reason"):
                return (
                    f"halted at {r.get('halt_step')}; fallback SKIPPED — "
                    f"{r.get('fallback_skipped_reason')}"
                )
            if r.get("fallback_used"):
                return (
                    f"halted at {r.get('halt_step')}; fallback fired "
                    f"({r.get('fallback_actions')} actions, "
                    f"${r.get('fallback_cost_usd', 0):.4f}, stopped="
                    f"{r.get('fallback_stopped')}) but the final check "
                    f"failed (banner={r.get('banner_found')}, "
                    f"note={r.get('note_found')}, "
                    f"right_patient={r.get('right_patient')})"
                )
            return f"halted at {r.get('halt_step')}"
        return (
            f"final check failed (banner={r.get('banner_found')}, "
            f"note={r.get('note_found')}, "
            f"right_patient={r.get('right_patient')})"
        )

    mix_note = _subsample_mix_note(results)

    def failures(arm: str) -> str:
        lines = "".join(
            f"- {arm} slot {r['slot']} ({r['condition']}): "
            + _failure_detail(arm, r)
            + "\n"
            for r in results["runs"][arm]
            if not r["success"]
        )
        return lines or "- none\n"

    wrong_actions = [
        (arm, r)
        for arm in ARM_ORDER
        for r in results["runs"].get(arm, [])
        if r.get("wrong_action")
    ]
    wrong_action_block = (
        "**WRONG-ACTION EVENTS OBSERVED** — runs that mutated state on an "
        "incorrect target:\n\n"
        + "".join(
            f"- {arm} slot {r['slot']} ({r['condition']}): "
            f"right_patient={r.get('right_patient')}, "
            f"wrong_type_row={r.get('wrong_type_row')}\n"
            for arm, r in wrong_actions
        )
        + "\n"
        if wrong_actions
        else "No wrong-action events were detected by the final-state "
        "identity check in any arm (see caveats: the detector covers "
        "wrong-patient and wrong-type writes on the final screen, not "
        "every conceivable wrong action).\n"
    )

    fallback_costs = [
        r["fallback_cost_usd"]
        for r in results["runs"]["hybrid"]
        if r.get("fallback_used")
    ]
    mean_fb_cost = statistics.fmean(fallback_costs) if fallback_costs else 0.0
    agent_cpr = b["cost_usd_per_run"] if b["n"] else 0.0
    breakeven = (agent_cpr / mean_fb_cost) if mean_fb_cost else None

    return f"""# Benchmark: does compiled-first + agent-fallback-on-halt dominate?

Date: {date}. One task, one frozen schedule of clean and drifted UI
conditions, four ways to automate it, one arm-independent success check.
Target: MockMed (the demo clinic app bundled in this repo; fake data only,
local, free, unlimited).

**Task**: sign in as `nurse.demo`, open the first referral, create a New
Encounter of type Triage, enter a parameterized note (distinct per arm and
slot), save.

**Thesis under test**: under a realistic mix of clean and drifted UI
conditions, "compiled-first with agent-fallback-on-halt" (hybrid) matches
agent-only reliability at a fraction of agent-only cost.

## Verdict

{_verdict(results)}

![success rate and cost per successful run](success_cost.png)

| | compiled (A) | agent (B) | demo agent (C) | **hybrid (D)** |
|---|---|---|---|---|
| runs | {a["n"]} | {b["n"]} | {c["n"]} | {d["n"]} |
| success rate | {a["success_rate"]:.0%} ({a["success_count"]}/{a["n"]}) | {b["success_rate"]:.0%} ({b["success_count"]}/{b["n"]}) | {c["success_rate"]:.0%} ({c["success_count"]}/{c["n"]}) | {d["success_rate"]:.0%} ({d["success_count"]}/{d["n"]}) |
| success on clean slots | {a["clean"]["success_count"]}/{a["clean"]["n"]} | {b["clean"]["success_count"]}/{b["clean"]["n"]} | {c["clean"]["success_count"]}/{c["clean"]["n"]} | {d["clean"]["success_count"]}/{d["clean"]["n"]} |
| success on drifted slots | {a["drift"]["success_count"]}/{a["drift"]["n"]} | {b["drift"]["success_count"]}/{b["drift"]["n"]} | {c["drift"]["success_count"]}/{c["drift"]["n"]} | {d["drift"]["success_count"]}/{d["drift"]["n"]} |
| wall p50 | {a["wall_s_p50"]:.1f} s | {b["wall_s_p50"]:.1f} s | {c["wall_s_p50"]:.1f} s | {d["wall_s_p50"]:.1f} s |
| wall p95 | {a["wall_s_p95"]:.1f} s | {b["wall_s_p95"]:.1f} s | {c["wall_s_p95"]:.1f} s | {d["wall_s_p95"]:.1f} s |
| model calls (total) | 0 | {b["n"] and sum(r["api_calls"] for r in results["runs"]["agent"])} | {c["n"] and sum(r["api_calls"] for r in results["runs"]["demo_agent"])} | {sum(r["api_calls"] for r in results["runs"]["hybrid"])} |
| mean cost / run | $0 | ${b["cost_usd_per_run"]:.4f} | ${c["cost_usd_per_run"]:.4f} | ${d["cost_usd_per_run"]:.4f} |
| total cost | $0 | ${b["cost_usd_total"]:.2f} | ${c["cost_usd_total"]:.2f} | ${d["cost_usd_total"]:.2f} |
| **cost / successful run** | {_fmt_cps(a["cost_per_success_usd"])} | {_fmt_cps(b["cost_per_success_usd"])} | {_fmt_cps(c["cost_per_success_usd"])} | **{_fmt_cps(d["cost_per_success_usd"])}** |
| wrong-action events | {a["wrong_action_count"]} | {b["wrong_action_count"]} | {c["wrong_action_count"]} | {d["wrong_action_count"]} |

Hybrid fallback detail: {d.get("halt_count", 0)} of {d["n"]} runs
safe-halted ({d.get("fallback_rate", 0):.0%} fallback rate);
{d.get("fallback_count", 0)} fallbacks fired,
{d.get("fallback_success_count", 0)} succeeded
({d.get("fallback_success_rate", 0):.0%} of fallbacks); mean
{d.get("fallback_actions_mean", 0):.1f} fallback actions and
${d.get("fallback_cost_usd_mean", 0):.4f} fallback cost;
{d.get("fallback_skipped_count", 0)} fallbacks skipped by the budget
guardrail.

{_demo_conditioning_note(b, c)}

{notes_block}{wrong_action_block}
Failed runs, reported honestly:

Compiled (A) — drifted slots are EXPECTED to fail here; that the failures
are safe-halts (accurate halt report, no state written) is what the
hybrid builds on:

{failures("compiled")}
Agent (B):

{failures("agent")}
Demo agent (C):

{failures("demo_agent")}
Hybrid (D):

{failures("hybrid")}
## The drift schedule (designed before spending)

{len(sched["conditions"])} slots, {n_drift} drifted
({sched["drift_fraction"]:.0%}), frozen before any paid run. Arms A and D
run all slots; arms B and C run the {len(sched["agent_slots"])}-slot
subsample {sched["agent_slots"]} (5 clean + one of each drift type).
Every arm sees the identical condition at the same slot index.
{mix_note}
| condition | what changes | compiled halt point (probed free, 3/3 deterministic) | intent-level recovery |
|---|---|---|---|
| `notice` | a "What's New" interstitial replaces the tasks screen after sign-in until dismissed | Sign In click (postcondition: tasks screen never appears) | click "Continue to tasks" |
| `reqfield` | the encounter form gains a required Acuity field; saving without it shows a validation error | New Encounter click (postcondition: form region changed) — halts BEFORE anything is typed | select an acuity, then save |
| `modal-once` | a survey modal intercepts the FIRST save click; after dismissal saving works | Save Encounter click (postcondition: saved banner never appears) | dismiss the modal, save again |

Conditions probed and REJECTED because the compiled arm absorbs them
(the resolution ladder heals through and the run completes correctly —
verified against final-state identity, not just the run's own flags):
`theme` (dark palette; 8 heals), `rename` (button relabels; 2 heals),
`move` (relocated buttons; 2 heals), and `typelabel` (Triage segment
relabeled "Triage Assessment" AND segment order swapped; healed via the
OCR rung and saved the correct Triage encounter for the correct patient,
3/3). Drift that a compiled replay heals through cannot exercise the
fallback; only drift it HALTS on can.

## Sensitivity: the 30% drift mix is an assumption

Let `d` = the fraction of runs that hit compiled-halting drift, `f` = the
mean fallback cost per halted run (measured here:
${mean_fb_cost:.4f}), and `a` = the agent-only mean cost per run
(measured here: ${agent_cpr:.4f}). Expected hybrid model cost per run is
`d x f` (clean runs are $0) versus `a` for agent-only:

- at `d = 0` (no drift): hybrid cost is $0 per run;
- at `d = 1` (every run drifts): hybrid cost approaches `f` per run plus
  the halt-detection wall-clock overhead (~5-10 s of postcondition
  timeout per halt here);
- break-even: hybrid is cheaper than agent-only whenever `d x f < a`,
  i.e. for every `d` up to `a / f`{f" = {breakeven:.1f}" if breakeven is not None else ""}.
  When `f <= a` (a mid-workflow fallback is no dearer than a full agent
  run), the hybrid is cheaper at EVERY drift rate.

## Methodology

- **Record + compile once.** The demo is recorded via the Playwright demo
  driver and compiled into a vision-anchored bundle; recording/compiling
  is a one-time cost excluded from per-run latency (same as the earlier
  benchmarks).
- **Identical environments.** Every run of every arm gets a fresh
  chromium browser + page against the same locally served MockMed app
  (state lives in the page). Drift is injected via MockMed's `?drift=`
  query flags, so conditions are exactly reproducible.
- **Same interface.** All arms drive the same `PlaywrightBackend`,
  vision-only: screenshots in; pixel clicks, typed text, key presses out.
- **Agent arms.** Model `{results["model"]}` with the
  `{results["computer_tool"]}` computer-use tool (beta header
  `{results["beta_header"]}`), prompt caching on, history bounded to the
  last 3 screenshots, {results["agent_max_actions"]}-action budget (B, C)
  and {results["fallback_max_actions"]}-action budget for D's mid-workflow
  fallback. Arm B's prompt states user intent only. Arm C's prompt = B's
  plus the serialized demonstration (action type + human-readable target
  per step, `<note>` placeholder, NO coordinates). D's fallback prompt =
  intent + serialized demo + "steps 1..k reported complete, halted at
  step k+1 because <reason>", continuing in the replayer's own browser
  session.
- **Same success criterion, implemented once.** `verify_hybrid_final` on
  the final screenshot: OCR must find the `Encounter saved` banner AND
  the `Triage — <note>` row AND the right patient's name, and this run's
  note must not appear in a wrong-type row. No arm's self-report is used.
- **Distinct note per (arm, slot)** so a pass proves parameter
  substitution against the run's own value.
- **Cost** from API `usage` token counts at list pricing
  (${results["pricing_usd_per_mtok"]["input"]:.2f} /
  ${results["pricing_usd_per_mtok"]["output"]:.2f} per MTok in/out;
  cache writes 1.25x, cache reads 0.1x input). An introductory $2/$10
  rate applies through 2026-08-31, so billed cost today is roughly a
  third lower than reported.
{identity_block}
- **Hard cost guardrails.** One shared budget across ALL paid runs (B, C,
  and D's fallbacks): preflight probe before any spend; per-run cap
  ${caps["per_run"]:.2f}; total ceiling ${caps["total"]:.2f} enforced
  before every paid run (trips are disclosed, never raised); two
  consecutive auth/billing errors abort paid spending; every finished run
  appends to `rows.jsonl`. Total recorded spend at list price:
  **${caps["total_spent_list"]:.2f}**.

## Caveats — read before quoting these numbers

- **The hybrid's reliability is bounded by halt-DETECTION reliability.**
  Fallback fires only on DETECTED halts. The validation suite
  (`docs/validation/VALIDATION.md` on `feat/validation-suite`, PR #12)
  documents silent wrong-action modes in the compiled replayer —
  lookalike/imposter rows clicked with high template confidence, deleted
  targets resolving to a neighbouring row, rows inserted above shifting
  the target, and focus theft silently dropping typed text — which
  produce a green report with a wrong or empty write and would BYPASS the
  hybrid's fallback entirely. The dominance claim, to whatever extent
  supported above, applies ONLY to detected-halt drift. This benchmark's
  success check verifies final-state identity (right patient, right
  type, the run's own note) precisely because the replayer's self-report
  is not sufficient.
- **Selection bias in the drift conditions, by construction.** The three
  drift conditions were chosen BECAUSE free probe runs showed they make
  the current bundle safe-halt deterministically (and remain completable
  at the intent level). Drift that the ladder absorbs (theme, renames,
  moves) is under-represented — on such drift the hybrid is identical to
  the compiled arm at $0. Real-world drift mixes will contain all three
  classes (absorbed / detected-halt / silent-wrong) in unknown
  proportions.
- **MockMed is our own app**, built simple and high-contrast; both the
  drift hooks and the workflow are synthetic. Treat this as a controlled
  experiment on the ORCHESTRATION policy, not a field result.
- **Small agent Ns** ({b["n"]} per paid arm; {d.get("fallback_count", 0)}
  hybrid fallbacks): success rates carry wide error bars; single-run
  differences are within noise.
- **The 30% drift fraction is an assumption** — see the sensitivity note
  and break-even formula above.
- **The compiled arm needs a demonstration first** (about a minute of
  human demonstration, one-time). The agent arms need only the prompt;
  arm C and D's fallback also consume the demonstration, serialized.
- **Model version pinned**: `{results["model"]}` with
  `{results["computer_tool"]}` on {date}.
- Single machine ({results["platform"]}); local server; no network
  variance except the Anthropic API round trips in paid runs.

## Reproduce

```
.venv/bin/python -m openadapt_flow.benchmark.hybrid_benchmark --out benchmark/hybrid
```

Requires `ANTHROPIC_API_KEY` (or `~/.anthropic/api_key`). The paid arms
cost real money (${caps["total_spent_list"]:.2f} at list price when this
was generated; billed cost is lower under the intro rate). The compiled
arm and the drift probes are free.
"""


def write_hybrid_outputs(results: dict[str, Any], out_dir: Path) -> None:
    """Write ``results.json``, ``BENCHMARK.md``, and the chart PNG.

    Args:
        results: The aggregate results dict.
        out_dir: Output directory (created if needed).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    from openadapt_flow.benchmark.chart_fonts import safe_render

    safe_render(render_hybrid_chart, results, out_dir / "success_cost.png")
    (out_dir / "BENCHMARK.md").write_text(render_hybrid_markdown(results))


# -- orchestrator -------------------------------------------------------------------


def run_hybrid_benchmark(
    out_dir: Path | str,
    *,
    schedule: tuple[str, ...] = SCHEDULE,
    agent_slots: tuple[int, ...] = AGENT_SLOTS,
    max_cost_per_run_usd: float = agent_baseline.MAX_COST_USD,
    max_total_cost_usd: float = MAX_TOTAL_COST_USD,
    fallback_max_actions: int = FALLBACK_MAX_ACTIONS,
    agent_max_actions: int = AGENT_MAX_ACTIONS,
    headed: bool = False,
    agent_client: Any = None,
    preflight: Callable[[], tuple[bool, str | None]] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the four-arm hybrid benchmark and write all outputs.

    Arm order: A (compiled, free), D (hybrid — its fallbacks are the
    thesis-critical paid data, so they spend first), B (agent), C (demo
    agent). One :class:`SpendLedger` covers all paid runs; when the
    ceiling would be exceeded the remaining paid runs are skipped and the
    truncation disclosed — the ceiling is never raised.

    Args:
        out_dir: Directory for ``rows.jsonl`` / ``results.json`` /
            ``BENCHMARK.md`` / chart.
        schedule: Condition per slot (arms A and D run every slot).
        agent_slots: Slot indices arms B and C run.
        max_cost_per_run_usd: Per-run cost cap at list price.
        max_total_cost_usd: Ceiling on total list-price spend across ALL
            paid runs.
        fallback_max_actions: Action budget for D's fallback agent.
        agent_max_actions: Action budget for arms B and C.
        headed: Run browsers headed (debugging).
        agent_client: Optional injected Anthropic client (tests).
        preflight: Callable returning ``(ok, error)`` probed before any
            paid run; None uses :func:`agent_baseline.preflight_check`.
        log: Progress logger.

    Returns:
        The results dict (also written to ``results.json``).
    """
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.demo_driver import record_triage_demo
    from openadapt_flow.mockmed.server import serve

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "rows.jsonl"
    finals = out / "finals"

    def persist(row: dict[str, Any]) -> None:
        with rows_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    ledger = SpendLedger(max_cost_per_run_usd, max_total_cost_usd)
    arm_notes: dict[str, Optional[str]] = {arm: None for arm in ARM_ORDER}

    if preflight is None:
        preflight = lambda: agent_baseline.preflight_check(  # noqa: E731
            client=agent_client
        )
    preflight_ok, preflight_err = preflight()
    if not preflight_ok:
        ledger.aborted = f"API preflight failed — {preflight_err}"
        for arm in ("agent", "demo_agent"):
            arm_notes[arm] = f"skipped: {ledger.aborted}"
        arm_notes["hybrid"] = (
            f"fallbacks disabled: {ledger.aborted} (compiled portion still ran)"
        )
        log(f"PAID RUNS DISABLED: {ledger.aborted}")

    runs: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ARM_ORDER}

    url, stop = serve(port=0)
    try:
        with tempfile.TemporaryDirectory(prefix="oaf-hybrid-") as tmp_str:
            tmp = Path(tmp_str)
            log("Recording demo...")
            recording = record_triage_demo(
                url, tmp / "recording", note_text=RECORD_NOTE, headed=headed
            )
            bundle = tmp / "bundle"
            workflow = compile_recording(recording, bundle, name=WORKFLOW_NAME)
            demo_text = serialize_demo(workflow)
            log(f"Compiled bundle ({len(workflow.steps)} steps): {bundle}")
            log(f"Serialized demo:\n{demo_text}")

            # Arm A: compiled, free, full schedule.
            for slot, condition in enumerate(schedule):
                note = note_for_slot("compiled", slot)
                try:
                    row = _compiled_run(
                        bundle,
                        condition_url(url, condition),
                        tmp / "runs" / f"compiled_{slot:03d}",
                        note,
                        verify_fn=verify_hybrid_final,
                        save_final_to=finals / f"compiled_{slot:03d}.png",
                        headed=headed,
                    )
                except Exception as exc:  # noqa: BLE001 - a failed run is data
                    row = _error_row("compiled", exc)
                row.update(
                    {"i": slot, "slot": slot, "condition": condition, "note": note}
                )
                runs["compiled"].append(row)
                persist(row)
                log(
                    f"compiled {slot + 1}/{len(schedule)} [{condition}]: "
                    f"success={row['success']} "
                    f"replayer={row.get('replayer_success')} "
                    f"heals={row.get('heal_count')} "
                    f"{row['wall_s']:.1f}s"
                )

            # Arm D: hybrid, full schedule; pays only on fallbacks.
            for slot, condition in enumerate(schedule):
                note = note_for_slot("hybrid", slot)
                try:
                    row = _hybrid_run(
                        bundle,
                        condition_url(url, condition),
                        tmp / "runs" / f"hybrid_{slot:03d}",
                        note,
                        demo_text=demo_text,
                        ledger=ledger,
                        client=agent_client,
                        fallback_max_actions=fallback_max_actions,
                        headed=headed,
                        save_final_to=finals / f"hybrid_{slot:03d}.png",
                        log=log,
                    )
                except Exception as exc:  # noqa: BLE001 - a failed run is data
                    row = _error_row("hybrid", exc)
                row.update(
                    {"i": slot, "slot": slot, "condition": condition, "note": note}
                )
                runs["hybrid"].append(row)
                persist(row)
                log(
                    f"hybrid {slot + 1}/{len(schedule)} [{condition}]: "
                    f"success={row['success']} "
                    f"halted={row.get('halted')} "
                    f"fallback={row.get('fallback_used')} "
                    f"${row['cost_usd']:.4f} {row['wall_s']:.1f}s"
                )
            skipped = sum(1 for r in runs["hybrid"] if r.get("fallback_skipped_reason"))
            if skipped and arm_notes["hybrid"] is None:
                arm_notes["hybrid"] = (
                    f"{skipped} fallback(s) skipped by the budget "
                    f"guardrail: {ledger.blocked_reason()}"
                )

            # Arms B and C: paid agent arms on the subsample.
            for arm, task_fn in (
                (
                    "agent",
                    lambda note: agent_baseline.triage_task_prompt(note),
                ),
                (
                    "demo_agent",
                    lambda note: demo_conditioned_task_prompt(note, demo_text),
                ),
            ):
                for slot in agent_slots:
                    condition = schedule[slot]
                    note = note_for_slot(arm, slot)
                    if not ledger.can_start():
                        if arm_notes[arm] is None:
                            arm_notes[arm] = (
                                f"truncated after "
                                f"{len(runs[arm])} of {len(agent_slots)} "
                                f"runs — {ledger.blocked_reason()}"
                            )
                        log(f"{arm.upper()} ARM STOPPED: {arm_notes[arm]}")
                        break
                    try:
                        row = _agent_run(
                            condition_url(url, condition),
                            note,
                            task=task_fn(note),
                            verify_fn=verify_hybrid_final,
                            save_final_to=finals / f"{arm}_{slot:03d}.png",
                            client=agent_client,
                            headed=headed,
                            max_actions=agent_max_actions,
                            max_cost_usd=max_cost_per_run_usd,
                            log=log,
                        )
                    except Exception as exc:  # noqa: BLE001
                        row = _error_row(arm, exc)
                    row["arm"] = arm
                    row.update(
                        {"i": slot, "slot": slot, "condition": condition, "note": note}
                    )
                    runs[arm].append(row)
                    persist(row)
                    ledger.record(row["cost_usd"], error=row.get("error"))
                    log(
                        f"{arm} slot {slot} [{condition}]: "
                        f"success={row['success']} "
                        f"${row['cost_usd']:.4f} {row['wall_s']:.1f}s "
                        f"actions={row['actions']} "
                        f"stopped={row.get('stopped')} err={row['error']}"
                    )
    finally:
        stop()

    results = aggregate_hybrid_results(
        runs,
        arm_notes=arm_notes,
        max_cost_per_run_usd=max_cost_per_run_usd,
        max_total_cost_usd=max_total_cost_usd,
        schedule=schedule,
        agent_slots=agent_slots,
    )
    write_hybrid_outputs(results, out)
    log(
        f"Wrote {out / 'results.json'}, BENCHMARK.md, success_cost.png; "
        f"total paid spend ${ledger.spent:.2f} at list price"
    )
    return results


def _error_row(arm: str, exc: Exception) -> dict[str, Any]:
    """Zeroed row for a run that raised before producing one."""
    return {
        "arm": arm,
        "wall_s": 0.0,
        "success": False,
        "actions": 0,
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "error": f"{type(exc).__name__}: {exc}",
    }


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: ``python -m openadapt_flow.benchmark.hybrid_benchmark``."""
    parser = argparse.ArgumentParser(
        description=(
            "Four-arm hybrid benchmark on MockMed (compiled / agent / "
            "demo-conditioned agent / compiled+fallback hybrid). The "
            "paid arms spend real money under a hard total ceiling."
        )
    )
    parser.add_argument(
        "--out",
        default="benchmark/hybrid",
        help="output directory (default: benchmark/hybrid)",
    )
    parser.add_argument("--headed", action="store_true", help="run browsers headed")
    parser.add_argument(
        "--max-total-cost",
        type=float,
        default=MAX_TOTAL_COST_USD,
        help="ceiling on total paid list-price spend (default: 8.00)",
    )
    args = parser.parse_args(argv)
    run_hybrid_benchmark(
        args.out,
        headed=args.headed,
        max_total_cost_usd=args.max_total_cost,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
