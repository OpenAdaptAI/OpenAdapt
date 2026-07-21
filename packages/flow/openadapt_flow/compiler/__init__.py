"""Compiler: recording directory -> workflow bundle."""

from openadapt_flow.compiler.annotate import (
    AnnotationResult,
    AnthropicStepAnnotator,
    FakeStepAnnotator,
    StepAnnotator,
    WorkflowProposals,
    apply_annotations,
)
from openadapt_flow.compiler.codegen import render_workflow_py
from openadapt_flow.compiler.compile import compile_recording, lint_param_leakage
from openadapt_flow.compiler.effect_mining import (
    StepEffectMining,
    mine_step_effects,
)
from openadapt_flow.compiler.induction import (
    HeldOutValidation,
    InductionResult,
    Proposer,
    induce_program,
    reproduction_score,
    structural_trace_coverage,
    validate_held_out,
)
from openadapt_flow.compiler.loop_authoring import (
    LoopAuthoringError,
    author_data_driven_loop,
    body_param_names,
    resolve_column_map,
)

__all__ = [
    "compile_recording",
    "lint_param_leakage",
    "render_workflow_py",
    "mine_step_effects",
    "StepEffectMining",
    # Multi-trace induction (RFC §3 [4]+[5]): one demo is the single-trace
    # bootstrap; multiple demos induce a parameterized program or refuse.
    "induce_program",
    "validate_held_out",
    # Structural trace-shape coverage (NOT behavioral validation). The old name
    # ``reproduction_score`` is a deprecated alias kept for back-compat.
    "structural_trace_coverage",
    "reproduction_score",
    # Data-driven LOOP authoring (RFC §2.3): wrap a single-demonstration linear
    # body in a LOOP over a declared worklist, reusing the built interpreter.
    "author_data_driven_loop",
    "resolve_column_map",
    "body_param_names",
    "LoopAuthoringError",
    "InductionResult",
    "HeldOutValidation",
    "Proposer",
    "apply_annotations",
    "AnnotationResult",
    "AnthropicStepAnnotator",
    "FakeStepAnnotator",
    "StepAnnotator",
    "WorkflowProposals",
]
