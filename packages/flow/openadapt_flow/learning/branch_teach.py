"""Teach a branch from two or more resolved halts (``openadapt-flow teach-branch``).

:func:`openadapt_flow.learning.teach.teach` learns a fix from ONE halted run.
Its guard is the first text that was new on the screen where that run stopped,
so it can key on something specific to that one run: a reference number, a
patient's name, a timestamp. A real portal shows the same condition with
different details each time ("Prior Auth Required for <payer>"), and may show
a banner for every payer while only one payer needs the extra steps.

This module learns the branch from several examples of the same resolution:

1. Every example is a halted run of the same bundle, stopped at the same step
   in the same way, and resolved by the same fix (the same corrective intents,
   in the same order). Examples that disagree are refused.
2. The branch is keyed on the **screen signature** the examples share: text
   that was new on every example's halt screen (a whole line, or a run of at
   least two words that every example shows, such as "Prior Auth Required").
   Text that any earlier screen of the same run showed is not a signature.
3. Optional **negative runs** are runs of the bundle that went the normal way.
   When the signature also appears in a negative run, the branch must be
   narrower than the screen. If one run parameter has the same value in every
   example and a different value in every such negative run, the guard becomes
   "signature on screen AND <param> equals <value>". Otherwise teach-branch
   refuses, because nothing it observed separates the two cases.
4. All example corrections go through the UNCHANGED governed learn loop
   together (induce, regression gate, held-out validation, where examples can
   be held out from fitting), with the negative runs as contrast traces. After
   promotion every example and every negative run must still replay
   symbolically along the expected path; otherwise nothing is written.
5. The result is written as a repair CANDIDATE (``repair/candidate.json``,
   source ``teach``) plus ``repair/branch_induction.json`` describing the
   examples. It is not active. Activation goes through the same governed
   lifecycle as any repair: campaigns, human approval and a canary
   (``openadapt-flow repair``).

Narration (``narration.json`` beside a fix recording or a halted run, see
:mod:`openadapt_flow.narration`) can name the narrowing parameter: "if the
payer type is medicaid, dismiss the notice". It is used only when every example
has that value and every normal run that shows the signature has another one.
Otherwise it is reported as contradicted and teach-branch decides as before.
The sentence is kept beside the guard (``Workflow.guard_sources``).

At runtime the branch is an ordinary guarded program state: an OCR text check
and, when narrowed, a parameter compare. It makes zero model calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Optional, Sequence

from pydantic import BaseModel, Field

from openadapt_flow.ir import (
    Guard,
    GuardSource,
    ParamKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    RunReport,
    StateKind,
    Step,
    Workflow,
)
from openadapt_flow.learning.halt_loop import (
    execution_trace_from_halt,
    promoted_workflow,
)
from openadapt_flow.learning.interpreter import program_reproduces
from openadapt_flow.learning.library import SkillLibrary
from openadapt_flow.learning.loop import LearnOutcome, RegressionGate, learn_from_traces
from openadapt_flow.learning.synth_stream import StructuralDiffInducer
from openadapt_flow.learning.teach import (
    CorrectionSpec,
    TeachError,
    _copy_carried,
    _copy_templates,
    _halted_result,
    _load_fix,
    _norm,
    _plan_correction,
    _real_target_canary,
    _seed_or_check_library,
    _similar,
    _spec_from_recording,
    _texts_seen_before_halt,
    _was_delivered,
    check_provenance,
    load_base_program,
    load_halt_report,
)
from openadapt_flow.learning.trace import ExecutionTrace, TraceStep
from openadapt_flow.runtime.authorization import runtime_params_for_gui

__all__ = [
    "BranchExample",
    "BranchSignature",
    "BranchTeachResult",
    "find_signature",
    "teach_branch",
]

#: A shared phrase must be at least this many words and characters, so a
#: single common word ("Required", "OK") can't become a branch condition.
_MIN_PHRASE_WORDS = 2
_MIN_PHRASE_CHARS = 8
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")


class BranchExample(BaseModel):
    """One resolved halt: the halted run directory and its fix."""

    run_dir: Path
    fix: Path


class BranchSignature(BaseModel):
    """The condition a taught branch keys on."""

    text: str = Field(description="Screen text every example showed at the halt")
    param: Optional[str] = Field(
        default=None, description="Run parameter that narrows the branch, if any"
    )
    value: Optional[str] = Field(default=None, description="Its value in the branch")

    def predicate(self) -> Predicate:
        text = Predicate(
            kind=PredicateKind.TEXT_PRESENT,
            text=self.text,
            intent=f"{self.text} present",
        )
        if self.param is None:
            return text
        return Predicate(
            kind=PredicateKind.AND,
            intent=f"{self.text} present and {self.param} is {self.value}",
            operands=[
                text,
                Predicate(
                    kind=PredicateKind.PARAM_EQUALS,
                    param=self.param,
                    value=self.value,
                ),
            ],
        )

    def describe(self) -> str:
        if self.param is None:
            return f"the screen shows {self.text!r}"
        return f"the screen shows {self.text!r} and {self.param} is {self.value!r}"


class BranchTeachResult(BaseModel):
    """What :func:`teach_branch` did."""

    promoted: bool
    reason: str = ""
    signature: Optional[BranchSignature] = None
    examples: int = 0
    negatives: int = 0
    outcome: Optional[LearnOutcome] = None
    out_bundle: Optional[Path] = None
    narration: Optional[str] = Field(
        default=None, description="The narrated sentence that named the condition"
    )
    narration_status: Optional[str] = Field(
        default=None, description="'certified' (every run agrees) | 'contradicted'"
    )

    def summary(self) -> str:
        lines = [
            ("PROMOTED" if self.promoted else "REFUSED")
            + f": branch from {self.examples} examples"
            + (f" and {self.negatives} normal runs" if self.negatives else "")
        ]
        if self.signature is not None:
            lines.append(f"  condition: {self.signature.describe()}")
        if self.narration is not None:
            verdict = (
                "every example and normal run agrees"
                if self.narration_status == "certified"
                else "a run contradicts it, so it was not used"
            )
            lines.append(f"  narration: {self.narration!r} ({verdict})")
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        if self.outcome is not None and self.outcome.gate is not None:
            gate = "passed" if self.outcome.gate.passed else "FAILED"
            lines.append(f"  regression gate: {gate}")
            for failure in self.outcome.gate.failures:
                lines.append(f"    - {failure}")
        if self.promoted and self.out_bundle is not None:
            lines.append(f"  repair candidate written to {self.out_bundle}")
            lines.append(
                "  it is not active. Run the repair campaigns and approve it "
                "with `openadapt-flow repair` before it runs"
            )
        else:
            lines.append("  bundle UNCHANGED -- the workflow stays halting here")
        return "\n".join(lines)


# -- the screen signature ------------------------------------------------------


def _phrases(line: str) -> set[str]:
    """Every run of at least two words in ``line``, as written."""
    words = _WORD.findall(line)
    out: set[str] = set()
    for i in range(len(words)):
        for j in range(i + _MIN_PHRASE_WORDS, len(words) + 1):
            phrase = " ".join(words[i:j])
            if len(phrase) >= _MIN_PHRASE_CHARS:
                out.add(phrase)
    return out


def _contains(lines: Sequence[str], phrase: str) -> bool:
    """``phrase`` is on one of ``lines`` (whitespace and case ignored)."""
    target = _norm(phrase)
    return any(target in _norm(line) or _similar(line, phrase) for line in lines)


def _new_texts(observed: Sequence[str], seen: Optional[Sequence[str]]) -> list[str]:
    if seen is None:
        return list(observed)
    return [t for t in observed if not any(_similar(t, s) for s in seen)]


def find_signature(
    new_per_example: Sequence[Sequence[str]],
    *,
    labels: Sequence[str] = (),
) -> list[str]:
    """Screen text shared by every example, best first.

    ``new_per_example`` holds, per example, the text that was new on its halt
    screen. A candidate is a whole line that every example shows (allowing
    small OCR differences) or a run of at least two words that appears in a
    line of every example. Candidates contained in a longer candidate are
    dropped. The label of a control the fix clicks ranks first, then longer
    candidates.
    """
    if not new_per_example or any(not texts for texts in new_per_example):
        return []
    first, rest = new_per_example[0], new_per_example[1:]
    candidates: set[str] = set()
    for line in first:
        if all(any(_similar(line, other) for other in texts) for texts in rest):
            candidates.add(line.strip())
        for phrase in _phrases(line):
            if all(_contains(texts, phrase) for texts in rest):
                candidates.add(phrase)
    maximal = [
        c
        for c in candidates
        if not any(c != o and _norm(c) in _norm(o) for o in candidates)
    ]

    def rank(text: str) -> tuple[int, int, str]:
        is_label = any(_similar(text, label) for label in labels)
        return (0 if is_label else 1, -len(_norm(text)), text)

    return sorted(maximal, key=rank)


# -- the inducer: the shipped one, with a narrowed guard -----------------------


class _NarrowedInducer(StructuralDiffInducer):
    """The reference inducer with a guard that may also compare a parameter.

    The guard fact must be true in every trace that takes the new step. A trace
    that does not take it must either lack the fact or have a different value
    for the narrowing parameter.
    """

    def __init__(self, signature: BranchSignature, recorded_steps: dict[str, Step]):
        super().__init__(recorded_steps=recorded_steps)
        self._signature = signature

    def _guard_fact(  # type: ignore[override]
        self, successes: list[ExecutionTrace], new_intent: str
    ) -> Optional[str]:
        sig = self._signature
        for trace in successes:
            takes = any(s.intent == new_intent for s in trace.steps)
            has_fact = trace.facts.get(sig.text, False)
            in_branch = has_fact and (
                sig.param is None or trace.params.get(sig.param) == sig.value
            )
            if takes != in_branch:
                return None
        return sig.text

    def _fact_guard(self, fact: str, on_unmet: str) -> Guard:  # type: ignore[override]
        return Guard(predicate=self._signature.predicate(), on_unmet=on_unmet)  # type: ignore[arg-type]


# -- the flow ------------------------------------------------------------------


class _Example(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    run_dir: Path
    report: RunReport
    spec: CorrectionSpec
    recorded: dict[str, Step]
    carried: dict[str, Path]
    delivered: bool
    new_texts: list[str]
    labels: list[str]
    params: dict[str, str]


def _load_example(
    example: BranchExample, base: Workflow, program: ProgramGraph, bundle: Path
) -> _Example:
    report = load_halt_report(example.run_dir)
    check_provenance(report, base, program, run_dir=example.run_dir, bundle_dir=bundle)
    spec = _load_fix(example.fix)
    recorded: dict[str, Step] = {}
    carried: dict[str, Path] = {}
    if spec is None:
        spec, recorded, carried = _spec_from_recording(Path(example.fix), report)
    halted = _halted_result(report)
    delivered = _was_delivered(halted)
    assert report.halt is not None
    seen = _texts_seen_before_halt(Path(example.run_dir), report, halted, delivered)
    labels = [
        step.anchor.ocr_text
        for step in recorded.values()
        if step.anchor is not None and step.anchor.ocr_text
    ]
    return _Example(
        run_dir=Path(example.run_dir),
        report=report,
        spec=spec,
        recorded=recorded,
        carried=carried,
        delivered=delivered,
        new_texts=_new_texts(report.halt.observed_texts, seen),
        labels=labels,
        params=runtime_params_for_gui(report.params),
    )


def _run_texts(run_dir: Path, report: RunReport) -> list[str]:
    """OCR text of every screen a run recorded (PHI-scrubbed)."""
    from openadapt_flow.privacy import scrub_text
    from openadapt_flow.vision.ocr import ocr

    texts: list[str] = []
    for result in report.results:
        if result.before_png is None:
            continue
        frame = run_dir / result.before_png
        if not frame.is_file():
            continue
        for line in ocr(frame.read_bytes()):
            text = (scrub_text(line.text) or "").strip()
            if text:
                texts.append(text)
    return texts


def _load_negative(
    run_dir: Path,
    base: Workflow,
    screen_texts: Callable[[Path, RunReport], list[str]],
) -> tuple[RunReport, list[str]]:
    path = Path(run_dir) / "report.json"
    if not path.is_file():
        raise TeachError(f"no run report at {path} for a normal run")
    report = RunReport.model_validate_json(path.read_text(encoding="utf-8"))
    if report.workflow_name != base.name:
        raise TeachError(
            f"the normal run at {run_dir} came from {report.workflow_name!r}, "
            f"not {base.name!r}"
        )
    if not report.success or report.halt is not None:
        raise TeachError(
            f"the normal run at {run_dir} did not succeed. A normal run must "
            "have gone the usual way to the end"
        )
    return report, screen_texts(Path(run_dir), report)


def _categorical(base: Workflow, name: str) -> bool:
    """A parameter a guard may compare: declared, not secret, and an enum or
    boolean (so the value is a category such as a payer type, not a person's
    data)."""
    if name in base.secret_params:
        return False
    spec = base.param_specs.get(name)
    return spec is not None and spec.type in (ParamKind.ENUM, ParamKind.BOOLEAN)


def _narrowing_param(
    examples: Sequence[_Example],
    overlapping: Sequence[dict[str, str]],
    base: Workflow,
) -> tuple[Optional[tuple[str, str]], str]:
    shared = dict(examples[0].params)
    for ex in examples[1:]:
        shared = {k: v for k, v in shared.items() if ex.params.get(k) == v}
    usable = [
        (k, v)
        for k, v in sorted(shared.items())
        if _categorical(base, k) and all(n.get(k) not in (None, v) for n in overlapping)
    ]
    if not usable:
        return None, (
            "the shared screen text also appears in a normal run, and no enum "
            "or boolean parameter has one value in every example and another "
            "in every such normal run, so nothing observed tells the cases apart"
        )
    if len(usable) > 1:
        names = ", ".join(k for k, _ in usable)
        return None, (
            f"several parameters ({names}) separate the examples from the "
            "normal runs. Add a normal run that tells them apart"
        )
    return usable[0], ""


def _example_narration(example: BranchExample, extra: Sequence) -> list[str]:
    """Sentences spoken while demonstrating this example's fix (or its run)."""
    from openadapt_flow.narration import NarrationRefused, load_narration

    texts: list[str] = []
    for where in (Path(example.fix), Path(example.run_dir)):
        if not where.is_dir():
            continue
        try:
            nar = load_narration(where)
        except (NarrationRefused, ValueError):
            nar = None
        if nar is not None:
            texts.extend(line.text for line in nar.lines)
    for nar in extra:
        texts.extend(line.text for line in nar.lines)
    return texts


