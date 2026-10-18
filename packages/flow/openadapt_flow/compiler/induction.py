"""Multi-trace induction: infer a parameterized PROGRAM from several demos.

One demonstration is *evidence*, not *specification* (RFC
``docs/design/WORKFLOW_PROGRAM_IR.md`` §1, §3): the same single trace is
consistent with many distinct programs. The whole programming-by-demonstration
lineage a demonstration compiler descends from -- Rousillon/Helena, WebRobot,
Skill-DisCo, PROLEX -- closes that gap with an **induction loop** that
generalizes from *multiple* demonstrations of the same task. This module
implements RFC §3 step [4] (multi-trace induction) + step [5] (held-out
validation / quarantine): it turns ``list[Workflow | recording-dir]`` into a
:class:`~openadapt_flow.ir.ProgramGraph` -- the Phase-2 state machine -- *or an
honest refusal*.

What it infers, and how (all deterministic + structural -- ZERO model calls):

* **Parameters.** A value that VARIES across traces at the same aligned
  position is a typed :class:`~openadapt_flow.ir.ParamSpec`; a value that is
  CONSTANT across traces stays a baked literal (WebRobot-style value
  speculation, made *determinate* by cross-trace evidence rather than guessed).
* **Loops.** A repeated aligned sub-sequence whose repetition count DIFFERS
  across traces (a worklist of length 2 vs 3) is a
  :class:`~openadapt_flow.ir.LoopSpec` over an inferred
  :class:`~openadapt_flow.ir.Relation`, its body the repeated subflow
  (Rousillon/Helena "for every row ...").

  **LIMITS (loop inference is a PROTOTYPE -- keep the claim honest).** The only
  loop shape detected is a *consecutive* repeated sub-sequence
  (:func:`_reduce_trace`). It does NOT recognize a **search -> process ->
  return** loop (navigate away and back each iteration), **pagination** (a
  "next page" step between bodies), or a **per-row conditional body** (the body
  differs row to row). When the repeated body contains a CONSEQUENTIAL
  (irreversible / effect-bearing) action, whether those repeats are a
  data-driven worklist or a fixed unrolled sequence cannot be certified from
  trace *shape* alone -- inducing the wrong loop would repeat an irreversible
  action -- so induction emits an :class:`Uncertainty` (``ambiguous_loop``)
  and REFUSES rather than emit a possibly-wrong loop.
* **Branches.** A step present/divergent in some traces but not others UNDER A
  DETECTABLE CONDITION is a ``branch`` state with guarded transitions -- the
  guard is *proposed and flagged for confirmation* (Skill-DisCo: divergences
  localize the branch automatically). When the branch's guarded action is
  CONSEQUENTIAL the proposed guard is UNCONFIRMED evidence, so induction ALSO
  emits an :class:`Uncertainty` (operator confirmation required) and leaves
  ``certified=False`` -- a flagged proposal never auto-certifies (RFC §3 [5]).
* **Optional steps.** Present in some, absent in others, with NO derivable
  condition -> an optional/guarded step that SKIPS when its own target is
  absent. But "absent in some traces" must NOT silently become "optional/skip"
  for a CONSEQUENTIAL step: skipping (or wrongly running) an irreversible
  action is not a safe default, so such a step becomes an :class:`Uncertainty`
  (a question), not a silent skip.

**Refuse rather than guess** (RFC §3 [5]; mirrors ``runtime.identity`` and
``compiler.disambiguation``). When traces CONTRADICT or intent stays
underdetermined -- a divergent branch with no detectable condition, an
irreconcilable ordering -- induction does NOT fabricate a program. It marks the
point ``underdetermined`` with the specific ambiguity, routes it to the
disambiguation flow (:mod:`openadapt_flow.compiler.disambiguation`), leaves
``certified=False``, and does not emit a program. A wrong branch on an
irreversible node is the failure class the whole repo is organized to avoid.

Each Uncertainty also carries a **record-next** worklist item: a one-line
operator instruction naming the missing demonstration ("Record the path where
Save is disabled.", "Record one more worklist row.", "Record the consent-dialog
Skip path."). The instruction is a scenario, never an invented click path, and
the default path makes ZERO model calls. When a program *is* emitted, uncovered
branches the traces never showed ("never saw Save disabled") remain
non-blocking notes -- not certified guards.

**The compile-time model only PROPOSES.** An optional :class:`Proposer` (the
compile-time StepAnnotator lives behind this interface) may propose an
interpretation for an ambiguous point, but every proposal is recorded in
``proposed`` / attached to the uncertainty and is NEVER silently trusted: a
proposal cannot flip an ``underdetermined`` point to certified. A Proposer may
also attach a *hint class* on a record-next item; that hint is flagged and
cannot certify. Deterministic structural inference is the core; the model is
advisory and flagged.

Touch-points (kept minimal per the stacking constraint on PR #79):

* Reuses the Phase-2 IR (``ProgramGraph`` / ``State`` / ``Transition`` /
  ``LoopSpec`` / ``Relation``) and Phase-1 ``ParamSpec`` / ``Predicate`` /
  ``Guard`` VERBATIM -- adds NO new IR fields.
* Reuses ``compiler.disambiguation``'s question model to route uncertainties to
  the SAME ask-don't-guess flow (#74).
* ``compiler/compile.py`` is UNCHANGED -- induction runs as a pass OVER
  already-compiled workflows (``compile_recording`` is the single-trace
  bootstrap, RFC §3 [1]); a recording-dir trace is compiled through it.
* The emitted program replays through the EXISTING Phase-2 interpreter
  (``runtime.replayer``) with zero changes -- proven by the round-trip test.
"""

from __future__ import annotations

import tempfile
import warnings
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, Union, runtime_checkable

from pydantic import BaseModel, Field

