"""Interactive disambiguation: compile-time Socrates-style questions.

A single demonstration under-determines the operator's intent (RFC
``docs/design/WORKFLOW_PROGRAM_IR.md`` §1, §3): the same trace is consistent
with several distinct programs. The compiler already *guesses* at some of
these (volatility mining, the ``exclude_texts`` param machinery) -- but where
the guess is CONSEQUENTIAL, guessing silently freezes an accidental
interpretation. This module implements the RFC §3 step [3] induction stage:
where candidate interpretations disagree, **do not guess -- ask the operator a
concrete, grounded, multiple-choice question**, and apply the chosen answer
DETERMINISTICALLY as a Phase-1 guard/param (``ir.Guard`` / ``ir.ParamSpec``,
from the workflow-program IR).

Detected ambiguity kinds (all detected structurally, ZERO model calls):

- **parameter candidate** -- a value typed in the demo that was NOT tagged as
  a parameter is plausibly a parameter the author forgot to mark, not a fixed
  constant (RFC §1 "which literal values are parameters"). Answer maps to a
  ``ParamSpec`` + param binding (make it vary per run) or a no-op (keep the
  literal).
- **absent-result handling** -- a step that selects a SEARCHED entity has no
  recorded branch for the 0-results / >1-match case (RFC §1
  "absent-result handling", ``docs/LIMITS.md`` "no conditionals"). Answer maps
  to a ``Guard`` on the selection step (the searched entity must resolve on the
  results screen, else HALT) -- so a 0-result run halts instead of clicking the
  recorded position blindly.
- **optional dialog** -- a popup dismissed once in the demo whose
  expected-vs-exceptional status is unknown (RFC §1 "branches and
  expected-vs-exceptional popups"). Answer maps to a guarded/optional step
  (``Guard`` with ``on_unmet="skip"`` gated on the dialog's presence -- the RFC
  "guarded branch WITHOUT the Phase-2 state machine") or a no-op (always
  expected).

**Refuse rather than guess** (mirrors ``runtime.identity`` and the RFC §3 step
[5] quarantine): an UNANSWERED ambiguity on a CONSEQUENTIAL step (one that, or
whose downstream, performs an irreversible write) is NOT silently defaulted --
it is flagged and the resolved skill is marked NOT certified until the operator
answers. Non-consequential unanswered ambiguities fall back to the conservative
no-op default (leave the demo interpretation as recorded).

The CORE is non-interactive and testable: :func:`detect_ambiguities` and
:func:`apply_answers` are pure functions over a :class:`~openadapt_flow.ir.
Workflow`. A thin CLI wrapper (``openadapt-flow disambiguate``) prompts a human
and calls the same API.

Touch-points (kept minimal, per the stacking constraint on PR #71):
- Reuses #71's ``ir.Guard`` / ``ir.Predicate`` / ``ir.ParamSpec`` verbatim; adds
  NO new IR fields.
- ``compiler/compile.py`` is UNCHANGED -- disambiguation runs as an opt-in pass
  over an already-compiled bundle (``detect_ambiguities`` / ``apply_answers``),
  not inside ``compile_recording``.
- ``__main__.py`` gains one thin ``disambiguate`` subcommand.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import (
    ActionKind,
    Guard,
    ParamKind,
    ParamSpec,
    Predicate,
    PredicateKind,
    Step,
    Workflow,
)

# A typed value shorter than this (stripped) is too weak to be a meaningful
# parameter candidate (single keystrokes, "OK") -- skip it.
MIN_PARAM_VALUE_CHARS = 2

# Dialog-dismissal labels: a click whose target label / intent matches one of
# these is plausibly dismissing an optional popup rather than driving the main
# task. Matched case-insensitively on WORD boundaries so "ok" does not fire on
# "lookup" and "close" does not fire on "closed". Conservative on purpose --
# an over-broad list would flag ordinary navigation as an optional dialog.
_DIALOG_STEMS: tuple[str, ...] = (
    r"dismiss",
    r"close",
    r"okay",
    r"ok",
    r"accept",
    r"got\s*it",
    r"no\s*thanks",
    r"not\s*now",
    r"maybe\s*later",
    r"survey",
)
_DIALOG_RE = re.compile(r"\b(?:" + "|".join(_DIALOG_STEMS) + r")\b", re.IGNORECASE)


class AmbiguityKind(str, Enum):
    """The three deterministically-detected under-specification kinds."""

    PARAMETER_CANDIDATE = "parameter_candidate"
    ABSENT_RESULT = "absent_result"
    OPTIONAL_DIALOG = "optional_dialog"


class OptionEffect(str, Enum):
    """What applying an option does to the workflow. Data-driven so
    :func:`apply_answers` needs no per-kind branching on option identity."""

    #: No change -- keep the recorded interpretation (the conservative default).
    NONE = "none"
    #: Bind the step's typed value to a new ``ParamSpec`` (make it vary/run).
    MAKE_PARAM = "make_param"
    #: Add a ``Guard`` that HALTs when its predicate is unmet.
    GUARD_HALT = "guard_halt"
    #: Add a ``Guard`` that SKIPs the step when its predicate is unmet.
    GUARD_SKIP = "guard_skip"


class QuestionOption(BaseModel):
    """One multiple-choice answer, carrying the deterministic effect it
    applies. ``policy`` records a run-time strategy the operator intends that
    Phase 1 cannot yet express (e.g. "pick the newest match") -- it is stored
    on the applied guard's predicate intent as an audit note; the Phase-1
    realization is always the safe HALT."""

    key: str
    label: str
    effect: OptionEffect
    on_unmet: Optional[str] = None  # "halt" | "skip" for GUARD_* effects
    policy: Optional[str] = None  # deferred run-time strategy (audit note)


class DisambiguationQuestion(BaseModel):
    """A concrete, screenshot-grounded multiple-choice question about one
    ambiguity, plus the data :func:`apply_answers` needs to apply any answer
    deterministically."""

    id: str = Field(description="Stable '<kind>:<step_id>' identifier")
    kind: AmbiguityKind
    step_id: str
    prompt: str
    options: list[QuestionOption]
    default_key: str = Field(
        description="The conservative no-op-or-safe option, used only for "
        "NON-consequential unanswered ambiguities."
    )
    consequential: bool = Field(
        description="True when a wrong answer could cause a wrong irreversible "
        "action (this or a downstream step is irreversible). Unanswered "
        "consequential questions are refused, never defaulted."
    )
    evidence: str = Field(
        description="The concrete demonstrated fact the question is about "
        "(the typed value, the selected entity, the dialog label)."
    )
    # Apply-time payloads (kind-specific; unused fields stay None).
    param_name: Optional[str] = None  # PARAMETER_CANDIDATE
    example_value: Optional[str] = None  # PARAMETER_CANDIDATE
    dialog_text: Optional[str] = None  # OPTIONAL_DIALOG

    def option(self, key: str) -> QuestionOption:
        for opt in self.options:
            if opt.key == key:
                return opt
        raise ValueError(
            f"{self.id}: unknown answer {key!r} "
            f"(choices: {', '.join(o.key for o in self.options)})"
        )


class DisambiguationResult(BaseModel):
    """Outcome of applying answers: the resolved workflow, plus the audit
    trail (which questions were applied, which consequential ones remain
    unresolved, and the certification verdict)."""

    workflow: Workflow
    questions: list[DisambiguationQuestion]
    applied: dict[str, str] = Field(
        default_factory=dict, description="question id -> chosen option key"
    )
    defaulted: list[str] = Field(
        default_factory=list,
        description="Non-consequential questions left at their safe default.",
    )
    unresolved_consequential: list[str] = Field(
        default_factory=list,
        description="Consequential questions with no answer -- the reason the "
        "skill is not certified.",
    )

    @property
    def certified(self) -> bool:
        """False iff a consequential ambiguity is unresolved (refuse rather
        than guess -- mirrors the identity gate)."""
        return not self.unresolved_consequential

    def render(self) -> str:
        lines = [
            f"Disambiguation: {len(self.questions)} question(s), "
            f"{len(self.applied)} answered, {len(self.defaulted)} defaulted, "
            f"{len(self.unresolved_consequential)} unresolved (consequential)."
        ]
        for q in self.questions:
            if q.id in self.applied:
                mark, note = "answered", self.applied[q.id]
            elif q.id in self.defaulted:
                mark, note = "defaulted", q.default_key
            else:
                mark, note = "UNRESOLVED", "(consequential -- must answer)"
            lines.append(f"  [{mark}] {q.id}: {note}")
        verdict = "CERTIFIED" if self.certified else "NOT CERTIFIED"
        lines.append(
            f"Certification: {verdict}"
            + (
                ""
                if self.certified
                else " -- resolve the consequential question(s) above."
            )
        )
        return "\n".join(lines)


# -- consequentiality ---------------------------------------------------------


def _is_consequential(workflow: Workflow, step_index: int) -> bool:
    """True when the ambiguity at ``step_index`` gates an irreversible action:
    the step itself or any LATER step is ``risk="irreversible"``. A wrong
    answer then risks a wrong write, so the question must be answered, never
    silently defaulted (RFC §3 [5] quarantine; ``docs/LIMITS.md`` posture)."""
    return any(s.risk == "irreversible" for s in workflow.steps[step_index:])


# -- parameter-name synthesis -------------------------------------------------


def _slug(text: str) -> str:
    """Deterministic short identifier from a typed value: lowercase, first
    three word-tokens joined by underscore, non-alphanumerics dropped."""
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    return "_".join(words[:3])


def _unique_param_name(base: str, taken: set[str], fallback: str) -> str:
    name = base or fallback
    if name not in taken:
        return name
    i = 2
    while f"{name}_{i}" in taken:
        i += 1
    return f"{name}_{i}"


# -- question synthesis (templated, NOT model-generated) ----------------------


def _parameter_question(
    step: Step, consequential: bool, param_name: str
) -> DisambiguationQuestion:
    value = step.text or ""
    preview = value if len(value) <= 40 else value[:39] + "…"
    return DisambiguationQuestion(
        id=f"{AmbiguityKind.PARAMETER_CANDIDATE.value}:{step.id}",
        kind=AmbiguityKind.PARAMETER_CANDIDATE,
        step_id=step.id,
        prompt=(
            f"The demo typed {preview!r} but did not mark it as a parameter. "
            "Is this a FIXED value or a PARAMETER that varies per run?"
        ),
        options=[
            QuestionOption(
                key="fixed",
                label="Fixed value -- keep the literal as demonstrated",
                effect=OptionEffect.NONE,
            ),
            QuestionOption(
                key="param",
                label=(
                    f"Parameter -- vary per run (bind as '{param_name}', "
                    "demo value becomes the default)"
                ),
                effect=OptionEffect.MAKE_PARAM,
            ),
        ],
        default_key="fixed",
        consequential=consequential,
        evidence=f"typed value {preview!r} at {step.id}",
        param_name=param_name,
        example_value=value,
    )


def _absent_result_question(step: Step, consequential: bool) -> DisambiguationQuestion:
    entity = None
    if step.anchor is not None:
        entity = step.anchor.context_text or step.anchor.ocr_text
    entity_desc = f"'{entity}'" if entity else "the searched entity"
    return DisambiguationQuestion(
        id=f"{AmbiguityKind.ABSENT_RESULT.value}:{step.id}",
        kind=AmbiguityKind.ABSENT_RESULT,
        step_id=step.id,
        prompt=(
            f"The demo selected {entity_desc} from a search, but showed only "
            "the one-match case. When the search returns 0 matches or >1 "
            "matches at run time, what should the workflow do?"
        ),
        options=[
            QuestionOption(
                key="halt",
                label="Halt the run",
                effect=OptionEffect.GUARD_HALT,
                on_unmet="halt",
                policy="halt",
            ),
            QuestionOption(
                key="newest",
                label="Pick the newest match (deferred; halts until unique)",
                effect=OptionEffect.GUARD_HALT,
                on_unmet="halt",
                policy="select_newest",
            ),
            QuestionOption(
                key="compare",
                label=(
                    "Compare a second field (e.g. DOB) and pick the exact "
                    "match (deferred; halts until unique)"
                ),
                effect=OptionEffect.GUARD_HALT,
                on_unmet="halt",
                policy="compare_second_field",
            ),
            QuestionOption(
                key="ask",
                label="Ask the operator (durable pause; halts in Phase 1)",
                effect=OptionEffect.GUARD_HALT,
                on_unmet="halt",
                policy="escalate",
            ),
        ],
        default_key="halt",
        consequential=consequential,
        evidence=f"entity selection at {step.id} ({entity_desc})",
    )


def _optional_dialog_question(
    step: Step, consequential: bool, dialog_text: str
) -> DisambiguationQuestion:
    return DisambiguationQuestion(
        id=f"{AmbiguityKind.OPTIONAL_DIALOG.value}:{step.id}",
        kind=AmbiguityKind.OPTIONAL_DIALOG,
        step_id=step.id,
        prompt=(
            f"The demo handled a dialog ({dialog_text!r}) once. Is this dialog "
            "ALWAYS expected, or does it only appear SOMETIMES?"
        ),
        options=[
            QuestionOption(
                key="always",
                label="Always expected -- keep it as a required step",
                effect=OptionEffect.NONE,
            ),
            QuestionOption(
                key="sometimes",
                label=(
                    "Only sometimes -- dismiss it when present, skip when "
                    "absent (guarded optional step)"
                ),
                effect=OptionEffect.GUARD_SKIP,
                on_unmet="skip",
            ),
        ],
        default_key="always",
        consequential=consequential,
        evidence=f"dialog {dialog_text!r} handled at {step.id}",
        dialog_text=dialog_text,
    )


# -- detection ----------------------------------------------------------------


def detect_ambiguities(workflow: Workflow) -> list[DisambiguationQuestion]:
    """Deterministically detect under-specified points in a compiled workflow
    and synthesize one grounded multiple-choice question per point.

    ZERO model calls: the detection is structural (typed-but-untagged values,
    search-then-select without a branch, dialog-dismissal labels) and the
    question text is templated. Ordering follows step order, so the returned
    list is stable.
    """
    questions: list[DisambiguationQuestion] = []
    taken: set[str] = set(workflow.params) | set(workflow.param_specs)
    seen_type_before = False

    for idx, step in enumerate(workflow.steps):
        consequential = _is_consequential(workflow, idx)

        # (1) parameter candidate: an untagged, non-trivial typed value.
        if (
            step.action is ActionKind.TYPE
            and not step.param
            and step.text
            and len(step.text.strip()) >= MIN_PARAM_VALUE_CHARS
        ):
            base = _slug(step.text)
            name = _unique_param_name(base, taken, f"value_{idx}")
            taken.add(name)
            questions.append(_parameter_question(step, consequential, name))

        # (2) absent-result: an identity-armed entity selection that follows a
        #     typed search query, with no branch for 0/>1 matches yet.
        if (
            step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            and step.guard is None
            and step.identity_armed
            and seen_type_before
            and not _looks_like_dialog(step)
        ):
            questions.append(_absent_result_question(step, consequential))

        # (3) optional dialog: a click that dismisses a popup, no guard yet.
        if (
            step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
            and step.guard is None
            and _looks_like_dialog(step)
        ):
            dialog_text = _dialog_text(step)
            questions.append(
                _optional_dialog_question(step, consequential, dialog_text)
            )

        if step.action is ActionKind.TYPE:
            seen_type_before = True

    return questions


def _looks_like_dialog(step: Step) -> bool:
    label = step.anchor.ocr_text if step.anchor else None
    return bool(_DIALOG_RE.search(f"{step.intent} {label or ''}"))


def _dialog_text(step: Step) -> str:
    """A distinctive label for the dialog, preferring the target's own OCR
    text, else the matched keyword from the intent."""
    if step.anchor and step.anchor.ocr_text:
        return step.anchor.ocr_text
    m = _DIALOG_RE.search(step.intent)
    return m.group(0) if m else step.intent


# -- application --------------------------------------------------------------


def _apply_option(
    workflow: Workflow, q: DisambiguationQuestion, opt: QuestionOption
) -> None:
    """Mutate ``workflow`` in place to realize ``opt`` using #71's Phase-1
    IR types. ``workflow`` is expected to be a caller-owned copy."""
    if opt.effect is OptionEffect.NONE:
        return

    step = _find_step(workflow, q.step_id)

    if opt.effect is OptionEffect.MAKE_PARAM:
        name = q.param_name or _slug(q.example_value or "") or q.step_id
        step.param = name
        step.text = q.example_value
        workflow.params[name] = q.example_value or ""
        workflow.param_specs[name] = ParamSpec(
            name=name,
            type=ParamKind.STRING,
            example=q.example_value,
            required=True,
        )
        return

    if opt.effect in (OptionEffect.GUARD_HALT, OptionEffect.GUARD_SKIP):
        predicate = _guard_predicate(q, opt)
        step.guard = Guard(
            predicate=predicate,
            on_unmet=opt.on_unmet or "halt",
        )
        return

    raise ValueError(f"unhandled option effect {opt.effect!r}")


def _guard_predicate(q: DisambiguationQuestion, opt: QuestionOption) -> Predicate:
    if q.kind is AmbiguityKind.ABSENT_RESULT:
        # The searched entity must RESOLVE on the results screen; on 0 results
        # (or a screen where it does not resolve) the guard is unmet and the
        # run halts instead of clicking the recorded position blindly. The
        # richer run-time strategy the operator chose (newest / DOB compare /
        # escalate) is recorded as the predicate intent for a later phase.
        note = f" [policy={opt.policy}]" if opt.policy else ""
        return Predicate(
            kind=PredicateKind.ANCHOR_RESOLVES,
            intent=f"searched entity resolves before selection{note}",
        )
    if q.kind is AmbiguityKind.OPTIONAL_DIALOG:
        # The step runs only when the dialog is actually present; otherwise it
        # is a no-op success (on_unmet="skip") -- a guarded branch without the
        # Phase-2 state machine (RFC §2.2).
        return Predicate(
            kind=PredicateKind.TEXT_PRESENT,
            text=q.dialog_text,
            intent="optional dialog is present",
        )
    raise ValueError(f"no guard predicate for kind {q.kind!r}")


def _find_step(workflow: Workflow, step_id: str) -> Step:
    for step in workflow.steps:
        if step.id == step_id:
            return step
    raise ValueError(f"no step {step_id!r} in workflow {workflow.name!r}")


def apply_answers(
    workflow: Workflow, answers: Optional[dict[str, str]] = None
) -> DisambiguationResult:
    """Resolve a workflow's ambiguities from an answers map.

    Pure and deterministic -- no model calls, no I/O. ``answers`` maps a
    question id (see :func:`detect_ambiguities`) to a chosen option key.

    For each detected ambiguity:
      * answered -> apply the chosen option deterministically (ParamSpec /
        Guard, using #71's Phase-1 IR types);
      * unanswered + NON-consequential -> apply the conservative default
        (typically a no-op that keeps the demonstrated interpretation);
      * unanswered + CONSEQUENTIAL -> refuse: leave the step untouched and flag
        it. The result is then NOT certified (``result.certified is False``),
        mirroring the identity ladder's refuse-rather-than-guess posture -- the
        skill must not silently freeze an accidental interpretation on a step
        that gates an irreversible write.

    An unknown option key for a question raises ``ValueError`` (an invalid
    answer is not silently ignored).

    Returns:
        A :class:`DisambiguationResult` with the resolved (copied) workflow and
        the full audit trail.
    """
    answers = answers or {}
    resolved = workflow.model_copy(deep=True)
    questions = detect_ambiguities(resolved)
    valid_ids = {q.id for q in questions}
    for qid in answers:
        if qid not in valid_ids:
            raise ValueError(
                f"answer for unknown question {qid!r} "
                f"(questions: {', '.join(sorted(valid_ids)) or 'none'})"
            )

    result = DisambiguationResult(workflow=resolved, questions=questions)
    for q in questions:
        if q.id in answers:
            opt = q.option(answers[q.id])
            _apply_option(resolved, q, opt)
            result.applied[q.id] = opt.key
        elif q.consequential:
            # Refuse rather than guess: do not default a consequential edge.
            result.unresolved_consequential.append(q.id)
        else:
            opt = q.option(q.default_key)
            _apply_option(resolved, q, opt)
            result.defaulted.append(q.id)

    # Re-attach the (possibly mutated) workflow.
    result.workflow = resolved
    return result
