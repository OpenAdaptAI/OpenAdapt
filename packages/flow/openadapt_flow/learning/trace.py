"""The synthetic EXECUTION TRACE the continuous-learning loop consumes.

A :class:`ExecutionTrace` is a deterministic, model-free record of ONE run of a
skill: the ordered actions the operator/agent actually performed (by intent),
the branch-relevant screen facts observed at decision points, and the run's
outcome. It is the substrate the learn/promote loop clusters, replays against a
candidate :class:`~openadapt_flow.ir.ProgramGraph`, and validates on.

Crucially it carries the parts a straight-line trajectory cannot: which OPTIONAL
dialogs actually appeared (``facts``), and the run parameters (``params``), so a
symbolic graph walk can decide the SAME branches the live Phase-2 interpreter
would (``learning.interpreter``). No pixels, no model calls -- a trace is the
structural shadow of an execution, exactly what a multi-trace inducer generalises
over (``docs/design/WORKFLOW_PROGRAM_IR.md`` section 3).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import ActionKind

Outcome = Literal["success", "failure"]


class TraceStep(BaseModel):
    """One action actually performed in an execution, identified by intent.

    ``intent`` is the human-readable purpose that a compiled
    :class:`~openadapt_flow.ir.Step` also carries -- it is the join key the
    symbolic interpreter matches an ``action`` state against, so a trace is
    substrate-neutral (no pixel coordinates, no template paths).
    """

    intent: str
    action: ActionKind = ActionKind.CLICK
    #: The identity band observed for this action, when the step is
    #: identity-relevant (a click on an entity row). Diagnostic for clustering
    #: and induction; the loop never trusts it as a live verification.
    identity: Optional[str] = None


class ExecutionTrace(BaseModel):
    """A single deterministic execution trace of a skill.

    Attributes:
        trace_id: Stable unique id (drives deterministic clustering / splits).
        t: Logical time index within a stream (lets a stream inject drift "at
            trace K").
        outcome: Whether the run reached the skill's success terminal.
        steps: The ordered actions performed, by intent.
        facts: Observed branch-relevant screen facts -- keyed by the TEXT a
            ``TEXT_PRESENT`` predicate would test (e.g. ``{"Consent Required":
            True}``). ``True`` means the text/dialog was present on screen.
        params: The run's parameter values (drives ``PARAM_EQUALS`` branches).
        failure_reason: Free-text note when ``outcome == "failure"``.
    """

    trace_id: str
    t: int = 0
    outcome: Outcome = "success"
    steps: list[TraceStep] = Field(default_factory=list)
    facts: dict[str, bool] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    failure_reason: str = ""

    @property
    def signature(self) -> str:
        """The ordered-intent STRUCTURAL signature -- the deterministic key
        clustering groups variants by. Two traces share a signature iff they
        performed the same actions in the same order."""
        return " > ".join(s.intent for s in self.steps)

    @property
    def succeeded(self) -> bool:
        return self.outcome == "success"
