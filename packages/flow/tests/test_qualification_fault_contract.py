"""Adversarial coverage for qualification fault and environment bindings."""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image, ImageDraw

from openadapt_flow import vision as vision_module
from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.deployment import build_replayer
from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
    qualified_effect_requirements,
)
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionKind,
    Anchor,
    ApiBinding,
    ApiIdentityBinding,
    EffectVerificationEvidence,
    Postcondition,
    PostconditionKind,
    SafetyRefusalEvidence,
    Step,
    StepResult,
    StructuralHandle,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityEvidenceSource,
    IdentityPolicy,
    IdentitySignalKey,
    IdentitySignalPolicy,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationOutcome,
    _case_run_report_integrity_error,
    add_case,
    init_project,
    set_action_classification,
    set_effect_policy,
    set_identity_policy,
    workflow_contract_sha256,
)
from openadapt_flow.qualification_environment import (
    BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256,
    BackendQualificationEnvironmentObserver,
    QualificationEnvironmentObservation,
)
from openadapt_flow.qualification_faults import (
    FaultMutationReceipt,
    QualificationFaultContext,
    QualificationFaultMutation,
    effect_verifier_input_sha256,
    fault_detector_contract_error,
    sha256_bytes,
    sign_fault_mutation_receipt,
    verify_fault_mutation_receipt,
)
from openadapt_flow.runtime import Replayer
from openadapt_flow.runtime.actuators import ActuationStatus, ApiActuationResult
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_bytes,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.resolver import (
    structural_resolution_fingerprint,
    visual_resolution_point_fingerprint,
)
from openadapt_flow.verification import VerificationTier

_SESSION = "a" * 64
_ENVIRONMENT = "b" * 64


def test_unbound_environment_and_empty_fault_store_preserve_legacy_shape() -> None:
    workflow = Workflow(name="legacy-qualification", steps=[])
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Legacy display label",
            application_version="1",
            environment_digest="0" * 64,
            runtime_version="legacy",
        ),
    )

    assert workflow.qualification is not None
    dumped = workflow.qualification.model_dump(mode="json")
    assert "trusted_fault_driver_keys" not in dumped
    assert "application_identity" not in dumped["environment"]
    assert "environment_observer_id" not in dumped["environment"]
    assert "environment_observer_contract_sha256" not in dumped["environment"]


def test_multi_action_fault_case_requires_one_explicit_fault_target() -> None:
    actions = [
        QualificationActionTarget(step_id="prepare", actuation_path="gui"),
        QualificationActionTarget(step_id="submit", actuation_path="gui"),
    ]

    with pytest.raises(ValueError, match="requires one exact fault target"):
        QualificationCase(
            id="fault-later-write",
            kind=QualificationCaseKind.MISSING_EFFECT,
            action_targets=actions,
            expected_outcome=QualificationOutcome.HALTED,
        )

    case = QualificationCase(
        id="fault-later-write",
        kind=QualificationCaseKind.MISSING_EFFECT,
        action_targets=actions,
        fault_target=actions[1],
        expected_outcome=QualificationOutcome.HALTED,
    )
    assert case.resolved_fault_target() == actions[1]


