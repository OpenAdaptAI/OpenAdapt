"""Opt-in COMPILE-TIME model annotation of a compiled workflow.

The compiler already labels steps, classifies risk, and infers typed parameters
HEURISTICALLY: intent strings are templated from the click's OCR label
(``compile._`` intent building), risk is keyword-matched
(:mod:`openadapt_flow.risk`, issue #65), and every recorded value is typed as a
bare ``string`` (``ParamSpec`` in ``compile_recording``, Phase 1). Those
heuristics are cheap and model-free, but they miss things a language model would
catch at a glance: a write-shaped control the keyword list doesn't cover
(``"Commit charges"``), a demonstrated value that is really a DATE or an
ENTITY_REF rather than a constant string, a terse ``click at (640, 424)`` intent
that a model could render as ``"submit the triage encounter"``.

This module adds a model-backed pass that runs at COMPILE time and is OFF by
default. Compiling is a one-time step, so a single model call there buys runtime
robustness at ZERO per-run cost -- the replayer (``runtime.replayer``) never
calls a model on this path, and nothing here is read at replay (the annotations
live in a bundle sidecar, ``annotations.json``, never in ``workflow.json``'s hot
fields beyond the risk upgrades described below). The runtime ``$0`` guarantee is
therefore preserved unchanged.

Confirm-don't-trust (mirrors #74 disambiguation and #75 effect-mining)
---------------------------------------------------------------------
A model PROPOSES; the compiler never blindly trusts it. Proposals are applied
only in the SAFE direction and are otherwise FLAGGED for an operator, exactly as
the identity ladder refuses rather than guesses:

- A **risk UPGRADE** (``reversible`` -> ``irreversible``) is APPLIED. It only ever
  ARMS a safeguard (the irreversible-step refusals in ``runtime.Replayer``); the
  cost of a false upgrade is availability, never a silent wrong write.
- A **risk DOWNGRADE** (``irreversible`` -> ``reversible``) is NEVER applied -- it
  would DISARM a safeguard. It is flagged ``needs_operator_confirmation`` and the
  heuristic risk stands.
- A **label** proposal is advisory (a human-readable intent for review); it never
  changes replay behaviour, so it is always attached, never gated.
- A **parameter** proposal that only ENRICHES the TYPE of an already-declared
  parameter (``string`` -> ``date`` / ``enum`` / ``entity_ref``) is metadata-only
  and APPLIED. A CONSEQUENTIAL parameter inference -- one that would turn a
  demonstrated constant into a new run-varying parameter, changing what the
  workflow does -- is FLAGGED, never applied.

The model call sits behind the :class:`StepAnnotator` ``Protocol``. Tests use
:class:`FakeStepAnnotator` (deterministic, no network). The real
:class:`AnthropicStepAnnotator` calls Anthropic ONLY at compile and ONLY when the
caller opts in (``compile_recording(..., annotate=True)`` with no annotator, or
by passing an ``AnthropicStepAnnotator`` explicitly); ``anthropic`` and the API
key are imported/resolved LAZILY, so nothing here needs a key unless the real
annotator actually runs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from openadapt_flow.ir import ParamKind, ParamSpec, Step, Workflow

logger = logging.getLogger(__name__)

Risk = str  # "reversible" | "irreversible" (mirrors ir.Step.risk)


# -- proposals (what a StepAnnotator returns) --------------------------------


class LabelProposal(BaseModel):
    """A richer human-readable intent for a step (advisory only)."""

    label: str = Field(description="Proposed human-readable intent")
    rationale: str = ""


class RiskProposal(BaseModel):
    """A proposed refinement to a step's heuristic risk classification."""

    proposed_risk: Risk = Field(description="'reversible' or 'irreversible'")
    rationale: str = ""


class ParamProposal(BaseModel):
    """A proposed typed parameter inference for a demonstrated value.

    ``consequential`` is True when applying the proposal would change what the
    workflow DOES -- i.e. it would turn a demonstrated CONSTANT into a new
    run-varying parameter. A non-consequential proposal only ENRICHES the TYPE of
    a parameter the demonstration already declared (Phase-1 ``string`` ->
    ``date`` / ``enum`` / ``entity_ref``), which is metadata and safe to apply.
    """

    name: str = Field(description="Parameter name this value maps to")
    type: ParamKind = ParamKind.STRING
    example: Optional[str] = None
    choices: list[str] = Field(default_factory=list)
    consequential: bool = Field(
        default=False,
        description=(
            "True when applying would make a demonstrated constant vary per run"
            " (a behaviour change -> FLAG, never apply). False for a pure"
            " type-enrichment of an existing parameter (safe -> apply)."
        ),
    )
    rationale: str = ""