from openadapt_flow.compiler.disambiguation import (
    AmbiguityKind,
    DisambiguationQuestion,
    OptionEffect,
    QuestionOption,
)
from openadapt_flow.ir import (
    ActionKind,
    Guard,
    GuardSource,
    LoopSpec,
    ParamKind,
    ParamSpec,
    Predicate,
    PredicateKind,
    ProgramGraph,
    Relation,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.authorization import runtime_param_text

TraceInput = Union[Workflow, str, Path]

# A typed value shorter than this (stripped) is too weak to speculate on as a
# parameter -- mirrors disambiguation.MIN_PARAM_VALUE_CHARS.
MIN_PARAM_VALUE_CHARS = 1


# ===========================================================================
# Compile-time model, behind an interface: it PROPOSES, it never decides.
# ===========================================================================


class Proposal(BaseModel):
    """A compile-time-model suggestion for an under-specified point.

    ADVISORY ONLY. A proposal is surfaced in :attr:`InductionResult.proposed`
    (and attached to any uncertainty it concerns) so a reviewer sees it, but it
    NEVER silently changes the deterministic structural decision and NEVER flips
    an ``underdetermined`` point to certified (``trusted`` is always False --
    the field exists to make that contract explicit and auditable).
    """

    target: str = Field(description="Column / state id the proposal concerns")
    kind: str = Field(description="'guard' | 'param' | 'label' | 'hint'")
    content: str = Field(description="Human-readable proposed interpretation")
    source: str = "annotator"
    trusted: bool = Field(
        default=False,
        description="Always False: proposals are flagged, never auto-applied.",
    )


@runtime_checkable
class Proposer(Protocol):
    """Compile-time interpretation proposer (the #78 StepAnnotator fits here).

    Called at MOST once per ambiguous/branch point at COMPILE time. Any
    implementation that reaches a model does so here, once -- never at run time
    (the runtime's audited $0 / 0-call property is preserved). Tests pass a
    deterministic fake; a real annotator may consult a VLM. Either way the
    return value is advisory (see :class:`Proposal`).
    """

    def propose(self, target: str, kind: str, context: dict[str, Any]) -> Optional[str]:
        """Return a human-readable proposed interpretation, or None to abstain."""
        ...


# ===========================================================================
# Result model
# ===========================================================================


class RecordNext(BaseModel):
    """One missing demonstration the operator should record next.

    A worklist item, not a guessed click path. ``code`` is the stable
    uncertainty (or uncovered-note) kind tests pin; ``instruction`` is a
    one-line scenario. An optional ``hint_class`` from a :class:`Proposer` is
    flagged and never certifies. ``blocking`` is False for uncovered-branch
    notes on an emitted program (not certified guards).
    """

    code: str = Field(description="Uncertainty kind or uncovered-note code")
    instruction: str = Field(
        description="One-line operator instruction naming the missing demo."
    )
    location: str = ""
    blocking: bool = True
    hint_class: Optional[str] = Field(
        default=None,
        description="Advisory proposer hint class -- flagged, cannot certify.",
    )


class Uncertainty(BaseModel):
    """A point where intent stays underdetermined -- the reason induction
    REFUSES to emit (RFC §3 [5] quarantine). Routed to the disambiguation flow
    via :attr:`question`. ``record_next`` is the one-line demonstration to
    record; it is a scenario, never an invented click path."""

    kind: str = Field(
        description=(
            "'ambiguous_branch' | 'ambiguous_loop' | 'unconfirmed_branch' | "
            "'unconfirmed_optional' | 'underdetermined_order' | "
            "'ambiguous_selection' | 'alignment_failure'"
        )
    )
    location: str = Field(description="Column / position the ambiguity is at")
    detail: str
    consequential: bool = Field(
        default=True,
        description="True when a wrong resolution risks an irreversible action.",
    )
    question: Optional[DisambiguationQuestion] = Field(
        default=None,
        description="The grounded question routed to compiler.disambiguation.",
    )
    proposal: Optional[Proposal] = Field(
        default=None,
        description="A compile-time-model suggestion -- flagged, NOT trusted.",
    )
    record_next: str = Field(
        default="",
        description="One-line operator instruction: what demonstration to record next.",
    )
    hint_class: Optional[str] = Field(
        default=None,
        description="Advisory proposer hint class -- flagged, cannot certify.",
    )


class ColumnDecision(BaseModel):
    """The induced interpretation of one aligned column, kept for the audit
    trail AND for structural trace-shape scoring
    (:func:`structural_trace_coverage`)."""

    index: int
    kind: str  # literal | param | loop | branch | optional | divergent
    align_sig: str = Field(description="Stringified alignment signature")
    field: str = ""
    literal_value: Optional[str] = None  # kind == literal
    param_name: Optional[str] = None  # kind == param / loop
    present_in: list[int] = Field(default_factory=list)  # trace indices
    counts: list[int] = Field(
        default_factory=list, description="Per-trace loop iteration counts."
    )
    note: str = ""


class ProbeRun(BaseModel):
    """One replay of one interpretation in one recording's context.

    Written for every run ``induce --probe-on`` makes on the test copy,
    whether or not it decided anything (:mod:`openadapt_flow.compiler.probing`).
    """

    location: str = Field(description="The underdetermined point probed")
    interpretation: str = Field(description="'incidental' | 'conditional'")
    context: str = Field(description="Label of the recording whose context ran")
    runtime_signal: str = Field(
        description="'completed' | 'halted_before_effect' | 'refused' | "
        "'failed_platform', from the replay"
    )
    outcome: str = Field(
        description="The serve-reward judge's outcome for the read-back"
    )
    produced_recorded_effect: bool = Field(
        description="True only when the judge says VERIFIED."
    )
    conclusive: bool = Field(
        description="False when the read-back could not decide (store "
        "unreachable, effect uncertain, platform failure)."
    )
    reason: str = ""
    model_calls: int = 0


class ProbeVerdict(BaseModel):
    """What probing decided about one underdetermined point."""

    location: str
    kind: str = Field(description="The uncertainty kind that was probed")
    interpretations: dict[str, str] = Field(
        default_factory=dict,
        description="interpretation -> 'passed' | 'failed' | 'inconclusive' "
        "| 'not_run'",
    )
    certified: Optional[str] = Field(
        default=None,
        description="The one interpretation that produced the recorded effect "
        "in every context while every other one conclusively failed; None "
        "when the point stays refused.",
    )
    detail: str = ""


class NarratedGuard(BaseModel):
    """A guard a spoken sentence suggested for one branch point.

    It starts as a flagged proposal. ``status`` is ``certified`` only when
    every recording agrees with it (``certified_by="recordings_agree"``),
    probing on a declared test copy confirmed it (``"probe"``), or a person
    accepted it (``"person"``). A recording that contradicts it keeps the point
    refused (``status="contradicted"``); a person cannot accept it then.
    """

    location: str = Field(description="opt_<column> or divergence_<column>")
    guard_id: str = Field(
        default="",
        description="Digest of the guard; accept it as <location>@<guard_id>",
    )
    sentence: str = Field(description="The scrubbed sentence")
    recording: int = Field(description="Index of the recording that said it")
    step_id: Optional[str] = None
    guard: Predicate = Field(description="Run the step (or arm) when this holds")
    rule: str = Field(description="The guard in plain words")
    source: str = "narration"
    status: str = Field(description="'certified' | 'proposed' | 'contradicted'")
    certified_by: Optional[str] = None
    accepted_by: Optional[str] = None
    agree: list[int] = Field(default_factory=list)
    contradict: list[int] = Field(default_factory=list)
    unknown: list[int] = Field(
        default_factory=list,
        description="Recordings where the guard could not be checked",
    )
    trace_params: dict[int, dict[str, str]] = Field(
        default_factory=dict,
        description="Per recording, the run parameter a param guard compares",
    )
    detail: str = ""


class InductionResult(BaseModel):
    """The induced program (or an honest refusal) plus the full audit trail."""

    n_traces: int
    program: Optional[ProgramGraph] = None
    workflow: Optional[Workflow] = Field(
        default=None,
        description="A replayable Workflow carrying program/subflows/"
        "param_specs/data_sources (None when quarantined).",
    )
    param_specs: dict[str, ParamSpec] = Field(default_factory=dict)
    column_decisions: list[ColumnDecision] = Field(default_factory=list)
    inferred: list[str] = Field(
        default_factory=list,
        description="What was inferred DETERMINISTICALLY (params/loops/opt).",
    )
    proposed: list[Proposal] = Field(
        default_factory=list,
        description="Compile-time-model suggestions -- flagged, never trusted.",
    )
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    record_next: list[RecordNext] = Field(
        default_factory=list,
        description=(
            "Worklist of missing demonstrations (blocking uncertainties) and "
            "uncovered-branch notes on an emitted program (non-blocking)."
        ),
    )
    narrated_guards: list[NarratedGuard] = Field(
        default_factory=list,
        description="Guards spoken narration suggested, with how each was decided.",
    )
    probe_target: Optional[str] = Field(
        default=None,
        description="Name of the declared test copy probed (None: no probing).",
    )
    probe_runs: list[ProbeRun] = Field(
        default_factory=list,
        description="Every probe run, in the order it ran.",
    )
    probe_verdicts: list[ProbeVerdict] = Field(
        default_factory=list,
        description="One verdict per underdetermined point probing looked at.",
    )

    @property
    def underdetermined(self) -> bool:
        return bool(self.uncertainties)

    @property
    def certified(self) -> bool:
        """False iff any point stays underdetermined (refuse rather than
        guess) OR no program was emitted. A flagged proposal does NOT certify."""
        return self.program is not None and not self.uncertainties

    def render(self) -> str:
        lines = [
            f"Induction over {self.n_traces} trace(s): "
            f"{len(self.inferred)} inferred, {len(self.proposed)} proposed "
            f"(flagged), {len(self.uncertainties)} underdetermined."
        ]
        for line in self.inferred:
            lines.append(f"  [inferred]  {line}")
        for p in self.proposed:
            lines.append(f"  [proposed]  {p.target}: {p.content}  (NOT trusted)")
        for u in self.uncertainties:
            lines.append(f"  [REFUSED]   {u.location}: {u.detail}")
        for ng in self.narrated_guards:
            if ng.status == "certified":
                how = {
                    "recordings_agree": "every recording agrees",
                    "probe": "confirmed on the test copy",
                    "person": f"accepted by {ng.accepted_by}",
                }.get(ng.certified_by or "", ng.certified_by or "")
                verdict = f"CERTIFIED ({how})"
            elif ng.status == "contradicted":
                verdict = "REFUSED (recording " + ", ".join(
                    str(t) for t in ng.contradict
                ) + " contradicts it)"
            else:
                verdict = "proposed, NOT trusted"
            lines.append(
                f"  [narrated]  {ng.location}@{ng.guard_id}: {ng.sentence!r} -> "
                f"{ng.rule} -- {verdict}"
            )
        if self.probe_target is not None:
            lines.append(
                f"probes on test copy {self.probe_target!r}: "
                f"{len(self.probe_runs)} run(s)"
            )
            for run in self.probe_runs:
                lines.append(
                    f"  [probe]     {run.location} {run.interpretation} in "
                    f"{run.context}: {run.outcome} ({run.runtime_signal})"
                    + (f" -- {run.reason}" if run.reason else "")
                )
            for pv in self.probe_verdicts:
                shown = ", ".join(f"{k}={v}" for k, v in pv.interpretations.items())
                head = f"certified {pv.certified}" if pv.certified else "still refused"
                lines.append(
                    f"  [verdict]   {pv.location}: {head} [{shown}]"
                    + (f" -- {pv.detail}" if pv.detail else "")
                )
        verdict = "CERTIFIED" if self.certified else "NOT CERTIFIED"
        lines.append(
            f"Certification: {verdict}"
            + ("" if self.certified else " -- resolve the point(s) above.")
        )
        if self.record_next:
            lines.append("")
            lines.append("record-next:")
            for item in self.record_next:
                prefix = "note " if not item.blocking else ""
                line = f"  {prefix}{item.code}: {item.instruction}"
                if item.hint_class:
                    line += f"  [hint: {item.hint_class} (NOT trusted)]"
                lines.append(line)
        return "\n".join(lines)


class HeldOutValidation(BaseModel):
    """Leave-one-out held-out STRUCTURAL check (RFC §3 [5]): infer from N-1
    traces, score how well the induced program's SHAPE covers the held-out
    trace (:func:`structural_trace_coverage`).

    This is a structural trace-shape signal ONLY -- it executes nothing and
    verifies no effect / identity, so a high ``mean`` is NOT certification and
    must not be reported as behavioral held-out validation."""

    per_trace: list[float] = Field(default_factory=list)
    mean: float = 0.0
    n_traces: int = 0

    def render(self) -> str:
        scores = ", ".join(f"{s:.2f}" for s in self.per_trace)
        return (
            f"Held-out STRUCTURAL coverage ({self.n_traces} folds): "
            f"mean={self.mean:.2f} [{scores}] "
            "(trace-shape only -- NOT behavioral validation)"
        )


# ===========================================================================
# Trace normalization + signatures
# ===========================================================================


def _as_workflow(trace: TraceInput) -> Workflow:
    """Normalize a trace to a linear ``Workflow``. A recording DIRECTORY is
    compiled through the single-trace bootstrap ``compile_recording`` (RFC §3
    [1]); a ``Workflow`` is used as-is."""
    if isinstance(trace, Workflow):
        return trace
    from openadapt_flow.compiler.compile import compile_recording

    recording = Path(trace)
    with tempfile.TemporaryDirectory(prefix="induce-boot-") as tmp:
        return compile_recording(recording, Path(tmp) / "bundle", name=recording.name)


_LEADING_VERBS = frozenset(
    {"type", "enter", "input", "click", "press", "select", "set", "fill"}
)


def _norm_intent(step: Step) -> str:
    """A value-free field label from a step's intent (lowercased, digits
    dropped so a per-run value in the intent does not fragment alignment, and a
    leading action verb dropped so 'type patient' keys as 'patient')."""
    text = "".join(c for c in step.intent.lower() if not c.isdigit())
    words = text.split()
    if words and words[0] in _LEADING_VERBS:
        words = words[1:]
    return " ".join(words) or " ".join(text.split())


def _field_key(step: Step) -> str:
    """A stable, VALUE-FREE identity for the field/target a step acts on -- the
    dimension alignment matches on (so the SAME field with a DIFFERENT value
    aligns, revealing a parameter rather than a structural change)."""
    if step.action is ActionKind.KEY:
        # Distinct keys are distinct control-flow (Enter vs Escape), so the key
        # IS part of the field identity here.
        return f"key:{step.key}"
    if step.param:
        return step.param
    if step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
        # A CLICK/selection's field identity is its PURPOSE, not the entity it
        # landed on: the anchor's ocr_text / structural name is the VALUE that
        # VARIES (which patient row), so keying on it would make the SAME
        # selection step in two traces mis-align as a structural change instead
        # of revealing a selection parameter. Key on the value-free structural
        # ROLE (a11y/DOM control type) when present, else the value-free intent.
        struct = step.anchor.structural if step.anchor is not None else None
        if struct is not None and struct.role:
            return f"select:{struct.role}"
        return _norm_intent(step)
    if step.anchor is not None and step.anchor.ocr_text:
        return step.anchor.ocr_text
    return _norm_intent(step)


def _sig(step: Step) -> tuple[str, str]:
    return (step.action.value, _field_key(step))


def _value(step: Step) -> Optional[str]:
    """The per-run VALUE a step carries (the thing that varies for a param).

    For a CLICK/selection this is the SELECTED ENTITY's identity (which row was
    clicked) -- the structural name when present (highest fidelity), else the
    anchor's OCR text -- because that is the dimension a selection parameter
    varies over."""
    if step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION):
        return step.text if step.text is not None else step.param
    if step.action is ActionKind.KEY:
        return step.key
    if step.anchor is not None:
        struct = step.anchor.structural
        if struct is not None and struct.name:
            return struct.name
        return step.anchor.ocr_text
    return None


