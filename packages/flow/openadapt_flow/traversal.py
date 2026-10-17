"""Canonical traversal of a compiled workflow's action steps.

A bundle keeps its executable actions in one of two shapes:

- a LINEAR (v0) bundle stores them in ``Workflow.steps``;
- a PROGRAM (state-machine, RFC ``docs/design/WORKFLOW_PROGRAM_IR.md`` §2)
  bundle stores them as ``ACTION`` states inside ``Workflow.program`` and its
  reusable ``Workflow.subflows`` — and such a bundle often has an EMPTY
  ``Workflow.steps`` (the actions live only in the graph).

Any analysis that must inspect EVERY action a bundle can execute — the policy
certifier and the linter especially, where "certified safe" must cover the
WHOLE program and not just the linear list — MUST walk both shapes. Iterating
``Workflow.steps`` alone silently sees "zero steps" for a program-mode bundle,
so a state-machine bundle full of unsafe writes would certify as vacuously
clean. This module is the ONE canonical generator that closes that hole; every
policy/lint check routes through it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from openadapt_flow.ir import StateKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openadapt_flow.ir import Step, Workflow


def iter_workflow_steps(workflow: "Workflow") -> Iterator["Step"]:
    """Yield every action :class:`~openadapt_flow.ir.Step` a bundle can execute.

    - Linear bundle (``workflow.program is None``): yields ``workflow.steps`` in
      order — identical to the pre-program behaviour.
    - Program bundle: yields the ``step`` of every ``ACTION`` state in the
      top-level ``program`` graph AND in every ``subflow`` graph. Non-action
      states (branch / loop / subflow_call / terminal) carry no step and are
      skipped, as are action states with no ``step`` set.

    The yielded steps are the exact objects a policy/lint check must inspect —
    no state that can drive a write is left out, regardless of bundle shape.
    """
    if workflow.program is None:
        yield from workflow.steps
        return
    for graph in (workflow.program, *workflow.subflows.values()):
        for state in graph.states.values():
            if state.kind is StateKind.ACTION and state.step is not None:
                yield state.step
