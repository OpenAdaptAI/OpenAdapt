# mypy: disable-error-code="empty-body"
"""The API tier and web-form spans: carry out a step through the application's API.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_ApiTierHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional  # noqa: F401
from urllib.parse import urlsplit

from openadapt_flow.ir import (
    ActionDeliveryUncertainty,
    ActionKind,
    IdentityCheck,
    IdentitySignalEvidence,
    SafetyRefusalEvidence,
    StepResult,
)
from openadapt_flow.qualification_faults import effect_verifier_input_sha256
from openadapt_flow.runtime.authorization import (
    runtime_param_text,
    runtime_params_for_gui,
)
from openadapt_flow.runtime.replay._shared import _WebFormApprovalView

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Mapping

    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import ApiBinding, ExecutionTargetKind, Step, Workflow
    from openadapt_flow.runtime.authorization import (
        GovernedRunAuthorization,
        RuntimeParamScalar,
    )
    from openadapt_flow.runtime.effects import Effect, EffectState


class _ApiTierHost:
    def _approval_gated(self) -> bool: ...

    _candidate_binding_refusal: Any
    _capture_effect_pre_state: Any

    def _check_postconditions(
        self,
        step: Step,
        frame_png: bytes,
        bundle_dir: Path,
        start_state: dict[str, Any],
        result: Optional[StepResult] = None,
        *,
        workflow: Optional[Workflow] = None,
    ) -> tuple[bool, bytes, list[str]]: ...

    _control_overlay_progress: Any
    _defer_readback: Any
    _delivery_authorization_refusal: Any
    _emit_control_overlay_phase: Any
    _keep_undo_prior_state: Any
    _may_pipeline_readback: Any
    _profile_effect_tier_refusal: Any
    _require_qualification_environment_current: Any
    _required_effect_pre_state_unreadable: Any
    _resolve_effects: Any

    def _run_step(self, step: Step, **kwargs: Any) -> StepResult: ...
    def _structural_state(self) -> dict[str, Any]: ...

    _verify_effects: Any
    _web_form_approval: dict[int, Any]
    _web_form_fallback_reason: Optional[str]
    _web_form_form_state: Optional[tuple[str, str]]
    _web_form_form_state_set: Any
    _web_form_span_active: Any
    api_actuator: Optional[Any]
    api_steps: bool
    api_steps_off_reason: Optional[str]
    backend: Backend
    deployment_surface: Optional[str]
    governed_authorization: Optional[GovernedRunAuthorization]
    use_structural: bool
    vision: Any
    web_form_actuator: Optional[Any]


if TYPE_CHECKING:
    _Base = _ApiTierHost
else:
    _Base = object


class ApiTier(_Base):
    """The API tier and web-form spans: carry out a step through the application's API."""

    def _web_form_span_at(
        self, workflow: Workflow, step_index: int, *, durable: bool
    ) -> Optional[tuple[int, Step, Optional[str]]]:
        """The API step whose span starts at ``step_index``, and why it can't run.

        Returns ``(save_index, save_step, reason)``; ``reason`` is None when
        the API step may run. None when no API step starts here.
        """

        from openadapt_flow.runtime.actuators.web_form import folded_checks_digest

        first = workflow.steps[step_index].id
        for end_index in range(step_index, len(workflow.steps)):
            save = workflow.steps[end_index]
            binding = save.api_binding
            if (
                binding is None
                or binding.kind != "web_form"
                or binding.web_form is None
                or binding.web_form.replaces_steps[0] != first
            ):
                continue
            ids = [s.id for s in workflow.steps[step_index : end_index + 1]]
            if not self.api_steps:
                reason = self.api_steps_off_reason or "API steps are off for this run"
            elif surface_reason := self._web_form_surface_refusal(
                workflow, step_index, end_index, binding
            ):
                reason = surface_reason
            elif self._approval_gated() and not callable(
                getattr(self._actuator_for(binding), "prepare", None)
            ):
                # Under an approval mode the person approves the exact request
                # the API step sends. An actuator that can't prepare the
                # request before sending it can't show it.
                reason = (
                    "approval mode needs the exact request before the save, and "
                    "this API actuator can't prepare it, so the GUI steps run"
                )
            elif not binding.admitted():
                reason = "the API step is not admitted (openadapt-flow api admit)"
            elif binding.web_form.other_writes_seen:
                # Proposed before such spans were refused: the steps it would
                # replace also wrote, and one request can't stand in for them.
                reason = (
                    "the steps the API step would replace also sent other writes "
                    f"({', '.join(binding.web_form.other_writes_seen)})"
                )
            elif save.identity_armed and not binding.identity:
                # The GUI save checks the right record on screen. The API step
                # must check it too, so it needs a right-record contract.
                reason = (
                    "the save checks the right record on screen, and the API "
                    "step names no right-record contract (openadapt-flow api "
                    "admit --identity)"
                )
            elif list(binding.effects) != list(save.effects):
                reason = (
                    "the save step's record check differs from the one admitted "
                    "with the API step (openadapt-flow api admit)"
                )
            elif ids != list(binding.web_form.replaces_steps):
                reason = "the program's steps no longer match the API step"
            elif (
                folded_checks_digest(workflow.steps[step_index : end_index + 1])
                != binding.web_form.folded_checks_digest
            ):
                reason = (
                    "the steps the API step replaces changed after it was "
                    "proposed (an action, value, target, check, guard or record "
                    "check), so the API step no longer stands for them "
                    "(openadapt-flow api propose)"
                )
            elif self.governed_authorization is not None and (
                governed_reason := self._governed_web_form_refusal(workflow, save)
            ):
                reason = governed_reason
            elif self._actuator_for(binding) is None:
                reason = "there is no live browser session for the API step"
            else:
                reason = None
            return end_index, save, reason
        return None

    def _web_form_surface_refusal(
        self, workflow: Workflow, start_index: int, end_index: int, binding: Any
    ) -> Optional[str]:
        """Why a multi-surface run can't use this API step, or None when it can.

        The API step is bound to its span's surface the way it is bound to the
        page in a single-surface run: every step it replaces runs on that one
        browser surface, none of them switches, opens or closes a surface, the
        surface is the active one, and the request goes to that surface's
        session on its declared origin.
        """

        router = getattr(self, "_surface_router", None)
        if router is None:
            return None
        span = workflow.steps[start_index : end_index + 1]
        surfaces = {s.surface or router.primary_id for s in span}
        if len(surfaces) != 1:
            return (
                "the steps the API step replaces run on more than one surface, "
                "so the GUI steps run"
            )
        if any(
            s.action is ActionKind.SWITCH_SURFACE or s.opens or s.closes_surface
            for s in span
        ):
            return (
                "the steps the API step replaces switch, open or close a "
                "surface, so the GUI steps run"
            )
        (surface_id,) = surfaces
        if surface_id != router.active_id:
            return "the API step's surface is not the active one, so the GUI steps run"
        spec = router.specs[surface_id]
        if spec.kind != "web":
            return "the API step's surface is not a browser, so the GUI steps run"
        actuator = self._actuator_for(binding)
        if actuator is None:
            return "there is no live browser session for the API step"
        from openadapt_flow.runtime.surfaces import url_origin

        if spec.is_page:
            expected = spec.origin
        else:
            seen = router.observations.get(surface_id)
            expected = seen.origin if seen is not None else None
        actual = url_origin(
            getattr(getattr(actuator, "transport", None), "base_url", None)
        )
        if expected is not None and actual != url_origin(expected):
            return (
                "the API step's session is on another origin than its surface, "
                "so the GUI steps run"
            )
        return None

    def _governed_web_form_refusal(
        self, workflow: Workflow, save: Step
    ) -> Optional[str]:
        """Why a governed run can't use this API step, or None when it can.

        The run's authorization must have admitted this exact API step. For a
        managed (Cloud) dispatch that is Cloud's authorization, whose
        ``api_steps`` are inside the dispatch binding digest Cloud signs.

        A qualification case runs the path its contract names for the save:
        the API step when that path is ``api``, the GUI steps otherwise. A run
        under a production qualification admission uses the API step only
        when the qualified effect contract, which the signed admission binds,
        names the save's API path; that contract exists only when the
        qualification campaign ran the API path's cases.
        """

        authorization = self.governed_authorization
        assert authorization is not None
        if authorization.qualification_case_id is not None:
            if authorization.qualification_case_action_paths.get(save.id) != "api":
                return "a qualification case runs the GUI path it names"
        elif authorization.production_qualification_admission_id is not None:
            if not authorization.effect_requirements(save.id, "api"):
                return (
                    "the production qualification admission didn't qualify this "
                    "API step's path, so the qualified GUI path runs"
                )
        return authorization.admitted_api_step(save, workflow)

    def _run_web_form_span(
        self,
        workflow: Workflow,
        start_index: int,
        end_index: int,
        *,
        params: Mapping[str, RuntimeParamScalar],
        bundle_dir: Path,
        run_dir: Path,
        new_crops: dict[str, bytes],
    ) -> Optional[list[StepResult]]:
        """Run the API step for steps ``start_index..end_index``.

        Returns the results (the folded GUI steps, then the save) when the API
        step took responsibility: it saved and the record check ran, or it
        stopped after sending. Returns None when the save was not sent, with
        the reason in ``self._web_form_fallback_reason``; the caller then runs
        the GUI steps.
        """

        save = workflow.steps[end_index]
        self._web_form_span_active = True
        try:
            result = self._run_step(
                save,
                workflow=workflow,
                step_index=end_index,
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                new_crops=new_crops,
            )
        finally:
            self._web_form_span_active = False
            self._web_form_approval.clear()
        if result.api_fallback is not None:
            self._web_form_fallback_reason = result.api_fallback
            return None
        router = getattr(self, "_surface_router", None)
        surface = router.active_id if router is not None else None
        folded = [
            StepResult(
                step_id=s.id,
                intent=s.intent,
                ok=True,
                skipped=True,
                actuation="api_folded",
                surface=surface,
                risk=s.risk,
                risk_explanation=s.risk_explanation,
                risk_review_required=s.risk_review_required,
                delivery_attempted=False,
            )
            for s in workflow.steps[start_index:end_index]
        ]
        return [*folded, result]

    def _lookup_capability_refusal(
        self,
        workflow: Workflow,
        execution_target_kind: Optional[ExecutionTargetKind],
    ) -> Optional[str]:
        """Refuse a surface that offers fewer lookups than were qualified.

        A workflow qualified with structure lookups (DOM / accessibility)
        must not run where only pixels exist, such as an RDP or Citrix window
        viewed from outside the session: it passed qualification on evidence
        this surface cannot produce. Checked before any action.
        """

        from openadapt_flow.lookup_capabilities import (
            DeploymentSurface,
            declared_surface_problem,
            lookup_capability_refusal,
            qualified_lookup_of,
            resolve_deployment_surface,
        )

        if qualified_lookup_of(workflow) is None:
            return None
        kind = execution_target_kind or workflow.surface
        surface: DeploymentSurface
        if declared_surface_problem(self.deployment_surface, kind) is not None:
            # A declaration that contradicts the backend is read the
            # conservative way: the remote session is viewed from outside.
            surface = "remote_display"
        else:
            try:
                surface = resolve_deployment_surface(self.deployment_surface, kind)
            except ValueError:
                surface = "remote_display"
        structure_available = (
            surface != "remote_display"
            and self.use_structural
            and callable(getattr(self.backend, "locate_structural", None))
        )
        return lookup_capability_refusal(
            workflow, surface=surface, structure_available=structure_available
        )

    @staticmethod
    def _url_path_targets_param(url_template: str, param: str) -> bool:
        """Require the identity placeholder as one whole URL path segment."""

        token = "{" + param + "}"
        return token in [
            segment for segment in urlsplit(url_template).path.split("/") if segment
        ]

    @staticmethod
    def _api_request_pointer_value(binding: ApiBinding, pointer: str) -> Any:
        """Resolve one admitted target-bearing pointer without consulting headers."""

        if pointer.startswith("/url/"):
            return binding.url_template
        if binding.kind == "web_form":
            from openadapt_flow.runtime.actuators.web_form import (
                web_form_pointer_template,
            )

            return web_form_pointer_template(binding, pointer)
        root_name, *tokens = pointer.lstrip("/").split("/")
        if root_name == "body":
            value: Any = binding.body_template
        elif root_name == "query":
            value = binding.query
        else:
            return None
        for token in tokens:
            decoded = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict):
                if decoded not in value:
                    return None
                value = value[decoded]
            elif isinstance(value, list):
                try:
                    index = int(decoded)
                except ValueError:
                    return None
                if index < 0 or index >= len(value):
                    return None
                value = value[index]
            else:
                return None
        return value

    @staticmethod
    def _api_request_pointer_field_path(pointer: str) -> tuple[str, ...]:
        """Return the fully decoded target-field path after the request root."""

        return tuple(
            token.replace("~1", "/").replace("~0", "~")
            for token in pointer.lstrip("/").split("/")[1:]
        )

    def _api_identity_refusal(
        self,
        step: Step,
        params: Mapping[str, RuntimeParamScalar],
        workflow: Workflow,
        effects: list["Effect"],
        result: StepResult,
    ) -> Optional[str]:
        """Validate an exact qualified identity binding before an API write."""

        authorization_requires = (
            self.governed_authorization is not None
            and self.governed_authorization.requires_verified_identity(step.id)
        )
        if step.api_binding is not None and step.api_binding.kind == "web_form":
            # A form save whose GUI step checks the right record, or whose
            # admission named a right-record parameter, is checked the same
            # way in every run, governed or not (its durable checkpoint
            # needs the evidence too).
            authorization_requires = authorization_requires or bool(
                step.identity_armed or step.api_binding.identity
            )
        project = workflow.qualification
        policy = project.identity_policies.get(step.id) if project is not None else None
        if not authorization_requires and policy is None:
            return None

        binding = step.api_binding
        assert binding is not None
        if not binding.identity:
            return (
                f"API identity verification HALTED step '{step.id}' "
                f"({step.intent}): the qualified action has no exact "
                "api_binding.identity contract — refusing to send the request"
            )

        allowed_keys: Optional[set[str]] = None
        required = 1
        if policy is not None and policy.enforcement.value == "signal_quorum":
            allowed_keys = {signal.key.value for signal in policy.signals}
            required = policy.quorum

        evidence: list[IdentitySignalEvidence] = []
        for identity in binding.identity:
            if allowed_keys is not None and identity.key not in allowed_keys:
                return (
                    f"API identity verification HALTED step '{step.id}' "
                    f"({step.intent}): semantic signal {identity.key!r} is not "
                    "part of the qualified identity policy"
                )
            raw_value = params.get(identity.param)
            value = runtime_param_text(raw_value) if raw_value is not None else None
            if value is None or value == "":
                return (
                    f"API identity verification HALTED step '{step.id}' "
                    f"({step.intent}): required identity parameter "
                    f"{identity.param!r} is missing"
                )
            pointed_templates = [
                self._api_request_pointer_value(binding, pointer)
                for pointer in identity.request_pointers
            ]
            exact_token = "{" + identity.param + "}"
            effect_target_path = tuple(identity.effect_field.split("."))
            # A form field's name is the page's, not the record's (form_lname
            # versus lname): for a form save the field is bound to the record
            # check by sending the same parameter the check selects on.
            names_must_match = binding.kind != "web_form"
            pointers_are_bound = all(
                template is not None
                and (
                    not names_must_match
                    or self._api_request_pointer_field_path(pointer)
                    == effect_target_path
                )
                and (
                    isinstance(template, str)
                    and self._url_path_targets_param(template, identity.param)
                    if pointer.startswith("/url/")
                    else isinstance(template, str) and template == exact_token
                )
                for pointer, template in zip(
                    identity.request_pointers, pointed_templates, strict=True
                )
            )
            if not pointers_are_bound:
                return (
                    f"API identity verification HALTED step '{step.id}' "
                    f"({step.intent}): identity parameter {identity.param!r} is "
                    "not bound at every declared target-bearing request pointer"
                )
            from openadapt_flow.runtime.actuators.web_form import (
                effect_selector_rows,
            )

            effect_exprs = [
                row.get(identity.effect_field)
                for effect in effects
                for row in effect_selector_rows(effect)
            ]
            if not any(
                expr is not None and expr.param == identity.param
                for expr in effect_exprs
            ):
                return (
                    f"API identity verification HALTED step '{step.id}' "
                    f"({step.intent}): effect selector {identity.effect_field!r} "
                    f"is not bound to identity parameter {identity.param!r}"
                )
            evidence.append(
                IdentitySignalEvidence(
                    signal=identity.key,
                    source="api_parameter",
                    verdict="verified",
                    evidence_class="api_request_effect_binding",
                    match="exact",
                )
            )

        if len(evidence) < required:
            return (
                f"API identity verification HALTED step '{step.id}' "
                f"({step.intent}): {len(evidence)}/{required} exact qualified "
                "identity bindings verified — refusing to send the request"
            )
        result.identity = IdentityCheck(
            status="verified",
            mode="signal_quorum",
            coverage=1.0,
            signal_evidence=evidence,
            quorum_required=required,
            quorum_verified=len(evidence),
        )
        return None

    def _actuator_for(self, binding: Any) -> Optional[Any]:
        """The actuator that runs ``binding``, or None when none can."""

        if binding is None:
            return None
        if binding.kind != "web_form":
            return self.api_actuator
        if self.web_form_actuator is not None:
            return self.web_form_actuator
        # In a multi-surface run the request goes through the active
        # surface's session, which on a resume may not be self.backend yet.
        router = getattr(self, "_surface_router", None)
        backend = router.active_backend if router is not None else self.backend
        page = getattr(backend, "page", None)
        if page is None:
            return None
        from openadapt_flow.runtime.actuators.web_form import (
            PlaywrightSessionTransport,
            WebFormActuator,
        )

        try:
            return WebFormActuator(PlaywrightSessionTransport(page))
        except Exception:  # noqa: BLE001 - no live session: the GUI runs
            return None

    def _try_api_tier(
        self,
        step: Step,
        params: Mapping[str, RuntimeParamScalar],
        result: StepResult,
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        effect_verifier: Any,
    ) -> bool:
        """Perform ``step.api_binding``'s write via the API and confirm it.

        The TOP of the capability ladder (RFC section 4 ``api`` tier): a
        deterministic, ``$0``, model-free write, gated by the SAME
        ``EffectVerifier`` as a GUI write. For an API write the target
        "identity" is the explicit API parameter -- stronger than a resolved
        pixel band -- so no identity gate is weakened by skipping the GUI.

        Fail-safe ordering (the no-double-write contract):

        - refuse BEFORE any request when the write could not be confirmed (no
          effect contract, or no verifier) -- an unverifiable consequential
          write never proceeds;
        - snapshot the system of record, then actuate ONCE;
        - UNAVAILABLE (request never sent) -> use the binding's configured GUI
          fallback or typed pre-delivery halt; nothing was written;
        - HALT (request sent, outcome unknown / rejected) -> run the configured
          postcondition and independent-effect checks without retry or GUI
          fallback; resolve only if the complete contract proves the effect;
        - ACTUATED (2xx) -> CONFIRM with the EffectVerifier; a non-CONFIRMED
          verdict HALTs exactly as it would for a GUI write.

        Returns True when the API tier took responsibility for the step (the
        result is final -- actuated+verified, HALTed, or a config-error HALT),
        False only when the API tier is UNAVAILABLE and the binding permits the
        caller to fall through to the GUI resolution ladder.
        """
        binding = step.api_binding
        assert binding is not None  # guaranteed by the caller
        api_actuator = self._actuator_for(binding)
        assert api_actuator is not None  # guaranteed by the caller

        # An API write MUST be confirmable against its own exact effect
        # contract.  ``step.effects`` belongs exclusively to the GUI fallback;
        # substituting it here could confirm a different record or operation.
        effects = binding.effects
        if not effects:
            result.effect_verified = False
            result.effect_results.append(
                "API binding declares no api_binding.effects contract to "
                "confirm its write -- refusing an "
                "unverifiable API write (fail-safe HALT)"
            )
            result.ok = False
            result.error = (
                f"Step '{step.id}' ({step.intent}) has an API binding but no "
                "system-of-record effect to CONFIRM the write -- an API write "
                "must be verifiable; refusing to actuate "
                "(deployment/configuration error); run aborted"
            )
            return True
        identity_refusal = self._api_identity_refusal(
            step,
            params,
            workflow,
            effects,
            result,
        )
        if identity_refusal is not None:
            result.ok = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.error = identity_refusal
            return True
        # Bind the effect contracts to this run before either the verifier
        # strength check or its detector-input digest is evaluated.
        effects = self._resolve_effects(effects, params)
        if effect_verifier is None:
            result.effect_verified = False
            result.effect_results.append(
                "API binding present but no EffectVerifier is configured to "
                "confirm the write (fail-safe HALT)"
            )
            result.ok = False
            result.error = (
                f"Step '{step.id}' ({step.intent}) would actuate via the API "
                "but no EffectVerifier is configured to confirm the write -- "
                "refusing an unverifiable consequential write; run aborted"
            )
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="effect_verifier",
                code="effect_verifier_missing",
                detector_input_sha256=effect_verifier_input_sha256(None, effects),
            )
            return True

        # The same parameter substitution that the ApiActuator applies to the
        # URL/query/body now also applies to the effect checked below.
        api_hash_start = len(result.effect_contract_hashes)
        result.effect_contract_hashes.extend(
            effect.contract_hash() for effect in effects
        )
        refusal = self._profile_effect_tier_refusal(
            workflow,
            step,
            "api",
            effects,
            effect_verifier,
        )
        if refusal is not None:
            result.effect_verified = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.effect_results.append(refusal)
            result.error = refusal
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="effect_strength",
                code="effect_strength_insufficient",
                detector_input_sha256=effect_verifier_input_sha256(
                    effect_verifier,
                    effects,
                ),
            )
            return True

        # Snapshot the system of record BEFORE the write so the verifier counts
        # only what THIS actuation wrote (delta / at-most-once / collateral
        # loss), then actuate exactly once.
        try:
            before = self._capture_effect_pre_state(effect_verifier, effects)
        except Exception as exc:  # noqa: BLE001 - deployment verifier boundary
            result.effect_verified = False
            result.effect_results.append(
                "[api] effect pre-state capture failed before request delivery "
                f"({type(exc).__name__}); API actuation was not attempted"
            )
            result.ok = False
            result.error = (
                f"Step '{step.id}' ({step.intent}) could not capture the "
                "system-of-record pre-state before API actuation; refusing to "
                "send the request — run aborted"
            )
            return True
        binding_refusal = self._candidate_binding_refusal(before, effects)
        if binding_refusal is not None:
            result.effect_verified = False
            result.effect_results.append(f"[api] {binding_refusal}")
            result.ok = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.error = binding_refusal
            return True
        if self._required_effect_pre_state_unreadable(effect_verifier, before, effects):
            result.effect_verified = False
            result.effect_results.append(
                "[api] effect pre-state is unreachable before request delivery"
            )
            result.ok = False
            result.error = (
                f"Step '{step.id}' ({step.intent}) could not read the "
                "system-of-record pre-state before API actuation; refusing to "
                "send the request — run aborted"
            )
            return True
        self._keep_undo_prior_state(workflow, step, before, effects, params)
        refusal = self._profile_effect_tier_refusal(
            workflow,
            step,
            "api",
            effects,
            effect_verifier,
        )
        if refusal is not None:
            result.effect_verified = False
            result.safety_halt = True
            result.failure_category = "governed_refusal"
            result.effect_results.append(refusal)
            result.error = refusal
            result.safety_refusal_evidence = SafetyRefusalEvidence(
                stage="effect_strength",
                code="effect_strength_insufficient",
                detector_input_sha256=effect_verifier_input_sha256(
                    effect_verifier,
                    effects,
                ),
            )
            return True
        overlay_current, overlay_total = self._control_overlay_progress(
            workflow, step_index, None
        )
        self._emit_control_overlay_phase(
            "executing",
            current_step=overlay_current,
            total_steps=overlay_total,
        )
        approved_view = self._web_form_approval.pop(id(result), None)
        send_prepared: Optional[Any] = None
        again: Any = None
        if approved_view is not None:
            # Under an approval mode: prepare the request again now (fresh
            # form, fresh pre-checks). The last-point gate compares its
            # operation digest with the one a person approved; a request that
            # can't be prepared again reads as an unreadable form.
            again = self._web_form_approval_view(step, params)
            self._web_form_form_state = (
                None if isinstance(again, str) else ("api_request", again.digest)
            )
            self._web_form_form_state_set = True
        try:
            refusal = self._delivery_authorization_refusal(
                workflow, params, step, result
            )
        finally:
            self._web_form_form_state = None
            self._web_form_form_state_set = False
        if refusal is None and approved_view is not None:
            if isinstance(again, str) or again.digest != approved_view.digest:
                # Also refused when the gate let it through (for example a
                # save that passed problems_only's checks without a person):
                # only the exact request prepared before the gate is sent.
                why = (
                    again
                    if isinstance(again, str)
                    else "the request, the record or the record check differs"
                )
                result.safety_halt = True
                result.failure_category = "governed_refusal"
                refusal = (
                    f"Step '{step.id}' ({step.intent}): the API request changed "
                    f"after the approval gate ({why}); the save was not sent "
                    "and the GUI steps do not run in its place"
                )
            else:
                # Exactly the request whose digest was approved and re-checked.
                send_prepared = again.prepared
        if refusal is not None:
            result.ok = False
            result.error = refusal
            return True
        # Structural postconditions compare against the last observation before
        # request delivery, not against state captured earlier during
        # authorization or verifier setup.
        start_state = self._structural_state()
        # This transition is the explicit delivery boundary. Set it BEFORE the
        # call because an exception/timeout may occur after the request left the
        # process but before the actuator can return a receipt-like outcome.
        result.delivery_attempted = True
        try:
            self._require_qualification_environment_current()
            if send_prepared is not None:
                # Exactly the request whose digest was approved and re-checked.
                outcome = api_actuator.send(binding, send_prepared)
            else:
                outcome = api_actuator.actuate(
                    binding,
                    runtime_params_for_gui(params),
                )
        except Exception as exc:  # noqa: BLE001 - external actuator boundary
            # The actuator contract is no-throw, but a deployment adapter can
            # still violate it. The delivery boundary was crossed immediately
            # before the call, so fail toward an attempted/unknown write and
            # never fall through to the GUI.
            result.actuation = "api"
            result.effect_verified = False
            result.effect_results.append(
                "[api] actuator raised after delivery may have begun "
                f"({type(exc).__name__}); outcome requires reconciliation"
            )
            return self._verify_uncertain_api_delivery(
                step,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                start_state=start_state,
                before=before,
                effects=effects,
                effect_verifier=effect_verifier,
                cause_type=type(exc).__name__,
            )

        from openadapt_flow.runtime.actuators import (
            ActuationStatus,
            ApiActuationResult,
            ApiHaltKind,
        )

        # Only the validated model can prove the pre-delivery UNAVAILABLE
        # state.  A deployment-owned actuator may return any object after its
        # call, including a duck-typed object that claims ``UNAVAILABLE``.
        # Treat that as possible delivery: collect evidence, never fall
        # through to the GUI, and never turn it into success.
        if type(outcome) is not ApiActuationResult:
            result.actuation = "api"
            result.effect_results.append(
                "[api] actuator returned an untyped result after delivery may "
                f"have begun ({type(outcome).__name__}); treating it as a "
                "protocol error that cannot resolve to success"
            )
            return self._verify_uncertain_api_delivery(
                step,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                start_state=start_state,
                before=before,
                effects=effects,
                effect_verifier=effect_verifier,
                cause_type="ApiActuatorProtocolError",
                allow_contract_resolution=False,
            )
        try:
            # Revalidate the return value because an adapter can mutate a
            # Pydantic instance after construction.  The runtime trusts the
            # copied, validated receipt only.
            outcome = ApiActuationResult.model_validate(
                outcome.model_dump(mode="python")
            )
        except Exception as exc:  # noqa: BLE001 - deployment actuator boundary
            result.actuation = "api"
            result.effect_results.append(
                "[api] actuator returned a malformed typed result after "
                "delivery may have begun "
                f"({type(exc).__name__}); treating it as a protocol error "
                "that cannot resolve to success"
            )
            return self._verify_uncertain_api_delivery(
                step,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                start_state=start_state,
                before=before,
                effects=effects,
                effect_verifier=effect_verifier,
                cause_type="ApiActuatorProtocolError",
                allow_contract_resolution=False,
            )

        outcome_status = outcome.status
        outcome_reason = outcome.reason

        if outcome_status is ActuationStatus.UNAVAILABLE:
            # The request was NEVER sent -- nothing was written. Remove the
            # unused API-path contracts before applying the binding's exact
            # unavailability policy. A GUI fallback result must describe only
            # the path responsible for delivery, never both alternatives.
            result.delivery_attempted = False
            del result.effect_contract_hashes[api_hash_start:]
            result.effect_results.append(f"[api] {outcome_reason}")
            if binding.on_unavailable == "halt":
                self._set_api_unavailable_refusal(
                    step,
                    result,
                    reason=outcome_reason,
                )
                return True
            return False

        # From here the request WAS attempted -- this step is API-tier and is
        # NEVER also GUI-written (the no-double-write contract).
        result.actuation = "api"

        if outcome_status is ActuationStatus.HALT:
            result.effect_results.append(f"[api] {outcome_reason}")
            halt_kind = getattr(outcome, "halt_kind", None)
            typed_halt = isinstance(halt_kind, ApiHaltKind)
            if not typed_halt:
                result.effect_results.append(
                    "[api] actuator returned an untyped HALT result; treating "
                    "the delivery as a protocol error that cannot resolve to "
                    "success"
                )
            return self._verify_uncertain_api_delivery(
                step,
                result,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                start_state=start_state,
                before=before,
                effects=effects,
                effect_verifier=effect_verifier,
                cause_type=(
                    "ApiActuatorProtocolError"
                    if not typed_halt
                    else (
                        "ApiResponseRejected"
                        if halt_kind is ApiHaltKind.RESPONSE_REJECTED
                        else "ApiDeliveryUncertain"
                    )
                ),
                allow_contract_resolution=(
                    typed_halt and halt_kind is ApiHaltKind.DELIVERY_UNCERTAIN
                ),
            )

        # ACTUATED (2xx): confirm the write against the system of record with
        # the same EffectVerifier that gates a GUI write. A non-CONFIRMED
        # verdict HALTs. The GUI resolve/act (and screen postconditions) are
        # SKIPPED -- the record, not the screen, is the oracle for an API write.
        result.effect_results.append(f"[api] actuated {outcome_reason}")
        self._emit_control_overlay_phase(
            "verifying",
            current_step=overlay_current,
            total_steps=overlay_total,
        )
        if self._may_pipeline_readback(effect_verifier, step):
            # Pipelined read-back (runtime/readback_pipeline.py), as for a GUI
            # save: the same record check on the side thread. The item's
            # report stays unsuccessful until it settles with the verdict.
            self._defer_readback(
                step,
                before,
                result,
                effects=effects,
                verifier=effect_verifier,
                actuation="api",
            )
            result.ok = True
            result.error = None
            return True
        try:
            error = self._verify_effects(
                step,
                before,
                result,
                effects=effects,
                verifier=effect_verifier,
            )
        except Exception as exc:  # noqa: BLE001 - deployment verifier boundary
            result.effect_verified = False
            result.effect_results.append(
                "[api] effect verification raised after API delivery "
                f"({type(exc).__name__}); outcome requires reconciliation"
            )
            error = (
                f"System-of-record verification for API step '{step.id}' "
                f"({step.intent}) raised {type(exc).__name__} after delivery; "
                "the write may have landed — run aborted"
            )
        result.ok = error is None
        result.error = error
        return True

    def _verify_uncertain_api_delivery(
        self,
        step: Step,
        result: StepResult,
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        start_state: dict[str, Any],
        before: EffectState,
        effects: list["Effect"],
        effect_verifier: Any,
        cause_type: str,
        allow_contract_resolution: bool = True,
    ) -> bool:
        """Resolve one possibly dispatched API write without another actuation.

        A response loss, rejected response, or non-conforming actuator
        exception does not end evidence collection. The runtime checks the
        configured postcondition and the independent system-of-record effect.
        A response loss can resolve only when both contracts confirm. An
        explicit response outside the binding's accepted contract always
        remains halted, even when later evidence confirms a business effect.
        The API request is never retried and the GUI fallback never runs.
        """

        result.delivery_uncertainty = ActionDeliveryUncertainty(
            operation="api_request",
            native=False,
            observed_at=datetime.now(timezone.utc).isoformat(),
            cause_type=cause_type,
        )
        overlay_current, overlay_total = self._control_overlay_progress(
            workflow, step_index, None
        )
        self._emit_control_overlay_phase(
            "verifying",
            current_step=overlay_current,
            total_steps=overlay_total,
        )
        verification_errors: list[str] = []

        if step.expect:
            try:
                after_png = self.vision.wait_settled(self.backend)
                postconditions_ok, _last_frame, failed = self._check_postconditions(
                    step,
                    after_png,
                    bundle_dir,
                    start_state,
                    result,
                    workflow=workflow,
                )
                result.postconditions_ok = postconditions_ok
                if not postconditions_ok:
                    verification_errors.append(
                        "postcondition did not confirm: "
                        + ("; ".join(failed) or "unknown postcondition")
                    )
            except Exception as exc:  # noqa: BLE001 - observation boundary
                result.postconditions_ok = False
                verification_errors.append(
                    f"postcondition observation failed ({type(exc).__name__})"
                )
        else:
            # An absent postcondition cannot resolve uncertain delivery.
            result.postconditions_ok = None
            verification_errors.append("no postcondition was declared")

        try:
            effect_error = self._verify_effects(
                step,
                before,
                result,
                effects=effects,
                verifier=effect_verifier,
            )
        except Exception as exc:  # noqa: BLE001 - deployment verifier boundary
            result.effect_verified = False
            result.effect_results.append(
                "[api] effect verification raised after uncertain delivery "
                f"({type(exc).__name__}); outcome requires reconciliation"
            )
            effect_error = (
                f"independent effect verification failed ({type(exc).__name__})"
            )
        if effect_error is not None:
            verification_errors.append(effect_error)

        uncertainty = result.delivery_uncertainty
        assert uncertainty is not None
        uncertainty.verification_attempted = True
        uncertainty.postconditions_confirmed = result.postconditions_ok is True
        uncertainty.effects_confirmed = result.effect_verified is True
        uncertainty.resolved_by_contract = (
            allow_contract_resolution
            and uncertainty.postconditions_confirmed
            and uncertainty.effects_confirmed
            and not verification_errors
        )
        result.ok = uncertainty.resolved_by_contract
        if result.ok:
            result.error = None
        else:
            result.safety_halt = True
            result.failure_category = (
                "governed_refusal"
                if self.governed_authorization is not None
                else "safety_halt"
            )
            detail = "; ".join(verification_errors) or "verification confirmed"
            if allow_contract_resolution:
                result.error = (
                    f"API delivery for step '{step.id}' ({step.intent}) was "
                    "uncertain and was not retried; the complete postcondition "
                    "and independent effect contract did not confirm: "
                    f"{detail}"
                )
            else:
                result.error = (
                    f"API delivery for step '{step.id}' ({step.intent}) "
                    "received a response outside the binding's accepted "
                    "contract and was not retried; later verification evidence "
                    f"was collected but cannot override that rejection: {detail}"
                )
        return True

    @staticmethod
    def _set_api_unavailable_refusal(
        step: Step,
        result: StepResult,
        *,
        reason: str,
    ) -> None:
        """Record an exact pre-delivery API-only refusal.

        The caller invokes this only when no request was sent. The typed
        evidence distinguishes that state from an attempted or uncertain API
        delivery and prevents any GUI fallback.
        """

        binding = step.api_binding
        assert binding is not None
        detector_input = json.dumps(
            {
                "binding": binding.model_dump(mode="json"),
                "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        result.ok = False
        result.delivery_attempted = False
        result.safety_halt = True
        result.failure_category = "governed_refusal"
        result.effect_verified = False
        result.error = (
            f"API-only step '{step.id}' ({step.intent}) is unavailable before "
            "delivery; refusing GUI fallback — run aborted"
        )
        result.safety_refusal_evidence = SafetyRefusalEvidence(
            stage="api_admission",
            code="api_path_unavailable",
            detector_input_sha256=hashlib.sha256(detector_input).hexdigest(),
        )

    def _web_form_approval_view(
        self, step: Step, params: Mapping[str, RuntimeParamScalar]
    ) -> "Any | str":
        """Prepare an API step's request for a person to approve.

        Returns the view (the prepared request, its operation digest, the
        rows and image the person sees), or the reason it can't be prepared.
        Only reads are sent.
        """

        from openadapt_flow.approval_mode.item_approval import ApiRequestRef
        from openadapt_flow.runtime.actuators import ApiActuationResult
        from openadapt_flow.runtime.actuators.web_form import (
            operation_digest,
            operation_rows,
            render_operation_png,
        )

        binding = step.api_binding
        assert binding is not None
        actuator = self._actuator_for(binding)
        if actuator is None or not callable(getattr(actuator, "prepare", None)):
            return "this API actuator can't prepare the request for approval"
        gui_params = runtime_params_for_gui(params)
        try:
            prepared = actuator.prepare(binding, gui_params)
        except Exception as exc:  # noqa: BLE001 - only reads were sent
            return f"preparing the request failed ({type(exc).__name__})"
        if isinstance(prepared, ApiActuationResult):
            return prepared.reason or "the API step is unavailable"
        effects = self._resolve_effects(list(binding.effects), params)
        # Several record checks usually select the same record (one per
        # field): the person reads, and the digest binds, each pair once.
        record = list(
            dict.fromkeys(
                (key, str(expr))
                for effect in effects
                for key, expr in sorted(effect.match.items())
            )
        )
        digest = operation_digest(
            prepared,
            record_check=[effect.contract_hash() for effect in effects],
            record=record,
        )
        rows = operation_rows(binding, prepared, gui_params, record=record)
        return _WebFormApprovalView(
            prepared=prepared,
            digest=digest,
            rows=rows,
            png=render_operation_png(
                f"Approve this request: {step.intent or step.id}", rows
            ),
            ref=ApiRequestRef(
                method=prepared.method.upper(),
                path=prepared.path,
                payload_sha256=prepared.payload_sha256(),
            ),
        )