# Dialog detection is shared with disambiguation so induction and interactive
# disambiguation agree on what "an optional popup" looks like.
from openadapt_flow.compiler.disambiguation import (  # noqa: E402
    _dialog_text,
    _looks_like_dialog,
)

# ===========================================================================
# Reduce a trace to tokens (collapse consecutive repeats into loop candidates)
# ===========================================================================


class _SingleTok:
    kind = "single"

    def __init__(self, step: Step):
        self.step = step
        self.align_sig: tuple = _sig(step)

    @property
    def value(self) -> Optional[str]:
        return _value(self.step)


class _LoopTok:
    kind = "loop"

    def __init__(self, body_steps: list[Step], iterations: list[list[Step]]):
        self.body_steps = body_steps
        self.iterations = iterations  # per-iteration list of steps
        body_sig = tuple(_sig(s) for s in body_steps)
        self.align_sig = ("__loop__", body_sig)

    @property
    def count(self) -> int:
        return len(self.iterations)

    def rows(self, field: str) -> list[dict[str, str]]:
        """Per-iteration binding of ``field`` -> value (single-column body)."""
        out: list[dict[str, str]] = []
        for it in self.iterations:
            val = _value(it[0]) if it else None
            out.append({field: val if val is not None else ""})
        return out


def _reduce_trace(steps: list[Step]) -> list:
    """Collapse maximal CONSECUTIVE repeated sub-sequences into ``_LoopTok``s;
    everything else stays a ``_SingleTok``. Smallest repeating block wins (the
    tightest loop body). Model-free and purely structural.

    LIMITS (loop detection is a PROTOTYPE). This recognizes ONLY a body that
    repeats BACK-TO-BACK in one trace. It does NOT recognize:
      * search -> process -> return loops (each iteration navigates away and
        back, so the body is not literally consecutive);
      * pagination (a "next page" step separates the per-page bodies);
      * per-row CONDITIONAL bodies (the body differs from row to row).
    A repeat is also, on shape alone, indistinguishable from a fixed unrolled
    sequence of distinct steps. The caller (:func:`_emit_loop`) therefore
    REFUSES (emits an ``ambiguous_loop`` Uncertainty) rather than emit a loop
    when the repeated body is CONSEQUENTIAL, where guessing wrong would repeat
    an irreversible action."""
    sigs = [_sig(s) for s in steps]
    n = len(steps)
    toks: list = []
    i = 0
    while i < n:
        found: Optional[tuple[int, int]] = None
        max_len = (n - i) // 2
        for length in range(1, max_len + 1):
            block = sigs[i : i + length]
            reps = 1
            while sigs[i + reps * length : i + reps * length + length] == block:
                reps += 1
            if reps >= 2:
                found = (length, reps)
                break
        if found is not None:
            length, reps = found
            body_steps = steps[i : i + length]
            iterations = [
                steps[i + r * length : i + r * length + length] for r in range(reps)
            ]
            toks.append(_LoopTok(body_steps, iterations))
            i += length * reps
        else:
            toks.append(_SingleTok(steps[i]))
            i += 1
    return toks


# ===========================================================================
# Multiple-trace alignment (incremental LCS merge of reduced token sequences)
# ===========================================================================


class _Column:
    """One aligned position across traces. ``tokens[t]`` is trace ``t``'s token
    at this position, or absent when the trace does not have this column."""

    def __init__(self, align_sig, kind: str):
        self.align_sig = align_sig
        self.kind = kind  # "single" | "loop"
        self.tokens: dict[int, Any] = {}
        # For a "replace" divergence: the id of the mutually-exclusive column
        # this one is paired against (same position, different content).
        self.divergent_group: Optional[int] = None


def _align(reduced: list[list]) -> list[_Column]:
    """Align the reduced token sequences of every trace into ordered columns.

    Incremental merge: start from trace 0's columns, then fold each subsequent
    trace in with a ``difflib`` alignment over the token ALIGN-SIGNATURES.
    ``equal`` blocks attach to existing columns; ``insert`` blocks add columns
    present only where seen; ``delete`` blocks leave columns absent in the new
    trace; ``replace`` blocks are recorded as mutually-exclusive DIVERGENCES
    (branch-or-contradiction candidates).
    """
    if not reduced:
        return []
    columns: list[_Column] = []
    for tok in reduced[0]:
        col = _Column(tok.align_sig, tok.kind)
        col.tokens[0] = tok
        columns.append(col)

    div_group = 0
    for t in range(1, len(reduced)):
        seq = reduced[t]
        a = [c.align_sig for c in columns]
        b = [tok.align_sig for tok in seq]
        sm = SequenceMatcher(a=a, b=b, autojunk=False)
        merged: list[_Column] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    col = columns[i1 + k]
                    col.tokens[t] = seq[j1 + k]
                    merged.append(col)
            elif tag == "delete":
                # In consensus, absent from this trace -> optional so far.
                merged.extend(columns[i1:i2])
            elif tag == "insert":
                # New in this trace only.
                for j in range(j1, j2):
                    tok = seq[j]
                    col = _Column(tok.align_sig, tok.kind)
                    col.tokens[t] = tok
                    merged.append(col)
            elif tag == "replace":
                # Mutually-exclusive divergence: keep BOTH sides, tagged as one
                # group, so interpretation can decide branch vs contradiction.
                for col in columns[i1:i2]:
                    col.divergent_group = div_group
                    merged.append(col)
                for j in range(j1, j2):
                    tok = seq[j]
                    col = _Column(tok.align_sig, tok.kind)
                    col.tokens[t] = tok
                    col.divergent_group = div_group
                    merged.append(col)
                div_group += 1
        columns = merged
    _mark_order_splits(columns, len(reduced))
    return columns


def _mark_order_splits(columns: list[_Column], n: int) -> None:
    """Tag complementary same-signature columns as one order-split group.

    SequenceMatcher often reports a reordering as delete+insert of the same
    align-sig rather than a replace. Those columns must not become optional
    skips -- they are ``underdetermined_order``.
    """
    by_sig: dict[Any, list[_Column]] = {}
    for col in columns:
        if col.divergent_group is not None:
            continue
        by_sig.setdefault(col.align_sig, []).append(col)

    split_cols: list[_Column] = []
    for cols in by_sig.values():
        if len(cols) < 2:
            continue
        union: set[int] = set()
        overlap = False
        for col in cols:
            present = set(col.tokens)
            if union & present:
                overlap = True
            union |= present
        if not overlap and len(union) == n:
            split_cols.extend(cols)

    if not split_cols:
        return
    used = {c.divergent_group for c in columns if c.divergent_group is not None}
    grp = 0
    while grp in used:
        grp += 1
    for col in split_cols:
        col.divergent_group = grp


# ===========================================================================
# Interpretation: aligned columns -> ProgramGraph (or refusal)
# ===========================================================================


def _is_irreversible(col: _Column) -> bool:
    return any(
        tok.kind == "single" and tok.step.risk == "irreversible"
        for tok in col.tokens.values()
    )


def _step_consequential(step: Step) -> bool:
    """True when getting this step wrong risks an IRREVERSIBLE / system-of-record
    effect -- the class induction must never certify on a mere proposal. A step
    is consequential when it is ``risk="irreversible"`` OR declares system-of-
    record ``effects`` (a transactional write). Reversible, effect-free steps
    are safe to auto-resolve (a wrong guess is recoverable)."""
    return step.risk == "irreversible" or bool(step.effects)


# Write-shaped control stems used only to NAME an observed control in a
# record-next instruction ("Record the path where Save is disabled."). Never
# used to invent a click path or a certified guard.
_WRITE_CONTROL_STEMS: tuple[str, ...] = ("save", "submit", "approve", "confirm")


def _write_control_label(step: Step) -> Optional[str]:
    """Observed Save/Submit-shaped label on a demonstrated CLICK, or None.

    Uses the step's own OCR / field / intent text. Does not invent a control
    the traces never showed.
    """
    if step.action not in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
        return None
    candidates = [
        step.anchor.ocr_text if step.anchor is not None else None,
        _field_key(step),
        step.intent,
    ]
    for text in candidates:
        if not text:
            continue
        low = text.lower()
        for stem in _WRITE_CONTROL_STEMS:
            if stem in low:
                return stem.capitalize()
    return None


def _instruction_for(
    kind: str,
    *,
    field: str = "",
    labels: Optional[list[str]] = None,
    dialog_text: str = "",
    write_label: str = "",
) -> str:
    """One-line operator instruction naming the missing demonstration.

    A scenario, never an invented click path. Copy is for operators; tests pin
    the uncertainty ``code``, not this English.
    """
    labels = labels or []
    if kind == "ambiguous_loop":
        return "Record one more worklist row."
    if kind == "unconfirmed_branch":
        name = dialog_text or field or "the dialog"
        return f"Record the {name} Skip path."
    if kind == "unconfirmed_optional":
        if write_label:
            return f"Record the path where {write_label} is disabled."
        name = field or "the step"
        return f"Record the path where {name} is absent."
    if kind == "underdetermined_order":
        return "Record the steps in the intended order."
    if kind == "ambiguous_branch":
        if len(labels) >= 2:
            shown = " vs ".join(labels[:4])
            return f"Record each of {shown} under a detectable condition."
        return "Record each divergent path under a detectable condition."
    if kind == "ambiguous_selection":
        name = field or "the selection"
        return f"Record how '{name}' is identified each run."
    if kind == "alignment_failure":
        return "Record traces of the same task."
    return "Record a demonstration that resolves this uncertainty."


def _add_uncertainty(
    result: InductionResult,
    *,
    kind: str,
    location: str,
    detail: str,
    consequential: bool = True,
    question: Optional[DisambiguationQuestion] = None,
    proposal: Optional[Proposal] = None,
    field: str = "",
    labels: Optional[list[str]] = None,
    dialog_text: str = "",
    write_label: str = "",
    propose: Optional[Proposer] = None,
) -> Uncertainty:
    """Append an Uncertainty with a record-next instruction.

    If a :class:`Proposer` is supplied it may attach a hint class; that hint
    is flagged, never trusted, and cannot certify. The default path (no
    proposer) makes ZERO model calls.
    """
    instruction = _instruction_for(
        kind,
        field=field,
        labels=labels,
        dialog_text=dialog_text,
        write_label=write_label,
    )
    hint_class = None
    if propose is not None:
        hint = _maybe_propose(
            propose,
            location,
            "hint",
            {
                "kind": kind,
                "field": field,
                "labels": list(labels or []),
                "dialog": dialog_text,
            },
            result,
        )
        if hint:
            hint_class = hint
            hint_proposal = Proposal(
                target=location, kind="hint", content=hint, trusted=False
            )
            result.proposed.append(hint_proposal)
            if proposal is None:
                proposal = hint_proposal
    u = Uncertainty(
        kind=kind,
        location=location,
        detail=detail,
        consequential=consequential,
        question=question,
        proposal=proposal,
        record_next=instruction,
        hint_class=hint_class,
    )
    result.uncertainties.append(u)
    return u


