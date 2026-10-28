"""Self-serve HALT -> LEARN -> RESOLVE, one command (``openadapt-flow teach``).

The halt->learn LOOP already exists as a governed LIBRARY capability
(:mod:`openadapt_flow.learning.halt_loop`): a run emits ``RunReport.halt``, and
:func:`~openadapt_flow.learning.halt_loop.learn_from_halt` induces the operator's
resolution as a guarded Phase-2 exception branch, gates it, validates it on
held-out coverage, and promotes ONLY a verified revision. What was missing was a
clean, one-command way for an OPERATOR to drive it. This module is that flow.

Given a halted run directory (its ``report.json`` carries the ``halt``) and a
DEMONSTRATION of the fix, :func:`teach` wires the EXISTING loop end to end:

1. Load the halted :class:`~openadapt_flow.ir.RunReport` and the base bundle that
   halted (its :class:`~openadapt_flow.ir.ProgramGraph`, or the degenerate lift
   of a linear bundle).
2. Turn the fix demonstration into the operator-correction
   :class:`~openadapt_flow.learning.trace.ExecutionTrace`. The fix source is
   flexible: a scripted/parametrized CORRECTION SPEC (deterministic, for CI) or a
   RECORDING directory the operator produced for the resolution (reusing the
   ordinary ``compile_recording`` path). The originally-blocked step and the
   remaining tail are read off the base program, and a clean pre-drift success is
   synthesized from the base happy path -- so the operator only demonstrates the
   NEW corrective actions (e.g. dismiss the dialog).
3. Run the UNCHANGED :func:`~openadapt_flow.learning.halt_loop.learn_from_halt`
   (induce -> RegressionGate -> held-out canary). ONLY if it promotes does
   :func:`teach` write an UPDATED, versioned bundle to ``out`` (the promoted
   program plus the base bundle's templates), so a re-run no longer halts on that
   situation. If the correction is underdetermined or would weaken a safety
   invariant, the loop REFUSES: nothing is written, the base bundle stays
   halting, and :func:`teach` reports why (a nonzero exit at the CLI).

Deterministic and ``$0`` on the shipped path: the resolution is induced by the
model-free reference inducer
(:class:`~openadapt_flow.learning.synth_stream.StructuralDiffInducer`), which
handles the "an unexpected dialog intercepted the workflow" resolution class
the halt->learn loop was built for. Three things make that work on a real
bundle, not only on the synthetic stream:

- **The recorded target is carried.** A fix RECORDING compiles to steps with
  real anchors. Those steps, and their template crops, become the spliced
  step, so the taught bundle clicks what the operator clicked. A fix given
  only as intents has no on-screen target and is refused.
- **The step goes where the dialog appeared.** When the halted step never
  acted (the dialog blocked its target), the dismiss step is spliced before
  it. When the halted step was delivered and the dialog appeared in response,
  the dismiss step becomes that step's exception handler. If the fix
  recording ends by doing the step again, the learned program retries it;
  otherwise the dismiss step must also reach the step's expected screen. A
  delivered step that can write (irreversible, or with system-of-record
  effects) is never retried: teach refuses, because nothing proves the first
  delivery wrote nothing.
- **The guard keys on new text.** The branch guard uses text that appeared on
  the screen where the run stopped and on no earlier screen of the same run,
  preferring the label of the control the operator clicked. Text that every
  screen shows can't tell the two situations apart.

A model-backed inducer wires in behind the same
:class:`~openadapt_flow.learning.loop.Inducer` seam without touching this flow.
"""

from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Predicate,
    ProgramGraph,
    RunReport,
    StateKind,
    Step,
    StepResult,
    Workflow,
    lift_to_program,
)
from openadapt_flow.learning.halt_loop import (
    execution_trace_from_halt,
    learn_from_halt,
    promoted_workflow,
    resolution_demonstration,
)
from openadapt_flow.learning.library import SkillLibrary
from openadapt_flow.learning.loop import (
    CanaryContext,
    Inducer,
    LearnOutcome,
    ProgramCanaryFn,
)
from openadapt_flow.learning.synth_stream import StructuralDiffInducer
from openadapt_flow.learning.trace import ExecutionTrace, TraceStep
from openadapt_flow.runtime.authorization import runtime_params_for_gui


class TeachError(Exception):
    """A teach cannot even be attempted (bad inputs), as distinct from a
    governed REFUSAL to promote a correction (which is a normal, reported
    outcome -- see :attr:`TeachResult.promoted`)."""