class StepAnnotation(BaseModel):
    """A model's proposals for ONE compiled step (all fields optional)."""

    step_id: str
    label: Optional[LabelProposal] = None
    risk: Optional[RiskProposal] = None
    params: list[ParamProposal] = Field(default_factory=list)


class WorkflowProposals(BaseModel):
    """The raw proposals a :class:`StepAnnotator` returns -- nothing applied."""

    steps: list[StepAnnotation] = Field(default_factory=list)

    def for_step(self, step_id: str) -> Optional[StepAnnotation]:
        for sa in self.steps:
            if sa.step_id == step_id:
                return sa
        return None


# -- the Protocol + a fake + the real (Anthropic) annotator ------------------


@runtime_checkable
class StepAnnotator(Protocol):
    """Given a compiled workflow (+ optional captured state), propose label,
    risk, and parameter refinements. Implementations MUST NOT mutate the
    workflow -- they only PROPOSE; :func:`apply_annotations` decides what is
    safe to apply. The real implementation calls a model; the fake does not."""

    def annotate(
        self, workflow: Workflow, *, captured_state: Optional[dict] = None
    ) -> WorkflowProposals: ...


class FakeStepAnnotator:
    """A deterministic, network-free :class:`StepAnnotator` for tests.

    It returns exactly the ``WorkflowProposals`` it was constructed with (keyed
    per step id), so a test scripts the "model's" output and asserts how
    :func:`apply_annotations` applies vs flags it. No model, no key, no network.
    """

    def __init__(self, proposals: Optional[WorkflowProposals] = None) -> None:
        self._proposals = proposals or WorkflowProposals()

    def annotate(
        self, workflow: Workflow, *, captured_state: Optional[dict] = None
    ) -> WorkflowProposals:
        # Return only proposals whose step id actually exists in the workflow,
        # so a scripted proposal for a removed step is silently ignored (the
        # real model is prompted from the same workflow and cannot invent ids).
        ids = {s.id for s in workflow.steps}
        return WorkflowProposals(
            steps=[sa for sa in self._proposals.steps if sa.step_id in ids]
        )