def _worklist_from_uncertainties(result: InductionResult) -> list[RecordNext]:
    return [
        RecordNext(
            code=u.kind,
            instruction=u.record_next,
            location=u.location,
            blocking=True,
            hint_class=u.hint_class,
        )
        for u in result.uncertainties
        if u.record_next
    ]


def _note_uncovered(
    result: InductionResult, *, code: str, instruction: str, location: str
) -> None:
    """Non-blocking uncovered-branch note on an emitted program -- not a guard."""
    result.record_next.append(
        RecordNext(
            code=code,
            instruction=instruction,
            location=location,
            blocking=False,
        )
    )


def _tok_field(tok: Any) -> str:
    if getattr(tok, "kind", None) == "loop":
        body = getattr(tok, "body_steps", None) or []
        return _field_key(body[0]) if body else "loop"
    return _field_key(tok.step)


def _is_order_divergence(group_cols: list) -> bool:
    """True when traces performed the SAME steps in different order.

    A ``replace`` whose per-trace field sets are equal is an ordering
    disagreement, not mutually exclusive branches.
    """
    fields_by_trace: dict[int, set[str]] = {}
    for col in group_cols:
        for t, tok in col.tokens.items():
            fields_by_trace.setdefault(t, set()).add(_tok_field(tok))
    sets = list(fields_by_trace.values())
    # Same steps in every trace at this replace = reordering, not exclusive
    # arms -- even a single field that SequenceMatcher failed to align
    # (it sat on opposite sides of a swapped neighbor).
    return len(sets) >= 2 and bool(sets[0]) and all(s == sets[0] for s in sets)


def _confirmation_question(
    location: str,
    kind: AmbiguityKind,
    prompt: str,
    evidence: str,
    *,
    consequential: bool,
    dialog_text: Optional[str] = None,
) -> DisambiguationQuestion:
    """Build the grounded operator question for an UNCONFIRMED inference (a
    proposed branch guard, an optional-consequential step, a varying selection
    target). Routed to :mod:`openadapt_flow.compiler.disambiguation` -- the SAME
    ask-don't-guess flow the divergence path uses. The only Phase-1-safe applied
    effect is the conservative HALT; the confirming answer is the operator's."""
    return DisambiguationQuestion(
        id=f"{kind.value}:{location}",
        kind=kind,
        step_id=location,
        prompt=prompt,
        options=[
            QuestionOption(
                key="halt",
                label="Halt until confirmed (safe default)",
                effect=OptionEffect.NONE,
            ),
        ],
        default_key="halt",
        consequential=consequential,
        evidence=evidence,
        dialog_text=dialog_text,
    )


def _param_name_for(field: str, taken: set[str], fallback: str) -> str:
    base = "".join(c if c.isalnum() else "_" for c in field.lower()).strip("_")
    base = "_".join(p for p in base.split("_") if p) or fallback
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


class _Node:
    """A contiguous chunk of emitted graph with one ``entry`` state and a set of
    ``exits`` (transitions whose target is patched to the following node)."""

    def __init__(self, entry: str):
        self.entry = entry
        self.states: dict[str, State] = {}
        self.exits: list[Transition] = []

    def set_next(self, next_id: str) -> None:
        for tr in self.exits:
            tr.target = next_id


def induce_program(
    traces: list[TraceInput],
    *,
    propose: Optional[Proposer] = None,
    probe_resolutions: Optional[dict[str, str]] = None,
    narration: Optional[Sequence[Optional[Any]]] = None,
    screen_texts: Optional[Callable[[int, str], Optional[Sequence[str]]]] = None,
    interpreter: Optional[Any] = None,
    accepted_narration: Optional[dict[str, str]] = None,
) -> InductionResult:
    """Induce a parameterized :class:`ProgramGraph` from multiple demonstrations
    of the same task -- the RFC §3 induction loop (step [4] + [5]).

    Deterministic and model-free at its core (align -> params / loops /
    branches / optional). An optional :class:`Proposer` may PROPOSE an
    interpretation for an ambiguous point; every proposal is flagged and never
    silently trusted. When intent stays underdetermined the induced program is
    QUARANTINED (not emitted) and ``result.certified is False``.

    Args:
        traces: Two or more traces (compiled ``Workflow``s or recording dirs)
            of the SAME task. One trace is the degenerate bootstrap.
        propose: Optional compile-time interpretation proposer (advisory).
        probe_resolutions: Optional ``{location: interpretation}`` for
            consequential optional points that probing on a declared test copy
            settled (:func:`openadapt_flow.compiler.probing.probe_and_induce`
            is the only caller that sets it). ``"incidental"`` drops the step;
            ``"conditional"`` keeps it guarded on its own presence. A point
            not listed here is refused exactly as without probing.
            ``"narrated@<guard_id>"`` certifies that narrated guard.
        narration: One :class:`~openadapt_flow.narration.Narration` (or None)
            per trace. Default: read ``narration.json`` from each trace that is
            a recording or bundle directory. A sentence lined up near a branch
            point PROPOSES a guard (:mod:`.narration_rules`); it is certified
            only when every recording agrees, probing confirms it, or a person
            accepts it (``accepted_narration``).
        screen_texts: ``(trace, step_id) -> screen lines`` used to check a
            screen-element guard. Default for recording directories: OCR of
            the step's before-frame.
        interpreter: Optional compile-time
            :class:`~.narration_rules.NarrationInterpreter` for sentences the
            parser cannot read. Its answer is checked like any narrated guard.
        accepted_narration: ``{"<location>@<guard_id>": person}`` --
            narrated guards a person accepted. The guard id pins the exact
            guard the report showed; a different guard at that location is
            not accepted. A guard a recording contradicts stays refused.

    Returns:
        An :class:`InductionResult` with the induced program (or a refusal) and
        the full audit trail.
    """
    workflows = [_as_workflow(t) for t in traces]
    n = len(workflows)
    result = InductionResult(n_traces=n)
    if n == 0:
        return result
    if narration is None:
        narration = _narration_from_traces(traces)
    if screen_texts is None:
        screen_texts = _default_screen_texts(traces)

    reduced = [_reduce_trace(wf.steps) for wf in workflows]
    columns = _align(reduced)

    # Detect an alignment failure: too little shared structure to trust that
    # these are traces of the SAME task (refuse rather than induce noise).
    shared = sum(1 for c in columns if len(c.tokens) == n)
    if n >= 2 and columns and shared == 0:
        _add_uncertainty(
            result,
            kind="alignment_failure",
            location="<whole trace>",
            detail=(
                "traces share no aligned steps -- cannot infer a common "
                "program (are these the same task?)."
            ),
        )
        result.record_next = _worklist_from_uncertainties(result)
        return result

    decisions: list[ColumnDecision] = []
    nodes: list[_Node] = []
    param_specs: dict[str, ParamSpec] = {}
    data_sources: dict[str, Relation] = {}
    taken: set[str] = set()
    seen_div_groups: set[int] = set()
    optional_nodes: dict[int, _Node] = {}
    divergences: dict[int, tuple[list[_Column], int]] = {}

    for idx, col in enumerate(columns):
        present = sorted(col.tokens)

        # --- divergence (replace region): branch or contradiction ----------
        if col.divergent_group is not None:
            grp = col.divergent_group
            if grp in seen_div_groups:
                continue  # the whole group is handled once, at its first column
            seen_div_groups.add(grp)
            group_cols = [c for c in columns if c.divergent_group == grp]
            _handle_divergence(idx, group_cols, workflows, result, propose, decisions)
            # Underdetermined divergence => refuse to emit (handled below),
            # unless narration settles it after the loop.
            divergences[idx] = (group_cols, len(nodes))
            continue

        # --- loop ----------------------------------------------------------
        if col.kind == "loop":
            node, dec = _emit_loop(
                idx, col, present, n, taken, param_specs, data_sources, result, propose
            )
            decisions.append(dec)
            if node is not None:
                nodes.append(node)
                result.inferred.append(dec.note)
            continue

        # --- single step present in ALL traces: literal vs param ----------
        if len(present) == n:
            node, dec = _emit_required_single(
                idx, col, present, taken, param_specs, result, propose
            )
            nodes.append(node)
            decisions.append(dec)
            continue

        # --- single step present in SOME traces: branch or optional -------
        node, dec = _emit_optional_single(
            idx, col, present, n, result, propose, probe_resolutions or {}
        )
        if node is not None:
            nodes.append(node)
            optional_nodes[idx] = node
        decisions.append(dec)

    guard_sources: dict[str, GuardSource] = {}
    if narration and any(narration):
        _apply_narration(
            result=result,
            columns=columns,
            decisions=decisions,
            nodes=nodes,
            optional_nodes=optional_nodes,
            divergences=divergences,
            narration=list(narration),
            n=n,
            screen_texts=screen_texts,
            interpreter=interpreter,
            probe_resolutions=probe_resolutions or {},
            accepted=accepted_narration or {},
            guard_sources=guard_sources,
        )

    result.column_decisions = decisions
    result.param_specs = param_specs

    # Refuse rather than guess: any underdetermined point quarantines the whole
    # program (RFC §3 [5]) -- do NOT emit a program that guesses a branch.
    # Surface the missing-demonstration worklist instead.
    if result.uncertainties:
        result.record_next = _worklist_from_uncertainties(result)
        return result

    program, subflows = _wire(nodes)
    if program is None:
        return result
    workflow = Workflow(
        name="induced-program",
        program=program,
        subflows=subflows,
        param_specs=param_specs,
        params={
            key: runtime_param_text(spec.example) if spec.example is not None else ""
            for key, spec in param_specs.items()
        },
        data_sources=data_sources,
        guard_sources=guard_sources,
    )
    result.program = program
    result.workflow = workflow
    return result