def _narrated_param(
    sentences: Sequence[str],
    examples: Sequence["_Example"],
    base: Workflow,
    others: Sequence[dict[str, str]] = (),
) -> Optional[tuple[str, str, str]]:
    """``(param, value, sentence)`` a narrated rule names, or None.

    Only a categorical parameter (see :func:`_categorical`) whose value some
    example or normal run (``others``) had can be named. Whether the runs
    agree is checked by the caller.
    """
    from openadapt_flow.compiler.narration_rules import (
        FieldValues,
        _match_field,
        parse_rule,
    )

    runs = [ex.params for ex in examples] + list(others)
    names = sorted({k for r in runs for k in r if _categorical(base, k)})
    fields = [
        FieldValues(param=k, label=k, values=[r.get(k) for r in runs]) for k in names
    ]
    for sentence in sentences:
        rule = parse_rule(sentence)
        if rule is None or not rule.runs_when_condition_holds:
            continue
        matched = _match_field(rule.subject, rule.value, fields)
        if matched is not None:
            return matched[0].param, matched[1], rule.sentence
    return None


def _narrated_guard_state(workflow: Workflow, predicate: Predicate) -> Optional[str]:
    graph = workflow.program
    if graph is None:
        return None
    for sid, state in graph.states.items():
        if any(tr.guard == predicate for tr in state.transitions):
            return sid
        if state.step is not None and state.step.guard is not None:
            if state.step.guard.predicate == predicate:
                return sid
    return None