# -- fix demonstration: a scripted correction spec ---------------------------


class CorrectionStep(BaseModel):
    """One corrective action the operator performed to resolve the halt,
    identified by intent (the join key the symbolic interpreter matches on)."""

    intent: str
    action: ActionKind = ActionKind.CLICK


class CorrectionSpec(BaseModel):
    """A scripted/parametrized fix demonstration (deterministic, CI-friendly).

    Only ``resolution_steps`` -- the NEW corrective actions the workflow did not
    know to perform -- are required. Everything else is derived from the halt and
    the base program unless explicitly overridden:

    - ``tail_intents``: the originally-blocked step and any remainder the fix then
      completes. ``None`` reads them off the base program from the halted state.
    - ``facts``: the observed screen fact(s) the learned branch guard keys on.
      ``None`` carries the halt's ``observed_texts`` forward (the normal case).
    - ``params``: the run parameters in scope for the correction.
    """

    resolution_steps: list[CorrectionStep] = Field(default_factory=list)
    tail_intents: Optional[list[str]] = None
    facts: Optional[dict[str, bool]] = None
    params: dict[str, str] = Field(default_factory=dict)


# -- loading the halted run + the base bundle --------------------------------


def load_halt_report(run_dir: Path | str) -> RunReport:
    """Load ``<run_dir>/report.json`` and require a learnable ``halt``.

    Raises :class:`TeachError` when the directory has no report, or the run did
    not halt (there is nothing to teach a successful run).
    """
    run = Path(run_dir)
    report_path = run / "report.json"
    if not report_path.is_file():
        raise TeachError(
            f"no run report at {report_path} -- teach needs the directory of a "
            "run that HALTED (holding report.json)"
        )
    report = RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    if report.halt is None:
        raise TeachError(
            f"the run at {run} did not halt (report.json has no halt observation) "
            "-- there is nothing to teach. teach resolves a HALTED run."
        )
    return report


def load_base_program(bundle_dir: Path | str) -> tuple[Workflow, ProgramGraph]:
    """Load the base bundle that halted and return ``(workflow, program)``.

    A Phase-2 bundle carries its ``program`` graph directly; a linear bundle is
    lifted to the degenerate single-path graph (:func:`lift_to_program`) so the
    same governed learn path applies to both.
    """
    bundle = Path(bundle_dir)
    if not (bundle / "workflow.json").is_file():
        raise TeachError(
            f"no bundle at {bundle} -- teach needs the base bundle that halted "
            "(the one whose replay produced the run report), via --bundle"
        )
    workflow = Workflow.load(bundle)
    program = workflow.program or lift_to_program(workflow)
    return workflow, program


def check_provenance(
    report: RunReport,
    base_workflow: Workflow,
    program: ProgramGraph,
    *,
    run_dir: Path | str,
    bundle_dir: Path | str,
) -> None:
    """Require that the halted run came from the bundle passed as ``--bundle``.

    A mismatched pair can't be taught: the correction would be spliced into a
    program that never ran, and the learn loop would only report a generic
    refusal. Raises :class:`TeachError` before anything is written.
    """
    if report.workflow_name != base_workflow.name:
        raise TeachError(
            f"the run at {run_dir} came from the workflow "
            f"{report.workflow_name!r}, but --bundle {bundle_dir} is the "
            f"workflow {base_workflow.name!r}. Pass the bundle whose replay "
            "stopped."
        )
    halt = report.halt
    assert halt is not None  # guaranteed by load_halt_report
    graphs = [program, *base_workflow.subflows.values()]
    if any(halt.state_id in graph.states for graph in graphs):
        return
    if halt.intent and any(
        state.kind is StateKind.ACTION
        and state.step is not None
        and state.step.intent == halt.intent
        for graph in graphs
        for state in graph.states.values()
    ):
        return
    raise TeachError(
        f"the step where the run at {run_dir} stopped ({halt.intent!r}) is not "
        f"in --bundle {bundle_dir}. Pass the bundle whose replay stopped."
    )