def _screen_png(*, ambiguous: bool = False, wrong_identity: bool = False) -> bytes:
    image = Image.new("RGB", (20, 20), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((5, 5, 10, 10), fill="black")
    if ambiguous:
        draw.rectangle((12, 5, 17, 10), fill="black")
    if wrong_identity:
        draw.rectangle((0, 12, 19, 19), fill="orange")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class _ObservedBackend:
    viewport = (20, 20)

    def __init__(self, *, structural: bool = True) -> None:
        self.actions: list[tuple[str, int, int]] = []
        self.ambiguous = False
        self.wrong_identity = False
        self.structural = structural
        self._input_guard: Any = None
        self._guarded_keyboard_point: tuple[int, int] | None = None
        self._last_structural_locator: StructuralLocator | None = None
        self._last_structural_handle: StructuralHandle | None = None
        self._selected_value = ""

    def screenshot(self) -> bytes:
        return _screen_png(
            ambiguous=self.ambiguous,
            wrong_identity=self.wrong_identity,
        )

    def locate_structural(self, locator: StructuralLocator) -> StructuralHandle | None:
        if not self.structural:
            return None
        if self.ambiguous:
            raise StructuralResolutionRefused("two submit controls match")
        if locator.selector == "#drag-destination":
            handle = StructuralHandle(
                point=(15, 15),
                region=(12, 12, 6, 6),
                target_fingerprint="d" * 64,
            )
        else:
            handle = StructuralHandle(
                point=(8, 8),
                region=(5, 5, 6, 6),
                target_fingerprint="c" * 64,
            )
        self._last_structural_locator = locator
        self._last_structural_handle = handle
        return handle

    def set_qualification_input_guard(self, guard: Any) -> None:
        self._input_guard = guard

    def _guard(self) -> None:
        if self._input_guard is not None:
            self._input_guard()

    def arm_guarded_coordinate(self, _x: int, _y: int) -> None:
        return None

    def cancel_guarded_coordinate(self) -> None:
        return None

    def guarded_keyboard_frame(self) -> bytes:
        return self.screenshot()

    def arm_guarded_keyboard(self, x: int, y: int) -> None:
        self._guarded_keyboard_point = (int(x), int(y))

    def cancel_guarded_keyboard(self) -> None:
        self._guarded_keyboard_point = None

    def press_guarded(
        self, _key: str, *, expected_frame_sha256: str
    ) -> ActionDeliveryReceipt:
        del expected_frame_sha256
        raise AssertionError("the selection fixture does not use guarded press")

    def type_text_guarded(
        self, _text: str, *, expected_frame_sha256: str
    ) -> ActionDeliveryReceipt:
        del expected_frame_sha256
        raise AssertionError("the selection fixture does not use guarded type")

    def select_option(self, text: str, _commit_key: str) -> None:
        self._selected_value = text

    def select_option_guarded(
        self,
        text: str,
        commit_key: str,
        *,
        target_point: tuple[int, int],
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        assert hashlib.sha256(self.screenshot()).hexdigest() == expected_frame_sha256
        assert self._guarded_keyboard_point == target_point
        assert self._last_structural_locator is not None
        assert self._last_structural_handle is not None
        self._guarded_keyboard_point = None
        self.select_option(text, commit_key)
        self.actions.append(("select_option", *target_point))
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="guarded_select_option",
            native=False,
            target_fingerprint=structural_resolution_fingerprint(
                self._last_structural_locator,
                self._last_structural_handle,
            ),
            selection_value_sha256=hashlib.sha256(text.encode()).hexdigest(),
            selection_commit_key=commit_key,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def text_value_at(self, _x: int, _y: int) -> str:
        return self._selected_value

    def focused_text_value(self) -> str:
        return self._selected_value

    def act_guarded_coordinate(
        self,
        _x: int,
        _y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
        button: str = "left",
    ) -> ActionDeliveryReceipt:
        del expected_frame_sha256
        if button != "right" or double:
            raise AssertionError(
                "the structural fixture uses coordinate actuation only for right click"
            )
        self._guard()
        self.actions.append(("right_click", int(_x), int(_y)))
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="guarded_coordinate_right_click",
            native=False,
            target_fingerprint="c" * 64,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def drag_guarded(
        self,
        _x: int,
        _y: int,
        _end_x: int,
        _end_y: int,
        *,
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        del expected_frame_sha256
        raise AssertionError("the structural fixture must not use coordinate drag")

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        assert not double
        self._guard()
        self.actions.append(("click", x, y))

    def act_structural(
        self,
        _locator: StructuralLocator,
        handle: StructuralHandle,
        *_args: Any,
        **_kwargs: Any,
    ) -> ActionDeliveryReceipt:
        self._guard()
        self.actions.append(("structural", *handle.point))
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="uia_invoke",
            native=True,
            target_fingerprint=handle.target_fingerprint,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def drag_structural_guarded(
        self,
        source_locator: StructuralLocator,
        source_handle: StructuralHandle,
        destination_locator: StructuralLocator,
        destination_handle: StructuralHandle,
    ) -> ActionDeliveryReceipt:
        self._guard()
        self.actions.append(("drag", *source_handle.point))
        assert destination_handle.point == (15, 15)
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="guarded_dom_drag",
            native=False,
            target_fingerprint=structural_resolution_fingerprint(
                source_locator,
                source_handle,
            ),
            destination_fingerprint=structural_resolution_fingerprint(
                destination_locator,
                destination_handle,
            ),
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def structured_text_at(self, _x: int, _y: int) -> str:
        return "Wrong record" if self.wrong_identity else "Synthetic record"

    def qualification_environment_identity(self) -> tuple[str, str, str, str]:
        return "https://fixture.example", "1", _SESSION, _ENVIRONMENT


def _pixel_identity_png(*, wrong_identity: bool = False) -> bytes:
    image = Image.new("L", (240, 48), "white")
    if wrong_identity:
        ImageDraw.Draw(image).rectangle((100, 0, 104, 47), fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class _PixelObservedBackend(_ObservedBackend):
    viewport = (240, 48)

    def screenshot(self) -> bytes:
        return _pixel_identity_png(wrong_identity=self.wrong_identity)

    def locate_structural(self, _locator: StructuralLocator) -> StructuralHandle | None:
        return StructuralHandle(
            point=(120, 24),
            region=(100, 14, 40, 20),
            target_fingerprint="c" * 64,
        )

    def structured_text_at(self, _x: int, _y: int) -> None:
        return None


class _SelectRemoteObservedBackend(_ObservedBackend):
    def prepare_pointer_actuation(self, _x: int, _y: int) -> None:
        return None

    def acquire_actuation_frame(self) -> bytes:
        return self.screenshot()

    def arm_focused_element_lease(self, x: int, y: int) -> None:
        self._guarded_keyboard_point = (int(x), int(y))

    def cancel_focused_element_lease(self) -> None:
        self._guarded_keyboard_point = None

    def click_guarded(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        assert hashlib.sha256(self.screenshot()).hexdigest() == expected_frame_sha256
        self._guard()
        self.actions.append(("double_click" if double else "click", x, y))
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="rdp_double_click" if double else "rdp_click",
            native=False,
            target_fingerprint=visual_resolution_point_fingerprint(
                expected_frame_sha256,
                (x, y),
            ),
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def right_click_guarded(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        assert hashlib.sha256(self.screenshot()).hexdigest() == expected_frame_sha256
        self._guard()
        self.actions.append(("right_click", x, y))
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="rdp_right_click",
            native=False,
            target_fingerprint=visual_resolution_point_fingerprint(
                expected_frame_sha256,
                (x, y),
            ),
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def drag_guarded(
        self,
        x: int,
        y: int,
        end_x: int,
        end_y: int,
        *,
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        assert hashlib.sha256(self.screenshot()).hexdigest() == expected_frame_sha256
        self._guard()
        self.actions.append(("drag", x, y))
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="rdp_drag",
            native=False,
            target_fingerprint=visual_resolution_point_fingerprint(
                expected_frame_sha256,
                (x, y),
            ),
            destination_fingerprint=visual_resolution_point_fingerprint(
                expected_frame_sha256,
                (end_x, end_y),
            ),
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def select_option_guarded(
        self,
        text: str,
        commit_key: str,
        *,
        target_point: tuple[int, int],
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        assert hashlib.sha256(self.screenshot()).hexdigest() == expected_frame_sha256
        assert self._guarded_keyboard_point == target_point
        self._guarded_keyboard_point = None
        self.select_option(text, commit_key)
        self.actions.append(("select_option", *target_point))
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="rdp_select_option",
            native=False,
            target_fingerprint=visual_resolution_point_fingerprint(
                expected_frame_sha256,
                target_point,
            ),
            selection_value_sha256=hashlib.sha256(text.encode()).hexdigest(),
            selection_commit_key=commit_key,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )


@dataclass(frozen=True)
class _SettleResult:
    png: bytes
    settled: bool = True


class _Vision:
    def text_present(
        self,
        _screen_png: bytes,
        _text: str,
        **_kwargs: Any,
    ) -> bool:
        return False

    def wait_settled(self, backend: _ObservedBackend, **_kwargs: Any) -> bytes:
        return backend.screenshot()

    def wait_settled_result(
        self, backend: _ObservedBackend, **_kwargs: Any
    ) -> _SettleResult:
        return _SettleResult(backend.screenshot())


class _StrongEffectVerifier:
    substrate = "test"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def capture_pre_state(self, _context: Any = None) -> EffectState:
        return EffectState(substrate=self.substrate, reachable=True)

    def verify(
        self,
        effect: Effect,
        _before: EffectState,
        _context: Any = None,
    ) -> EffectVerdict:
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=effect.kind,
            substrate=self.substrate,
        )


class _WeakEffectVerifier:
    verification_tier = VerificationTier.IMMEDIATE_SCREEN


class _NonRefusingEffectVerifier:
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def capture_pre_state(self) -> object:
        return object()


class _UnavailableApiActuator:
    def __init__(self) -> None:
        self.calls = 0

    def actuate(self, _binding: ApiBinding, _params: dict[str, str]):
        self.calls += 1
        return ApiActuationResult(
            status=ActuationStatus.UNAVAILABLE,
            reason="fixture request was not sent",
        )


class _FaultDriver:
    key_id = "test-fault-driver"

    def __init__(
        self,
        kind: QualificationCaseKind,
        *,
        decline: bool = False,
        change_binding: str | None = None,
        non_refusing_effect_mutation: bool = False,
        stale_identity_mismatch: bool = False,
        signing_key: Ed25519PrivateKey | None = None,
    ) -> None:
        self.kind = kind
        self._driver_id = f"test.{kind.value}-driver"
        self._contract_sha256 = hashlib.sha256(
            f"test {kind.value} driver v1".encode()
        ).hexdigest()
        self.decline = decline
        self.change_binding = change_binding
        self.non_refusing_effect_mutation = non_refusing_effect_mutation
        self.stale_identity_mismatch = stale_identity_mismatch
        self.calls = 0
        self._identity_reads = {"id": 0, "contract": 0, "key": 0}
        self._signing_key = signing_key or Ed25519PrivateKey.generate()
        self.private_key = self._signing_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        self.public_key_base64 = base64.b64encode(
            self._signing_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).decode("ascii")

    def _bound_value(self, field: str, value: str) -> str:
        self._identity_reads[field] += 1
        if self.change_binding == field and self._identity_reads[field] > 1:
            return f"changed-{value}" if field != "contract" else "d" * 64
        return value

    @property
    def driver_id(self) -> str:
        return self._bound_value("id", self._driver_id)

    @property
    def contract_sha256(self) -> str:
        return self._bound_value("contract", self._contract_sha256)

    @property
    def attestation_key_id(self) -> str:
        return self._bound_value("key", self.key_id)

    def mutate(
        self, context: QualificationFaultContext
    ) -> QualificationFaultMutation | None:
        self.calls += 1
        if self.decline or context.fault_kind != self.kind.value:
            return None
        replacement = context.effect_verifier
        replace_effect_verifier = False
        if self.kind is QualificationCaseKind.STALE_IDENTITY and (
            self.stale_identity_mismatch
        ):
            context.backend.wrong_identity = True
            after_sha256 = sha256_bytes(context.backend.screenshot())
        elif self.kind in {
            QualificationCaseKind.AMBIGUITY,
            QualificationCaseKind.STALE_IDENTITY,
        }:
            context.backend.ambiguous = True
            after_sha256 = sha256_bytes(context.backend.screenshot())
        elif self.kind is QualificationCaseKind.WRONG_IDENTITY:
            context.backend.wrong_identity = True
            after_sha256 = sha256_bytes(context.backend.screenshot())
        else:
            replace_effect_verifier = True
            if self.non_refusing_effect_mutation:
                replacement = _NonRefusingEffectVerifier()
            else:
                replacement = (
                    None
                    if self.kind is QualificationCaseKind.MISSING_EFFECT
                    else _WeakEffectVerifier()
                )
            after_sha256 = effect_verifier_input_sha256(
                replacement,
                context.effects,
            )
        receipt = FaultMutationReceipt(
            project_id=context.project_id,
            project_revision=context.project_revision,
            project_contract_sha256=context.project_contract_sha256,
            campaign_id_sha256=context.campaign_id_sha256,
            case_id_sha256=context.case_id_sha256,
            case_input_sha256=context.case_input_sha256,
            run_id_sha256=context.run_id_sha256,
            step_id_sha256=sha256_bytes(context.step_id.encode("utf-8")),
            actuation_path=context.actuation_path,
            fault_kind=context.fault_kind,
            gate=context.gate,
            driver_id=self._driver_id,
            driver_contract_sha256=self._contract_sha256,
            before_input_sha256=context.before_input_sha256,
            after_input_sha256=after_sha256,
            mutation_artifact_sha256=sha256_bytes(
                f"test {self.kind.value} fixture v1".encode()
            ),
            attestation_key_id=self.key_id,
        )
        return QualificationFaultMutation(
            receipt=sign_fault_mutation_receipt(
                receipt,
                private_key=self.private_key,
            ),
            replace_effect_verifier=replace_effect_verifier,
            effect_verifier=replacement,
        )


def _fault_workflow(
    tmp_path: Path,
    kind: QualificationCaseKind,
    driver: _FaultDriver,
    *,
    api_effect_only: bool = False,
    actuation_path: Literal["gui", "api"] = "gui",
    action: ActionKind = ActionKind.CLICK,
    search_pad: int = 96,
) -> tuple[Workflow, Path]:
    bundle = tmp_path / f"bundle-{kind.value}"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "button.png").write_bytes(_screen_png())
    anchor = Anchor(
        template="templates/button.png",
        structural=StructuralLocator(selector="#submit"),
        region=(5, 5, 6, 6),
        click_point=(8, 8),
        search_pad=search_pad,
    )
    anchor.structured_identity = "Synthetic record"
    step = Step(
        id="submit",
        intent="Submit",
        action=action,
        anchor=anchor,
        drag_end_anchor=(
            Anchor(
                template="templates/button.png",
                structural=StructuralLocator(selector="#drag-destination"),
                region=(12, 12, 6, 6),
                click_point=(15, 15),
            )
            if action is ActionKind.DRAG
            else None
        ),
        identity_armed=True,
        risk="irreversible",
    )
    effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"record_id": ValueExpr(param="record_id")},
            risk="irreversible",
        )
    ]
    step.effects = effects
    if api_effect_only:
        step.api_binding = ApiBinding(
            method="POST",
            url_template="/synthetic-records/{record_id}",
            effects=effects,
            identity=[
                ApiIdentityBinding(
                    key="record_id",
                    param="record_id",
                    effect_field="record_id",
                    request_pointers=["/url/record_id"],
                )
            ],
        )
    backend = _ObservedBackend()
    observer = BackendQualificationEnvironmentObserver(backend)
    workflow = Workflow(name=f"fault-{kind.value}", surface="web", steps=[step])
    workflow.params["record_id"] = "synthetic-1"
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Fixture application",
            application_identity="https://fixture.example",
            application_version="1",
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=(
                BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256
            ),
            environment_digest=_ENVIRONMENT,
            runtime_version="test",
        ),
    )
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="submit",
            classification=ActionRiskClass.IRREVERSIBLE,
            explanation="qualification fault target changes business state",
            operator_confirmed=True,
        ),
    )
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id="submit",
            enforcement=(
                IdentityEnforcement.SIGNAL_QUORUM
                if actuation_path == "api"
                else IdentityEnforcement.CANONICAL_LADDER
            ),
            signals=(
                [
                    IdentitySignalPolicy(
                        key=IdentitySignalKey.RECORD_ID,
                        source=IdentityEvidenceSource.STRUCTURED,
                        extract_pattern=r"^(?P<value>.+)$",
                    )
                ]
                if actuation_path == "api"
                else []
            ),
            quorum=1 if actuation_path == "api" else 0,
        ),
    )
    for qualified_path in ("gui", "api"):
        if qualified_path == "api" and step.api_binding is None:
            continue
        set_effect_policy(
            workflow,
            step_id="submit",
            effect_index=0,
            tier=VerificationTier.INDEPENDENT_SYSTEM,
            actuation_path=qualified_path,
        )
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    assert workflow.qualification is not None
    workflow.qualification.cases = [
        case.model_copy(
            update={
                "runtime_input_sha256": input_sha256,
                **(
                    {
                        "action_targets": [
                            QualificationActionTarget(
                                step_id="submit", actuation_path="gui"
                            )
                        ]
                    }
                    if case.kind is QualificationCaseKind.REPRESENTATIVE
                    else {
                        "action_targets": [
                            QualificationActionTarget(
                                step_id="submit", actuation_path=actuation_path
                            )
                        ]
                    }
                ),
            }
        )
        for case in workflow.qualification.cases
    ]
    workflow.qualification.trusted_fault_driver_keys[driver.key_id] = (
        driver.public_key_base64
    )
    workflow.save(bundle)
    return Workflow.load(bundle), bundle