def _emit_loop(
    idx, col, present, n, taken, param_specs, data_sources, result, propose=None
):
    loop_toks = [col.tokens[t] for t in present]
    body_steps = loop_toks[0].body_steps
    field = _field_key(body_steps[0]) if body_steps else f"row_{idx}"
    counts = [tok.count for tok in loop_toks]

    # LOOP HONESTY (RFC §3 [5]; see module LIMITS). Trace SHAPE alone cannot
    # certify that a consecutive repeat is a data-driven worklist rather than a
    # fixed unrolled sequence -- and it recognizes ONLY consecutive repeats (not
    # search->process->return, pagination, or per-row conditional bodies). When
    # the repeated body contains a CONSEQUENTIAL action, guessing wrong repeats
    # an irreversible / effect-bearing write, so REFUSE (emit an Uncertainty)
    # rather than emit a possibly-wrong loop.
    if any(_step_consequential(s) for s in body_steps):
        location = f"loop_{idx}"
        question = _confirmation_question(
            location,
            AmbiguityKind.OPTIONAL_DIALOG,
            prompt=(
                f"A consecutive repeat of {counts} step(s) over '{field}' was "
                "observed, and its body performs a CONSEQUENTIAL (irreversible "
                "or effect-bearing) action. Is this a data-driven loop over a "
                "worklist, or a fixed sequence of distinct steps? (Trace shape "
                "cannot distinguish them, and a wrong loop repeats the "
                "irreversible action.)"
            ),
            evidence=f"repeated consequential body '{field}' x{counts} at column {idx}",
            consequential=True,
        )
        _add_uncertainty(
            result,
            kind="ambiguous_loop",
            location=location,
            detail=(
                f"repeated body '{field}' (counts {counts}) contains a "
                "consequential action; a worklist loop vs a fixed unrolled "
                "sequence is not distinguishable from trace shape -- "
                "refusing to guess a loop over an irreversible step."
            ),
            consequential=True,
            question=question,
            field=field,
            propose=propose,
        )
        dec = ColumnDecision(
            index=idx,
            kind="ambiguous_loop",
            align_sig=str(col.align_sig),
            field=field,
            present_in=present,
            counts=counts,
            note=f"underdetermined consequential loop over '{field}' (counts {counts})",
        )
        return None, dec

    param_name = _param_name_for(field, taken, f"row_{idx}")
    taken.add(param_name)
    # Representative worklist: the LONGEST demonstrated queue, inlined so the
    # bundle is self-contained (a run may override via Replayer worklists=...).
    rep = max(loop_toks, key=lambda tk: tk.count)
    rel_name = f"worklist_{idx}"
    data_sources[rel_name] = Relation(
        name=rel_name,
        rows=rep.rows(param_name),
        description=f"inferred worklist for the '{field}' loop",
    )
    param_specs[param_name] = ParamSpec(
        name=param_name,
        type=ParamKind.STRING,
        example=(rep.rows(param_name)[0][param_name] if rep.count else None),
        required=True,
    )

    # Body subflow: the repeated step(s), the varying field bound per row.
    body_id = f"body_{idx}"
    body_states: dict[str, State] = {}
    prev_ids: list[str] = []
    for bi, bstep in enumerate(body_steps):
        sid = f"{body_id}_s{bi}"
        step = bstep.model_copy(deep=True)
        if step.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION):
            step.param = param_name
            step.text = None
        st = State(
            id=sid,
            kind=StateKind.ACTION,
            step=step,
            transitions=[Transition(target="__PATCH__")],
        )
        body_states[sid] = st
        prev_ids.append(sid)
    body_end = f"{body_id}_end"
    body_states[body_end] = State(
        id=body_end, kind=StateKind.TERMINAL, outcome="success"
    )
    ordered = list(body_states)
    for k, sid in enumerate(ordered):
        st = body_states[sid]
        if st.kind is StateKind.ACTION:
            st.transitions[0].target = (
                ordered[k + 1] if k + 1 < len(ordered) else body_end
            )
    body_graph = ProgramGraph(entry=ordered[0], states=body_states)

    loop_state_id = f"loop_{idx}"
    node = _Node(loop_state_id)
    exit_tr = Transition(target="__PATCH__")
    node.states[loop_state_id] = State(
        id=loop_state_id,
        kind=StateKind.LOOP,
        loop=LoopSpec(relation=rel_name, body=body_id, var=field),
        transitions=[exit_tr],
    )
    node.exits = [exit_tr]
    node._subflow = (body_id, body_graph)  # type: ignore[attr-defined]

    dec = ColumnDecision(
        index=idx,
        kind="loop",
        align_sig=str(col.align_sig),
        field=field,
        param_name=param_name,
        present_in=present,
        counts=counts,
        note=(
            f"loop over '{field}' -- body repeats {counts} across traces "
            f"(counts differ => a worklist, not unrolled steps)"
        ),
    )
    return node, dec


def _emit_required_single(idx, col, present, taken, param_specs, result, propose=None):
    toks = [col.tokens[t] for t in present]
    step0 = toks[0].step
    field = _field_key(step0)
    values = [tok.value for tok in toks]
    sid = f"s{idx}"
    step = step0.model_copy(deep=True)
    exit_tr = Transition(target="__PATCH__")

    varies = len({v for v in values}) > 1
    if step0.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK) and varies:
        # SELECTION PARAMETER (RFC §3 [4]; the "which patient" fix). A CLICK /
        # selection whose TARGET varies across traces is the most important
        # real-workflow parameter -- and the wrong-ENTITY failure this repo is
        # built to avoid. It is NOT a typed string the runtime can substitute:
        # the runtime clicks the resolved ANCHOR (runtime.replayer clicks
        # resolution.point, NOT params[step.param]), so freezing this as a baked
        # literal would SILENTLY re-select the DEMO entity every run. Robust
        # generalization needs run-time ENTITY_REF re-resolution of the click
        # target (ParamKind.ENTITY_REF), which is deferred (see PR follow-up).
        # Until then, induction REFUSES to freeze the demo target: it emits an
        # Uncertainty (with an advisory entity_ref proposal) so an operator
        # binds the real entity source. NEVER a silent hardcoded demo entity.
        location = f"s{idx}"
        proposal = Proposal(
            target=location,
            kind="param",
            content=(
                f"selection target '{field}' varies {values} -- make it an "
                "entity_ref parameter re-resolved by the identity ladder per run"
            ),
        )
        result.proposed.append(proposal)
        question = _confirmation_question(
            location,
            AmbiguityKind.PARAMETER_CANDIDATE,
            prompt=(
                f"The selected target for '{field}' VARIES across traces "
                f"{values}. Which entity should each run act on? This must be a "
                "parameter re-resolved per run -- the demo's target must NOT be "
                "frozen."
            ),
            evidence=f"selection target '{field}' varies {values} at column {idx}",
            consequential=True,
        )
        _add_uncertainty(
            result,
            kind="ambiguous_selection",
            location=location,
            detail=(
                f"selection target '{field}' varies across traces {values} "
                "-- a which-entity parameter, not a fixed target; refusing "
                "to freeze the demo entity (needs entity_ref re-resolution)."
            ),
            consequential=True,
            question=question,
            proposal=proposal,
            field=field,
            propose=propose,
        )
        kind, param_name, literal = "ambiguous_selection", None, None
    elif step0.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION) and varies:
        # A value that VARIES across traces at the same field is a PARAMETER
        # (cross-trace evidence makes WebRobot value-speculation determinate).
        name = _param_name_for(field, taken, f"value_{idx}")
        taken.add(name)
        step.param = name
        step.text = None
        example = next((v for v in values if v), None)
        param_specs[name] = ParamSpec(
            name=name, type=ParamKind.STRING, example=example, required=True
        )
        result.inferred.append(
            f"param '{name}' -- '{field}' varies across traces {values}"
        )
        kind, param_name, literal = "param", name, None
    else:
        literal = values[0]
        if step0.action in (ActionKind.TYPE, ActionKind.SELECT_OPTION):
            result.inferred.append(
                f"literal '{field}' = {literal!r} -- constant across traces"
            )
        kind, param_name = "literal", None

    node = _Node(sid)
    node.states[sid] = State(
        id=sid, kind=StateKind.ACTION, step=step, transitions=[exit_tr]
    )
    node.exits = [exit_tr]
    # Uncovered branches the traces never showed -- non-blocking notes on an
    # emitted program, never certified guards. Default path: no VLM, no
    # invented click path; we only name a control the traces already clicked.
    if kind != "ambiguous_selection":
        if _looks_like_dialog(step0):
            dialog_text = _dialog_text(step0)
            _note_uncovered(
                result,
                code="unobserved_skip",
                instruction=f"never saw the {dialog_text} Skip path",
                location=sid,
            )
        else:
            write_label = _write_control_label(step0)
            if write_label:
                _note_uncovered(
                    result,
                    code="unobserved_disabled",
                    instruction=f"never saw {write_label} disabled",
                    location=sid,
                )
    dec = ColumnDecision(
        index=idx,
        kind=kind,
        align_sig=str(col.align_sig),
        field=field,
        literal_value=literal if kind == "literal" else None,
        param_name=param_name,
        present_in=present,
    )
    return node, dec


def optional_guard(step: Step) -> Guard:
    """The skip-when-absent guard induction puts on an optional step.

    A dialog step runs when its dialog text is on screen; any other step runs
    when its own target resolves (or, with no anchor, its field text shows).
    Probing replays exactly this guard for the ``conditional`` interpretation.
    """
    field = _field_key(step)
    if _looks_like_dialog(step):
        predicate = Predicate(
            kind=PredicateKind.TEXT_PRESENT,
            text=_dialog_text(step),
            intent="optional dialog is present",
        )
    elif step.anchor is not None:
        predicate = Predicate(
            kind=PredicateKind.ANCHOR_RESOLVES,
            anchor=step.anchor,
            intent=f"optional step '{field}' target present",
        )
    else:
        predicate = Predicate(
            kind=PredicateKind.TEXT_PRESENT,
            text=field,
            intent=f"optional step '{field}' present",
        )
    return Guard(predicate=predicate, on_unmet="skip")


class OptionalPoint(BaseModel):
    """A consequential step some traces have and others lack.

    These are the points induction refuses as ``unconfirmed_optional`` or
    ``unconfirmed_branch``, located well enough to replay each recording with
    the step dropped or kept (see :mod:`openadapt_flow.compiler.probing`).
    """

    model_config = {"arbitrary_types_allowed": True}

    location: str
    kind: str
    column: int
    source_trace: int = Field(description="First trace that has the step")
    step: Step
    #: trace -> index of the step in that trace's ``steps`` (present traces).
    index_in_trace: dict[int, int] = Field(default_factory=dict)
    #: trace -> index the step would be inserted at (absent traces).
    insert_at: dict[int, int] = Field(default_factory=dict)


