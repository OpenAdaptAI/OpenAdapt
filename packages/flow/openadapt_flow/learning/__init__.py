"""Continuous skill learning: a versioned, GOVERNED learn/promote loop.

The review's item 7 -- "cluster successful and failed traces, update the inferred
state machine, test candidate revisions on held-out executions, and promote only
verified versions" -- assembled as ORCHESTRATION over parts that already exist:

- the Phase-2 :class:`~openadapt_flow.ir.ProgramGraph` state machine (the thing
  learned), replayed for coverage by a symbolic ``$0`` interpreter
  (:mod:`openadapt_flow.learning.interpreter`);
- PR #70's promotion posture -- a deterministic regression GATE (identity /
  effect / risk may not regress) then a CANARY -- lifted from one heal patch to a
  whole program revision (:mod:`openadapt_flow.learning.gate`,
  :mod:`openadapt_flow.learning.loop`);
- a versioned, persistent :class:`~openadapt_flow.learning.library.SkillLibrary`
  that keeps every revision's provenance and status, never silently adopting an
  unverified one.

Multi-trace INDUCTION (turning traces into a revised graph) is a sibling PR,
depended on only through the :class:`~openadapt_flow.learning.loop.Inducer`
Protocol; :mod:`openadapt_flow.learning.synth_stream` provides a synthetic
drift-stream harness and a deterministic reference inducer to exercise the loop.

No model calls anywhere on the runtime path.
"""

from openadapt_flow.learning.clustering import (  # noqa: F401
    TraceClusters,
    cluster_traces,
)
from openadapt_flow.learning.gate import (  # noqa: F401
    ProgramGateReport,
    StepGateVerdict,
    program_regression_gate,
)
from openadapt_flow.learning.halt_loop import (  # noqa: F401
    execution_trace_from_halt,
    learn_from_halt,
    promoted_workflow,
    resolution_demonstration,
)
from openadapt_flow.learning.interpreter import (  # noqa: F401
    ReproResult,
    predicate_holds,
    program_reproduces,
)
from openadapt_flow.learning.library import (  # noqa: F401
    Provenance,
    Skill,
    SkillLibrary,
    SkillVersion,
)
from openadapt_flow.learning.loop import (  # noqa: F401
    CanaryContext,
    Inducer,
    LearnOutcome,
    learn_from_traces,
)
from openadapt_flow.learning.trace import (  # noqa: F401
    ExecutionTrace,
    TraceStep,
)

__all__ = [
    # trace
    "ExecutionTrace",
    "TraceStep",
    # clustering
    "cluster_traces",
    "TraceClusters",
    # interpreter
    "program_reproduces",
    "predicate_holds",
    "ReproResult",
    # library
    "SkillLibrary",
    "Skill",
    "SkillVersion",
    "Provenance",
    # gate
    "program_regression_gate",
    "ProgramGateReport",
    "StepGateVerdict",
    # loop
    "learn_from_traces",
    "LearnOutcome",
    "Inducer",
    "CanaryContext",
    # halt->learn loop
    "execution_trace_from_halt",
    "resolution_demonstration",
    "learn_from_halt",
    "promoted_workflow",
]