def _seed_or_check_library(
    library: SkillLibrary,
    skill_id: str,
    program: ProgramGraph,
    subflows: dict[str, ProgramGraph],
    *,
    bundle_dir: Path | str,
) -> None:
    """Seed the skill from the bundle, or check an existing skill came from it.

    The library lives beside ``--out`` and outlives a refused teach. A skill
    seeded from a different bundle would make every later teach to that
    ``--out`` learn against the wrong program and refuse, so it is reported
    instead of reused. A skill with any version equal to this bundle's program
    descends from it and is reused, which keeps the promotion lineage.
    """
    if not library.has(skill_id):
        library.create_skill(skill_id, program, subflows=subflows)
        return
    if any(
        _same_program(version.graph, version.subflows, program, subflows)
        for version in library.get(skill_id).versions
    ):
        return
    raise TeachError(
        f"the skill library at {library.root} holds a different program for "
        f"{skill_id!r} than --bundle {bundle_dir}, probably from an earlier "
        "teach with another bundle. Pass a new --out, or --library with a new "
        "directory, or remove that library directory."
    )


def _same_program(
    graph: ProgramGraph,
    subflows: dict[str, ProgramGraph],
    other_graph: ProgramGraph,
    other_subflows: dict[str, ProgramGraph],
) -> bool:
    if graph.model_dump(mode="json") != other_graph.model_dump(mode="json"):
        return False
    return {k: g.model_dump(mode="json") for k, g in subflows.items()} == {
        k: g.model_dump(mode="json") for k, g in other_subflows.items()
    }


# -- deriving the correction from the base program + halt --------------------


def _tail_intents(program: ProgramGraph, report: RunReport) -> tuple[str, ...]:
    """The originally-blocked intent and the linear remainder, read off the base
    program from the halted state.

    Walks unconditional ACTION states from the halt point to the terminal (the
    steps the fix must complete once the obstruction is cleared). Bounded by the
    state count so a cyclic program cannot loop forever.
    """
    halt = report.halt
    assert halt is not None  # guaranteed by load_halt_report
    start_id = halt.state_id
    if start_id not in program.states:
        # Fall back to matching the halted intent (a linear-lifted bundle keys
        # states by a synthetic id, not by the run's state_id).
        start_id = next(
            (
                sid
                for sid, s in program.states.items()
                if s.kind is StateKind.ACTION
                and s.step is not None
                and s.step.intent == halt.intent
            ),
            "",
        )
    intents: list[str] = []
    seen: set[str] = set()
    cur = start_id
    while (
        cur
        and cur in program.states
        and cur not in seen
        and len(intents) <= len(program.states)
    ):
        seen.add(cur)
        state = program.states[cur]
        if state.kind is not StateKind.ACTION or state.step is None:
            break
        intents.append(state.step.intent)
        if not state.transitions:
            break
        cur = state.transitions[0].target
    return tuple(intents)


def _baseline_success(
    program: ProgramGraph, report: RunReport, tail: tuple[str, ...]
) -> ExecutionTrace:
    """A clean, pre-drift success of the base skill -- the contrast trace the
    inducer needs to isolate the branch-guard fact (a fact present WITH the new
    corrective step, absent WITHOUT it).

    The base happy path is ``completed_intents`` (everything that ran before the
    halt) followed by the ``tail`` (the blocked step + remainder), carrying NO
    screen fact -- exactly a run in which the obstruction never appeared.
    """
    halt = report.halt
    assert halt is not None
    steps = [TraceStep(intent=i) for i in halt.completed_intents]
    steps += [TraceStep(intent=i) for i in tail]
    return ExecutionTrace(
        trace_id=f"{report.workflow_name}-clean-baseline",
        outcome="success",
        steps=steps,
    )


# -- where the run stopped -----------------------------------------------------


def _halted_result(report: RunReport) -> Optional[StepResult]:
    """The result of the step the run stopped on, when the report holds it."""
    halt = report.halt
    assert halt is not None
    for result in reversed(report.results):
        if result.step_id == halt.state_id and not result.ok and not result.skipped:
            return result
    return None


def _halted_step(program: ProgramGraph, report: RunReport) -> Optional[Step]:
    """The base program's step the run stopped on (by state id, step id, then a
    unique intent), or None when it can't be told apart."""
    halt = report.halt
    assert halt is not None
    state = program.states.get(halt.state_id)
    if state is not None and state.step is not None:
        return state.step
    steps = [
        s.step
        for s in program.states.values()
        if s.kind is StateKind.ACTION and s.step is not None
    ]
    by_id = [step for step in steps if step.id == halt.state_id]
    if len(by_id) == 1:
        return by_id[0]
    by_intent = [step for step in steps if step.intent == halt.intent]
    return by_intent[0] if len(by_intent) == 1 else None


