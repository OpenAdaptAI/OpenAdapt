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
handles the "an unexpected optional dialog intercepted the workflow" resolution
class the halt->learn loop was built for (splice a guarded, reversible dismiss
step keyed on the observed screen fact). A model-backed inducer wires in behind
the same :class:`~openadapt_flow.learning.loop.Inducer` seam without touching
this flow.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import (
    ActionKind,
    ProgramGraph,
    RunReport,
    StateKind,
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
from openadapt_flow.learning.loop import Inducer, LearnOutcome
from openadapt_flow.learning.synth_stream import StructuralDiffInducer
from openadapt_flow.learning.trace import ExecutionTrace, TraceStep


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


def _correction_from_spec(
    spec: CorrectionSpec,
    program: ProgramGraph,
    report: RunReport,
) -> tuple[ExecutionTrace, ExecutionTrace]:
    """Build ``(correction, baseline)`` from a scripted correction spec."""
    if not spec.resolution_steps:
        raise TeachError(
            "the correction spec has no resolution_steps -- a fix must "
            "demonstrate at least one corrective action"
        )
    tail = (
        tuple(spec.tail_intents)
        if spec.tail_intents is not None
        else _tail_intents(program, report)
    )
    halt_trace = execution_trace_from_halt(
        report, trace_id=f"{report.workflow_name}-halt-probe"
    )
    if spec.facts is not None:
        halt_trace = halt_trace.model_copy(update={"facts": dict(spec.facts)})
    resolution_steps = [
        TraceStep(intent=s.intent, action=s.action) for s in spec.resolution_steps
    ]
    correction = resolution_demonstration(
        halt_trace,
        resolution_steps=resolution_steps,
        tail_intents=tail,
        trace_id=f"{report.workflow_name}-correction",
        params=spec.params or dict(report.params),
    )
    return correction, _baseline_success(program, report, tail)


def _correction_from_recording(
    recording_dir: Path,
    program: ProgramGraph,
    report: RunReport,
) -> tuple[ExecutionTrace, ExecutionTrace]:
    """Build ``(correction, baseline)`` from a RECORDING of the resolution.

    The recording is compiled through the ordinary single-trace bootstrap
    (:func:`compile_recording`); its steps become the corrective
    ``resolution_steps`` (the NEW actions the operator demonstrated, e.g. dismiss
    the dialog). The originally-blocked tail is read off the base program and the
    observed screen fact is carried from the halt -- so the operator records ONLY
    the fix, not a re-run of the whole task.
    """
    from openadapt_flow.compiler.compile import compile_recording

    recording = Path(recording_dir)
    if not (recording / "events.jsonl").is_file():
        raise TeachError(
            f"{recording} is not a recording directory (no events.jsonl). Pass a "
            "recording produced by `openadapt-flow record`, or a .json "
            "correction spec."
        )
    compiled = compile_recording(
        recording, recording / "_teach_compiled", name=f"{report.workflow_name}-fix"
    )
    spec = CorrectionSpec(
        resolution_steps=[
            CorrectionStep(intent=s.intent, action=s.action) for s in compiled.steps
        ]
    )
    if not spec.resolution_steps:
        raise TeachError(
            f"the fix recording {recording} compiled to zero steps -- it "
            "demonstrates no corrective action"
        )
    return _correction_from_spec(spec, program, report)


def _load_fix(fix: Path | str) -> Optional[CorrectionSpec]:
    """Return a parsed :class:`CorrectionSpec` when ``fix`` is a spec file, else
    ``None`` (signalling a recording directory)."""
    path = Path(fix)
    if path.is_file() and path.suffix.lower() == ".json":
        try:
            return CorrectionSpec.model_validate_json(path.read_text())
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
        else:
            lines.append("  bundle UNCHANGED -- the workflow stays halting here")
        return "\n".join(lines)


def _copy_templates(src_bundle: Path, out_bundle: Path) -> None:
    """Carry the base bundle's template crops into the updated bundle so the
    promoted program can resolve its anchors on re-run."""
    src_templates = src_bundle / "templates"
    if not src_templates.is_dir():
        return
    dst_templates = out_bundle / "templates"
    dst_templates.mkdir(parents=True, exist_ok=True)
    for crop in src_templates.iterdir():
        if crop.is_file():
            shutil.copy2(crop, dst_templates / crop.name)


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
            :class:`StructuralDiffInducer`).

    Returns:
        A :class:`TeachResult`. ``promoted`` True means an updated bundle was
        written to ``out``; ``promoted`` False is a GOVERNED refusal (bundle
        unchanged, reason in the outcome) -- NOT raised, so the CLI can report it
        and exit nonzero.

    Raises:
        TeachError: The inputs are unusable (no halt, no bundle, a malformed fix).
    """
    report = load_halt_report(run_dir)
    base_workflow, program = load_base_program(bundle)
    sid = skill_id or report.workflow_name

    spec = _load_fix(fix)
    if spec is not None:
        correction, baseline = _correction_from_spec(spec, program, report)
    else:
        correction, baseline = _correction_from_recording(Path(fix), program, report)

    lib_root = (
        Path(library_dir)
        if library_dir is not None
        else Path(out).parent / f"{Path(out).name}.skills"
    )
    library = SkillLibrary(lib_root)
    if not library.has(sid):
        library.create_skill(sid, program, subflows=dict(base_workflow.subflows))

    outcome, _ = learn_from_halt(
        library,
        sid,
        halt_report=report,
        correction=correction,
        inducer=inducer or StructuralDiffInducer(),
        baseline=[baseline],
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
    updated.save(out_bundle)
    _copy_templates(Path(bundle), out_bundle)
    return TeachResult(
        skill_id=sid, promoted=True, outcome=outcome, out_bundle=out_bundle
    )