def _fault_authorization(
    workflow: Workflow,
    kind: QualificationCaseKind,
    driver: _FaultDriver,
    *,
    run_id: str,
    actuation_path: Literal["gui", "api"] = "gui",
    action_paths: dict[str, Literal["gui", "api"]] | None = None,
    required_identity_step_ids: tuple[str, ...] = ("submit",),
    fault_step_id: str = "submit",
) -> GovernedRunAuthorization:
    assert workflow.manifest is not None and workflow.qualification is not None
    case_id = f"fault-{kind.value.replace('_', '-')}"
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    return GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=input_sha256,
        admitted_policy_name="clinical-write",
        admitted_policy_contract_sha256="e" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        qualified_effect_requirements=qualified_effect_requirements(
            workflow, ExecutionProfile.STANDARD
        ),
        required_identity_step_ids=required_identity_step_ids,
        approval_source="qualification-campaign",
        qualification_project_id=workflow.qualification.project_id,
        qualification_project_revision=workflow.qualification.revision,
        qualification_project_contract_sha256=(
            workflow.qualification.contract_sha256()
        ),
        qualification_case_id=case_id,
        qualification_campaign_id_sha256=sha256_bytes(b"campaign"),
        qualification_case_input_sha256=input_sha256,
        qualification_run_id_sha256=sha256_bytes(run_id.encode("utf-8")),
        qualification_case_kind=kind.value,
        qualification_case_action_paths=(action_paths or {"submit": actuation_path}),
        qualification_fault_driver_id=driver._driver_id,
        qualification_fault_driver_contract_sha256=driver._contract_sha256,
        qualification_fault_driver_key_id=driver.key_id,
        qualification_fault_step_id_sha256=sha256_bytes(fault_step_id.encode("utf-8")),
    )


def _run_fault(
    tmp_path: Path,
    kind: QualificationCaseKind,
    driver: _FaultDriver,
    *,
    api_effect_only: bool = False,
    api_actuator: Any = None,
):
    actuation_path = "api" if api_effect_only and api_actuator is not None else "gui"
    workflow, bundle = _fault_workflow(
        tmp_path,
        kind,
        driver,
        api_effect_only=api_effect_only,
        actuation_path=actuation_path,
    )
    run_id = f"run-{kind.value}-{id(driver)}"
    backend = _ObservedBackend()
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            kind,
            driver,
            run_id=run_id,
            actuation_path=actuation_path,
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        api_actuator=api_actuator,
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / f"run-{kind.value}",
        run_id=run_id,
        execution_target_kind="web",
    )
    return report, backend


def _two_write_fault_workflow(
    tmp_path: Path,
    driver: _FaultDriver,
    *,
    first_action: ActionKind = ActionKind.CLICK,
    remote_first: bool = False,
    first_read_only: bool = False,
) -> tuple[Workflow, Path]:
    bundle = tmp_path / "bundle-two-write-fault"
    (bundle / "templates").mkdir(parents=True)
    template_png = _screen_png()
    if first_action is ActionKind.SELECT_OPTION:
        with Image.open(io.BytesIO(template_png)) as image:
            cropped = io.BytesIO()
            image.crop((5, 5, 11, 11)).save(cropped, format="PNG")
        template_png = cropped.getvalue()
    (bundle / "templates" / "button.png").write_bytes(template_png)

    def write_step(step_id: str) -> Step:
        action = first_action if step_id == "prepare" else ActionKind.CLICK
        read_only = step_id == "prepare" and first_read_only
        return Step(
            id=step_id,
            intent=f"Write {step_id}",
            action=action,
            param="choice" if action is ActionKind.SELECT_OPTION else None,
            selection_commit_key=(
                "Enter" if action is ActionKind.SELECT_OPTION else None
            ),
            selection_region=(
                (5, 5, 6, 6) if action is ActionKind.SELECT_OPTION else None
            ),
            anchor=Anchor(
                template="templates/button.png",
                structural=StructuralLocator(selector=f"#{step_id}"),
                region=(5, 5, 6, 6),
                click_point=(8, 8),
                structured_identity="Synthetic record",
            ),
            identity_armed=True,
            risk="reversible" if read_only else "irreversible",
            expect=[
                Postcondition(
                    kind=PostconditionKind.TEXT_ABSENT,
                    text="Save failed",
                )
            ],
            drag_end_anchor=(
                Anchor(
                    template="templates/button.png",
                    structural=StructuralLocator(selector="#drag-destination"),
                    region=(12, 12, 6, 6),
                    click_point=(15, 15),
                )
                if action is ActionKind.DRAG
                else None
            ),
            effects=(
                []
                if read_only
                else [
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"record_id": ValueExpr(literal=step_id)},
                        risk="irreversible",
                    )
                ]
            ),
        )

    remote_surface = first_action is ActionKind.SELECT_OPTION or remote_first
    backend = _SelectRemoteObservedBackend() if remote_surface else _ObservedBackend()
    observer = BackendQualificationEnvironmentObserver(backend)
    workflow = Workflow(
        name="two-write-fault",
        surface="rdp" if remote_surface else "web",
        execution_mode="external" if remote_surface else None,
        steps=[write_step("prepare"), write_step("submit")],
    )
    if first_action is ActionKind.SELECT_OPTION:
        workflow.params["choice"] = "Approved"
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="rdp" if remote_surface else "web",
            application="Fixture application",
            application_identity="https://fixture.example",
            application_version="1",
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=(
                BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256
            ),
            environment_digest=_ENVIRONMENT,
            runtime_version="test",
        ),
    )
    for step_id in ("prepare", "submit"):
        read_only = step_id == "prepare" and first_read_only
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step_id,
                classification=(
                    ActionRiskClass.READ_ONLY
                    if read_only
                    else ActionRiskClass.IRREVERSIBLE
                ),
                explanation="qualification fixture changes business state",
                operator_confirmed=True,
            ),
        )
        set_identity_policy(
            workflow,
            IdentityPolicy(
                step_id=step_id,
                enforcement=IdentityEnforcement.CANONICAL_LADDER,
            ),
        )
        if not read_only:
            set_effect_policy(
                workflow,
                step_id=step_id,
                effect_index=0,
                tier=VerificationTier.INDEPENDENT_SYSTEM,
            )
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    paths = [
        QualificationActionTarget(step_id="prepare", actuation_path="gui"),
        QualificationActionTarget(step_id="submit", actuation_path="gui"),
    ]
    fault_target = QualificationActionTarget(
        step_id="submit",
        actuation_path="gui",
    )
    assert workflow.qualification is not None
    workflow.qualification.cases = [
        case.model_copy(
            update={
                "runtime_input_sha256": input_sha256,
                "action_targets": paths,
                "fault_target": (
                    None
                    if case.kind is QualificationCaseKind.REPRESENTATIVE
                    else fault_target
                ),
            }
        )
        for case in workflow.qualification.cases
    ]
    workflow.qualification.trusted_fault_driver_keys[driver.key_id] = (
        driver.public_key_base64
    )
    workflow.save(bundle)
    return Workflow.load(bundle), bundle