def _was_delivered(result: Optional[StepResult]) -> bool:
    """True when the halted step's input reached the application before the run
    stopped: its postcondition failed, not its target resolution."""
    return bool(result is not None and result.delivery_attempted)


def _can_write(step: Step) -> bool:
    """The step may change a system of record (the gate's definition)."""
    return step.risk == "irreversible" or bool(step.effects)


# -- the branch guard ----------------------------------------------------------

_QUOTED_LABEL = re.compile(r"'([^']+)'")


def _norm(text: str) -> str:
    return "".join(text.lower().split())


def _similar(a: str, b: str) -> bool:
    """The same on-screen text, allowing small OCR differences."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.9


def _texts_seen_before_halt(
    run_dir: Path,
    report: RunReport,
    halted: Optional[StepResult],
    delivered: bool,
) -> Optional[list[str]]:
    """The text of every screen the run passed through before the obstruction.

    Reads the starting frame of each step that completed, plus the starting
    frame of the halted step when it was delivered (the obstruction appeared
    only after it acted). Returns None when no frame can be read or OCR is
    unavailable.
    """
    frames: list[Path] = []
    for result in report.results:
        if result.before_png is None:
            continue
        completed = result.ok and not result.skipped
        if completed or (delivered and result is halted):
            frames.append(run_dir / result.before_png)
    readable = [frame for frame in frames if frame.is_file()]
    if not readable:
        return None
    try:
        from openadapt_flow.privacy import scrub_text
        from openadapt_flow.vision.ocr import ocr

        seen: list[str] = []
        for frame in readable:
            for line in ocr(frame.read_bytes()):
                text = (scrub_text(line.text) or "").strip()
                if text:
                    seen.append(text)
    except Exception:
        return None
    return seen


def _fix_labels(spec: CorrectionSpec, recorded: dict[str, Step]) -> list[str]:
    """On-screen labels of the controls the fix clicks: a recorded step's OCR
    text, else the quoted label in its intent (``click 'Maybe later'``)."""
    labels: list[str] = []
    for correction_step in spec.resolution_steps:
        step = recorded.get(correction_step.intent)
        if step is not None and step.anchor is not None and step.anchor.ocr_text:
            labels.append(step.anchor.ocr_text)
        labels.extend(_QUOTED_LABEL.findall(correction_step.intent))
    return [label for label in labels if label.strip()]


def _choose_guard_fact(
    observed: list[str], seen: Optional[list[str]], labels: list[str]
) -> Optional[str]:
    """Pick the screen text the learned branch keys on.

    Only text that appeared where the run stopped and on no earlier screen of
    the run qualifies: text every screen shows can't tell the obstruction from
    a normal run. Among the new texts, the label of the control the operator
    clicked wins; otherwise the first in reading order. Without earlier frames
    to compare with, only the fixing control's own label is accepted.
    """
    if seen is None:
        new_texts = [t for t in observed if any(_similar(t, lb) for lb in labels)]
    else:
        new_texts = [t for t in observed if not any(_similar(t, x) for x in seen)]
    for label in labels:
        for text in new_texts:
            if _similar(text, label):
                return text
    return new_texts[0] if new_texts else None


# -- the correction ----------------------------------------------------------


class _Plan(BaseModel):
    """The correction to learn, or the reason teach won't attempt it."""

    model_config = {"arbitrary_types_allowed": True}

    correction: Optional[ExecutionTrace] = None
    baseline: Optional[ExecutionTrace] = None
    refusal: str = ""