def _happy_path(program: ProgramGraph) -> list[str]:
    """Intents along the unconditional first edges from the entry."""
    intents: list[str] = []
    seen: set[str] = set()
    cur = program.entry
    while cur in program.states and cur not in seen:
        seen.add(cur)
        state = program.states[cur]
        if state.kind is not StateKind.ACTION or state.step is None:
            break
        intents.append(state.step.intent)
        if not state.transitions:
            break
        cur = state.transitions[0].target
    return intents


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _refused(reason: str, **kw) -> BranchTeachResult:  # type: ignore[no-untyped-def]
    return BranchTeachResult(promoted=False, reason=reason, **kw)


def teach_branch(
    examples: Sequence[BranchExample],
    out: Path | str,
    *,
    bundle: Path | str,
    negatives: Sequence[Path | str] = (),
    skill_id: Optional[str] = None,
    library_dir: Optional[Path | str] = None,
    screen_texts: Optional[Callable[[Path, RunReport], list[str]]] = None,
    narration: Sequence = (),
) -> BranchTeachResult:
    """Learn one guarded branch from two or more resolved halts.

    Returns a :class:`BranchTeachResult`. ``promoted`` False is a governed
    refusal: nothing is written to ``out``. Raises :class:`TeachError` for
    unusable inputs. ``screen_texts`` reads the text of a normal run's screens
    (default: OCR of its recorded frames). ``narration`` adds
    :class:`~openadapt_flow.narration.Narration` objects to the
    ``narration.json`` files found beside the fixes and halted runs.
    """
    if len(examples) < 2:
        raise TeachError(
            "teach-branch needs at least two resolved halts. For one, use "
            "`openadapt-flow teach`"
        )
    base, program = load_base_program(bundle)
    loaded = [_load_example(ex, base, program, Path(bundle)) for ex in examples]
    n_neg = len(negatives)

    first = loaded[0]
    assert first.report.halt is not None
    for ex in loaded[1:]:
        assert ex.report.halt is not None
        if (ex.report.halt.state_id, ex.report.halt.intent) != (
            first.report.halt.state_id,
            first.report.halt.intent,
        ):
            return _refused(
                "the examples stopped at different steps "
                f"({first.report.halt.intent!r} and {ex.report.halt.intent!r}). "
                "A branch is taught for one place in the workflow",
                examples=len(loaded),
                negatives=n_neg,
            )
        if ex.delivered != first.delivered:
            return _refused(
                "in some examples the stopped step had acted and in others it "
                "had not, so the fix would go in different places",
                examples=len(loaded),
                negatives=n_neg,
            )
        fix_a = [(s.intent, s.action) for s in first.spec.resolution_steps]
        fix_b = [(s.intent, s.action) for s in ex.spec.resolution_steps]
        if fix_a != fix_b:
            return _refused(
                "the examples were resolved differently "
                f"({[i for i, _ in fix_a]} and {[i for i, _ in fix_b]}). A "
                "branch needs the same fix each time",
                examples=len(loaded),
                negatives=n_neg,
            )

    labels = [label for ex in loaded for label in ex.labels]
    candidates = find_signature([ex.new_texts for ex in loaded], labels=labels)
    if not candidates:
        return _refused(
            "no text was new on every example's halt screen, so there is no "
            "shared signature to key the branch on",
            examples=len(loaded),
            negatives=n_neg,
        )

    negative_runs = [
        _load_negative(Path(n), base, screen_texts or _run_texts) for n in negatives
    ]
    clean = [
        c for c in candidates if not any(_contains(t, c) for _, t in negative_runs)
    ]
    text = clean[0] if clean else candidates[0]
    overlapping = [
        runtime_params_for_gui(r.params)
        for r, t in negative_runs
        if _contains(t, text)
    ]
    sentences = [
        line for ex in examples for line in _example_narration(ex, ())
    ] + [line.text for nar in narration for line in nar.lines]
    spoken = _narrated_param(
        sentences,
        loaded,
        base,
        [runtime_params_for_gui(r.params) for r, _ in negative_runs],
    )
    spoken_ok = spoken is not None and all(
        ex.params.get(spoken[0]) == spoken[1] for ex in loaded
    ) and all(n.get(spoken[0]) not in (None, spoken[1]) for n in overlapping)
    said = {
        "narration": spoken[2] if spoken else None,
        "narration_status": (
            None if spoken is None else "certified" if spoken_ok else "contradicted"
        ),
    }
    if spoken_ok:
        assert spoken is not None
        signature = BranchSignature(text=text, param=spoken[0], value=spoken[1])
    elif clean:
        signature = BranchSignature(text=text)
    else:
        narrowed, why = _narrowing_param(loaded, overlapping, base)
        if narrowed is None:
            return _refused(why, examples=len(loaded), negatives=n_neg, **said)
        signature = BranchSignature(text=text, param=narrowed[0], value=narrowed[1])

    # One correction trace per example, all keyed on the shared signature.
    recorded: dict[str, Step] = {}
    carried: dict[str, Path] = {}
    for ex in loaded:
        for intent, step in ex.recorded.items():
            recorded.setdefault(intent, step)
        if not carried and ex.carried:
            carried = dict(ex.carried)
    corrections: list[ExecutionTrace] = []
    baseline: Optional[ExecutionTrace] = None
    for i, ex in enumerate(loaded):
        spec = ex.spec.model_copy(
            update={"facts": {signature.text: True}, "params": ex.params}
        )
        plan = _plan_correction(
            spec, program, ex.report, run_dir=ex.run_dir, recorded=recorded
        )
        if plan.refusal:
            return _refused(
                plan.refusal, signature=signature, examples=len(loaded), negatives=n_neg
            )
        assert plan.correction is not None
        corrections.append(
            plan.correction.model_copy(
                update={"trace_id": f"{base.name}-branch-example-{i}"}
            )
        )
        baseline = baseline or plan.baseline

    happy = _happy_path(program)
    contrasts = [
        ExecutionTrace(
            trace_id=f"{base.name}-normal-run-{i}",
            outcome="success",
            steps=[TraceStep(intent=intent) for intent in happy],
            facts={signature.text: _contains(texts, signature.text)},
            params=runtime_params_for_gui(report.params),
        )
        for i, (report, texts) in enumerate(negative_runs)
    ]

    sid = skill_id or base.name
    lib_root = (
        Path(library_dir)
        if library_dir is not None
        else Path(out).parent / f"{Path(out).name}.skills"
    )
    library = SkillLibrary(lib_root)
    _seed_or_check_library(
        library, sid, program, dict(base.subflows), bundle_dir=bundle
    )
    library.extend_corpus(
        sid,
        [
            execution_trace_from_halt(ex.report, trace_id=f"{sid}-branch-halt-{i}")
            for i, ex in enumerate(loaded)
        ],
    )
    library.extend_corpus(sid, [t for t in [baseline] if t is not None] + contrasts)
    outcome = learn_from_traces(
        library,
        sid,
        corrections,
        inducer=_NarrowedInducer(signature, recorded),
        gate=RegressionGate(),
        canary=_real_target_canary(Path(bundle), carried),
    )
    if not outcome.promoted:
        return BranchTeachResult(
            promoted=False,
            reason=outcome.reason,
            signature=signature,
            examples=len(loaded),
            negatives=n_neg,
            outcome=outcome,
        )

    updated = promoted_workflow(library, sid, name=base.name)
    graph = updated.program
    assert graph is not None
    for trace in [*corrections, *contrasts]:
        if not program_reproduces(
            graph, trace, subflows=dict(updated.subflows)
        ).reproduced:
            return BranchTeachResult(
                promoted=False,
                reason=(
                    f"the learned program does not follow {trace.trace_id!r} "
                    "the way that run went, so it is not written"
                ),
                signature=signature,
                examples=len(loaded),
                negatives=n_neg,
                outcome=outcome,
            )

    updated = updated.model_copy(
        update={
            "params": dict(base.params),
            "param_specs": dict(base.param_specs),
            "secret_params": list(base.secret_params),
            "data_sources": dict(base.data_sources),
        }
    )
    if spoken_ok:
        assert spoken is not None
        where = _narrated_guard_state(updated, signature.predicate())
        if where is not None:
            updated = updated.model_copy(
                update={
                    "guard_sources": {
                        **updated.guard_sources,
                        where: GuardSource(
                            sentence=spoken[2],
                            rule=f"take the fix only when {signature.describe()}",
                            certified_by="recordings_agree",
                        ),
                    }
                }
            )
    out_bundle = Path(out)
    _copy_templates(Path(bundle), out_bundle)
    _copy_carried(carried, out_bundle)
    evidence = {
        "kind": "taught_branch",
        "examples": len(loaded),
        "normal_runs": n_neg,
        "stopped_at": first.report.halt.intent,
        "fix": [s.intent for s in first.spec.resolution_steps],
        "condition": {
            "text_sha256": _digest(signature.text),
            "param": signature.param,
            "narration_sha256": _digest(spoken[2]) if spoken_ok and spoken else None,
        },
        "example_run_reports_sha256": [
            _digest((ex.run_dir / "report.json").read_text(encoding="utf-8"))
            for ex in loaded
        ],
        "runtime_model_calls": 0,
    }
    (out_bundle / "repair").mkdir(parents=True, exist_ok=True)
    (out_bundle / "repair" / "branch_induction.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    updated.save(out_bundle)

    from openadapt_flow.repair.registration import register_bundle_candidate

    register_bundle_candidate(
        Path(bundle), out_bundle, source="teach", evidence_run_dir=first.run_dir
    )
    return BranchTeachResult(
        promoted=True,
        signature=signature,
        examples=len(loaded),
        negatives=n_neg,
        outcome=outcome,
        out_bundle=out_bundle,
        **said,
    )