def optional_points(traces: list[TraceInput]) -> dict[str, OptionalPoint]:
    """Locate every consequential optional point, keyed by its location id.

    Uses the same reduction, alignment and location ids as
    :func:`induce_program`, so a key here matches an
    :attr:`Uncertainty.location` there.
    """
    workflows = [_as_workflow(t) for t in traces]
    n = len(workflows)
    columns = _align([_reduce_trace(wf.steps) for wf in workflows])
    positions = [{id(step): k for k, step in enumerate(wf.steps)} for wf in workflows]

    def last_index(tok: Any, t: int) -> int:
        if tok.kind == "loop":
            return positions[t][id(tok.iterations[-1][-1])]
        return positions[t][id(tok.step)]

    points: dict[str, OptionalPoint] = {}
    for idx, col in enumerate(columns):
        present = sorted(col.tokens)
        if (
            col.divergent_group is not None
            or col.kind != "single"
            or not 0 < len(present) < n
        ):
            continue
        step0 = col.tokens[present[0]].step
        if not _step_consequential(step0):
            continue
        is_dialog = _looks_like_dialog(step0)
        point = OptionalPoint(
            location=f"branch_{idx}" if is_dialog else f"opt_{idx}",
            kind="unconfirmed_branch" if is_dialog else "unconfirmed_optional",
            column=idx,
            source_trace=present[0],
            step=step0,
        )
        for t in range(n):
            if t in col.tokens:
                point.index_in_trace[t] = positions[t][id(col.tokens[t].step)]
                continue
            before = 0
            for prev in reversed(columns[:idx]):
                if t in prev.tokens:
                    before = last_index(prev.tokens[t], t) + 1
                    break
            point.insert_at[t] = before
        points[point.location] = point
    return points


def _emit_optional_single(idx, col, present, n, result, propose, resolutions=None):
    tok = col.tokens[present[0]]
    step0 = tok.step
    field = _field_key(step0)
    is_dialog = _looks_like_dialog(step0)
    location = f"branch_{idx}" if is_dialog else f"opt_{idx}"
    resolved = (resolutions or {}).get(location)
    if resolved is not None and not _step_consequential(step0):
        # Probing only ever settles a point induction refused; a step that
        # was never refused keeps its ordinary interpretation.
        resolved = None
    if resolved == "incidental":
        result.inferred.append(
            f"step '{field}' at column {idx} (present in {present}/{n} "
            "traces) is incidental -- dropped; certified by probing on a "
            "declared test copy"
        )
        return None, ColumnDecision(
            index=idx,
            kind="incidental",
            align_sig=str(col.align_sig),
            field=field,
            present_in=present,
            note=f"incidental step '{field}' dropped (probe-certified)",
        )

    if is_dialog:
        # A DETECTABLE condition (the dialog's own presence) -> a guarded BRANCH
        # (RFC §2.2). The guard is PROPOSED and flagged for confirmation. The
        # dialog LABEL is extracted the same way disambiguation does (#74), so
        # both agree on what text signals the popup.
        dialog_text = _dialog_text(step0)
        branch_id = f"branch_{idx}"
        do_id = f"opt_{idx}"
        do_step = step0.model_copy(deep=True)
        fall_tr = Transition(target="__PATCH__")
        do_tr = Transition(target="__PATCH__")
        node = _Node(branch_id)
        node.states[branch_id] = State(
            id=branch_id,
            kind=StateKind.BRANCH,
            transitions=[
                Transition(
                    guard=Predicate(
                        kind=PredicateKind.TEXT_PRESENT,
                        text=dialog_text,
                        intent="optional dialog is present",
                    ),
                    target=do_id,
                    label="dialog present",
                ),
                fall_tr,
            ],
        )
        node.states[do_id] = State(
            id=do_id, kind=StateKind.ACTION, step=do_step, transitions=[do_tr]
        )
        node.exits = [fall_tr, do_tr]
        result.inferred.append(
            f"branch on optional dialog {dialog_text!r} at column {idx} "
            f"(present in {present}/{n} traces) -- guard TEXT_PRESENT"
        )
        proposed = _maybe_propose(
            propose, branch_id, "guard", {"dialog": dialog_text}, result
        )
        proposal = Proposal(
            target=branch_id,
            kind="guard",
            content=(proposed or f"confirm guard: TEXT_PRESENT({dialog_text!r})"),
        )
        result.proposed.append(proposal)
        # A flagged proposal NEVER auto-certifies (RFC §3 [5]). When the guarded
        # arm's action is CONSEQUENTIAL, the proposed guard is UNCONFIRMED
        # evidence -- running (or skipping) an irreversible action on a GUESSED
        # condition is exactly the failure class to avoid -- so ALSO emit an
        # Uncertainty requiring operator confirmation (certified stays False).
        if resolved == "conditional":
            result.inferred.append(
                f"guard TEXT_PRESENT({dialog_text!r}) at {branch_id} certified "
                "by probing on a declared test copy"
            )
        elif _step_consequential(do_step):
            _add_uncertainty(
                result,
                kind="unconfirmed_branch",
                location=branch_id,
                detail=(
                    f"optional dialog branch {dialog_text!r} guards a "
                    "CONSEQUENTIAL action; the guard is PROPOSED, not "
                    "confirmed -- operator must confirm before certifying."
                ),
                consequential=True,
                question=_confirmation_question(
                    branch_id,
                    AmbiguityKind.OPTIONAL_DIALOG,
                    prompt=(
                        f"A dialog {dialog_text!r} appeared in some traces "
                        "and its handling performs a CONSEQUENTIAL action. "
                        "Confirm the guard condition before this certifies."
                    ),
                    evidence=(
                        f"optional dialog {dialog_text!r} guarding a "
                        f"consequential action at column {idx}"
                    ),
                    consequential=True,
                    dialog_text=dialog_text,
                ),
                proposal=proposal,
                field=field,
                dialog_text=dialog_text,
                propose=propose,
            )
        dec = ColumnDecision(
            index=idx,
            kind="branch",
            align_sig=str(col.align_sig),
            field=field,
            present_in=present,
            note=f"optional dialog branch ({dialog_text!r})",
        )
        return node, dec

    # No derivable condition -> an OPTIONAL guarded step that SKIPs when its own
    # target is absent (Guard on_unmet='skip', predicate ANCHOR_RESOLVES).
    sid = f"opt_{idx}"
    step = step0.model_copy(deep=True)
    exit_tr = Transition(target="__PATCH__")
    step.guard = optional_guard(step0)
    node = _Node(sid)
    node.states[sid] = State(
        id=sid, kind=StateKind.ACTION, step=step, transitions=[exit_tr]
    )
    node.exits = [exit_tr]

    # "Absent in some traces" must NOT silently become "optional/skip" for a
    # CONSEQUENTIAL step: whether the demonstrator OMITTED an irreversible /
    # effect-bearing action deliberately (truly optional) or the traces just did
    # not cover the case is UNDERDETERMINED, and auto-skipping an irreversible
    # action on an unconfirmed guard is not a safe default. So it becomes a
    # QUESTION (Uncertainty), not a silent skip -- certified stays False.
    if resolved == "conditional":
        result.inferred.append(
            f"optional step '{field}' at column {idx} (present in "
            f"{present}/{n} traces) -- run when present, skip when absent; "
            "certified by probing on a declared test copy"
        )
    elif _step_consequential(step0):
        _add_uncertainty(
            result,
            kind="unconfirmed_optional",
            location=sid,
            detail=(
                f"step '{field}' is present in only {present}/{n} traces and "
                "performs a CONSEQUENTIAL action; whether it is truly "
                "optional (safe to skip) or under-observed is "
                "underdetermined -- refusing to auto-skip an irreversible "
                "step."
            ),
            consequential=True,
            question=_confirmation_question(
                sid,
                AmbiguityKind.ABSENT_RESULT,
                prompt=(
                    f"A CONSEQUENTIAL step '{field}' appeared in only "
                    f"{present}/{n} traces. Is it genuinely OPTIONAL (skip "
                    "when its target is absent) or should its absence HALT?"
                ),
                evidence=(
                    f"consequential step '{field}' present in {present}/{n} "
                    f"traces at column {idx}"
                ),
                consequential=True,
            ),
            field=field,
            write_label=_write_control_label(step0) or "",
            propose=propose,
        )
    else:
        result.inferred.append(
            f"optional step '{field}' at column {idx} "
            f"(present in {present}/{n} traces) -- skip when absent"
        )
    dec = ColumnDecision(
        index=idx,
        kind="optional",
        align_sig=str(col.align_sig),
        field=field,
        present_in=present,
        note=f"optional step '{field}' (guarded skip)",
    )
    return node, dec


def _handle_divergence(idx, group_cols, workflows, result, propose, decisions):
    """A ``replace`` divergence: mutually-exclusive content at the same aligned
    position. If a discriminating condition were DETECTABLE it would be a
    branch; here none is derivable structurally, so this is a contradiction the
    traces do not resolve -- REFUSE (RFC §3 [5]) and route to disambiguation.
    A :class:`Proposer` may suggest a guard, but it is flagged, NEVER trusted.
    """
    labels = []
    for col in group_cols:
        for tok in col.tokens.values():
            if tok.kind == "single":
                labels.append(_value(tok.step) or _field_key(tok.step))
    labels = list(dict.fromkeys(labels))
    consequential = any(_is_irreversible(c) for c in group_cols)

    proposal_content = _maybe_propose(
        propose,
        f"divergence_{idx}",
        "guard",
        {"arms": labels},
        result,
    )
    proposal = (
        Proposal(
            target=f"divergence_{idx}",
            kind="guard",
            content=proposal_content,
        )
        if proposal_content
        else None
    )
    if proposal is not None:
        result.proposed.append(proposal)

    order = _is_order_divergence(group_cols)
    kind = "underdetermined_order" if order else "ambiguous_branch"
    if order:
        prompt = (
            f"Traces show the same steps {labels} in different orders. Record "
            "the steps in the intended order. (No order was detectable from "
            "the demonstrations.)"
        )
        detail = (
            f"traces show the same steps {labels} in different orders -- "
            "cannot decide a sequence; refusing to guess an order"
            + (" on a consequential" if consequential else "")
            + " node."
        )
        note = f"underdetermined order of {labels}"
    else:
        prompt = (
            f"Traces diverge here between {labels}. Under what condition should "
            "the workflow take each branch? (No condition was detectable from "
            "the demonstrations.)"
        )
        detail = (
            f"traces diverge between {labels} with no detectable "
            "condition -- cannot decide the guard; refusing to guess a "
            "branch on a" + (" consequential" if consequential else "") + " node."
        )
        note = f"underdetermined divergence between {labels}"
    question = DisambiguationQuestion(
        id=f"{AmbiguityKind.OPTIONAL_DIALOG.value}:divergence_{idx}",
        kind=AmbiguityKind.OPTIONAL_DIALOG,
        step_id=f"divergence_{idx}",
        prompt=prompt,
        options=[
            QuestionOption(
                key="halt",
                label="Halt -- the condition is unknown (safe default)",
                effect=OptionEffect.NONE,
            ),
        ],
        default_key="halt",
        consequential=consequential,
        evidence=f"divergent branch between {labels} at column {idx}",
    )
    _add_uncertainty(
        result,
        kind=kind,
        location=f"column {idx}",
        detail=detail,
        consequential=consequential,
        question=question,
        proposal=proposal,
        labels=labels,
        propose=propose,
    )
    decisions.append(
        ColumnDecision(
            index=idx,
            kind="underdetermined_order" if order else "divergent",
            align_sig="__divergent__",
            note=note,
        )
    )