def _plan_correction(
    spec: CorrectionSpec,
    program: ProgramGraph,
    report: RunReport,
    *,
    run_dir: Path,
    recorded: dict[str, Step],
) -> _Plan:
    """Build the ``(correction, baseline)`` traces, or a refusal reason.

    Places the fix where the obstruction appeared. ``tail`` is the halted step
    and the remainder of the program.

    - The halted step never acted (the obstruction blocked its target):
      ``completed + fix + tail``.
    - The halted step acted and its postcondition failed, and the fix ends by
      doing that step again (the obstruction stopped it from taking effect):
      ``completed + [halted step, failed] + fix + tail``. The learned program
      retries the step after the fix.
    - The halted step acted and the fix does not repeat it (the step took
      effect and the obstruction covered the result):
      ``completed + [halted step, failed] + fix + tail[1:]``.

    Retrying a step that can write is refused: nothing shows that its first
    delivery wrote nothing.
    """
    if not spec.resolution_steps:
        raise TeachError(
            "the correction spec has no resolution_steps -- a fix must "
            "demonstrate at least one corrective action"
        )
    halt = report.halt
    assert halt is not None
    halted = _halted_result(report)
    delivered = _was_delivered(halted)
    halted_step = _halted_step(program, report)
    fix_steps = list(spec.resolution_steps)
    # A fix that ends by doing the halted step again: the learned program
    # performs that step after the fix itself, so it is not a new step.
    repeats_halted = fix_steps[-1].intent == halt.intent
    if repeats_halted:
        fix_steps = fix_steps[:-1]
    if not fix_steps:
        raise TeachError(
            "the fix demonstrates no action other than the step where the run stopped"
        )
    tail = (
        tuple(spec.tail_intents)
        if spec.tail_intents is not None
        else _tail_intents(program, report)
    )
    clean_tail = tail  # a run where the obstruction never appeared
    halt_trace = execution_trace_from_halt(
        report, trace_id=f"{report.workflow_name}-halt-probe"
    )
    if delivered:
        if halted_step is None:
            return _Plan(
                refusal=(
                    f"the run stopped after {halt.intent!r} acted, and teach "
                    "can't find that step in --bundle, so it can't place the "
                    "fix after it"
                )
            )
        if repeats_halted and _can_write(halted_step):
            return _Plan(
                refusal=(
                    f"the run stopped after {halt.intent!r} was delivered, and "
                    "that step can write to the record. The fix repeats it, "
                    "and nothing shows that the first delivery wrote nothing, "
                    "so the learned workflow could save twice. Check the "
                    "record before you run the workflow again"
                )
            )
        if not repeats_halted and halted_step.effects:
            return _Plan(
                refusal=(
                    f"the run stopped after {halt.intent!r} was delivered, and "
                    "its write is checked against the system of record only "
                    "when its screen check passes. After the fix nothing would "
                    "check that write, so teach won't learn this fix"
                )
            )
        halt_trace.steps.append(
            TraceStep(intent=halt.intent, action=halted_step.action, failed=True)
        )
        if not repeats_halted and tail and tail[0] == halt.intent:
            tail = tail[1:]
    if spec.facts is not None:
        facts = dict(spec.facts)
    else:
        fact = _choose_guard_fact(
            list(halt.observed_texts),
            _texts_seen_before_halt(run_dir, report, halted, delivered),
            _fix_labels(spec, recorded),
        )
        if fact is None:
            return _Plan(
                refusal=(
                    "no text on the screen where the run stopped was new, so "
                    "teach can't tell when the fix applies. Pass the text to "
                    "key on as 'facts' in a .json correction spec"
                )
            )
        facts = {fact: True}
    halt_trace = halt_trace.model_copy(update={"facts": facts})
    correction = resolution_demonstration(
        halt_trace,
        resolution_steps=[
            TraceStep(intent=s.intent, action=s.action) for s in fix_steps
        ],
        tail_intents=tail,
        trace_id=f"{report.workflow_name}-correction",
        params=spec.params or runtime_params_for_gui(report.params),
    )
    return _Plan(
        correction=correction,
        baseline=_baseline_success(program, report, clean_tail),
    )


def _rebase_anchor(
    anchor: Optional[Anchor], prefix: str, carried: dict[str, Path], source: Path
) -> Optional[Anchor]:
    """Point an anchor's crops at ``templates/<prefix>/...`` in the taught
    bundle and remember which source file each new path copies from."""
    if anchor is None:
        return None
    update: dict[str, object] = {}
    for field in ("template", "identifier_crop"):
        rel = getattr(anchor, field)
        if rel and rel.startswith("templates/"):
            new_rel = f"templates/{prefix}/{rel[len('templates/') :]}"
            carried[new_rel] = source / rel
            update[field] = new_rel
    return anchor.model_copy(update=update) if update else anchor


def _rebase_predicate(
    pred: Optional[Predicate], prefix: str, carried: dict[str, Path], source: Path
) -> Optional[Predicate]:
    if pred is None:
        return None
    return pred.model_copy(
        update={
            "anchor": _rebase_anchor(pred.anchor, prefix, carried, source),
            "operands": [
                _rebase_predicate(op, prefix, carried, source) for op in pred.operands
            ],
        }
    )


