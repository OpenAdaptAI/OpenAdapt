"""Compile-time certified REST ``ApiBinding`` synthesis (propose, then admit).

The capability ladder (``docs/design/WORKFLOW_PROGRAM_IR.md`` §4.5, §9) puts
``api`` above ``dom_uia`` / ``vision_rdp``: a step that carries an
:class:`~openadapt_flow.ir.ApiBinding` writes through
:class:`~openadapt_flow.runtime.actuators.api.ApiActuator` instead of the GUI.
This module is the *compile-time* gate that decides whether a REST binding may
land on the step at all.

Two phases, never skipped:

1. **Propose.** For a consequential write whose demonstration observed a
   system-of-record delta, build a REST-only :class:`~openadapt_flow.ir.ApiBinding`
   from that delta (or from an ``api_write`` capture on the event). FHIR, MCP,
   and generic ``tool`` bindings are refused here -- :mod:`runtime.actuators.api`
   is REST only. A placeholder effect is not a proposal: there is no real
   endpoint in the recording, and we will not invent one
   (:mod:`compiler.effect_mining`).
2. **Admit.** Execute the proposal against a *held-out* fixture (MockMed's
   in-process REST store is the built-in one) with the same
   :class:`~openadapt_flow.runtime.effects.EffectVerifier` that gates replay.
   The binding is copied onto the step only when every proposed
   :class:`~openadapt_flow.runtime.effects.Effect` CONFIRMs. Anything else
   (REFUTED, INDETERMINATE, non-ACTUATED delivery, non-REST kind) leaves the
   step on the GUI ladder.

Admission is not replay. It writes the held-out fixture, never the
demonstration's store, and it never GUI-retries a request that may have
landed (the no-double-write contract in ``runtime/actuators/api.py``). An
admitted binding still HALTs at run time on indeterminate delivery.

Provenance of an admitted program is recorded in
:class:`~openadapt_flow.learning.library.SkillLibrary` when a library root is
supplied -- the same versioned, never-silently-adopted store the learn loop
uses.

Zero model calls. Opt-in from :func:`compile_recording` via
``admit_api_bindings=True``; default off keeps bundles byte-identical.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

from openadapt_flow.compiler.effect_mining import (
    SOR_AFTER_KEY,
    SOR_BEFORE_KEY,
    SURROGATE_ID_FIELDS,
    _as_records,
    _new_records,
    mine_step_effects,
)
from openadapt_flow.ir import ApiBinding, Step, Workflow, lift_to_program
from openadapt_flow.learning.library import Provenance, SkillLibrary, SkillVersion
from openadapt_flow.runtime.actuators.api import (
    ActuationStatus,
    ApiActuationResult,
    ApiActuator,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, EffectVerdict
from openadapt_flow.runtime.effects.rest import RestRecordVerifier

logger = logging.getLogger(__name__)

#: Event key for an observed REST write (method / url_template / body_template).
API_WRITE_KEY = "api_write"

#: Record fields assigned by MockMed (and typical REST stores) at write time,
#: never sent in the POST body and never used as a proposal identity.
_BODY_SKIP_FIELDS = SURROGATE_ID_FIELDS | frozenset({"source"})

#: The only ``ApiBinding.kind`` this module will propose or admit.
_REST_KIND: Literal["rest"] = "rest"

#: Kinds the IR permits but this MVP will not synthesize.
_REFUSED_KINDS = frozenset({"fhir", "mcp", "tool"})


@dataclass(frozen=True)
class RestWriteProposal:
    """A REST binding the compiler is willing to *try* on a held-out fixture."""

    step_id: str
    binding: ApiBinding
    effects: list[Effect]
    reason: str
    source: str = "sor_delta"


@dataclass
class AdmissionDecision:
    """Whether a proposal was copied onto the step.

    ``admitted`` is True only when the actuator returned ACTUATED and every
    effect CONFIRMed. A false decision never mutates ``step.api_binding``.
    """

    step_id: str
    admitted: bool
    reason: str
    proposal: Optional[RestWriteProposal] = None
    binding: Optional[ApiBinding] = None
    actuation: Optional[ApiActuationResult] = None
    verdicts: list[EffectVerdict] = field(default_factory=list)


@dataclass(frozen=True)
class AdmissionFixture:
    """Held-out REST system of record used to certify a proposed binding.

    MockMed (``openadapt_flow.mockmed.fault_server``) is the built-in fixture:
    ``POST /api/encounter`` writes, ``GET /api/db`` is the verifier read.
    """

    base_url: str
    write_path: str = "/api/encounter"
    write_method: str = "POST"
    params: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 2.0
    session: Any = None


def _param_placeholders(
    record: dict[str, Any], params: dict[str, str]
) -> dict[str, str]:
    """Map observed record fields to ``{param}`` leaves or literal strings."""
    inverse = {str(value): name for name, value in params.items() if value}
    body: dict[str, str] = {}
    for key, value in record.items():
        if key in _BODY_SKIP_FIELDS or value is None or str(value) == "":
            continue
        text = str(value)
        if text in inverse:
            body[key] = "{" + inverse[text] + "}"
        else:
            body[key] = text
    return body


def _captured_write(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    captured = event.get(API_WRITE_KEY)
    if isinstance(captured, dict):
        return captured
    return None


def propose_rest_binding(
    step: Step,
    event: dict[str, Any],
    *,
    fixture: AdmissionFixture,
    exclude_texts: tuple[str, ...] = (),
    params: Optional[dict[str, str]] = None,
) -> Optional[RestWriteProposal]:
    """Propose a REST ``ApiBinding`` for a consequential write, or None.

    REST only. A captured ``api_write`` whose ``kind`` is fhir/mcp/tool is
    refused (no proposal). A step that already carries an ``api_binding`` is
    left alone (operator-authored bindings are not overwritten). A mined
    placeholder / on-screen read-back is not a REST write -- no proposal.
    """
    if step.api_binding is not None:
        return None

    captured = _captured_write(event)
    if captured is not None:
        kind = str(captured.get("kind") or _REST_KIND).lower()
        if kind in _REFUSED_KINDS or kind != _REST_KIND:
            logger.info(
                "binding-propose %s: refusing non-REST api_write kind %r",
                step.id,
                kind,
            )
            return None

    params = dict(params or fixture.params)
    effects = list(step.effects)
    source = "step.effects"
    if not effects or any(eff.needs_operator_confirmation for eff in effects):
        mined = mine_step_effects(event, step, exclude_texts=exclude_texts)
        if not mined.derived:
            logger.debug(
                "binding-propose %s: no derived SoR effect (%s)",
                step.id,
                mined.reason,
            )
            return None
        effects = list(mined.effects)
        source = "effect_mining"
        if not step.effects:
            step.effects = list(effects)

    if not any(eff.kind is EffectKind.RECORD_WRITTEN for eff in effects):
        return None
    if any(eff.needs_operator_confirmation for eff in effects):
        return None

    method = fixture.write_method
    url_template = fixture.write_path
    body: dict[str, Any] = {}
    if captured is not None:
        method = str(captured.get("method") or method).upper()
        url_template = str(captured.get("url_template") or url_template)
        raw_body = captured.get("body_template", captured.get("body"))
        if isinstance(raw_body, dict):
            body = dict(raw_body)
        source = API_WRITE_KEY
    else:
        before = _as_records(event.get(SOR_BEFORE_KEY))
        after = _as_records(event.get(SOR_AFTER_KEY))
        if before is None or after is None:
            return None
        new_records = _new_records(before, after)
        if len(new_records) != 1:
            return None
        body = _param_placeholders(new_records[0], params)
        source = "sor_delta"

    if not url_template or not body:
        return None

    binding = ApiBinding(
        kind=_REST_KIND,
        method=method,
        url_template=url_template,
        body_template=body,
        effects=[eff.model_copy(deep=True) for eff in effects],
        timeout_s=fixture.timeout_s,
        on_unavailable="gui",
    )
    reason = (
        f"proposed REST {method} {url_template} from {source} "
        f"({len(effects)} effect(s) to confirm on the held-out fixture)"
    )
    logger.info("binding-propose %s: %s", step.id, reason)
    return RestWriteProposal(
        step_id=step.id,
        binding=binding,
        effects=effects,
        reason=reason,
        source=source,
    )


def admit_rest_binding(
    proposal: RestWriteProposal,
    *,
    fixture: AdmissionFixture,
) -> AdmissionDecision:
    """Actuate ``proposal`` on ``fixture``; admit iff every Effect CONFIRMs.

    Uses :class:`ApiActuator` (REST) and :class:`RestRecordVerifier`. A
    non-ACTUATED result (UNAVAILABLE or HALT) is not admitted -- including
    HALT, because the write's outcome is not a CONFIRMed Effect. The
    caller must not GUI-retry on the fixture: this function never does.
    """
    binding = proposal.binding
    if binding.kind != _REST_KIND:
        return AdmissionDecision(
            step_id=proposal.step_id,
            admitted=False,
            reason=(
                f"refusing non-REST binding kind {binding.kind!r} "
                "(certified synthesis is REST only)"
            ),
            proposal=proposal,
        )

    verifier = RestRecordVerifier(fixture.base_url, timeout_s=fixture.timeout_s)
    actuator = ApiActuator(
        fixture.base_url,
        session=fixture.session,
        timeout_s=fixture.timeout_s,
    )
    before = verifier.capture_pre_state()
    if not before.reachable:
        return AdmissionDecision(
            step_id=proposal.step_id,
            admitted=False,
            reason=(
                "held-out fixture system of record unreachable before "
                "actuation -- not admitted, GUI ladder remains"
            ),
            proposal=proposal,
        )

    actuation = actuator.actuate(binding, dict(fixture.params))
    if actuation.status is not ActuationStatus.ACTUATED:
        reason = (
            f"proposal not admitted: actuation {actuation.status.value} "
            f"({actuation.reason}) -- GUI ladder remains"
        )
        logger.info("binding-admit %s: %s", proposal.step_id, reason)
        return AdmissionDecision(
            step_id=proposal.step_id,
            admitted=False,
            reason=reason,
            proposal=proposal,
            actuation=actuation,
        )

    verdicts: list[EffectVerdict] = []
    for effect in proposal.effects:
        verdict = verifier.verify(effect, before)
        verdicts.append(verdict)
        if not verdict.confirmed:
            reason = (
                f"proposal not admitted: effect {effect.kind.value} "
                f"{verdict.verdict.value} ({verdict.reason}) -- GUI ladder remains"
            )
            logger.info("binding-admit %s: %s", proposal.step_id, reason)
            return AdmissionDecision(
                step_id=proposal.step_id,
                admitted=False,
                reason=reason,
                proposal=proposal,
                actuation=actuation,
                verdicts=verdicts,
            )

    reason = (
        f"admitted REST {binding.method} {binding.url_template}: "
        f"{len(verdicts)} effect(s) CONFIRMED on held-out fixture"
    )
    logger.info("binding-admit %s: %s", proposal.step_id, reason)
    return AdmissionDecision(
        step_id=proposal.step_id,
        admitted=True,
        reason=reason,
        proposal=proposal,
        binding=binding,
        actuation=actuation,
        verdicts=verdicts,
    )


def certify_step_rest_binding(
    step: Step,
    event: dict[str, Any],
    *,
    fixture: AdmissionFixture,
    exclude_texts: tuple[str, ...] = (),
    params: Optional[dict[str, str]] = None,
) -> AdmissionDecision:
    """Propose then admit one step. Mutates ``step.api_binding`` only on admit."""
    proposal = propose_rest_binding(
        step,
        event,
        fixture=fixture,
        exclude_texts=exclude_texts,
        params=params,
    )
    if proposal is None:
        return AdmissionDecision(
            step_id=step.id,
            admitted=False,
            reason="no REST binding proposed (GUI ladder remains)",
        )
    decision = admit_rest_binding(proposal, fixture=fixture)
    if decision.admitted and decision.binding is not None:
        step.api_binding = decision.binding
        if not step.effects:
            step.effects = list(proposal.effects)
    return decision


def record_admission_provenance(
    steps: Sequence[Step],
    decisions: Sequence[AdmissionDecision],
    *,
    library: SkillLibrary,
    skill_id: str,
    workflow_name: str = "certified-rest",
) -> Optional[SkillVersion]:
    """Store the admitted program in ``library`` with an audit note.

    No-op (returns None) when nothing was admitted -- we do not record a
    skill version for a GUI-only refusal.
    """
    admitted = [d for d in decisions if d.admitted]
    if not admitted:
        return None
    workflow = Workflow(name=workflow_name, steps=list(steps))
    graph = lift_to_program(workflow)
    note = (
        "certified REST ApiBinding admission: "
        + ", ".join(d.step_id for d in admitted)
        + "; EffectVerifier CONFIRMED the same Effect on the held-out fixture"
    )
    provenance = Provenance(
        trace_ids=[d.step_id for d in admitted],
        note=note,
    )
    if library.has(skill_id):
        return library.add_candidate(
            skill_id,
            graph,
            provenance=provenance,
            validation_score=1.0,
        )
    return library.create_skill(
        skill_id,
        graph,
        provenance=provenance,
        validation_score=1.0,
    )


def certify_steps_on_fixture(
    steps: Sequence[Step],
    events: Sequence[dict[str, Any]],
    *,
    fixture: AdmissionFixture,
    exclude_texts: tuple[str, ...] = (),
    params: Optional[dict[str, str]] = None,
    library: Optional[SkillLibrary] = None,
    skill_id: Optional[str] = None,
    workflow_name: str = "certified-rest",
) -> list[AdmissionDecision]:
    """Propose and admit each paired ``(step, event)`` against ``fixture``.

    Pairing is positional and truncated to ``min(len(steps), len(events))``.
    When ``library`` and ``skill_id`` are set, an admitted program is stored
    with :class:`Provenance`.
    """
    decisions: list[AdmissionDecision] = []
    count = min(len(steps), len(events))
    for index in range(count):
        decisions.append(
            certify_step_rest_binding(
                steps[index],
                events[index],
                fixture=fixture,
                exclude_texts=exclude_texts,
                params=params,
            )
        )
    if library is not None and skill_id:
        record_admission_provenance(
            steps,
            decisions,
            library=library,
            skill_id=skill_id,
            workflow_name=workflow_name,
        )
    return decisions


def certify_workflow_rest_bindings(
    workflow: Workflow,
    events: Sequence[dict[str, Any]],
    *,
    fixture: AdmissionFixture,
    exclude_texts: tuple[str, ...] = (),
    library_root: Optional[Path | str] = None,
    skill_id: Optional[str] = None,
) -> list[AdmissionDecision]:
    """Admit REST bindings onto ``workflow.steps``; optional SkillLibrary write."""
    library = SkillLibrary(library_root) if library_root is not None else None
    return certify_steps_on_fixture(
        workflow.steps,
        events,
        fixture=fixture,
        exclude_texts=exclude_texts,
        params=dict(workflow.params),
        library=library,
        skill_id=skill_id or workflow.name,
        workflow_name=workflow.name,
    )