def _maybe_propose(
    propose: Optional[Proposer], target, kind, context, result
) -> Optional[str]:
    if propose is None:
        return None
    try:
        return propose.propose(target, kind, context)
    except Exception:  # pragma: no cover - advisory path never breaks induction
        return None


# ===========================================================================
# Narration: a spoken sentence PROPOSES a guard; the recordings, a probe on a
# test copy, or a person certify it (see compiler.narration_rules).
# ===========================================================================


def _narration_from_traces(traces: list[TraceInput]) -> list[Optional[Any]]:
    """``narration.json`` of each trace that is a directory (None otherwise).

    A file that does not say it was scrubbed is refused, which here means it
    is not used.
    """
    from openadapt_flow.narration import NarrationRefused, load_narration

    out: list[Optional[Any]] = []
    for trace in traces:
        if isinstance(trace, (str, Path)) and Path(trace).is_dir():
            try:
                out.append(load_narration(Path(trace)))
            except (NarrationRefused, ValueError):
                out.append(None)
        else:
            out.append(None)
    return out


def _default_screen_texts(traces: list[TraceInput]):
    from openadapt_flow.compiler.narration_rules import recording_screen_texts

    dirs = {
        i: Path(t)
        for i, t in enumerate(traces)
        if isinstance(t, (str, Path)) and (Path(t) / "frames").is_dir()
    }
    return recording_screen_texts(dirs) if dirs else None


def _tok_steps(tok: Any) -> list[Step]:
    if tok.kind == "single":
        return [tok.step]
    return [s for iteration in tok.iterations for s in iteration]


def _step_words(step: Step) -> set[str]:
    from openadapt_flow.compiler.narration_rules import tokens

    parts = [step.intent or "", step.field_label or "", step.param or ""]
    if step.anchor is not None:
        parts.append(step.anchor.ocr_text or "")
    return tokens(" ".join(parts))


def _window_ids(columns: list[_Column], lo: int, hi: int, t: int) -> set[str]:
    """Step ids of trace ``t`` around columns ``lo..hi``: its steps there plus
    its nearest step before and after. A sentence lined up with one of these
    was spoken at that point."""
    ids: set[str] = set()
    for c in columns[lo : hi + 1]:
        if t in c.tokens:
            ids.update(s.id for s in _tok_steps(c.tokens[t]))
    for c in reversed(columns[:lo]):
        if t in c.tokens:
            ids.add(_tok_steps(c.tokens[t])[-1].id)
            break
    for c in columns[hi + 1 :]:
        if t in c.tokens:
            ids.add(_tok_steps(c.tokens[t])[0].id)
            break
    return ids


def _next_step_id(columns: list[_Column], hi: int, t: int) -> Optional[str]:
    for c in columns[hi + 1 :]:
        if t in c.tokens:
            return _tok_steps(c.tokens[t])[0].id
    return None


def _param_fields(columns, decisions, n):
    from openadapt_flow.compiler.narration_rules import FieldValues

    fields = []
    for dec in decisions:
        if dec.kind != "param" or dec.param_name is None:
            continue
        col = columns[dec.index]
        fields.append(
            FieldValues(
                param=dec.param_name,
                label=dec.field,
                values=[
                    _value(col.tokens[t].step)
                    if t in col.tokens and col.tokens[t].kind == "single"
                    else None
                    for t in range(n)
                ],
            )
        )
    return fields


def _narrated_candidate(columns, lo, hi, narration, fields, descriptors, interpreter):
    """The first sentence that reads as a rule about this point, or None.

    Sentences lined up at the point come first, then sentences anywhere whose
    action names the step. With several arms (a divergence) the sentence must
    be about exactly one of them.
    """
    from openadapt_flow.compiler import narration_rules as nr

    ranked = []
    for t, nar in enumerate(narration):
        if nar is None:
            continue
        window = _window_ids(columns, lo, hi, t)
        for line in nar.lines:
            ranked.append((0 if line.step_id in window else 1, t, line.t_start, line))
    ranked.sort(key=lambda r: r[:3])
    for rank, t, _, line in ranked:
        rule = nr.parse_rule(line.text)
        if rule is None:
            continue
        about = [
            k
            for k, words in enumerate(descriptors)
            if nr.is_about(rule, words, in_window=rank == 0)
        ]
        if len(about) != 1:
            continue
        cand = nr.candidate_from_rule(rule, trace=t, step_id=line.step_id, fields=fields)
        cand.target_arm = about[0]
        return cand
    if interpreter is None or len(descriptors) != 1:
        return None
    for rank, t, _, line in ranked:
        if rank != 0 or nr.parse_rule(line.text) is not None:
            continue
        try:
            data = interpreter.interpret(
                line.text,
                {
                    "fields": [f.param for f in fields],
                    "step": " ".join(sorted(descriptors[0])),
                },
            )
        except Exception:  # noqa: BLE001 - advisory path never breaks induction
            data = None
        if not data:
            continue
        cand = nr.candidate_from_model(
            data,
            sentence=line.text,
            trace=t,
            step_id=line.step_id,
            fields=fields,
            source=getattr(interpreter, "source", "narration+model"),
        )
        if cand is not None:
            cand.target_arm = 0
            return cand
    return None


_HOW = {
    "recordings_agree": "every recording agrees",
    "probe": "confirmed on the test copy",
    "person": "accepted by a person",
}


def _decide_narrated(
    cand, *, location, rule, expected, screen_step, fields, screen_texts,
    probe_resolutions, accepted,
) -> NarratedGuard:
    from openadapt_flow.compiler.narration_rules import check_against_recordings

    agreement = check_against_recordings(
        cand.guard,
        expected,
        fields=fields,
        screen_step=screen_step,
        screen_texts=screen_texts,
    )
    certified_by = accepted_by = None
    pinned = f"{location}@{cand.guard_id}"
    if agreement.contradict:
        status = "contradicted"
        detail = (
            "recording(s) "
            + ", ".join(str(t) for t in agreement.contradict)
            + " did the opposite of what the sentence says"
        )
    elif agreement.all_agree:
        status, certified_by = "certified", "recordings_agree"
        detail = f"all {len(agreement.agree)} recordings agree"
    elif probe_resolutions.get(location) == f"narrated@{cand.guard_id}":
        status, certified_by = "certified", "probe"
        detail = "replaying it on the declared test copy produced every recorded effect"
    elif accepted.get(pinned):
        status, certified_by, accepted_by = "certified", "person", accepted[pinned]
        detail = f"accepted by {accepted_by}"
    else:
        status = "proposed"
        detail = (
            "could not be checked in recording(s) "
            + ", ".join(str(t) for t in agreement.unknown)
            if agreement.unknown
            else "not checked"
        )
    trace_params: dict[int, dict[str, str]] = {}
    cond = cand.condition
    if cond.kind is PredicateKind.PARAM_EQUALS:
        f = next(x for x in fields if x.param == cond.param)
        trace_params = {
            t: {f.param: v} for t, v in enumerate(f.values) if v is not None
        }
    return NarratedGuard(
        location=location,
        guard_id=cand.guard_id,
        sentence=cand.sentence,
        recording=cand.trace,
        step_id=cand.step_id,
        guard=cand.guard,
        rule=rule,
        source=cand.source,
        status=status,
        certified_by=certified_by,
        accepted_by=accepted_by,
        agree=agreement.agree,
        contradict=agreement.contradict,
        unknown=agreement.unknown,
        trace_params=trace_params,
        detail=detail,
    )


def _guard_source(ng: NarratedGuard) -> GuardSource:
    return GuardSource(
        sentence=ng.sentence,
        rule=ng.rule,
        certified_by=ng.certified_by,  # type: ignore[arg-type]
        accepted_by=ng.accepted_by,
        recording=ng.recording,
        source=ng.source,  # type: ignore[arg-type]
    )


def _flag_on_uncertainty(result, uncertainty_location, ng, proposal) -> None:
    for u in result.uncertainties:
        if u.location != uncertainty_location:
            continue
        u.proposal = proposal
        if ng.status == "contradicted":
            u.detail += (
                f" Narration suggested {ng.rule!r}, but {ng.detail}, so it is "
                "not used."
            )
            continue
        u.detail += f" Narration suggests: {ng.rule} ({ng.detail})."
        u.record_next = (
            (u.record_next + " ") if u.record_next else ""
        ) + (
            "Or accept the narrated rule: induce --accept-narrated "
            f"{ng.location}@{ng.guard_id} --accepted-by NAME."
        )
        if u.question is not None and not any(
            o.key == "narrated" for o in u.question.options
        ):
            u.question.options.append(
                QuestionOption(
                    key="narrated",
                    label=f"Use the narrated rule: {ng.rule}",
                    effect=OptionEffect.NONE,
                )
            )


def _divergence_arms(group_cols, n):
    """The two paths a divergence takes: (steps per arm, traces per arm).

    None unless there are exactly two, each the same steps with the same
    values in every recording that took it.
    """
    arms: dict[tuple, list[tuple[int, list[Step]]]] = {}
    for t in range(n):
        steps: list[Step] = []
        for c in group_cols:
            tok = c.tokens.get(t)
            if tok is None:
                continue
            if tok.kind != "single":
                return None
            steps.append(tok.step)
        arms.setdefault(tuple(_sig(s) for s in steps), []).append((t, steps))
    if len(arms) != 2:
        return None
    arm_steps, members = [], []
    for entries in arms.values():
        first = entries[0][1]
        for _, steps in entries[1:]:
            if [_value(s) for s in steps] != [_value(s) for s in first]:
                return None
        arm_steps.append(first)
        members.append([t for t, _ in entries])
    return arm_steps, members


def _branch_node(idx: int, guard: Predicate, arm_steps, target: int) -> _Node:
    bid = f"branch_div_{idx}"
    node = _Node(bid)
    entries: list[Optional[str]] = []
    for k, steps in enumerate(arm_steps):
        ids = [f"div{idx}_{k}_{j}" for j in range(len(steps))]
        for j, step in enumerate(steps):
            tr = Transition(target=ids[j + 1] if j + 1 < len(ids) else "__PATCH__")
            node.states[ids[j]] = State(
                id=ids[j],
                kind=StateKind.ACTION,
                step=step.model_copy(deep=True),
                transitions=[tr],
            )
            if j + 1 == len(ids):
                node.exits.append(tr)
        entries.append(ids[0] if ids else None)
    other = 1 - target
    t_rule = Transition(
        guard=guard, target=entries[target] or "__PATCH__", label="narrated rule holds"
    )
    t_else = Transition(target=entries[other] or "__PATCH__", label="otherwise")
    for tr, entry in ((t_rule, entries[target]), (t_else, entries[other])):
        if entry is None:
            node.exits.append(tr)
    node.states[bid] = State(id=bid, kind=StateKind.BRANCH, transitions=[t_rule, t_else])
    return node


