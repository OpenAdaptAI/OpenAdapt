"""Synthetic trace-stream harness: a MockMed skill drifting over "time".

To exercise the learn/promote loop end-to-end without a live app, this module

1. builds a base MockMed skill program (``add-patient-note`` from
   ``docs/design/WORKFLOW_PROGRAM_IR.md`` section 2.5: sign in -> open the
   patient record -> new encounter -> type note -> SAVE), and
2. emits a STREAM of :class:`~openadapt_flow.learning.trace.ExecutionTrace`s over
   a logical time axis with INJECTED DRIFT at a chosen trace index -- e.g. a new
   consent dialog starts appearing mid-stream, so the active program stops
   reproducing the new successful traces and the loop must detect the novelty,
   induce a revision, validate it, and promote it.

The reference inducer that revises the program,
:class:`~openadapt_flow.learning.induce.StructuralDiffInducer`, ships in the
engine; this stream is a test and benchmark fixture and lives in the source
checkout (``packages/flow/benchmark/harness``), outside the wheel.

All deterministic, ``$0``, no model calls.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from openadapt_flow.ir import (
    ActionKind,
    Guard,
    ProgramGraph,
    State,
    StateKind,
)
from openadapt_flow.learning.induce import StructuralDiffInducer
from openadapt_flow.learning.induce import _action_state as _induce_action_state
from openadapt_flow.learning.trace import ExecutionTrace, TraceStep
from openadapt_flow.runtime.effects.effect import Effect, EffectKind

__all__ = [
    "Drift",
    "StructuralDiffInducer",
    "generate_stream",
    "mockmed_base_program",
]

# -- the MockMed skill's canonical intents ------------------------------------

INTENT_LOGIN = "Sign in"
INTENT_OPEN = "Open patient record"
INTENT_ENCOUNTER = "Start new encounter"
INTENT_NOTE = "Enter note"
INTENT_SAVE = "Save encounter"
INTENT_CONSENT = "Acknowledge consent notice"

#: The screen fact that co-occurs with the injected consent dialog -- the text a
#: ``TEXT_PRESENT`` guard keys on.
FACT_CONSENT = "Consent Required"

#: A representative identity band for the patient row (name + DOB) -- the
#: evidence the pre-click identity gate keys on; makes the "open patient" step
#: identity-ARMED so an identity regression is meaningful.
PATIENT_BAND = "Belford, Phil DOB 1990-01-01"


def _action_state(
    sid: str,
    intent: str,
    target: str,
    *,
    action: ActionKind = ActionKind.CLICK,
    armed: bool = False,
    risk: str = "reversible",
    effects: Optional[list[Effect]] = None,
    guard: Optional[Guard] = None,
    text: Optional[str] = None,
) -> State:
    return _induce_action_state(
        sid,
        intent,
        target,
        action=action,
        armed=armed,
        risk=risk,
        effects=effects,
        guard=guard,
        text=text,
        context_text=PATIENT_BAND,
    )


def mockmed_base_program() -> ProgramGraph:
    """The base ``add-patient-note`` program (no consent dialog).

    A straight-line state machine: sign in -> open patient (identity-armed) ->
    new encounter -> type note -> SAVE (irreversible, with a ``record_written``
    system-of-record effect) -> success. The identity band + effect + risk on
    the sensitive steps are what a rigged revision could weaken, and what the
    regression gate protects.
    """
    save_effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"type": "encounter", "patient": "p1"},
        expected_count=1,
        risk="irreversible",
        probe="encounter record written for patient",
    )
    states = {
        "s_login": _action_state("s_login", INTENT_LOGIN, "s_open"),
        "s_open": _action_state("s_open", INTENT_OPEN, "s_encounter", armed=True),
        "s_encounter": _action_state("s_encounter", INTENT_ENCOUNTER, "s_note"),
        "s_note": _action_state(
            "s_note", INTENT_NOTE, "s_save", action=ActionKind.TYPE, text="Follow-up"
        ),
        "s_save": _action_state(
            "s_save",
            INTENT_SAVE,
            "__end__",
            risk="irreversible",
            effects=[save_effect],
        ),
        "__end__": State(id="__end__", kind=StateKind.TERMINAL, outcome="success"),
    }
    return ProgramGraph(entry="s_login", states=states)


# -- the drift stream ---------------------------------------------------------


class Drift(str, Enum):
    """The injected drift/variant a stream can carry from trace K onward."""

    NONE = "none"
    #: A new consent dialog appears after sign-in (a new OPTIONAL step + branch).
    CONSENT_DIALOG = "consent_dialog"
    #: The note field is renamed (the observed intent changes) -- a structural
    #: change the additive reference inducer CANNOT cover (used to show the loop
    #: correctly refuses an inadequate revision).
    FIELD_RENAME = "field_rename"


def _baseline_steps() -> list[TraceStep]:
    return [
        TraceStep(intent=INTENT_LOGIN),
        TraceStep(intent=INTENT_OPEN, identity=PATIENT_BAND),
        TraceStep(intent=INTENT_ENCOUNTER),
        TraceStep(intent=INTENT_NOTE, action=ActionKind.TYPE),
        TraceStep(intent=INTENT_SAVE),
    ]


def _drifted_steps(drift: Drift) -> tuple[list[TraceStep], dict[str, bool]]:
    steps = _baseline_steps()
    facts: dict[str, bool] = {}
    if drift is Drift.CONSENT_DIALOG:
        # The consent dialog appears right after sign-in.
        steps.insert(1, TraceStep(intent=INTENT_CONSENT))
        facts[FACT_CONSENT] = True
    elif drift is Drift.FIELD_RENAME:
        for s in steps:
            if s.intent == INTENT_NOTE:
                s.intent = "Enter clinical note"
    return steps, facts


def generate_stream(
    *,
    n_baseline: int = 6,
    n_drift: int = 6,
    drift: Drift = Drift.CONSENT_DIALOG,
    n_failures: int = 0,
    prefix: str = "mockmed",
) -> list[ExecutionTrace]:
    """Emit a deterministic stream of traces with drift injected at trace
    ``n_baseline``.

    The first ``n_baseline`` traces are clean successful runs of the base skill;
    from index ``n_baseline`` the injected ``drift`` appears in every successful
    run. ``n_failures`` failure traces (noise) are appended -- runs that halted
    partway with no novel successful structure, so a batch of them alone must NOT
    trigger a revision.
    """
    traces: list[ExecutionTrace] = []
    t = 0
    for _ in range(n_baseline):
        traces.append(
            ExecutionTrace(
                trace_id=f"{prefix}-{t:04d}",
                t=t,
                outcome="success",
                steps=_baseline_steps(),
                params={"encounter_type": "Triage"},
            )
        )
        t += 1
    for _ in range(n_drift):
        steps, facts = _drifted_steps(drift)
        traces.append(
            ExecutionTrace(
                trace_id=f"{prefix}-{t:04d}",
                t=t,
                outcome="success",
                steps=steps,
                facts=facts,
                params={"encounter_type": "Triage"},
            )
        )
        t += 1
    for _ in range(n_failures):
        # A run that halted after opening the patient (e.g. a transient
        # resolution failure): truncated, no novel successful structure.
        traces.append(
            ExecutionTrace(
                trace_id=f"{prefix}-{t:04d}",
                t=t,
                outcome="failure",
                steps=_baseline_steps()[:2],
                failure_reason="transient resolution failure on 'Start new encounter'",
                params={"encounter_type": "Triage"},
            )
        )
        t += 1
    return traces