def _rebase_step(
    step: Step, prefix: str, carried: dict[str, Path], source: Path
) -> Step:
    """A copy of a compiled fix step whose crops resolve in the taught bundle."""
    expect = []
    for post in step.expect:
        if post.template and post.template.startswith("templates/"):
            new_rel = f"templates/{prefix}/{post.template[len('templates/') :]}"
            carried[new_rel] = source / post.template
            post = post.model_copy(update={"template": new_rel})
        expect.append(post)
    guard = step.guard
    if guard is not None:
        guard = guard.model_copy(
            update={
                "predicate": _rebase_predicate(guard.predicate, prefix, carried, source)
            }
        )
    return step.model_copy(
        deep=True,
        update={
            "anchor": _rebase_anchor(step.anchor, prefix, carried, source),
            "drag_end_anchor": _rebase_anchor(
                step.drag_end_anchor, prefix, carried, source
            ),
            "expect": expect,
            "guard": guard,
            "wait_until": _rebase_predicate(step.wait_until, prefix, carried, source),
        },
    )


def _spec_from_recording(
    recording_dir: Path, report: RunReport
) -> tuple[CorrectionSpec, dict[str, Step], dict[str, Path]]:
    """Compile a RECORDING of the resolution into a spec plus its real targets.

    The recording is compiled through the ordinary single-trace bootstrap
    (:func:`compile_recording`). Its steps become the corrective
    ``resolution_steps`` (the NEW actions the operator demonstrated, e.g.
    dismiss the dialog), and each compiled step, with its recorded anchor, is
    returned so the inducer splices the real target rather than a placeholder.
    When the recording ends by doing the step the run stopped on again, the
    learned program retries that step (see :func:`_plan_correction`). Returns
    ``(spec, recorded steps by intent, taught-bundle path -> source crop)``.
    """
    from openadapt_flow.compiler.compile import compile_recording

    recording = Path(recording_dir)
    if not (recording / "events.jsonl").is_file():
        raise TeachError(
            f"{recording} is not a recording directory (no events.jsonl). Pass a "
            "recording produced by `openadapt-flow record`, or a .json "
            "correction spec."
        )
    compiled_dir = recording / "_teach_compiled"
    compiled = compile_recording(
        recording, compiled_dir, name=f"{report.workflow_name}-fix"
    )
    fix_steps = list(compiled.steps)
    if not fix_steps:
        raise TeachError(
            f"the fix recording {recording} compiled to zero steps -- it "
            "demonstrates no corrective action"
        )
    prefix = "teach-" + re.sub(r"[^A-Za-z0-9_-]+", "-", compiled.recording_id or "fix")
    carried: dict[str, Path] = {}
    recorded = {
        s.intent: _rebase_step(s, prefix, carried, compiled_dir) for s in fix_steps
    }
    spec = CorrectionSpec(
        resolution_steps=[
            CorrectionStep(intent=s.intent, action=s.action) for s in fix_steps
        ]
    )
    return spec, recorded, carried


def _load_fix(fix: Path | str) -> Optional[CorrectionSpec]:
    """Return a parsed :class:`CorrectionSpec` when ``fix`` is a spec file, else
    ``None`` (signalling a recording directory)."""
    path = Path(fix)
    if path.is_file() and path.suffix.lower() == ".json":
        try:
            return CorrectionSpec.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise TeachError(f"invalid correction spec {path}: {e}")
    if path.is_dir():
        return None
    raise TeachError(
        f"--fix {path} is neither a .json correction spec nor a recording directory"
    )


# -- the flow ----------------------------------------------------------------