class AnthropicStepAnnotator:
    """The real, COMPILE-TIME-ONLY, opt-in :class:`StepAnnotator`.

    Calls Anthropic once per compile to propose richer labels, risk refinements,
    and typed-parameter inferences over the compiled workflow's step evidence
    (intent, action, OCR label, current risk, declared params). ``anthropic`` is
    imported LAZILY and the API key is resolved LAZILY (env
    ``ANTHROPIC_API_KEY`` or ``~/.anthropic/api_key`` via the existing
    ``benchmark.agent_baseline.load_api_key``), so importing this module needs no
    key and no network -- only :meth:`annotate` does. NEVER called at replay.
    """

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        client: Any = None,
        max_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._client = client
        self._max_tokens = max_tokens

    def annotate(
        self, workflow: Workflow, *, captured_state: Optional[dict] = None
    ) -> WorkflowProposals:
        client = self._client
        model = self._model
        if client is None or model is None:
            # Lazy: resolve the SDK, key, and default model only when actually
            # invoked (keeps the module import-light and key-free). The default
            # model reuses the repo's single Anthropic-model constant so a bump
            # there follows here too.
            from openadapt_flow.benchmark.agent_baseline import (
                MODEL as DEFAULT_MODEL,
            )
            from openadapt_flow.benchmark.agent_baseline import (
                load_api_key,
            )

            if model is None:
                model = DEFAULT_MODEL
            if client is None:
                import anthropic

                client = anthropic.Anthropic(api_key=load_api_key())

        prompt = _build_prompt(workflow, captured_state)
        response = client.messages.create(
            model=model,
            max_tokens=self._max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return _parse_proposals(text, workflow)


# -- prompt + parsing for the real annotator ---------------------------------

_SYSTEM_PROMPT = (
    "You label steps of a compiled desktop/web automation. You PROPOSE "
    "refinements only; a downstream safety layer decides what to apply. Reply "
    "with a single JSON object matching the requested schema and nothing else. "
    "Bias risk toward 'irreversible' for any step that writes, submits, sends, "
    "pays, deletes, or otherwise commits a consequential change."
)


def _step_evidence(step: Step) -> dict:
    label = step.anchor.ocr_text if step.anchor is not None else None
    return {
        "id": step.id,
        "action": step.action.value,
        "intent": step.intent,
        "ocr_label": label,
        "current_risk": step.risk,
        "param": step.param,
        "text": None if step.secret else step.text,
    }


def _build_prompt(workflow: Workflow, captured_state: Optional[dict]) -> str:
    payload = {
        "workflow_name": workflow.name,
        "declared_params": {
            name: spec.type.value for name, spec in workflow.param_specs.items()
        },
        "steps": [_step_evidence(s) for s in workflow.steps],
        "captured_state": captured_state or {},
    }
    schema_hint = {
        "steps": [
            {
                "step_id": "step_000",
                "label": {"label": "human-readable intent", "rationale": "..."},
                "risk": {
                    "proposed_risk": "reversible|irreversible",
                    "rationale": "...",
                },
                "params": [
                    {
                        "name": "param_name",
                        "type": "string|date|enum|number|entity_ref",
                        "example": "value",
                        "choices": [],
                        "consequential": False,
                        "rationale": "...",
                    }
                ],
            }
        ]
    }
    return (
        "Compiled workflow:\n"
        + json.dumps(payload, indent=2)
        + "\n\nReturn a JSON object with this shape (omit fields you have no "
        "proposal for; only include steps you want to annotate):\n"
        + json.dumps(schema_hint, indent=2)
    )


def _parse_proposals(text: str, workflow: Workflow) -> WorkflowProposals:
    """Parse the model's JSON reply into ``WorkflowProposals``, defensively.

    The reply may be wrapped in prose or a ``json`` code fence; extract the first
    balanced JSON object. Unknown step ids and malformed entries are dropped
    (fail-safe: a bad model reply yields NO proposals, never a crash or a
    fabricated annotation).
    """
    ids = {s.id for s in workflow.steps}
    obj = _extract_json_object(text)
    if not isinstance(obj, dict):
        logger.warning("annotator: no JSON object in model reply; ignoring")
        return WorkflowProposals()
    steps: list[StepAnnotation] = []
    for raw in obj.get("steps", []) or []:
        if not isinstance(raw, dict) or raw.get("step_id") not in ids:
            continue
        try:
            steps.append(StepAnnotation.model_validate(raw))
        except Exception as exc:  # noqa: BLE001 - one bad step must not crash all
            logger.warning("annotator: dropping malformed step proposal: %s", exc)
    return WorkflowProposals(steps=steps)


def _extract_json_object(text: str) -> Any:
    text = text.strip()
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# -- application: confirm-don't-trust ----------------------------------------


class AppliedAnnotation(BaseModel):
    """A proposal that was APPLIED (safe direction)."""

    kind: str  # "label" | "risk_upgrade" | "param_type"
    step_id: str
    detail: str


class FlaggedAnnotation(BaseModel):
    """A proposal that was NOT applied and needs operator confirmation.

    Mirrors ``effect.needs_operator_confirmation`` (#75) and the disambiguation
    ``unresolved_consequential`` posture (#74): a model-proposed weakening of a
    safeguard, or a consequential behaviour change, is surfaced for a human --
    never silently trusted.
    """

    kind: str  # "risk_downgrade" | "consequential_param"
    step_id: str
    detail: str
    needs_operator_confirmation: bool = True


class AnnotationResult(BaseModel):
    """Outcome of applying a :class:`StepAnnotator`'s proposals to a workflow.

    ``workflow`` is the (copied) workflow with SAFE proposals applied (risk
    upgrades in ``step.risk``, parameter type enrichments in ``param_specs``).
    ``labels`` maps step id -> proposed human-readable intent (advisory).
    ``applied`` / ``flagged`` are the audit trail. ``proposals`` is the raw model
    output for the record.
    """

    workflow: Workflow
    proposals: WorkflowProposals = Field(default_factory=WorkflowProposals)
    labels: dict[str, str] = Field(default_factory=dict)
    applied: list[AppliedAnnotation] = Field(default_factory=list)
    flagged: list[FlaggedAnnotation] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True iff nothing needs operator confirmation (mirrors #74
        ``certified``): no safeguard was proposed-weakened and no consequential
        parameter inference is outstanding."""
        return not self.flagged

    def render(self) -> str:
        lines = [
            f"Annotation: {len(self.labels)} label(s), "
            f"{len(self.applied)} applied, {len(self.flagged)} flagged "
            "(need operator confirmation)."
        ]
        for a in self.applied:
            lines.append(f"  [applied]  {a.step_id}: {a.kind} -- {a.detail}")
        for f in self.flagged:
            lines.append(f"  [FLAGGED]  {f.step_id}: {f.kind} -- {f.detail}")
        verdict = "CLEAN" if self.clean else "NEEDS OPERATOR CONFIRMATION"
        lines.append(f"Verdict: {verdict}")
        return "\n".join(lines)


def apply_annotations(
    workflow: Workflow,
    annotator: StepAnnotator,
    *,
    captured_state: Optional[dict] = None,
) -> AnnotationResult:
    """Run ``annotator`` over a COPY of ``workflow`` and apply its proposals
    with the confirm-don't-trust asymmetry (see the module docstring).

    Pure with respect to ``workflow`` (it is deep-copied first). The model call,
    if any, happens inside ``annotator.annotate`` -- this function itself makes no
    network calls and is safe to unit-test with :class:`FakeStepAnnotator`.

    Returns:
        An :class:`AnnotationResult` carrying the annotated (copied) workflow and
        the full applied/flagged audit trail.
    """
    resolved = workflow.model_copy(deep=True)
    proposals = annotator.annotate(resolved, captured_state=captured_state)
    result = AnnotationResult(workflow=resolved, proposals=proposals)
    by_id = {s.id: s for s in resolved.steps}

    for sa in proposals.steps:
        step = by_id.get(sa.step_id)
        if step is None:
            continue  # unknown step id: ignore (fail-safe)

        # LABEL -- advisory, never changes replay behaviour: always attach.
        if sa.label is not None and sa.label.label.strip():
            result.labels[step.id] = sa.label.label
            result.applied.append(
                AppliedAnnotation(kind="label", step_id=step.id, detail=sa.label.label)
            )

        # RISK -- upgrade applied (arms a safeguard), downgrade flagged.
        if sa.risk is not None:
            _apply_risk(step, sa.risk, result)

        # PARAMS -- type-enrichment applied, consequential flagged.
        for p in sa.params:
            _apply_param(resolved, step, p, result)

    return result


def _apply_risk(step: Step, proposal: RiskProposal, result: AnnotationResult) -> None:
    proposed = proposal.proposed_risk
    if proposed not in ("reversible", "irreversible"):
        logger.warning("annotator: ignoring invalid risk %r for %s", proposed, step.id)
        return
    current = step.risk
    if proposed == current:
        return  # agrees with the heuristic: nothing to do
    if proposed == "irreversible":
        # UPGRADE: safe direction -- only ever arms a safeguard. Apply.
        step.risk = "irreversible"
        detail = "reversible -> irreversible"
        if proposal.rationale:
            detail += f" ({proposal.rationale})"
        result.applied.append(
            AppliedAnnotation(kind="risk_upgrade", step_id=step.id, detail=detail)
        )
    else:
        # DOWNGRADE: would DISARM a safeguard. Never apply silently -- flag.
        detail = (
            "model proposed irreversible -> reversible; heuristic risk "
            "'irreversible' KEPT"
        )
        if proposal.rationale:
            detail += f" (model: {proposal.rationale})"
        result.flagged.append(
            FlaggedAnnotation(kind="risk_downgrade", step_id=step.id, detail=detail)
        )


def _apply_param(
    resolved: Workflow,
    step: Step,
    proposal: ParamProposal,
    result: AnnotationResult,
) -> None:
    name = proposal.name
    existing = resolved.param_specs.get(name)
    # A proposal is safe to apply ONLY when it is a pure TYPE enrichment of a
    # parameter the demonstration already declared. Anything else -- explicitly
    # flagged consequential, or naming a value that is not yet a parameter --
    # would change what the workflow does, so it is flagged, never applied.
    if proposal.consequential or existing is None:
        detail = f"model proposed making {name!r} a {proposal.type.value} parameter"
        if existing is None:
            detail += " (would make a demonstrated constant vary per run)"
        if proposal.rationale:
            detail += f"; {proposal.rationale}"
        result.flagged.append(
            FlaggedAnnotation(
                kind="consequential_param", step_id=step.id, detail=detail
            )
        )
        return
    if existing.type == proposal.type and not proposal.choices:
        return  # no change
    # Type-only enrichment: keep the recorded name/example/required, refine type
    # (and enum choices when given). Metadata only -- the value still comes from
    # params[name] at replay exactly as before.
    resolved.param_specs[name] = ParamSpec(
        name=existing.name,
        type=proposal.type,
        example=existing.example,
        required=existing.required,
        choices=proposal.choices or existing.choices,
    )
    result.applied.append(
        AppliedAnnotation(
            kind="param_type",
            step_id=step.id,
            detail=f"{name}: {existing.type.value} -> {proposal.type.value}",
        )
    )