def _apply_narration(
    *, result, columns, decisions, nodes, optional_nodes, divergences, narration,
    n, screen_texts, interpreter, probe_resolutions, accepted, guard_sources,
) -> None:
    fields = _param_fields(columns, decisions, n)

    # -- optional steps (present in some recordings, absent in others) ------
    for idx, node in optional_nodes.items():
        col = columns[idx]
        present = sorted(col.tokens)
        step0 = col.tokens[present[0]].step
        sid = f"opt_{idx}"
        if _looks_like_dialog(step0) or probe_resolutions.get(sid) in (
            "incidental",
            "conditional",
        ):
            continue
        state = node.states.get(sid)
        if state is None or state.step is None:
            continue
        cand = _narrated_candidate(
            columns, idx, idx, narration, fields, [_step_words(step0)], interpreter
        )
        if cand is None:
            continue
        field = _field_key(step0)
        ng = _decide_narrated(
            cand,
            location=sid,
            rule=cand.describe(f"run {step0.intent!r}"),
            expected={t: t in col.tokens for t in range(n)},
            screen_step={
                t: col.tokens[t].step.id if t in col.tokens else _next_step_id(columns, idx, t)
                for t in range(n)
            },
            fields=fields,
            screen_texts=screen_texts,
            probe_resolutions=probe_resolutions,
            accepted=accepted,
        )
        result.narrated_guards.append(ng)
        if ng.status == "certified":
            state.step.guard = Guard(predicate=cand.guard, on_unmet="skip")
            result.uncertainties = [u for u in result.uncertainties if u.location != sid]
            result.inferred = [
                line
                for line in result.inferred
                if not line.startswith(f"optional step '{field}' at column {idx} ")
            ]
            result.inferred.append(
                f"guard at {sid}: {ng.rule} -- suggested by narration, "
                f"certified: {_HOW[ng.certified_by]}"
            )
            guard_sources[sid] = _guard_source(ng)
            for dec in decisions:
                if dec.index == idx:
                    dec.kind = "branch"
                    dec.note = f"narrated guard on '{field}' ({ng.certified_by})"
            continue
        proposal = Proposal(
            target=sid,
            kind="guard",
            content=f"{ng.rule} (narration: {ng.sentence!r})",
            source=ng.source,
        )
        result.proposed.append(proposal)
        _flag_on_uncertainty(result, sid, ng, proposal)

    # -- divergences (recordings took different paths at the same point) ----
    # Insert from the back so earlier positions stay valid; at equal positions
    # the later column goes in first, so the earlier one ends up in front.
    for idx, (group_cols, pos) in sorted(
        divergences.items(), key=lambda kv: (-kv[1][1], -kv[0])
    ):
        where = f"column {idx}"
        location = f"divergence_{idx}"
        if not any(
            u.location == where and u.kind == "ambiguous_branch"
            for u in result.uncertainties
        ):
            continue
        arms = _divergence_arms(group_cols, n)
        if arms is None:
            continue
        arm_steps, members = arms
        indices = [i for i, c in enumerate(columns) if any(c is g for g in group_cols)]
        lo, hi = min(indices), max(indices)
        if indices != list(range(lo, hi + 1)):
            # Another column sits inside the divergence; one branch node at
            # its first position would reorder steps. Leave it refused.
            continue
        descriptors = [
            set().union(*(_step_words(s) for s in steps)) if steps else set()
            for steps in arm_steps
        ]
        cand = _narrated_candidate(
            columns, lo, hi, narration, fields, descriptors, interpreter
        )
        if cand is None:
            continue
        target = cand.target_arm or 0
        label = " then ".join(s.intent for s in arm_steps[target]) or "empty"
        screen_step = {}
        for t in members[0] + members[1]:
            first = next(
                (c.tokens[t].step.id for c in group_cols if t in c.tokens), None
            )
            screen_step[t] = first or _next_step_id(columns, hi, t)
        ng = _decide_narrated(
            cand,
            location=location,
            rule=cand.describe(f"take the path {label!r}"),
            expected={t: t in members[target] for t in members[0] + members[1]},
            screen_step=screen_step,
            fields=fields,
            screen_texts=screen_texts,
            probe_resolutions=probe_resolutions,
            accepted=accepted,
        )
        result.narrated_guards.append(ng)
        if ng.status == "certified":
            nodes.insert(pos, _branch_node(idx, cand.guard, arm_steps, target))
            result.uncertainties = [u for u in result.uncertainties if u.location != where]
            result.inferred.append(
                f"branch at {location}: {ng.rule} -- suggested by narration, "
                f"certified: {_HOW[ng.certified_by]}"
            )
            guard_sources[f"branch_div_{idx}"] = _guard_source(ng)
            for dec in decisions:
                if dec.index == idx:
                    dec.kind = "branch"
                    dec.note = f"narrated branch ({ng.certified_by})"
            continue
        proposal = Proposal(
            target=location,
            kind="guard",
            content=f"{ng.rule} (narration: {ng.sentence!r})",
            source=ng.source,
        )
        result.proposed.append(proposal)
        _flag_on_uncertainty(result, where, ng, proposal)


def _wire(nodes: list[_Node]):
    """Chain nodes into a single program graph + collect loop-body subflows."""
    if not nodes:
        return None, {}
    states: dict[str, State] = {}
    subflows: dict[str, ProgramGraph] = {}
    end_id = "__end__"
    for i, node in enumerate(nodes):
        states.update(node.states)
        sub = getattr(node, "_subflow", None)
        if sub is not None:
            subflows[sub[0]] = sub[1]
        node.set_next(nodes[i + 1].entry if i + 1 < len(nodes) else end_id)
    states[end_id] = State(id=end_id, kind=StateKind.TERMINAL, outcome="success")
    return ProgramGraph(entry=nodes[0].entry, states=states), subflows


# ===========================================================================
# Held-out validation (RFC §3 [5]) + reproduction scoring
# ===========================================================================


def structural_trace_coverage(result: InductionResult, trace: TraceInput) -> float:
    """STRUCTURAL / trace-SHAPE coverage of the induced program against ``trace``,
    in [0, 1]. This is a WEAK, structural signal -- NOT behavioral held-out
    validation and NOT certification. Do not treat it, alone, as evidence the
    program is correct.

    What it DOES check (deterministic, backend-free -- pure token-shape
    comparison of column decisions vs the held-out trace's reduced tokens):

    * a PARAM column is given FULL credit for any value (it does not check the
      value is one the run could actually supply / re-resolve);
    * a LITERAL column matches only its frozen value (so a param wrongly frozen
      as a constant scores lower on a trace with a different value);
    * a LOOP column is credited as "reproduced" whenever the held token is a
      loop token -- it does NOT check the iteration count, the body, or the
      worklist binding;
    * BRANCH / OPTIONAL columns are credited for whatever presence the trace
      shows; extra held tokens the program cannot account for are penalized.

    What it does NOT do (why it is not certification): it does NOT execute the
    program against any app, does NOT drive the runtime, does NOT verify EFFECTS
    (system-of-record writes) or IDENTITY (which entity was acted on), and does
    NOT confirm the emitted anchors resolve. A high score means "the induced
    control-flow shape is consistent with the trace's shape", nothing stronger.
    Behavioral validation is a real replay through ``runtime.replayer`` with
    effect/identity verification -- a separate, heavier gate.

    Returns 0.0 for a quarantined (unemitted) program.
    """
    if result.program is None or not result.column_decisions:
        return 0.0
    held = _reduce_trace(_as_workflow(trace).steps)

    # Only EMITTED columns are scorable; refusal markers (a divergence or an
    # ambiguous loop/selection) correspond to no emitted state.
    _refusal_kinds = {
        "divergent",
        "ambiguous_loop",
        "ambiguous_selection",
        "underdetermined_order",
    }
    col_sigs = [
        d.align_sig for d in result.column_decisions if d.kind not in _refusal_kinds
    ]
    decs = [d for d in result.column_decisions if d.kind not in _refusal_kinds]
    held_sigs = [str(tok.align_sig) for tok in held]

    sm = SequenceMatcher(a=col_sigs, b=held_sigs, autojunk=False)
    matched: dict[int, int] = {}  # col index -> held index
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                matched[i1 + k] = j1 + k

    score = 0.0
    for ci, dec in enumerate(decs):
        if ci in matched:
            tok = held[matched[ci]]
            if dec.kind == "literal":
                score += 1.0 if tok.value == dec.literal_value else 0.0
            elif dec.kind == "param":
                score += 1.0  # a param reproduces any observed value
            elif dec.kind == "loop":
                score += 1.0 if tok.kind == "loop" else 0.5
            else:  # branch / optional
                score += 1.0
        else:
            # Column not present in the held trace.
            score += 1.0 if dec.kind in ("branch", "optional") else 0.0

    total = len(decs)
    extra = len(held) - len(matched)
    denom = max(total + max(extra, 0), 1)
    return score / denom


def reproduction_score(result: InductionResult, trace: TraceInput) -> float:
    """DEPRECATED alias for :func:`structural_trace_coverage`.

    The old name over-claimed: it reads like behavioral reproduction, but the
    metric is a structural trace-SHAPE coverage that executes nothing and checks
    no effect / identity (see :func:`structural_trace_coverage`). Kept as a
    thin, warning-emitting alias for backward compatibility; call
    ``structural_trace_coverage`` in new code.
    """
    warnings.warn(
        "reproduction_score() is deprecated and misleadingly named; it is a "
        "structural trace-shape coverage, NOT behavioral validation. Use "
        "structural_trace_coverage().",
        DeprecationWarning,
        stacklevel=2,
    )
    return structural_trace_coverage(result, trace)


def validate_held_out(
    traces: list[TraceInput], *, propose: Optional[Proposer] = None
) -> HeldOutValidation:
    """Leave-one-out held-out STRUCTURAL check (RFC §3 [5]): for each trace,
    induce a program from the OTHER N-1 traces and score how well the induced
    program's SHAPE covers the held one (:func:`structural_trace_coverage`).
    Reports the per-fold scores and their mean. Requires >= 2 traces.

    This is a structural signal ONLY -- it executes nothing and verifies no
    effect / identity, so a high mean is NOT certification (see
    :func:`structural_trace_coverage` and :class:`HeldOutValidation`)."""
    n = len(traces)
    if n < 2:
        return HeldOutValidation(per_trace=[], mean=0.0, n_traces=n)
    scores: list[float] = []
    for i in range(n):
        train = traces[:i] + traces[i + 1 :]
        result = induce_program(train, propose=propose)
        scores.append(structural_trace_coverage(result, traces[i]))
    mean = sum(scores) / len(scores) if scores else 0.0
    return HeldOutValidation(per_trace=scores, mean=mean, n_traces=n)