class TeachResult(BaseModel):
    """The outcome of one :func:`teach` invocation."""

    model_config = {"arbitrary_types_allowed": True}

    skill_id: str
    promoted: bool
    outcome: LearnOutcome
    out_bundle: Optional[Path] = None

    @property
    def refused(self) -> bool:
        return not self.promoted

    def summary(self) -> str:
        """A short operator-facing verdict (what it learned / why it refused)."""
        verdict = "LEARNED" if self.promoted else "REFUSED"
        lines = [
            f"{verdict}: {self.outcome.action} (skill {self.skill_id!r})",
            f"  reason: {self.outcome.reason}",
        ]
        if self.outcome.clusters:
            lines.append(f"  clusters: {self.outcome.clusters}")
        if self.outcome.action != _NOT_ATTEMPTED:
            lines.append(
                "  held-out coverage: "
                f"{self.outcome.coverage_before:.2f} -> "
                f"{self.outcome.coverage_after:.2f}"
            )
        if self.outcome.gate is not None:
            gate = "passed" if self.outcome.gate.passed else "FAILED"
            lines.append(f"  regression gate: {gate}")
            for failure in self.outcome.gate.failures:
                lines.append(f"    - {failure}")
        if self.promoted and self.out_bundle is not None:
            lines.append(f"  updated bundle written to {self.out_bundle}")
            lines.append(
                "  the updated bundle is an unverified repair candidate. "
                "Replay it on the screen where the run stopped before you "
                "rely on it"
            )
        else:
            lines.append("  bundle UNCHANGED -- the workflow stays halting here")
        return "\n".join(lines)


#: ``LearnOutcome.action`` for a refusal decided before the learn loop ran.
_NOT_ATTEMPTED = "not_attempted"


def _refusal(skill_id: str, reason: str) -> TeachResult:
    return TeachResult(
        skill_id=skill_id,
        promoted=False,
        outcome=LearnOutcome(
            skill_id=skill_id,
            action=_NOT_ATTEMPTED,
            reason=reason,
            active_version=0,
        ),
    )


_TARGETED_ACTIONS = frozenset(
    {
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
        ActionKind.DRAG,
    }
)


def _anchor_key(anchor: Anchor) -> tuple[object, ...]:
    return (anchor.template, anchor.region, anchor.click_point)


def _real_target_canary(bundle_dir: Path, carried: dict[str, Path]) -> ProgramCanaryFn:
    """Veto a candidate whose NEW on-screen target has no crop to resolve.

    A step the base program already had keeps its anchors. A spliced click or
    drag must point at a crop that exists, either in the base bundle or among
    the crops carried from the fix recording. A fix given only as intents has
    no recorded target, so the inducer fills in a placeholder whose crop does
    not exist: the taught bundle would stop at the same place, so it is
    refused instead.
    """

    def canary(ctx: CanaryContext) -> tuple[bool, str]:
        known: set[tuple[object, ...]] = set()
        for graph in [ctx.active, *ctx.subflows.values()]:
            for state in graph.states.values():
                if state.step is None:
                    continue
                for anchor in (state.step.anchor, state.step.drag_end_anchor):
                    if anchor is not None:
                        known.add(_anchor_key(anchor))
        for graph in [ctx.candidate, *ctx.subflows.values()]:
            for state in graph.states.values():
                step = state.step
                if step is None or step.action not in _TARGETED_ACTIONS:
                    continue
                anchors = [step.anchor]
                if step.action is ActionKind.DRAG:
                    anchors.append(step.drag_end_anchor)
                for anchor in anchors:
                    if anchor is not None and _anchor_key(anchor) in known:
                        continue
                    if anchor is not None and (
                        anchor.template in carried
                        or (bundle_dir / anchor.template).is_file()
                    ):
                        continue
                    return False, (
                        f"the fix step {step.intent!r} has no recorded on-screen "
                        "target, so the taught bundle would stop at the same "
                        "place. Record the fix (openadapt-flow record) and pass "
                        "the recording as --fix"
                    )
        return True, ""

    return canary


def _copy_templates(src_bundle: Path, out_bundle: Path) -> None:
    """Carry the base bundle's template crops (subfolders included, such as
    identifier crops) into the updated bundle so the promoted program can
    resolve its anchors on re-run."""
    src_templates = src_bundle / "templates"
    if not src_templates.is_dir():
        return
    shutil.copytree(src_templates, out_bundle / "templates", dirs_exist_ok=True)