def test_later_fault_target_allows_prior_required_write_then_halts(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(tmp_path, driver)
    run_id = "two-write-later-fault"
    backend = _ObservedBackend()
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
            action_paths={"prepare": "gui", "submit": "gui"},
            required_identity_step_ids=("prepare", "submit"),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "run-two-write-later-fault",
        run_id=run_id,
        execution_target_kind="web",
    )

    assert report.execution_outcome == "HALTED"
    assert [result.step_id for result in report.results] == ["prepare", "submit"]
    assert report.results[0].ok is True
    assert report.results[0].delivery_attempted is True
    assert report.results[0].effect_verified is True
    assert report.results[1].safety_halt is True
    assert report.results[1].delivery_attempted is False
    assert len(backend.actions) == 1
    assert len(report.qualification_fault_mutations) == 1
    receipt = report.qualification_fault_mutations[0]
    assert receipt.step_id_sha256 == sha256_bytes(b"submit")
    assert fault_detector_contract_error(report, receipt) is None

    assert (
        classify_execution_outcome(
            report,
            workflow,
            ExecutionProfile.STANDARD,
            _qualification_fault_target_step_id="submit",
        )
        is ExecutionOutcome.VERIFIED
    )
    insufficient_prior_evidence = report.model_copy(
        update={
            "results": [
                report.results[0].model_copy(update={"effect_evidence": []}),
                report.results[1],
            ]
        }
    )
    assert (
        classify_execution_outcome(
            insufficient_prior_evidence,
            workflow,
            ExecutionProfile.STANDARD,
            _qualification_fault_target_step_id="submit",
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


def _fault_case_integrity_result(
    *,
    workflow: Workflow,
    report: Any,
    evidence_root: Path,
    run_dir: Path,
    case_id: str = "fault-missing-effect",
) -> tuple[QualificationCase, QualificationCaseResult]:
    """Retain one real two-write fault run for exact qualification checks."""

    project = workflow.qualification
    assert project is not None
    case = next(item for item in project.cases if item.id == case_id)
    input_bytes = runtime_inputs_bytes(workflow, None, None)
    report_bytes = report.model_dump_json().encode()
    receipt = report.qualification_fault_mutations[0]
    receipt_bytes = receipt.artifact_bytes()
    mutation_bytes = f"test {receipt.fault_kind} fixture v1".encode()
    artifacts = {
        "report.json": report_bytes,
        "input.json": input_bytes,
        "receipt.json": receipt_bytes,
        "mutation.bin": mutation_bytes,
    }
    for item in report.results:
        if item.before_png is not None:
            artifacts[item.before_png] = (run_dir / item.before_png).read_bytes()
        for resolution in (item.resolution, item.drag_end_resolution):
            if resolution is None or resolution.visual_evidence is None:
                continue
            visual = resolution.visual_evidence
            for relative_path in (
                visual.frame_inventory_ref,
                visual.template_inventory_ref,
            ):
                artifacts[relative_path] = (run_dir / relative_path).read_bytes()
        if item.identity is not None and item.identity.pixel_evidence is not None:
            pixel = item.identity.pixel_evidence
            for relative_path in (
                pixel.recorded_crop_inventory_ref,
                pixel.live_crop_inventory_ref,
            ):
                artifacts[relative_path] = (run_dir / relative_path).read_bytes()
    evidence_root.mkdir()
    for name, payload in artifacts.items():
        path = evidence_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    result = QualificationCaseResult(
        case_id=case.id,
        project_id=project.project_id,
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=project.environment.contract_sha256(),
        environment_digest=project.environment.environment_digest,
        runtime_version=project.environment.runtime_version,
        runner_id="fixture-runner",
        status="passed",
        observed_outcome=QualificationOutcome.HALTED,
        campaign_id_sha256=report.governed_qualification_campaign_id_sha256,
        case_input_sha256=report.governed_qualification_case_input_sha256,
        run_id_sha256=report.governed_qualification_run_id_sha256,
        evidence=[
            EvidenceRef(
                kind="run_report",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                relative_path="report.json",
            ),
            EvidenceRef(
                kind="case_input",
                sha256=hashlib.sha256(input_bytes).hexdigest(),
                relative_path="input.json",
            ),
            EvidenceRef(
                kind="fault_receipt",
                sha256=receipt.receipt_sha256(),
                relative_path="receipt.json",
            ),
            EvidenceRef(
                kind="fault_mutation",
                sha256=hashlib.sha256(mutation_bytes).hexdigest(),
                relative_path="mutation.bin",
            ),
            *[
                EvidenceRef(
                    kind="other",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    relative_path=name,
                )
                for name, payload in artifacts.items()
                if name
                not in {"report.json", "input.json", "receipt.json", "mutation.bin"}
            ],
        ],
        attestation_key_id="fixture-runner",
    )
    return case, result


@pytest.mark.parametrize("mutation", ["identity", "resolution", "receipt"])
def test_prior_fault_prefix_action_requires_exact_qualification_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Only the terminal fault row can use the qualification fault exemption."""

    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(tmp_path, driver)
    run_id = f"two-write-prefix-{mutation}"
    report = Replayer(
        _ObservedBackend(),
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
            action_paths={"prepare": "gui", "submit": "gui"},
            required_identity_step_ids=("prepare", "submit"),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    )
    run_dir = tmp_path / f"run-prefix-{mutation}"
    report = report.run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )
    project = workflow.qualification
    assert project is not None
    valid_root = tmp_path / f"evidence-valid-{mutation}"
    valid_case, valid_result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=valid_root,
        run_dir=run_dir,
    )
    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=valid_case,
            result=valid_result,
            evidence_root=valid_root,
        )
        is None
    )

    changed = report.model_copy(deep=True)
    if mutation == "identity":
        assert changed.results[0].identity is not None
        changed.results[0].identity = changed.results[0].identity.model_copy(
            update={"coverage": 0.0}
        )
    elif mutation == "resolution":
        changed.results[0].resolution = None
    else:
        changed.results[0].delivery_receipt = ActionDeliveryReceipt(
            receipt_id="forged",
            operation="delete_everything",
            native=False,
            target_fingerprint="f" * 64,
            delivered_at="2099-01-01T00:00:00+00:00",
        )

    evidence_root = tmp_path / f"evidence-{mutation}"
    case, result = _fault_case_integrity_result(
        workflow=workflow,
        report=changed,
        evidence_root=evidence_root,
        run_dir=run_dir,
    )
    error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=case,
        result=result,
        evidence_root=evidence_root,
    )

    assert error is not None
    assert "prior action" in error[1]


def test_read_only_prior_remote_click_has_exact_fault_prefix_proof(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(
        tmp_path,
        driver,
        remote_first=True,
        first_read_only=True,
    )
    assert workflow.qualification is not None
    submit_target = QualificationActionTarget(step_id="submit", actuation_path="gui")
    workflow.qualification.cases = [
        case.model_copy(
            update={
                "action_targets": [submit_target],
                "fault_target": (
                    submit_target
                    if case.kind is QualificationCaseKind.MISSING_EFFECT
                    else case.fault_target
                ),
            }
        )
        if case.kind is QualificationCaseKind.MISSING_EFFECT
        else case
        for case in workflow.qualification.cases
    ]
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    run_id = "prior-read-only-remote-click"
    run_dir = tmp_path / f"run-{run_id}"
    report = Replayer(
        _SelectRemoteObservedBackend(structural=False),
        vision=vision_module,
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
            action_paths={"submit": "gui"},
            required_identity_step_ids=("submit",),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="rdp",
    )

    receipt = report.results[0].delivery_receipt
    assert receipt is not None
    assert receipt.operation == "rdp_click"
    assert report.results[0].actuation == "remote_guarded"
    project = workflow.qualification
    assert project is not None
    valid_root = tmp_path / "evidence-read-only-remote-valid"
    valid_case, valid_result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=valid_root,
        run_dir=run_dir,
    )
    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=valid_case,
            result=valid_result,
            evidence_root=valid_root,
        )
        is None
    )

    forged = report.model_copy(deep=True)
    forged_receipt = forged.results[0].delivery_receipt
    assert forged_receipt is not None
    forged.results[0].delivery_receipt = forged_receipt.model_copy(
        update={"target_fingerprint": "0" * 64}
    )
    forged_root = tmp_path / "evidence-read-only-remote-forged"
    forged_case, forged_result = _fault_case_integrity_result(
        workflow=workflow,
        report=forged,
        evidence_root=forged_root,
        run_dir=run_dir,
    )
    error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=forged_case,
        result=forged_result,
        evidence_root=forged_root,
    )
    assert error is not None
    assert "resolved target" in error[1]

    forged_identity = report.model_copy(deep=True)
    prior_identity = forged_identity.results[0].identity
    assert prior_identity is not None
    forged_identity.results[0].identity = prior_identity.model_copy(
        update={"coverage": 0.0}
    )
    forged_identity_root = tmp_path / "evidence-read-only-remote-identity-forged"
    forged_identity_case, forged_identity_result = _fault_case_integrity_result(
        workflow=workflow,
        report=forged_identity,
        evidence_root=forged_identity_root,
        run_dir=run_dir,
    )
    forged_identity_error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=forged_identity_case,
        result=forged_identity_result,
        evidence_root=forged_identity_root,
    )
    assert forged_identity_error is not None
    assert "prior action identity is not exact" in forged_identity_error[1]


@pytest.mark.parametrize(
    ("action", "operation", "mutation"),
    [
        (ActionKind.CLICK, "rdp_click", {"target_fingerprint": "0" * 64}),
        (
            ActionKind.DOUBLE_CLICK,
            "rdp_double_click",
            {"operation": "rdp_click"},
        ),
        (
            ActionKind.RIGHT_CLICK,
            "rdp_right_click",
            {"operation": "remote_click"},
        ),
        (ActionKind.DRAG, "rdp_drag", {"destination_fingerprint": "0" * 64}),
    ],
)
def test_prior_remote_pointer_receipt_binds_exact_fault_prefix(
    tmp_path: Path,
    action: ActionKind,
    operation: str,
    mutation: dict[str, str],
) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(
        tmp_path,
        driver,
        first_action=action,
        remote_first=True,
    )
    run_id = f"prior-remote-{action.value}"
    run_dir = tmp_path / f"run-{run_id}"
    report = Replayer(
        _SelectRemoteObservedBackend(structural=False),
        vision=vision_module,
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
            action_paths={"prepare": "gui", "submit": "gui"},
            required_identity_step_ids=("prepare", "submit"),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="rdp",
    )

    receipt = report.results[0].delivery_receipt
    assert receipt is not None
    assert receipt.operation == operation
    assert report.results[0].actuation == "remote_guarded"
    project = workflow.qualification
    assert project is not None
    valid_root = tmp_path / f"evidence-{run_id}-valid"
    valid_case, valid_result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=valid_root,
        run_dir=run_dir,
    )
    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=valid_case,
            result=valid_result,
            evidence_root=valid_root,
        )
        is None
    )

    forged = report.model_copy(deep=True)
    forged_receipt = forged.results[0].delivery_receipt
    assert forged_receipt is not None
    forged.results[0].delivery_receipt = forged_receipt.model_copy(update=mutation)
    forged_root = tmp_path / f"evidence-{run_id}-forged"
    forged_case, forged_result = _fault_case_integrity_result(
        workflow=workflow,
        report=forged,
        evidence_root=forged_root,
        run_dir=run_dir,
    )
    error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=forged_case,
        result=forged_result,
        evidence_root=forged_root,
    )
    assert error is not None
    assert "prior action" in error[1]


@pytest.mark.parametrize(
    ("kind", "actuation_path"),
    [
        (QualificationCaseKind.WEAK_EFFECT, "gui"),
        (QualificationCaseKind.MISSING_EFFECT, "gui"),
        (QualificationCaseKind.WEAK_EFFECT, "api"),
        (QualificationCaseKind.MISSING_EFFECT, "api"),
    ],
)
def test_exact_qualification_accepts_runtime_effect_fault_artifacts(
    tmp_path: Path,
    kind: QualificationCaseKind,
    actuation_path: Literal["gui", "api"],
) -> None:
    """Both GUI and API lanes retain valid weak and missing effect proofs."""

    driver = _FaultDriver(kind)
    workflow, bundle = _fault_workflow(
        tmp_path,
        kind,
        driver,
        api_effect_only=actuation_path == "api",
        actuation_path=actuation_path,
    )
    run_id = f"exact-{kind.value}-{actuation_path}"
    replayer = Replayer(
        _ObservedBackend(),
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            kind,
            driver,
            run_id=run_id,
            actuation_path=actuation_path,
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        api_actuator=(_UnavailableApiActuator() if actuation_path == "api" else None),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    )
    run_dir = tmp_path / f"run-{kind.value}-{actuation_path}"
    report = replayer.run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )
    project = workflow.qualification
    assert project is not None
    evidence_root = tmp_path / f"evidence-{kind.value}-{actuation_path}"
    case, result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=evidence_root,
        run_dir=run_dir,
        case_id=f"fault-{kind.value.replace('_', '-')}",
    )

    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=case,
            result=result,
            evidence_root=evidence_root,
        )
        is None
    )


def test_fault_target_requires_runtime_emittable_visual_resolution(
    tmp_path: Path,
) -> None:
    """A visual target is replayed from exact retained resolver inputs."""

    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.MISSING_EFFECT,
        driver,
        search_pad=0,
    )
    run_id = "visual-resolution-exact"
    run_dir = tmp_path / f"run-{run_id}"
    report = Replayer(
        _ObservedBackend(structural=False),
        vision=vision_module,
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )
    resolution = report.results[-1].resolution
    assert resolution is not None
    assert resolution.rung == "template_global"
    assert resolution.visual_evidence is not None
    project = workflow.qualification
    assert project is not None
    evidence_root = tmp_path / "evidence-visual-valid"
    case, result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=evidence_root,
        run_dir=run_dir,
    )

    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=case,
            result=result,
            evidence_root=evidence_root,
        )
        is None
    )

    changed = report.model_copy(deep=True)
    changed_resolution = changed.results[-1].resolution
    assert changed_resolution is not None
    changed.results[-1].resolution = changed_resolution.model_copy(
        update={"point": (1, 18), "confidence": 0.999999, "elapsed_ms": 1.0}
    )
    changed_root = tmp_path / "evidence-visual-forged"
    changed_case, changed_result = _fault_case_integrity_result(
        workflow=workflow,
        report=changed,
        evidence_root=changed_root,
        run_dir=run_dir,
    )
    error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=changed_case,
        result=changed_result,
        evidence_root=changed_root,
    )
    assert error is not None
    assert "fault refusal target resolution is not exact" in error[1]


def test_real_pixel_wrong_identity_retains_exact_fault_evidence(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.WRONG_IDENTITY)
    workflow, bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.WRONG_IDENTITY,
        driver,
    )
    identifier_path = "templates/identifiers/submit.png"
    (bundle / "templates" / "identifiers").mkdir(parents=True, exist_ok=True)
    (bundle / identifier_path).write_bytes(_pixel_identity_png())
    step = workflow.steps[0]
    assert step.anchor is not None
    step.anchor.region = (100, 14, 40, 20)
    step.anchor.click_point = (120, 24)
    step.anchor.structured_identity = None
    step.anchor.context_text = None
    step.anchor.identifier_crop = identifier_path
    step.anchor.identifier_region = (0, 0, 240, 48)
    backend = _PixelObservedBackend()
    assert workflow.qualification is not None
    pixel_observer = BackendQualificationEnvironmentObserver(backend)
    workflow.qualification.environment.environment_observer_id = (
        pixel_observer.observer_id
    )
    workflow.qualification.environment.environment_observer_contract_sha256 = (
        pixel_observer.contract_sha256
    )
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    run_id = "pixel-wrong-identity"
    run_dir = tmp_path / "run-pixel-wrong-identity"
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.WRONG_IDENTITY,
            driver,
            run_id=run_id,
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )
    target = report.results[-1]
    assert report.execution_outcome == "HALTED"
    assert target.delivery_attempted is False
    assert target.identity is not None
    assert target.identity.status == "mismatch"
    assert target.identity.mode == "pixel"
    assert target.identity.pixel_evidence is not None
    pixel = target.identity.pixel_evidence
    assert (run_dir / pixel.recorded_crop_inventory_ref).is_file()
    assert (run_dir / pixel.live_crop_inventory_ref).is_file()

    project = workflow.qualification
    assert project is not None
    valid_root = tmp_path / "evidence-pixel-wrong-identity"
    valid_case, valid_result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=valid_root,
        run_dir=run_dir,
        case_id="fault-wrong-identity",
    )
    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=valid_case,
            result=valid_result,
            evidence_root=valid_root,
        )
        is None
    )

    changed = report.model_copy(deep=True)
    changed_identity = changed.results[-1].identity
    assert changed_identity is not None and changed_identity.pixel_evidence is not None
    changed_identity.pixel_evidence = changed_identity.pixel_evidence.model_copy(
        update={
            "live_crop_sha256": pixel.recorded_crop_sha256,
            "live_crop_inventory_ref": pixel.recorded_crop_inventory_ref,
        }
    )
    changed_root = tmp_path / "evidence-pixel-forged"
    changed_case, changed_result = _fault_case_integrity_result(
        workflow=workflow,
        report=changed,
        evidence_root=changed_root,
        run_dir=run_dir,
        case_id="fault-wrong-identity",
    )
    error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=changed_case,
        result=changed_result,
        evidence_root=changed_root,
    )
    assert error is not None
    assert "pixel identity crop evidence does not reproduce" in error[1]


def test_drag_fault_target_halts_before_unreached_endpoint_resolution(
    tmp_path: Path,
) -> None:
    """A pre-delivery effect refusal does not invent a drag endpoint."""

    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.MISSING_EFFECT,
        driver,
        action=ActionKind.DRAG,
    )
    assert workflow.steps[0].action is ActionKind.DRAG
    assert workflow.steps[0].drag_end_anchor is not None
    run_id = "drag-target-refusal"
    run_dir = tmp_path / "run-drag-target-refusal"
    report = Replayer(
        _ObservedBackend(),
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )
    assert report.results[-1].resolution is not None
    assert report.results[-1].drag_end_resolution is None
    project = workflow.qualification
    assert project is not None
    evidence_root = tmp_path / "evidence-drag-target-refusal"
    case, result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=evidence_root,
        run_dir=run_dir,
    )

    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=case,
            result=result,
            evidence_root=evidence_root,
        )
        is None
    )


def test_prior_drag_requires_exact_compiled_endpoint_resolution(tmp_path: Path) -> None:
    """A successful prefix drag proves both compiled endpoints before delivery."""

    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(
        tmp_path,
        driver,
        first_action=ActionKind.DRAG,
    )
    run_id = "prior-drag-endpoint"
    run_dir = tmp_path / "run-prior-drag-endpoint"
    report = Replayer(
        _ObservedBackend(),
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
            action_paths={"prepare": "gui", "submit": "gui"},
            required_identity_step_ids=("prepare", "submit"),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )
    assert report.results[0].drag_end_resolution is not None
    project = workflow.qualification
    assert project is not None
    valid_root = tmp_path / "evidence-prior-drag-valid"
    valid_case, valid_result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=valid_root,
        run_dir=run_dir,
    )
    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=valid_case,
            result=valid_result,
            evidence_root=valid_root,
        )
        is None
    )

    changed = report.model_copy(deep=True)
    endpoint = changed.results[0].drag_end_resolution
    assert endpoint is not None and endpoint.structural_handle is not None
    changed_handle = endpoint.structural_handle.model_copy(update={"point": (12, 12)})
    changed.results[0].drag_end_resolution = endpoint.model_copy(
        update={"point": (12, 12), "structural_handle": changed_handle}
    )
    changed_root = tmp_path / "evidence-prior-drag-changed"
    changed_case, changed_result = _fault_case_integrity_result(
        workflow=workflow,
        report=changed,
        evidence_root=changed_root,
        run_dir=run_dir,
    )
    error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=changed_case,
        result=changed_result,
        evidence_root=changed_root,
    )
    assert error is not None
    assert "prior action" in error[1]
    assert "resolved drag destination" in error[1]

    changed_fingerprint = report.model_copy(deep=True)
    endpoint = changed_fingerprint.results[0].drag_end_resolution
    assert endpoint is not None and endpoint.structural_handle is not None
    changed_fingerprint.results[0].drag_end_resolution = endpoint.model_copy(
        update={
            "structural_handle": endpoint.structural_handle.model_copy(
                update={"target_fingerprint": "e" * 64}
            )
        }
    )
    fingerprint_root = tmp_path / "evidence-prior-drag-fingerprint"
    fingerprint_case, fingerprint_result = _fault_case_integrity_result(
        workflow=workflow,
        report=changed_fingerprint,
        evidence_root=fingerprint_root,
        run_dir=run_dir,
    )
    fingerprint_error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=fingerprint_case,
        result=fingerprint_result,
        evidence_root=fingerprint_root,
    )
    assert fingerprint_error is not None
    assert "resolved drag destination" in fingerprint_error[1]


def test_prior_right_click_accepts_only_the_runtime_guarded_receipt(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(
        tmp_path,
        driver,
        first_action=ActionKind.RIGHT_CLICK,
    )
    run_id = "prior-guarded-right-click"
    run_dir = tmp_path / "run-prior-guarded-right-click"
    report = Replayer(
        _ObservedBackend(),
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
            action_paths={"prepare": "gui", "submit": "gui"},
            required_identity_step_ids=("prepare", "submit"),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )
    receipt = report.results[0].delivery_receipt
    assert receipt is not None
    assert receipt.operation == "guarded_coordinate_right_click"
    project = workflow.qualification
    assert project is not None
    valid_root = tmp_path / "evidence-right-click-valid"
    valid_case, valid_result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=valid_root,
        run_dir=run_dir,
    )
    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=valid_case,
            result=valid_result,
            evidence_root=valid_root,
        )
        is None
    )

    changed = report.model_copy(deep=True)
    changed.results[0].delivery_receipt = receipt.model_copy(
        update={"operation": "coordinate_right_click"}
    )
    changed_root = tmp_path / "evidence-right-click-forged"
    changed_case, changed_result = _fault_case_integrity_result(
        workflow=workflow,
        report=changed,
        evidence_root=changed_root,
        run_dir=run_dir,
    )
    error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=changed_case,
        result=changed_result,
        evidence_root=changed_root,
    )
    assert error is not None
    assert "delivery receipt operation conflicts" in error[1]


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("operation", "guarded_dom_type"),
        ("selection_commit_key", "Tab"),
        ("selection_value_sha256", "0" * 64),
    ],
)
def test_prior_selection_receipt_binds_value_commit_and_target(
    tmp_path: Path,
    mutation: str,
    value: str,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(
        tmp_path,
        driver,
        first_action=ActionKind.SELECT_OPTION,
    )
    run_id = f"prior-select-{mutation}"
    run_dir = tmp_path / f"run-{run_id}"
    report = Replayer(
        _SelectRemoteObservedBackend(structural=False),
        vision=vision_module,
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
            action_paths={"prepare": "gui", "submit": "gui"},
            required_identity_step_ids=("prepare", "submit"),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="rdp",
    )
    receipt = report.results[0].delivery_receipt
    assert receipt is not None
    assert receipt.operation == "rdp_select_option"
    assert report.results[0].actuation == "remote_guarded"
    project = workflow.qualification
    assert project is not None
    valid_root = tmp_path / f"evidence-select-valid-{mutation}"
    valid_case, valid_result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=valid_root,
        run_dir=run_dir,
    )
    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=project,
            case=valid_case,
            result=valid_result,
            evidence_root=valid_root,
        )
        is None
    )

    changed = report.model_copy(deep=True)
    changed.results[0].delivery_receipt = receipt.model_copy(update={mutation: value})
    changed_root = tmp_path / f"evidence-select-forged-{mutation}"
    changed_case, changed_result = _fault_case_integrity_result(
        workflow=workflow,
        report=changed,
        evidence_root=changed_root,
        run_dir=run_dir,
    )
    error = _case_run_report_integrity_error(
        workflow=workflow,
        project=project,
        case=changed_case,
        result=changed_result,
        evidence_root=changed_root,
    )
    assert error is not None
    assert "prior action" in error[1]


def test_authorization_rejects_fault_target_removed_from_permitted_scope(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(tmp_path, driver)
    assert workflow.qualification is not None
    case = next(
        item
        for item in workflow.qualification.cases
        if item.kind is QualificationCaseKind.MISSING_EFFECT
    )
    case.action_targets = [
        QualificationActionTarget(step_id="prepare", actuation_path="gui")
    ]
    workflow.save(bundle)
    authorization = _fault_authorization(
        workflow,
        QualificationCaseKind.MISSING_EFFECT,
        driver,
        run_id="fault-target-outside-scope",
        action_paths={"prepare": "gui"},
        required_identity_step_ids=("prepare", "submit"),
        fault_step_id="submit",
    )

    assert authorization.validate_workflow(workflow) == (
        "qualification fault target is outside its permitted action scope"
    )


@pytest.mark.parametrize(
    ("kind", "expected_stage", "expected_code"),
    [
        (QualificationCaseKind.AMBIGUITY, "target_resolution", "target_ambiguous"),
        (
            QualificationCaseKind.WRONG_IDENTITY,
            "identity_verification",
            "identity_conflict",
        ),
        (
            QualificationCaseKind.STALE_IDENTITY,
            "actuation_revalidation",
            "actuation_observation_changed",
        ),
        (
            QualificationCaseKind.WEAK_EFFECT,
            "effect_strength",
            "effect_strength_insufficient",
        ),
        (
            QualificationCaseKind.MISSING_EFFECT,
            "effect_verifier",
            "effect_verifier_missing",
        ),
    ],
)
def test_all_fault_cases_mutate_real_detector_input_and_halt_before_delivery(
    tmp_path: Path,
    kind: QualificationCaseKind,
    expected_stage: str,
    expected_code: str,
) -> None:
    driver = _FaultDriver(kind)
    report, backend = _run_fault(tmp_path, kind, driver)

    assert report.execution_outcome == "HALTED"
    assert len(report.qualification_fault_mutations) == 1
    receipt = report.qualification_fault_mutations[0]
    assert report.governed_qualification_project_id is not None
    assert report.governed_qualification_project_revision == receipt.project_revision
    assert report.governed_qualification_project_contract_sha256 is not None
    assert report.governed_qualification_fault_driver_key_id == driver.key_id
    assert report.governed_qualification_fault_step_id_sha256 == sha256_bytes(b"submit")
    result = report.results[0]
    assert result.delivery_attempted is False
    assert result.safety_refusal_evidence is not None
    assert result.safety_refusal_evidence.stage == expected_stage
    assert result.safety_refusal_evidence.code == expected_code
    assert (
        fault_detector_contract_error(report, report.qualification_fault_mutations[0])
        is None
    )
    assert backend.actions == []


@pytest.mark.parametrize(
    ("stale_identity_mismatch", "expected_resolution", "expected_identity_status"),
    [
        (False, False, "verified"),
        (True, True, "mismatch"),
    ],
)
def test_stale_identity_integrity_accepts_both_runtime_emittable_shapes(
    tmp_path: Path,
    stale_identity_mismatch: bool,
    expected_resolution: bool,
    expected_identity_status: str,
) -> None:
    """Stale input can refuse at re-resolution or after exact identity proof."""

    driver = _FaultDriver(
        QualificationCaseKind.STALE_IDENTITY,
        stale_identity_mismatch=stale_identity_mismatch,
    )
    workflow, bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.STALE_IDENTITY,
        driver,
    )
    run_id = f"stale-identity-shape-{stale_identity_mismatch}"
    run_dir = tmp_path / f"run-stale-identity-shape-{stale_identity_mismatch}"
    backend = _ObservedBackend()
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.STALE_IDENTITY,
            driver,
            run_id=run_id,
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run_dir,
        run_id=run_id,
        execution_target_kind="web",
    )

    assert report.execution_outcome == "HALTED"
    assert len(report.results) == 1
    target = report.results[0]
    assert (target.resolution is not None) is expected_resolution
    assert target.identity is not None
    assert target.identity.status == expected_identity_status
    assert target.delivery_attempted is False
    assert backend.actions == []

    evidence_root = tmp_path / f"evidence-stale-{stale_identity_mismatch}"
    case, result = _fault_case_integrity_result(
        workflow=workflow,
        report=report,
        evidence_root=evidence_root,
        run_dir=run_dir,
        case_id="fault-stale-identity",
    )
    assert workflow.qualification is not None
    assert (
        _case_run_report_integrity_error(
            workflow=workflow,
            project=workflow.qualification,
            case=case,
            result=result,
            evidence_root=evidence_root,
        )
        is None
    )

    forged_effect = report.model_copy(deep=True)
    forged_effect.results[0] = target.model_copy(
        update={"effect_contract_hashes": ["sha256:" + "f" * 64]}
    )
    forged_effect_root = tmp_path / f"evidence-stale-effect-{stale_identity_mismatch}"
    forged_effect_case, forged_effect_result = _fault_case_integrity_result(
        workflow=workflow,
        report=forged_effect,
        evidence_root=forged_effect_root,
        run_dir=run_dir,
        case_id="fault-stale-identity",
    )
    forged_effect_error = _case_run_report_integrity_error(
        workflow=workflow,
        project=workflow.qualification,
        case=forged_effect_case,
        result=forged_effect_result,
        evidence_root=forged_effect_root,
    )
    assert forged_effect_error is not None
    assert "effect contracts" in forged_effect_error[1]

    if target.resolution is not None:
        forged_resolution = report.model_copy(deep=True)
        forged_resolution.results[0] = target.model_copy(
            update={
                "resolution": target.resolution.model_copy(update={"point": (9, 8)})
            }
        )
        forged_resolution_root = tmp_path / "evidence-stale-resolution"
        forged_resolution_case, forged_resolution_result = _fault_case_integrity_result(
            workflow=workflow,
            report=forged_resolution,
            evidence_root=forged_resolution_root,
            run_dir=run_dir,
            case_id="fault-stale-identity",
        )
        forged_resolution_error = _case_run_report_integrity_error(
            workflow=workflow,
            project=workflow.qualification,
            case=forged_resolution_case,
            result=forged_resolution_result,
            evidence_root=forged_resolution_root,
        )
        assert forged_resolution_error is not None
        assert "resolution" in forged_resolution_error[1]

        impossible_cross = report.model_copy(deep=True)
        impossible_cross.results[0] = target.model_copy(update={"resolution": None})
        impossible_cross_root = tmp_path / "evidence-stale-impossible-cross"
        impossible_cross_case, impossible_cross_result = _fault_case_integrity_result(
            workflow=workflow,
            report=impossible_cross,
            evidence_root=impossible_cross_root,
            run_dir=run_dir,
            case_id="fault-stale-identity",
        )
        impossible_cross_error = _case_run_report_integrity_error(
            workflow=workflow,
            project=workflow.qualification,
            case=impossible_cross_case,
            result=impossible_cross_result,
            evidence_root=impossible_cross_root,
        )
        assert impossible_cross_error is not None
        assert "required to observe" in impossible_cross_error[1]


@pytest.mark.parametrize("delivery_attempted", [None, True])
def test_detector_receipt_rejects_unknown_or_attempted_delivery(
    delivery_attempted: bool | None,
) -> None:
    receipt = FaultMutationReceipt(
        project_id="project",
        project_revision=1,
        project_contract_sha256="a" * 64,
        campaign_id_sha256="b" * 64,
        case_id_sha256="c" * 64,
        case_input_sha256="d" * 64,
        run_id_sha256="e" * 64,
        step_id_sha256=sha256_bytes(b"submit"),
        actuation_path="gui",
        fault_kind="ambiguity",
        gate="target_resolution",
        driver_id="driver",
        driver_contract_sha256="f" * 64,
        before_input_sha256="1" * 64,
        after_input_sha256="2" * 64,
        mutation_artifact_sha256="3" * 64,
        attestation_key_id="key",
    )
    result = StepResult(
        step_id="submit",
        intent="Submit",
        ok=False,
        safety_halt=True,
        delivery_attempted=delivery_attempted,
        safety_refusal_evidence=SafetyRefusalEvidence(
            stage="target_resolution",
            code="target_ambiguous",
            detector_input_sha256="2" * 64,
        ),
    )
    report = SimpleNamespace(
        execution_outcome="HALTED",
        results=[result],
        governed_qualification_case_action_paths={"submit": "gui"},
    )

    assert (
        fault_detector_contract_error(report, receipt)
        == "fault_detector_delivery_boundary_crossed"
    )


def test_detector_receipt_rejects_any_result_after_the_fault_refusal() -> None:
    receipt = FaultMutationReceipt(
        project_id="project",
        project_revision=1,
        project_contract_sha256="a" * 64,
        campaign_id_sha256="b" * 64,
        case_id_sha256="c" * 64,
        case_input_sha256="d" * 64,
        run_id_sha256="e" * 64,
        step_id_sha256=sha256_bytes(b"submit"),
        actuation_path="gui",
        fault_kind="ambiguity",
        gate="target_resolution",
        driver_id="driver",
        driver_contract_sha256="f" * 64,
        before_input_sha256="1" * 64,
        after_input_sha256="2" * 64,
        mutation_artifact_sha256="3" * 64,
        attestation_key_id="key",
    )
    refusal = StepResult(
        step_id="submit",
        intent="Submit",
        ok=False,
        safety_halt=True,
        failure_category="safety_halt",
        delivery_attempted=False,
        starting_state_settled=True,
        error="two submit controls match",
        safety_refusal_evidence=SafetyRefusalEvidence(
            stage="target_resolution",
            code="target_ambiguous",
            detector_input_sha256="2" * 64,
        ),
    )
    later_action = StepResult(
        step_id="later-write",
        intent="Later write",
        ok=True,
        delivery_attempted=True,
        effect_verified=True,
        effect_results=["later effect was reported as verified"],
    )
    report = SimpleNamespace(
        execution_outcome="HALTED",
        results=[refusal, later_action],
        governed_qualification_case_action_paths={"submit": "gui"},
    )

    assert (
        fault_detector_contract_error(report, receipt)
        == "fault_detector_refusal_not_terminal"
    )

    program_terminal = StepResult(
        step_id="<terminal>",
        intent="program halt",
        ok=False,
        safety_halt=True,
        error=refusal.error,
    )
    program_report = SimpleNamespace(
        execution_outcome="HALTED",
        terminal_outcome="halt",
        results=[refusal, program_terminal],
        governed_qualification_case_action_paths={"submit": "gui"},
    )
    assert fault_detector_contract_error(program_report, receipt) is None

    program_report.results = [refusal]
    assert (
        fault_detector_contract_error(program_report, receipt)
        == "fault_detector_terminal_shape_invalid"
    )

    for terminal_outcome, terminal_update in (
        (
            "escalate",
            {"intent": "program escalate", "error": "operator review requested"},
        ),
        ("halt", {"error": "an unrelated terminal error"}),
    ):
        program_report.terminal_outcome = terminal_outcome
        program_report.results = [
            refusal,
            program_terminal.model_copy(update=terminal_update),
        ]
        assert (
            fault_detector_contract_error(program_report, receipt)
            == "fault_detector_terminal_shape_invalid"
        )

    program_report.terminal_outcome = "halt"

    for update in (
        {"delivery_attempted": True},
        {
            "delivery_receipt": ActionDeliveryReceipt(
                receipt_id="terminal-forgery",
                operation="click",
                native=False,
                delivered_at="2026-07-28T00:00:00Z",
            )
        },
    ):
        forged_terminal = program_terminal.model_copy(update=update)
        program_report.results = [refusal, forged_terminal]
        assert (
            fault_detector_contract_error(program_report, receipt)
            == "fault_detector_terminal_shape_invalid"
        )


def test_detector_receipt_rejects_success_fields_on_the_refusal(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY)
    report, _backend = _run_fault(
        tmp_path,
        QualificationCaseKind.AMBIGUITY,
        driver,
    )
    receipt = report.qualification_fault_mutations[0]

    for update in (
        {"ok": True},
        {
            "effect_verified": True,
            "effect_contract_hashes": ["sha256:" + "1" * 64],
            "effect_results": ["a forged confirmed effect"],
            "effect_evidence": [
                EffectVerificationEvidence(
                    effect_contract_hash="sha256:" + "1" * 64,
                    substrate="forged-system-of-record",
                    verification_tier=1,
                    initial_verdict="confirmed",
                    final_verdict="confirmed",
                    observed_effect="present",
                )
            ],
        },
    ):
        forged = report.model_copy(deep=True)
        forged.results[0] = forged.results[0].model_copy(update=update)
        assert (
            fault_detector_contract_error(forged, receipt)
            == "fault_detector_refusal_shape_invalid"
        )


@pytest.mark.parametrize(
    "kind",
    [QualificationCaseKind.WEAK_EFFECT, QualificationCaseKind.MISSING_EFFECT],
)
def test_detector_receipt_accepts_runtime_api_effect_refusal_shape(
    tmp_path: Path,
    kind: QualificationCaseKind,
) -> None:
    driver = _FaultDriver(kind)
    report, _backend = _run_fault(
        tmp_path,
        kind,
        driver,
        api_effect_only=True,
        api_actuator=_UnavailableApiActuator(),
    )

    assert (
        fault_detector_contract_error(
            report,
            report.qualification_fault_mutations[0],
        )
        is None
    )


def test_fault_receipt_signature_rejects_wrong_key_and_tampering() -> None:
    key = Ed25519PrivateKey.generate()
    private_key = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_key = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    receipt = FaultMutationReceipt(
        project_id="project",
        project_revision=1,
        project_contract_sha256="a" * 64,
        campaign_id_sha256="b" * 64,
        case_id_sha256="c" * 64,
        case_input_sha256="d" * 64,
        run_id_sha256="e" * 64,
        step_id_sha256="f" * 64,
        actuation_path="gui",
        fault_kind="ambiguity",
        gate="target_resolution",
        driver_id="driver",
        driver_contract_sha256="1" * 64,
        before_input_sha256="2" * 64,
        after_input_sha256="3" * 64,
        mutation_artifact_sha256="4" * 64,
        attestation_key_id="key",
    )
    signed = sign_fault_mutation_receipt(receipt, private_key=private_key)
    wrong_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )

    assert verify_fault_mutation_receipt(signed, trusted_public_key_base64=public_key)
    assert not verify_fault_mutation_receipt(
        signed,
        trusted_public_key_base64=base64.b64encode(wrong_key).decode("ascii"),
    )
    assert not verify_fault_mutation_receipt(
        signed.model_copy(update={"case_input_sha256": "5" * 64}),
        trusted_public_key_base64=public_key,
    )


def test_qualification_authorization_rejects_caller_supplied_case_input_digest(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY)
    workflow, _bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.AMBIGUITY,
        driver,
    )
    authorization = _fault_authorization(
        workflow,
        QualificationCaseKind.AMBIGUITY,
        driver,
        run_id="exact-input-run",
    )

    with pytest.raises(ValueError, match="exact governed runtime-input digest"):
        GovernedRunAuthorization.model_validate(
            {
                **authorization.model_dump(mode="json"),
                "qualification_case_input_sha256": "0" * 64,
            }
        )


@pytest.mark.parametrize("field", ["id", "contract", "key"])
def test_driver_binding_change_is_rejected_before_mutation(
    tmp_path: Path, field: str
) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY, change_binding=field)
    report, backend = _run_fault(tmp_path, QualificationCaseKind.AMBIGUITY, driver)

    assert report.execution_outcome in {"FAILED", "HALTED"}
    assert report.qualification_fault_mutations == []
    assert backend.actions == []
    assert "qualification_fault_driver_binding_changed" in (
        report.results[0].error or ""
    )


def test_declined_fault_cannot_cross_the_bound_input_edge(tmp_path: Path) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY, decline=True)
    report, backend = _run_fault(tmp_path, QualificationCaseKind.AMBIGUITY, driver)

    assert report.execution_outcome == "HALTED"
    assert report.qualification_fault_mutations == []
    assert backend.actions == []
    assert "did not produce its required detector refusal" in (
        report.results[0].error or ""
    )


def test_api_fault_mutation_cannot_fall_through_to_gui_when_api_is_unavailable(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(
        QualificationCaseKind.WEAK_EFFECT,
        non_refusing_effect_mutation=True,
    )
    actuator = _UnavailableApiActuator()

    report, backend = _run_fault(
        tmp_path,
        QualificationCaseKind.WEAK_EFFECT,
        driver,
        api_effect_only=True,
        api_actuator=actuator,
    )

    assert report.execution_outcome == "HALTED"
    assert actuator.calls == 1
    assert len(report.qualification_fault_mutations) == 1
    assert backend.actions == []
    assert report.results[0].delivery_attempted is False
    assert report.results[0].safety_refusal_evidence is None
    assert "requires the API actuation path" in (report.results[0].error or "")
    assert (
        fault_detector_contract_error(
            report,
            report.qualification_fault_mutations[0],
        )
        == "fault_detector_refusal_not_observed"
    )


def test_gui_qualification_path_bypasses_a_configured_api_tier(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY)
    workflow, bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.AMBIGUITY,
        driver,
        actuation_path="gui",
    )
    workflow.steps[0].api_binding = ApiBinding(
        method="POST",
        url_template="/synthetic-submit",
        effects=list(workflow.steps[0].effects),
    )
    set_effect_policy(
        workflow,
        step_id="submit",
        effect_index=0,
        tier=VerificationTier.INDEPENDENT_SYSTEM,
        actuation_path="api",
    )
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    run_id = "gui-path-bypasses-api"
    actuator = _UnavailableApiActuator()
    backend = _ObservedBackend()

    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.AMBIGUITY,
            driver,
            run_id=run_id,
            actuation_path="gui",
        ),
        qualification_fault_driver=driver,
        api_actuator=actuator,
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "run-gui-bypasses-api",
        run_id=run_id,
        execution_target_kind="web",
    )

    assert actuator.calls == 0
    assert report.execution_outcome == "HALTED"
    assert len(report.qualification_fault_mutations) == 1
    assert report.qualification_fault_mutations[0].actuation_path == "gui"


def test_qualification_run_halts_before_an_undeclared_write(tmp_path: Path) -> None:
    driver = _FaultDriver(QualificationCaseKind.WEAK_EFFECT)
    workflow, bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.WEAK_EFFECT,
        driver,
    )
    workflow.steps.append(Step(id="inspect", intent="Inspect", action=ActionKind.WAIT))
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="inspect",
            classification=ActionRiskClass.READ_ONLY,
            explanation="The inspection step does not change business state",
            operator_confirmed=True,
        ),
    )
    project = workflow.qualification
    assert project is not None
    representative = next(
        case
        for case in project.cases
        if case.kind is QualificationCaseKind.REPRESENTATIVE
    )
    representative.action_targets = [
        QualificationActionTarget(step_id="inspect", actuation_path="gui")
    ]
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None and workflow.qualification is not None
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    run_id = "undeclared-write"
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=input_sha256,
        admitted_policy_name="clinical-write",
        admitted_policy_contract_sha256="e" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        qualified_effect_requirements=qualified_effect_requirements(
            workflow, ExecutionProfile.STANDARD
        ),
        required_identity_step_ids=("submit",),
        approval_source="qualification-campaign",
        qualification_project_id=workflow.qualification.project_id,
        qualification_project_revision=workflow.qualification.revision,
        qualification_project_contract_sha256=(
            workflow.qualification.contract_sha256()
        ),
        qualification_case_id=representative.id,
        qualification_campaign_id_sha256=sha256_bytes(b"campaign"),
        qualification_case_input_sha256=input_sha256,
        qualification_run_id_sha256=sha256_bytes(run_id.encode()),
        qualification_case_kind="representative",
        qualification_case_action_paths={"inspect": "gui"},
    )
    backend = _ObservedBackend()

    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=authorization,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "run-undeclared-write",
        run_id=run_id,
        execution_target_kind="web",
    )

    assert report.execution_outcome == "HALTED"
    assert backend.actions == []
    assert "outside its exact authorized actuation-path map" in (
        report.results[0].error or ""
    )


class _ExternalObserver:
    def __init__(
        self,
        *,
        target_kind: str,
        change_field: str | None = None,
    ) -> None:
        self.target_kind = target_kind
        self.change_field = change_field
        self.calls = 0
        self.observer_id_reads = 0
        self.contract_reads = 0
        self._shared_observation: QualificationEnvironmentObservation | None = None

    @property
    def observer_id(self) -> str:
        self.observer_id_reads += 1
        if self.change_field == "observer_id" and self.observer_id_reads > 4:
            return "test.replacement-environment"
        return "test.external-environment"

    @property
    def contract_sha256(self) -> str:
        self.contract_reads += 1
        if self.change_field == "observer_contract" and self.contract_reads > 4:
            return "8" * 64
        return "9" * 64

    def observe(self, _backend: Any, _target_kind: str):
        self.calls += 1
        values = {
            "target_kind": self.target_kind,
            "application_identity": "third-party-app",
            "application_version": "2026.7",
            "session_identity_sha256": "6" * 64,
            "environment_digest": "7" * 64,
        }
        if self.change_field == "reused_object":
            if self._shared_observation is None:
                self._shared_observation = (
                    QualificationEnvironmentObservation.model_validate(values)
                )
            elif self.calls > 1:
                self._shared_observation.environment_digest = "8" * 64
            return self._shared_observation
        if self.calls > 1 and self.change_field is not None:
            if self.change_field == "application_identity":
                values[self.change_field] = "replacement-app"
            elif self.change_field == "application_version":
                values[self.change_field] = "2026.8"
            else:
                values[self.change_field] = "8" * 64
        return QualificationEnvironmentObservation.model_validate(values)


@pytest.mark.parametrize("surface", ["web", "windows", "macos", "linux"])
@pytest.mark.parametrize(
    "change_field",
    [
        "application_identity",
        "application_version",
        "session_identity_sha256",
        "environment_digest",
        "observer_id",
        "observer_contract",
        "reused_object",
    ],
)
def test_external_observer_rechecks_every_environment_signal_before_input(
    tmp_path: Path,
    surface: str,
    change_field: str,
) -> None:
    backend = _ObservedBackend()
    observer = _ExternalObserver(target_kind=surface, change_field=change_field)
    bundle = tmp_path / f"environment-{surface}-{change_field}"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "button.png").write_bytes(_screen_png())
    workflow = Workflow(
        name="environment-bound",
        surface="web",
        steps=[
            Step(
                id="submit",
                intent="Submit",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/button.png",
                    structural=StructuralLocator(selector="#submit"),
                    region=(5, 5, 6, 6),
                    click_point=(8, 8),
                    structured_identity="Synthetic record",
                ),
                identity_armed=True,
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"record_id": ValueExpr(literal="submit")},
                        risk="irreversible",
                    )
                ],
            )
        ],
    )
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind=surface,
            application="Third-party application",
            application_identity="third-party-app",
            application_version="2026.7",
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=observer.contract_sha256,
            environment_digest="7" * 64,
            runtime_version="test",
        ),
    )
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="submit",
            classification=ActionRiskClass.IRREVERSIBLE,
            explanation="qualification environment target changes business state",
            operator_confirmed=True,
        ),
    )
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id="submit",
            enforcement=IdentityEnforcement.CANONICAL_LADDER,
        ),
    )
    set_effect_policy(
        workflow,
        step_id="submit",
        effect_index=0,
        tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            runtime_input_sha256=input_sha256,
            action_targets=[
                QualificationActionTarget(step_id="submit", actuation_path="gui")
            ],
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None and workflow.qualification is not None
    run_id = f"environment-{surface}-{change_field}"
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=input_sha256,
        admitted_policy_name="clinical-write",
        admitted_policy_contract_sha256="5" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        qualified_effect_requirements=qualified_effect_requirements(
            workflow, ExecutionProfile.STANDARD
        ),
        required_identity_step_ids=("submit",),
        approval_source="qualification-campaign",
        qualification_project_id=workflow.qualification.project_id,
        qualification_project_revision=workflow.qualification.revision,
        qualification_project_contract_sha256=(
            workflow.qualification.contract_sha256()
        ),
        qualification_case_id="representative-1",
        qualification_campaign_id_sha256="1" * 64,
        qualification_case_input_sha256=input_sha256,
        qualification_run_id_sha256=sha256_bytes(run_id.encode("utf-8")),
        qualification_case_kind="representative",
        qualification_case_action_paths={"submit": "gui"},
    )
    report = Replayer(
        backend,
        vision=_Vision(),
        effect_verifier=_StrongEffectVerifier(),
        governed_authorization=authorization,
        qualification_environment_observer=observer,
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / f"environment-run-{surface}-{change_field}",
        run_id=run_id,
        execution_target_kind=surface,
    )

    assert report.execution_outcome in {"FAILED", "HALTED"}
    assert backend.actions == []
    assert report.observed_environment_digest == "7" * 64
    assert report.observed_environment_binding_sha256 is not None
    expected_refusal = (
        "qualification environment observer binding changed before input"
        if change_field in {"observer_id", "observer_contract"}
        else "qualification environment changed before input"
    )
    assert expected_refusal in (report.results[0].error or "")
    assert backend._input_guard is None


def test_playwright_external_observer_does_not_require_target_owned_meta_tags() -> None:
    class _Page:
        url = "https://third-party.example/private/path"

    backend = PlaywrightBackend(_Page())  # type: ignore[arg-type]
    observer = _ExternalObserver(target_kind="web")

    observed = observer.observe(backend, "web")

    assert observed.application_identity == "third-party-app"
    assert observed.environment_digest == "7" * 64


def test_real_deployment_constructor_wires_backend_environment_observer() -> None:
    backend = _ObservedBackend()
    replayer = build_replayer(
        backend,
        allow_egress=False,
        effect_verifier=None,
        api_actuator=None,
        durable=True,
        use_structural=False,
        governed_authorization=SimpleNamespace(
            qualification_case_id="case-1",
            execution_profile=None,
        ),
    )

    assert isinstance(
        replayer.qualification_environment_observer,
        BackendQualificationEnvironmentObserver,
    )
