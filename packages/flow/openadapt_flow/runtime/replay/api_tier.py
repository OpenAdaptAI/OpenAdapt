# mypy: disable-error-code="empty-body"
"""Moved replayer code.

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
from openadapt_flow.runtime.authorization import (
    runtime_param_text,
    runtime_params_for_gui,
)
from openadapt_flow.runtime.replay._shared import _WebFormApprovalView

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Callable, Mapping

    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import ApiBinding, ExecutionTargetKind, Step, Workflow
    from openadapt_flow.runtime.authorization import (
        GovernedRunAuthorization,
        RuntimeParamScalar,
    )
    from openadapt_flow.runtime.effects import Effect, EffectState
    from openadapt_flow.runtime.replay._shared import _GraphStepContext


class _ApiTierHost:
    def _actuator_for(self, binding: Any) -> Optional[Any]: ...
    def _approval_gated(self) -> bool: ...

    _check_postconditions: Any

    @staticmethod
    def _control_overlay_progress(
        workflow: Workflow, step_index: int, graph_ctx: Optional["_GraphStepContext"]
    ) -> tuple[Optional[int], Optional[int]]: ...

    _durable_continuation_guard: Optional[Any]

    def _emit_control_overlay_phase(
        self,
        phase: str,
        *,
        current_step: Optional[int] = None,
        total_steps: Optional[int] = None,
        target_tracking: Optional[Any] = None,
        observation_png: Optional[bytes] = None,
    ) -> None: ...

    _resolve_effects: Any

    def _run_step(self, step: Step, **kwargs: Any) -> StepResult: ...

    _verify_effects: Any
    _web_form_approval: dict[int, Any]
    _web_form_fallback_reason: Optional[str]
    _web_form_folded_checkpointed: Any
    _web_form_span_active: Any
    _web_form_write_ahead: Optional[Callable[[], None]]
    _web_form_write_ahead_pause: Any
    api_steps: bool
    api_steps_off_reason: Optional[str]
    backend: Backend
    deployment_surface: Optional[str]
    governed_authorization: Optional[GovernedRunAuthorization]
    use_structural: bool
    vision: Any


if TYPE_CHECKING:
    _Base = _ApiTierHost
else:
    _Base = object


class ApiTier(_Base):
    """Moved replayer code."""

    def _web_form_span_at(
        self, workflow: Workflow, step_index: int, *, durable: bool
    ) -> Optional[tuple[int, Step, Optional[str]]]:
        """The API step whose span starts at ``step_index``, and why it can't run.

        Returns ``(save_index, save_step, reason)``; ``reason`` is None when
        the API step may run. None when no API step starts here.
        """

        from openadapt_flow.runtime.actuators.web_form import (
            folded_checks_digest,
            web_form_effects,
        )

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
            elif list(binding.effects) != web_form_effects(workflow.steps, save):
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
        durable_run: Optional[Any] = None,
    ) -> Optional[list[StepResult]]:
        """Run the API step for steps ``start_index..end_index``.

        Returns the results (the folded GUI steps, then the save) when the API
        step took responsibility: it saved and the record check ran, or it
        stopped after sending. Returns None when the save was not sent, with
        the reason in ``self._web_form_fallback_reason``; the caller then runs
        the GUI steps.

        An earlier save's wire check that refused a request halts the run at
        the span's first step, before anything is sent. A check without a
        refusal stays armed through the span: the span's requests go through
        the context's APIRequestContext (``context.request``), which context
        routes never intercept, so the check can't block them and still
        aborts a stray submit from the live page. Don't route them.
        """

        save = workflow.steps[end_index]
        router = getattr(self, "_surface_router", None)
        surface = router.active_id if router is not None else None
        backend = self.backend
        if getattr(backend, "form_submit_guard_armed", False):
            # Read, never disarm: see the docstring.
            late = backend.form_submit_guard_refusal()  # type: ignore[attr-defined]
            if late is not None:
                first = workflow.steps[start_index]
                # Never None here: None runs the GUI steps instead.
                return [
                    StepResult(
                        step_id=first.id,
                        intent=first.intent,
                        ok=False,
                        safety_halt=True,
                        failure_category="safety_halt",
                        actuation="api",
                        delivery_attempted=False,
                        surface=surface,
                        error=(
                            f"Step '{first.id}' ({first.intent}): after the "
                            f"previous save, {late} — run aborted"
                        ),
                    )
                ]
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
        # A resumed leg already has its write-ahead: the continuation guard
        # moves the attempt to delivery_started before the first request, and
        # a restart then reconciles. A first leg gets one here.
        if durable_run is not None and self._durable_continuation_guard is None:

            def write_ahead() -> None:
                from openadapt_flow.runtime.api_delivery import record_span_write_ahead

                record_span_write_ahead(
                    self,
                    durable_run,
                    workflow,
                    folded,
                    start_index,
                    end_index,
                    params,
                    surface,
                    router.observations if router is not None else None,
                )

            self._web_form_write_ahead = write_ahead
        self._web_form_folded_checkpointed = False
        self._web_form_write_ahead_pause = ""
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
            self._web_form_write_ahead = None
            self._web_form_approval.clear()
        if result.api_fallback is not None:
            self._web_form_fallback_reason = result.api_fallback
            return None
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

        from openadapt_flow.runtime.api_delivery import managed_delivery_unacknowledged

        allow_contract_resolution = (
            allow_contract_resolution and not managed_delivery_unacknowledged(self)
        )
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