def _copy_carried(carried: dict[str, Path], out_bundle: Path) -> None:
    """Copy the fix recording's crops to the paths the spliced step uses."""
    for rel, source in carried.items():
        if not source.is_file():
            raise TeachError(f"the fix recording's crop {source} is missing")
        destination = out_bundle / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def teach(
    run_dir: Path | str,
    fix: Path | str,
    out: Path | str,
    *,
    bundle: Path | str,
    skill_id: Optional[str] = None,
    library_dir: Optional[Path | str] = None,
    inducer: Optional[Inducer] = None,
) -> TeachResult:
    """Drive the governed halt->learn loop from a halted run + a fix demonstration.

    Args:
        run_dir: The HALTED run directory (holds ``report.json`` with a ``halt``).
        fix: The fix demonstration -- a ``.json`` :class:`CorrectionSpec` (scripted
            / CI) or a recording directory of the resolution.
        out: Where to write the UPDATED bundle -- ONLY when the loop promotes.
        bundle: The base bundle that halted (seeds the skill's active version).
        skill_id: Skill id in the library (default: the run's workflow name).
        library_dir: Where the versioned :class:`SkillLibrary` lives (default:
            ``<out>.skills`` -- a SIBLING of ``out`` so ``out`` is created only
            when a bundle is actually promoted; the promotion lineage is kept).
        inducer: The resolution inducer (default: the model-free reference
            :class:`StructuralDiffInducer`, given the fix recording's steps).

    Returns:
        A :class:`TeachResult`. ``promoted`` True means an updated bundle was
        written to ``out``; ``promoted`` False is a GOVERNED refusal (bundle
        unchanged, reason in the outcome) -- NOT raised, so the CLI can report it
        and exit nonzero.

    Raises:
        TeachError: The inputs are unusable (no halt, no bundle, a malformed
            fix, a run that did not come from ``bundle``, or a library at
            ``library_dir`` seeded from a different bundle).
    """
    report = load_halt_report(run_dir)
    base_workflow, program = load_base_program(bundle)
    check_provenance(report, base_workflow, program, run_dir=run_dir, bundle_dir=bundle)
    sid = skill_id or report.workflow_name

    spec = _load_fix(fix)
    recorded: dict[str, Step] = {}
    carried: dict[str, Path] = {}
    if spec is None:
        spec, recorded, carried = _spec_from_recording(Path(fix), report)
    plan = _plan_correction(
        spec, program, report, run_dir=Path(run_dir), recorded=recorded
    )
    if plan.refusal:
        # Refused before learning: nothing is written, not even a library.
        return _refusal(sid, plan.refusal)
    assert plan.correction is not None and plan.baseline is not None

    lib_root = (
        Path(library_dir)
        if library_dir is not None
        else Path(out).parent / f"{Path(out).name}.skills"
    )
    library = SkillLibrary(lib_root)
    _seed_or_check_library(
        library, sid, program, dict(base_workflow.subflows), bundle_dir=bundle
    )

    outcome, _ = learn_from_halt(
        library,
        sid,
        halt_report=report,
        correction=plan.correction,
        inducer=inducer or StructuralDiffInducer(recorded_steps=recorded),
        baseline=[plan.baseline],
        canary=_real_target_canary(Path(bundle), carried),
    )

    if not outcome.promoted:
        # Governed refusal: leave the bundle untouched -- the workflow stays
        # halting exactly as before the teach was attempted.
        return TeachResult(
            skill_id=sid, promoted=False, outcome=outcome, out_bundle=None
        )

    updated = promoted_workflow(library, sid, name=base_workflow.name)
    # Preserve the base bundle's params / secret manifest on the updated program.
    updated = updated.model_copy(
        update={
            "params": dict(base_workflow.params),
            "param_specs": dict(base_workflow.param_specs),
            "secret_params": list(base_workflow.secret_params),
            "data_sources": dict(base_workflow.data_sources),
        }
    )
    out_bundle = Path(out)
    # Crops go in first: saving seals a manifest over the files present.
    _copy_templates(Path(bundle), out_bundle)
    _copy_carried(carried, out_bundle)
    updated.save(out_bundle)
    # Governed repair promotion (Section 9): the taught bundle is a lifecycle
    # CANDIDATE, never an implicitly active bundle. A detached
    # repair/candidate.json is written into it; promotion to active requires
    # the explicit lifecycle (campaigns, human approval, staged canary) via
    # `openadapt-flow repair`. Registration failure fails CLOSED by absence:
    # without the record the taught bundle cannot enter the lifecycle at all.
    from openadapt_flow.repair.registration import register_bundle_candidate

    try:
        register_bundle_candidate(
            Path(bundle),
            out_bundle,
            source="teach",
            evidence_run_dir=Path(run_dir),
        )
    except Exception as exc:
        print(
            "repair candidate registration failed (the taught bundle stays "
            f"ungoverned and non-promotable): {exc}"
        )
    return TeachResult(
        skill_id=sid, promoted=True, outcome=outcome, out_bundle=out_bundle
    )
