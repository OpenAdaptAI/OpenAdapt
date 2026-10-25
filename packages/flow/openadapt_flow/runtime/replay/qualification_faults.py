# mypy: disable-error-code="empty-body"
"""Qualification fault injection: the drills that prove a run halts when it should.

Moved verbatim from ``openadapt_flow.runtime.replayer.Replayer`` by
scripts/extract_mixin.py (docs/design/DECOMPOSITION.md, section 4.1).
``_QualificationFaultInjectionHost`` declares, for mypy only, what this code reads from the
Replayer it is mixed into.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, cast  # noqa: F401

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import ObservedSurfaceEnvironment
from openadapt_flow.qualification_faults import (
    QualificationFaultContext,
    QualificationFaultGate,
    QualificationFaultKind,
    sha256_bytes,
    verify_fault_mutation_receipt,
)

if TYPE_CHECKING:
    from typing import Callable, Literal, Optional

    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import ExecutionTargetKind, RunReport, Step, Workflow
    from openadapt_flow.qualification_environment import (
        QualificationEnvironmentObservation,
    )
    from openadapt_flow.qualification_faults import (
        FaultMutationReceipt,
        QualificationFaultDriver,
    )
    from openadapt_flow.runtime.authorization import GovernedRunAuthorization
    from openadapt_flow.runtime.surfaces import SurfaceRouter


class _QualificationFaultInjectionHost:
    def _bind_qualification_boundary(
        self, boundary: Any, *, surface_scoped: bool = False
    ) -> tuple[
        Optional[str],
        Optional[QualificationEnvironmentObservation],
        Optional[tuple[str, str]],
    ]: ...

    _qualification_case_action_paths: dict[str, Literal["gui", "api"]]
    _qualification_environment_bound: Optional[QualificationEnvironmentObservation]
    _qualification_environment_call: Optional[
        Callable[[], QualificationEnvironmentObservation]
    ]
    _qualification_fault_mutations: list[FaultMutationReceipt]
    _qualification_fault_public_key: Optional[str]
    _run_id: str
    _surface_environment_observations: dict[str, ObservedSurfaceEnvironment]
    _surface_router: Optional[SurfaceRouter]
    backend: Backend
    governed_authorization: Optional[GovernedRunAuthorization]
    qualification_fault_driver: Optional[QualificationFaultDriver]
    vision: Any


if TYPE_CHECKING:
    _Base = _QualificationFaultInjectionHost
else:
    _Base = object


class QualificationFaultInjection(_Base):
    """Qualification fault injection: the drills that prove a run halts when it should."""

    @staticmethod
    def _qualification_fault_gate_for_kind(
        kind: QualificationFaultKind,
    ) -> QualificationFaultGate:
        gates: dict[QualificationFaultKind, QualificationFaultGate] = {
            "ambiguity": "target_resolution",
            "wrong_identity": "identity_verification",
            "stale_identity": "actuation_revalidation",
            "weak_effect": "effect_strength",
            "missing_effect": "effect_verifier",
        }
        return gates[kind]

    def _qualification_fault_context(
        self,
        *,
        gate: QualificationFaultGate,
        step: Step,
        actuation_path: Literal["gui", "api"],
        before_input_sha256: str,
        effect_verifier: Any,
        effects: tuple[Any, ...] = (),
        capture_frame: Any = None,
    ) -> Optional[QualificationFaultContext]:
        authorization = self.governed_authorization
        if authorization is None or authorization.qualification_case_kind in {
            None,
            "representative",
        }:
            return None
        kind = cast(QualificationFaultKind, authorization.qualification_case_kind)
        if self._qualification_fault_gate_for_kind(kind) != gate:
            return None
        if authorization.qualification_fault_step_id_sha256 != sha256_bytes(
            step.id.encode("utf-8")
        ):
            return None
        if self._qualification_case_action_paths.get(step.id) != actuation_path:
            return None
        required = (
            authorization.qualification_project_id,
            authorization.qualification_project_revision,
            authorization.qualification_project_contract_sha256,
            authorization.qualification_campaign_id_sha256,
            self._qualification_id_sha256(authorization.qualification_case_id),
            authorization.qualification_case_input_sha256,
            authorization.qualification_run_id_sha256,
        )
        if any(value is None for value in required):
            raise RuntimeError("qualification_fault_binding_incomplete")
        return QualificationFaultContext(
            project_id=cast(str, required[0]),
            project_revision=cast(int, required[1]),
            project_contract_sha256=cast(str, required[2]),
            campaign_id_sha256=cast(str, required[3]),
            case_id_sha256=cast(str, required[4]),
            case_input_sha256=cast(str, required[5]),
            run_id_sha256=cast(str, required[6]),
            step_id=step.id,
            actuation_path=actuation_path,
            fault_kind=kind,
            gate=gate,
            before_input_sha256=before_input_sha256,
            backend=self.backend,
            vision=self.vision,
            effect_verifier=effect_verifier,
            effects=effects,
            capture_frame=capture_frame,
            surface_id=(
                self._surface_router.active_id
                if self._surface_router is not None
                else None
            ),
        )

    def _require_current_fault_driver_binding(self) -> QualificationFaultDriver:
        """Snapshot the configured driver identity immediately before mutation."""

        driver = self.qualification_fault_driver
        authorization = self.governed_authorization
        if driver is None or authorization is None:
            raise RuntimeError("qualification_fault_driver_missing")
        try:
            observed = (
                driver.driver_id,
                driver.contract_sha256,
                driver.attestation_key_id,
            )
        except Exception as exc:
            raise RuntimeError(
                "qualification_fault_driver_identity_unavailable"
            ) from exc
        expected = (
            authorization.qualification_fault_driver_id,
            authorization.qualification_fault_driver_contract_sha256,
            authorization.qualification_fault_driver_key_id,
        )
        if observed != expected:
            raise RuntimeError("qualification_fault_driver_binding_changed")
        return driver

    def _validate_fault_mutation(
        self,
        context: QualificationFaultContext,
        receipt: FaultMutationReceipt,
        *,
        after_input_sha256: str,
    ) -> None:
        authorization = self.governed_authorization
        if authorization is None:
            raise RuntimeError("qualification_fault_authorization_missing")
        expected = {
            "project_id": context.project_id,
            "project_revision": context.project_revision,
            "project_contract_sha256": context.project_contract_sha256,
            "campaign_id_sha256": context.campaign_id_sha256,
            "case_id_sha256": context.case_id_sha256,
            "case_input_sha256": context.case_input_sha256,
            "run_id_sha256": context.run_id_sha256,
            "step_id_sha256": sha256_bytes(context.step_id.encode("utf-8")),
            "actuation_path": context.actuation_path,
            "fault_kind": context.fault_kind,
            "gate": context.gate,
            "driver_id": authorization.qualification_fault_driver_id,
            "driver_contract_sha256": (
                authorization.qualification_fault_driver_contract_sha256
            ),
            "attestation_key_id": (authorization.qualification_fault_driver_key_id),
            "before_input_sha256": context.before_input_sha256,
            "after_input_sha256": after_input_sha256,
        }
        actual = receipt.model_dump(mode="python")
        if any(actual.get(key) != value for key, value in expected.items()):
            raise RuntimeError("qualification_fault_receipt_binding_invalid")
        if context.before_input_sha256 == after_input_sha256:
            raise RuntimeError("qualification_fault_input_unchanged")
        trusted_key = self._qualification_fault_public_key
        if trusted_key is None or not verify_fault_mutation_receipt(
            receipt,
            trusted_public_key_base64=trusted_key,
        ):
            raise RuntimeError("qualification_fault_receipt_signature_invalid")
        if self._qualification_fault_mutations:
            raise RuntimeError("qualification_fault_mutation_repeated")
        self._qualification_fault_mutations.append(receipt)

    def _apply_screen_fault_mutation(
        self,
        *,
        gate: QualificationFaultGate,
        step: Step,
        before_png: bytes,
        effect_verifier: Any,
        capture: Optional[Callable[[], bytes]] = None,
    ) -> bytes:
        if self._qualification_fault_mutations:
            return before_png
        # The receipt binds the frame the detector reads. ``capture`` is the
        # detector's own capture path (see _revalidation_frame_capture): two
        # paths can encode identical pixels as different PNG bytes.
        capture_frame = capture or self.backend.screenshot
        context = self._qualification_fault_context(
            gate=gate,
            step=step,
            actuation_path="gui",
            before_input_sha256=sha256_bytes(before_png),
            effect_verifier=effect_verifier,
            capture_frame=capture_frame,
        )
        if context is None:
            return before_png
        driver = self._require_current_fault_driver_binding()
        mutation = driver.mutate(context)
        if mutation is None:
            return before_png
        if mutation.replace_effect_verifier:
            raise RuntimeError("qualification_screen_fault_replaced_verifier")
        after_png = capture_frame()
        self._validate_fault_mutation(
            context,
            mutation.receipt,
            after_input_sha256=sha256_bytes(after_png),
        )
        return after_png

    def _bind_surface_qualification_environment(
        self, workflow: Workflow, surface_id: str
    ) -> Optional[str]:
        """Observe the newly active surface against its qualified boundary.

        Only a qualification case run observes environments. The primary
        surface is checked against the project's ``environment`` (as at run
        start); every other surface against ``surface_environments[id]``
        (``_begin_surfaces`` already refused a case whose project lacks one).
        On a match the input-edge guard moves to the new surface's backend,
        so every later input on that surface rechecks that environment.
        """

        authorization = self.governed_authorization
        if authorization is None or authorization.qualification_case_id is None:
            return None
        router = self._surface_router
        project = workflow.qualification
        if router is None or project is None:
            return "qualification environment evidence is unavailable"
        self._qualification_environment_call = None
        self._qualification_environment_bound = None
        boundary: Any
        if surface_id == router.primary_id:
            boundary = project.environment
        else:
            boundary = project.surface_environments.get(surface_id)
            if boundary is None:
                return "the qualification project does not bound this surface"
        refusal, observed, ids = self._bind_qualification_boundary(
            boundary, surface_scoped=surface_id != router.primary_id
        )
        if surface_id != router.primary_id and observed is not None and ids is not None:
            self._surface_environment_observations[surface_id] = (
                ObservedSurfaceEnvironment(
                    target_kind=observed.target_kind,
                    application_sha256=hashlib.sha256(
                        observed.application_identity.encode("utf-8")
                    ).hexdigest(),
                    application_version_sha256=hashlib.sha256(
                        observed.application_version.encode("utf-8")
                    ).hexdigest(),
                    session_sha256=observed.session_identity_sha256,
                    environment_digest=observed.environment_digest,
                    observer_id=ids[0],
                    observer_contract_sha256=ids[1],
                    binding_sha256=observed.binding_sha256(
                        observer_id=ids[0], observer_contract_sha256=ids[1]
                    ),
                )
            )
        return refusal

    def _is_qualification_fault_step(self, step: Step) -> bool:
        """Whether ``step`` is the fault step of the fault case this run is.

        A readiness drill (a fault qualification case) passes only when the
        engine's own detector refuses its changed input before delivery, so
        nothing is ever delivered at its fault step.
        """

        authorization = self.governed_authorization
        return bool(
            authorization is not None
            and authorization.qualification_case_kind not in {None, "representative"}
            and authorization.qualification_fault_step_id_sha256 is not None
            and authorization.qualification_fault_step_id_sha256
            == sha256_bytes(step.id.encode("utf-8"))
        )

    @staticmethod
    def _qualification_id_sha256(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _observe_qualification_environment(
        self,
        workflow: Workflow,
        report: RunReport,
        *,
        execution_target_kind: Optional[ExecutionTargetKind],
    ) -> Optional[str]:
        """Retain exact live environment evidence for a campaign run.

        Project labels are expected values only. They never become observed
        evidence. A qualification case refuses before workflow actuation when
        the active backend cannot independently return the application,
        application version, and session identity or when either expected
        application field differs. The one exception is the signed-in web
        session observer: its version is the operator's Check it confirmation,
        bound into its contract and environment digests.
        """

        authorization = self.governed_authorization
        if authorization is None or authorization.qualification_case_id is None:
            return None
        project = workflow.qualification
        if project is None or execution_target_kind is None:
            return "qualification environment evidence is unavailable"
        if (
            authorization.qualification_run_id_sha256
            != hashlib.sha256(self._run_id.encode("utf-8")).hexdigest()
        ):
            return "qualification run identity does not match its authorization"
        if execution_target_kind != project.environment.target_kind:
            return "qualification target kind does not match the project"
        refusal, observed, observer_ids = self._bind_qualification_boundary(
            project.environment
        )
        if observed is not None and observer_ids is not None:
            report.observed_application_sha256 = hashlib.sha256(
                observed.application_identity.encode("utf-8")
            ).hexdigest()
            report.observed_application_version_sha256 = hashlib.sha256(
                observed.application_version.encode("utf-8")
            ).hexdigest()
            report.observed_session_sha256 = observed.session_identity_sha256
            report.observed_environment_digest = observed.environment_digest
            report.qualification_environment_observer_id = observer_ids[0]
            report.qualification_environment_observer_contract_sha256 = observer_ids[1]
            report.observed_environment_binding_sha256 = observed.binding_sha256(
                observer_id=observer_ids[0],
                observer_contract_sha256=observer_ids[1],
            )
        return refusal

    def _require_qualification_environment_current(self) -> None:
        """Recheck the bound environment immediately before an input edge."""

        call = self._qualification_environment_call
        expected = self._qualification_environment_bound
        if call is None or expected is None:
            return
        try:
            observed = call()
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "qualification environment could not be observed before input"
            ) from exc
        if observed != expected:
            raise StructuralResolutionRefused(
                "qualification environment changed before input"
            )
