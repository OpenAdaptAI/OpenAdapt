"""Run-bound authorization for governed deployment execution.

The permissive ``replay`` path does not use this object.  ``run`` creates one
only after every admission gate passes, then hands it to the shared replayer.
Binding the decision to the sealed bundle and exact effect contracts prevents
an approval intended for one workflow from becoming a reusable bypass.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, TypeAlias, Union, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.ir import Interstitial, QualifiedEffectRequirement, Step, Workflow
from openadapt_flow.qualification_admission import (
    QualificationAdmissionEnvelope,
    contract_sha256,
)
from openadapt_flow.traversal import iter_workflow_steps

_CONSUMED_IDS: set[str] = set()
_CONSUMED_LOCK = threading.Lock()
_QUALIFICATION_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

RuntimeParamScalar: TypeAlias = Union[str, bool, int, float]
_JS_SAFE_INTEGER = 9_007_199_254_740_991


def is_runtime_param_scalar(value: object) -> bool:
    """Return whether ``value`` is one exact supported finite JSON scalar."""

    if type(value) in {str, bool}:
        return True
    if type(value) is int:
        if -_JS_SAFE_INTEGER <= value <= _JS_SAFE_INTEGER:
            return True
        # JSON has one Number type. Admit a large integer spelling only when
        # the JavaScript renderer of its finite IEEE-754 value returns that
        # same spelling. The normalizer converts it to that Number before any
        # retained artifact.
        try:
            number = float(value)
        except OverflowError:
            return False
        return math.isfinite(number) and _javascript_number_text(number) == str(value)
    return type(value) is float and math.isfinite(value)


def normalize_runtime_param_scalar(value: object) -> RuntimeParamScalar:
    """Return one exact finite JSON scalar in its cross-language representation."""

    if not is_runtime_param_scalar(value):
        raise ValueError("runtime parameter is not a finite interoperable JSON scalar")
    if type(value) is int and not -_JS_SAFE_INTEGER <= value <= _JS_SAFE_INTEGER:
        return float(value)
    return cast(RuntimeParamScalar, value)


def _javascript_number_text(value: int | float) -> str:
    """Return the finite number text used by JavaScript ``JSON.stringify``.

    The hosted service uses ``JSON.stringify`` for primitive values in its
    sorted-key canonical JSON. Python and JavaScript choose different exponent
    formatting thresholds, so ordinary ``json.dumps`` is not interoperable.
    An out-of-safe-range Python integer reaches this function only when its
    spelling is the canonical JavaScript rendering of the normalized Number.
    """

    if type(value) is int:
        if not is_runtime_param_scalar(value):
            raise ValueError("integer parameter exceeds the JSON safe-integer range")
        if not -_JS_SAFE_INTEGER <= value <= _JS_SAFE_INTEGER:
            return _javascript_number_text(float(value))
        return str(value)
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("number parameter must be finite")
    if value == 0:
        return "0"

    negative = value < 0
    source = repr(abs(value)).lower()
    mantissa, separator, exponent_text = source.partition("e")
    exponent = int(exponent_text) if separator else 0
    integer, dot, fraction = mantissa.partition(".")
    digits = integer + (fraction if dot else "")
    decimal_exponent = exponent - len(fraction)
    while len(digits) > 1 and digits.endswith("0"):
        digits = digits[:-1]
        decimal_exponent += 1
    decimal_point = len(digits) + decimal_exponent

    absolute = abs(value)
    if 1e-6 <= absolute < 1e21:
        if decimal_point <= 0:
            rendered = "0." + ("0" * -decimal_point) + digits
        elif decimal_point >= len(digits):
            rendered = digits + ("0" * (decimal_point - len(digits)))
        else:
            rendered = digits[:decimal_point] + "." + digits[decimal_point:]
    else:
        scientific_exponent = decimal_point - 1
        rendered = digits[0]
        if len(digits) > 1:
            rendered += "." + digits[1:]
        rendered += "e" + ("+" if scientific_exponent >= 0 else "")
        rendered += str(scientific_exponent)
    return ("-" if negative else "") + rendered


def _hosted_canonical_json(value: object) -> str:
    """Match the hosted sorted-key ``canonicalJson`` implementation exactly."""

    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in {int, float}:
        return _javascript_number_text(cast(Union[int, float], value))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_hosted_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be strings")
        # JavaScript Array.sort compares UTF-16 code units. Python's default
        # string order compares Unicode code points, which differs for astral
        # characters versus BMP characters at U+D800 and above.
        ordered_keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        members = (
            json.dumps(key, ensure_ascii=False)
            + ":"
            + _hosted_canonical_json(value[key])
            for key in ordered_keys
        )
        return "{" + ",".join(members) + "}"
    raise ValueError(f"unsupported canonical JSON value: {type(value).__name__}")


def runtime_param_text(value: RuntimeParamScalar) -> str:
    """Render one admitted scalar only at the final GUI text boundary."""

    value = normalize_runtime_param_scalar(value)
    if isinstance(value, str):
        return value
    if type(value) is bool:
        return "true" if value else "false"
    return _javascript_number_text(value)


def runtime_params_for_gui(
    params: Mapping[str, RuntimeParamScalar],
) -> dict[str, str]:
    """Convert the already-authorized typed parameter set to GUI text."""

    return {name: runtime_param_text(value) for name, value in params.items()}


def effective_runtime_params(
    workflow: Workflow, supplied: Mapping[str, RuntimeParamScalar] | None
) -> dict[str, RuntimeParamScalar]:
    """Resolve defaults exactly as :meth:`Replayer.run` does."""
    merged: dict[str, RuntimeParamScalar] = dict(workflow.params)
    for name, spec in workflow.param_specs.items():
        if spec.example is not None:
            merged.setdefault(name, spec.example)
    merged.update(supplied or {})
    try:
        return {
            name: normalize_runtime_param_scalar(value)
            for name, value in merged.items()
        }
    except ValueError as exc:
        invalid = [
            name for name, value in merged.items() if not is_runtime_param_scalar(value)
        ]
        raise ValueError(
            "runtime parameters must be strings, Booleans, or finite JSON-safe "
            "numbers: " + ", ".join(sorted(invalid))
        ) from exc


def runtime_inputs_bytes(
    workflow: Workflow,
    params: Mapping[str, RuntimeParamScalar] | None,
    worklists: dict[str, list[dict[str, str]]] | None,
    *,
    interstitials: list[Interstitial] | None = None,
) -> bytes:
    """Return the canonical exact bytes that a governed run authorizes."""

    payload: dict[str, object] = {
        "params": effective_runtime_params(workflow, params),
        "worklists": worklists or {},
    }
    if interstitials:
        # Runtime-supplied interstitials can issue pre-step key/click actions.
        # Bind their complete declarative shape into governed authorization so
        # a caller cannot add or change one after admission. Keep the empty case
        # byte-compatible with authorizations created before this input existed.
        payload["interstitials"] = [
            interstitial.model_dump(mode="json") for interstitial in interstitials
        ]
    canonical = _hosted_canonical_json(payload)
    return canonical.encode("utf-8")


def parse_runtime_inputs_bytes(
    value: bytes,
    *,
    workflow: Workflow,
) -> tuple[dict[str, RuntimeParamScalar], dict[str, list[dict[str, str]]]]:
    """Parse bytes that this workflow's runtime serializer can emit exactly."""

    try:
        payload = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime-input artifact is not canonical JSON") from exc
    if not isinstance(payload, dict) or set(payload).difference(
        {"params", "worklists", "interstitials"}
    ):
        raise ValueError("runtime-input artifact has an invalid shape")
    params = payload.get("params")
    worklists = payload.get("worklists")
    if not isinstance(params, dict) or any(
        not isinstance(key, str) or not is_runtime_param_scalar(item)
        for key, item in params.items()
    ):
        raise ValueError("runtime-input artifact has invalid parameters")
    if not isinstance(worklists, dict) or any(
        not isinstance(name, str)
        or not isinstance(rows, list)
        or any(
            not isinstance(row, dict)
            or any(
                not isinstance(key, str) or not isinstance(item, str)
                for key, item in row.items()
            )
            for row in rows
        )
        for name, rows in worklists.items()
    ):
        raise ValueError("runtime-input artifact has invalid worklists")
    interstitials = payload.get("interstitials")
    if interstitials is not None and not isinstance(interstitials, list):
        raise ValueError("runtime-input artifact has invalid interstitials")
    validated_interstitials: list[Interstitial] = []
    if interstitials is not None:
        try:
            validated_interstitials = [
                Interstitial.model_validate(item) for item in interstitials
            ]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "runtime-input artifact has invalid interstitials"
            ) from exc
        if [
            interstitial.model_dump(mode="json")
            for interstitial in validated_interstitials
        ] != interstitials:
            raise ValueError("runtime-input artifact has non-canonical interstitials")
    canonical_worklists = {
        name: [dict(row) for row in rows] for name, rows in worklists.items()
    }
    canonical = runtime_inputs_bytes(
        workflow,
        dict(params),
        canonical_worklists,
        interstitials=(validated_interstitials if interstitials is not None else None),
    )
    if canonical != value:
        raise ValueError("runtime-input artifact is not in canonical form")
    return effective_runtime_params(workflow, dict(params)), canonical_worklists


def runtime_inputs_digest(
    workflow: Workflow,
    params: Mapping[str, RuntimeParamScalar] | None,
    worklists: dict[str, list[dict[str, str]]] | None,
    *,
    interstitials: list[Interstitial] | None = None,
) -> str:
    """Hash the canonical exact runtime-input bytes without retaining values."""

    return hashlib.sha256(
        runtime_inputs_bytes(
            workflow,
            params,
            worklists,
            interstitials=interstitials,
        )
    ).hexdigest()


def interstitial_declarations_digest(
    workflow: Workflow,
    interstitials: list[Interstitial] | None,
) -> str:
    """Hash the exact bundle and runtime interstitial declarations.

    The workflow declarations are already covered by the bundle digest, while
    runtime declarations are also covered by :func:`runtime_inputs_digest`.
    This dedicated digest closes a different boundary: it proves that the run
    gate evaluated the same complete action surface later carried by the
    authorization factory.
    """

    payload = {
        "workflow": [
            interstitial.model_dump(mode="json")
            for interstitial in workflow.interstitials
        ],
        "runtime": [
            interstitial.model_dump(mode="json")
            for interstitial in (interstitials or [])
        ],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class UnverifiedWriteApproval(BaseModel):
    """Approval for one GUI step whose effects lack an independent verifier."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    effect_contract_hashes: tuple[str, ...] = Field(min_length=1)


class GovernedRunAuthorization(BaseModel):
    """Ephemeral capability carrying admission decisions into replay.

    ``approval_source`` is deliberately descriptive, not an authentication
    claim.  The local CLI can prove that its explicit flag was supplied, but it
    cannot identify a human.  Hosted callers can replace the source with their
    authenticated approval reference when they construct the capability.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    bundle_content_digest: str = Field(pattern="^[a-f0-9]{64}$")
    runtime_inputs_digest: str = Field(pattern="^[a-f0-9]{64}$")
    admitted_policy_name: str
    admitted_policy_contract_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    execution_profile: Literal["demo", "standard", "regulated"] | None = None
    minimum_effect_tier: int | None = Field(default=None, ge=1, le=4)
    qualified_effect_requirements: tuple[QualifiedEffectRequirement, ...] = Field(
        default_factory=tuple
    )
    required_identity_step_ids: tuple[str, ...] = Field(default_factory=tuple)
    unverified_write_approvals: tuple[UnverifiedWriteApproval, ...] = Field(
        default_factory=tuple
    )
    approval_source: str = "local-cli-explicit-flag"
    qualification_admission: QualificationAdmissionEnvelope | None = None
    qualification_admission_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    # v2 Production admission identity.  The signed artifact stays in the
    # private authority handoff; these PHI-free bindings enter the durable run
    # authorization so a different admission cannot replace it after the gate.
    production_qualification_admission_id: str | None = None
    production_qualification_admission_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    production_qualification_evidence_identity_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    production_qualification_runtime_validation_id: str | None = None
    production_qualification_signer_registry_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    production_qualification_signer_registry_revision: int | None = Field(
        default=None, ge=1
    )
    production_qualification_signer_registry_expires_at: str | None = None
    production_qualification_authority_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_project_id: str | None = None
    qualification_project_revision: int | None = Field(default=None, ge=1)
    qualification_project_contract_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_case_id: str | None = Field(
        default=None, pattern=_QUALIFICATION_ID_RE
    )
    qualification_campaign_id_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_case_input_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_run_id_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_campaign_permit_id: str | None = None
    qualification_campaign_permit_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_campaign_signer_registry_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_campaign_signer_registry_revision: int | None = Field(
        default=None, ge=1
    )
    qualification_campaign_signer_registry_expires_at: str | None = None
    qualification_campaign_authority_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_case_kind: (
        Literal[
            "representative",
            "ambiguity",
            "wrong_identity",
            "stale_identity",
            "weak_effect",
            "missing_effect",
        ]
        | None
    ) = None
    qualification_case_action_paths: dict[str, Literal["gui", "api"]] = Field(
        default_factory=dict
    )
    qualification_fault_driver_id: str | None = Field(
        default=None, pattern=_QUALIFICATION_ID_RE
    )
    qualification_fault_driver_contract_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )
    qualification_fault_driver_key_id: str | None = Field(
        default=None, pattern=_QUALIFICATION_ID_RE
    )
    qualification_fault_step_id_sha256: str | None = Field(
        default=None, pattern="^[a-f0-9]{64}$"
    )

    @model_validator(mode="after")
    def _qualification_binding_is_complete(self) -> "GovernedRunAuthorization":
        if (self.qualification_admission is None) != (
            self.qualification_admission_sha256 is None
        ):
            raise ValueError("qualification admission binding is incomplete")
        if (
            self.qualification_admission is not None
            and self.qualification_admission.artifact_sha256()
            != self.qualification_admission_sha256
        ):
            raise ValueError("qualification admission digest does not match")
        production_qualification = (
            self.production_qualification_admission_id,
            self.production_qualification_admission_sha256,
            self.production_qualification_evidence_identity_sha256,
            self.production_qualification_runtime_validation_id,
            self.production_qualification_signer_registry_sha256,
            self.production_qualification_signer_registry_revision,
            self.production_qualification_authority_sha256,
        )
        if any(value is not None for value in production_qualification) and not all(
            value is not None for value in production_qualification
        ):
            raise ValueError("Production qualification binding is incomplete")
        if all(value is not None for value in production_qualification):
            admission_id = str(self.production_qualification_admission_id)
            until_revoked = admission_id.startswith("sha256:")
            if (
                not until_revoked
                and self.production_qualification_signer_registry_expires_at is None
            ):
                raise ValueError("Production qualification binding is incomplete")
        requirement_refs = [
            (item.step_id, item.actuation_path, item.effect_index)
            for item in self.qualified_effect_requirements
        ]
        if len(requirement_refs) != len(set(requirement_refs)):
            raise ValueError("qualified effect requirements must be unique")
        if requirement_refs != sorted(requirement_refs):
            raise ValueError("qualified effect requirements must be ordered")
        values = (
            self.qualification_project_id,
            self.qualification_project_revision,
            self.qualification_project_contract_sha256,
            self.qualification_case_id,
            self.qualification_campaign_id_sha256,
            self.qualification_case_input_sha256,
            self.qualification_run_id_sha256,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("qualification-run authorization binding is incomplete")
        campaign_permit = (
            self.qualification_campaign_permit_id,
            self.qualification_campaign_permit_sha256,
            self.qualification_campaign_signer_registry_sha256,
            self.qualification_campaign_signer_registry_revision,
            self.qualification_campaign_signer_registry_expires_at,
            self.qualification_campaign_authority_sha256,
        )
        if any(value is not None for value in campaign_permit) and not all(
            value is not None for value in campaign_permit
        ):
            raise ValueError("qualification campaign permit binding is incomplete")
        if (
            self.qualification_case_kind is not None
            and self.qualification_case_id is None
        ):
            raise ValueError(
                "qualification case kind requires a complete case authorization"
            )
        if self.qualification_case_id is None and self.qualification_case_action_paths:
            raise ValueError(
                "qualification action paths require a complete case authorization"
            )
        if self.qualification_case_kind is not None:
            if not self.qualification_case_action_paths:
                raise ValueError(
                    "qualification cases require an exact actuation-path map"
                )
            if any(
                re.fullmatch(_QUALIFICATION_ID_RE, step_id) is None
                for step_id in self.qualification_case_action_paths
            ):
                raise ValueError(
                    "qualification action paths contain an invalid step id"
                )
        driver_values = (
            self.qualification_fault_driver_id,
            self.qualification_fault_driver_contract_sha256,
            self.qualification_fault_driver_key_id,
            self.qualification_fault_step_id_sha256,
        )
        if any(value is not None for value in driver_values) and not all(
            value is not None for value in driver_values
        ):
            raise ValueError("qualification fault-driver binding is incomplete")
        if self.qualification_case_kind in {
            "ambiguity",
            "wrong_identity",
            "stale_identity",
            "weak_effect",
            "missing_effect",
        } and not all(value is not None for value in driver_values):
            raise ValueError("qualification fault cases require a bound fault driver")
        if (
            self.qualification_case_kind
            in {
                "ambiguity",
                "wrong_identity",
                "stale_identity",
                "weak_effect",
                "missing_effect",
            }
            and not self.qualification_case_action_paths
        ):
            raise ValueError(
                "qualification fault cases require a permitted actuation path"
            )
        if self.qualification_case_kind == "representative" and any(
            value is not None for value in driver_values
        ):
            raise ValueError(
                "representative qualification cases cannot bind a fault driver"
            )
        if self.qualification_case_id is not None and (
            self.execution_profile != "standard"
            or self.approval_source != "qualification-campaign"
        ):
            raise ValueError(
                "qualification cases require the Standard profile and the "
                "qualification-campaign approval source"
            )
        if (
            self.qualification_case_id is not None
            and self.qualification_case_input_sha256 != self.runtime_inputs_digest
        ):
            raise ValueError(
                "qualification case input must be the exact governed runtime-input "
                "digest"
            )
        return self

    def _qualification_binding_error(self, workflow: Workflow) -> str | None:
        if self.qualification_case_id is None:
            return None
        project = workflow.qualification
        if project is None:
            return "qualification-run authorization requires a qualification project"
        if project.project_id != self.qualification_project_id:
            return "qualification-run authorization project id changed"
        if project.revision != self.qualification_project_revision:
            return "qualification-run authorization project revision changed"
        if project.contract_sha256() != self.qualification_project_contract_sha256:
            return "qualification-run authorization project contract changed"
        case = next(
            (case for case in project.cases if case.id == self.qualification_case_id),
            None,
        )
        if case is None:
            return "qualification-run authorization references an unknown case"
        if self.qualification_case_kind != case.kind.value:
            return "qualification-run authorization kind does not match its case"
        if case.runtime_input_sha256 is None:
            return "qualification case has no approved runtime-input digest"
        if self.qualification_case_input_sha256 != case.runtime_input_sha256:
            return "qualification-run input does not match its case contract"

        from openadapt_flow.qualification import qualification_action_requirements

        try:
            required_actions, required_identity = qualification_action_requirements(
                workflow
            )
        except ValueError:
            return "qualification action requirements are ambiguous"
        missing_identity = sorted(
            required_identity.difference(self.required_identity_step_ids)
        )
        if missing_identity:
            return (
                "qualification-run authorization omits required identity steps: "
                + ", ".join(missing_identity)
            )
        case_action_paths = {
            target.step_id: target.actuation_path for target in case.action_targets
        }
        if self.qualification_case_action_paths != case_action_paths:
            return "qualification-run action paths do not match the case contract"
        from openadapt_flow.policy import executable_actuation_paths
        from openadapt_flow.traversal import iter_workflow_steps

        workflow_steps = {step.id: step for step in iter_workflow_steps(workflow)}
        for step_id, actuation_path in case_action_paths.items():
            step = workflow_steps.get(step_id)
            if step is None:
                return "qualification case targets an unknown workflow action"
            if actuation_path == "api" and step.api_binding is None:
                return "qualification case targets a missing API actuation path"
            if actuation_path not in executable_actuation_paths(step):
                return "qualification case targets a non-executable actuation path"
        if case.kind.value == "representative":
            target_steps = set(case_action_paths)
            if not target_steps:
                return "representative qualification case has no required actions"
            unknown_targets = sorted(target_steps.difference(workflow_steps))
            if unknown_targets:
                return (
                    "representative case targets unknown workflow actions: "
                    + ", ".join(unknown_targets)
                )
            if self.qualification_fault_step_id_sha256 is not None:
                return "representative qualification case binds a fault target"
        else:
            target = case.resolved_fault_target()
            if target is None:
                return "qualification fault case has no target action"
            target_id = target.step_id
            if case_action_paths.get(target_id) != target.actuation_path:
                return (
                    "qualification fault target is outside its permitted action scope"
                )
            if target.actuation_path == "api" and case.kind.value not in {
                "weak_effect",
                "missing_effect",
            }:
                return (
                    "API qualification fault paths support only weak-effect and "
                    "missing-effect cases"
                )
            if target_id not in required_actions:
                return "qualification fault case targets an unqualified action"
            if case.kind.value in {"wrong_identity", "stale_identity"} and (
                target_id not in required_identity
            ):
                return "identity fault case target is not consequential"
            if (
                self.qualification_fault_step_id_sha256
                != hashlib.sha256(target_id.encode("utf-8")).hexdigest()
            ):
                return "qualification fault target does not match its case contract"
        if self.qualification_fault_step_id_sha256 is not None:
            from openadapt_flow.traversal import iter_workflow_steps

            matching_steps = [
                step
                for step in iter_workflow_steps(workflow)
                if hashlib.sha256(step.id.encode("utf-8")).hexdigest()
                == self.qualification_fault_step_id_sha256
            ]
            if len(matching_steps) != 1:
                return "qualification fault target step is missing or ambiguous"
        return None

    def validate_workflow(self, workflow: Workflow) -> str | None:
        """Return a refusal reason when this capability does not fit ``workflow``."""
        actual_digest = (
            workflow.manifest.content_digest if workflow.manifest is not None else None
        )
        if actual_digest != self.bundle_content_digest:
            return (
                "governed run authorization is bound to bundle digest "
                f"{self.bundle_content_digest[:16]}..., but the loaded bundle is "
                f"{(actual_digest or 'unsealed')[:16]}..."
            )
        if workflow.manifest is None:
            return "governed run authorization requires a sealed manifest"
        if self.qualification_admission is not None:
            payload = self.qualification_admission.payload
            template = workflow.manifest.provenance.governed_authorization_template
            if template is None:
                return "qualification admission requires a governed template"
            effect_contract_digest = contract_sha256(
                [
                    item.model_dump(mode="json")
                    for item in template.qualified_effect_requirements
                ]
            )
            if (
                payload.bundle_content_digest != self.bundle_content_digest
                or payload.governed_authorization_template_sha256
                != template.template_sha256
                or payload.environment_contract_sha256
                != template.qualification_environment_contract_sha256
                or payload.input_policy_sha256 != template.parameter_contract_sha256
                or payload.action_policy_sha256
                != template.qualification_project_contract_sha256
                or payload.identity_contract_sha256 != template.identity_contract_sha256
                or payload.effect_contract_sha256 != effect_contract_digest
            ):
                return (
                    "qualification admission does not bind the current governed "
                    "workflow contracts"
                )
        if self.execution_profile is not None and self.minimum_effect_tier is not None:
            from openadapt_flow.execution_profiles import required_effect_tier

            actual_tier = required_effect_tier(workflow, self.execution_profile)
            if actual_tier is None or int(actual_tier) != self.minimum_effect_tier:
                return (
                    "workflow effect-verification minimum changed after authorization"
                )

        from openadapt_flow.bundle_validation import compute_content_digest

        recomputed = compute_content_digest(workflow, workflow.manifest.file_hashes)
        if recomputed != self.bundle_content_digest:
            return (
                "governed run authorization no longer matches the current "
                "in-memory workflow semantics"
            )

        qualification_binding_error = self._qualification_binding_error(workflow)
        if qualification_binding_error is not None:
            return qualification_binding_error
        qualification_campaign = self.qualification_case_id is not None
        production_qualification = (
            self.execution_profile in {"standard", "regulated"}
            and workflow.qualification is not None
        )
        if production_qualification and self.admitted_policy_contract_sha256 is None:
            return "production qualification authorization has no exact policy digest"
        if production_qualification:
            from openadapt_flow.execution_profiles import (
                qualified_effect_requirements,
            )

            assert self.execution_profile is not None
            try:
                expected_requirements = qualified_effect_requirements(
                    workflow, self.execution_profile
                )
            except ValueError:
                return "workflow qualified effect requirements are invalid"
            if self.qualified_effect_requirements != expected_requirements:
                return (
                    "governed run authorization qualified effect requirements changed"
                )
        if production_qualification and not qualification_campaign:
            assert self.admitted_policy_contract_sha256 is not None
            from openadapt_flow.qualification import current_certification_matches

            if not current_certification_matches(
                workflow,
                policy_contract_digest=self.admitted_policy_contract_sha256,
            ):
                return (
                    "governed run authorization policy digest does not match the "
                    "current qualification certification"
                )

        steps = {step.id: step for step in iter_workflow_steps(workflow)}
        missing_identity = sorted(
            set(self.required_identity_step_ids).difference(steps)
        )
        if missing_identity:
            return (
                "governed run authorization requires unknown identity step(s): "
                + ", ".join(missing_identity)
            )

        seen: set[str] = set()
        for approval in self.unverified_write_approvals:
            if approval.step_id in seen:
                return (
                    "governed run authorization contains duplicate write approval "
                    f"for step {approval.step_id!r}"
                )
            seen.add(approval.step_id)
            step = steps.get(approval.step_id)
            if step is None:
                return (
                    "governed run authorization approves unknown write step "
                    f"{approval.step_id!r}"
                )
            expected = sorted(effect.contract_hash() for effect in step.effects)
            if sorted(approval.effect_contract_hashes) != expected:
                return (
                    "governed run authorization effect contract mismatch for step "
                    f"{approval.step_id!r}"
                )
        return None

    def validate_execution(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path | str,
        params: Mapping[str, RuntimeParamScalar] | None,
        worklists: dict[str, list[dict[str, str]]] | None,
        interstitials: list[Interstitial] | None = None,
        continuation: bool = False,
    ) -> str | None:
        """Validate semantics, sealed assets, inputs, and single-use status."""
        refusal, _assets = self.validate_execution_snapshot(
            workflow,
            bundle_dir=bundle_dir,
            params=params,
            worklists=worklists,
            interstitials=interstitials,
            continuation=continuation,
        )
        return refusal

    def validate_execution_snapshot(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path | str,
        params: Mapping[str, RuntimeParamScalar] | None,
        worklists: dict[str, list[dict[str, str]]] | None,
        interstitials: list[Interstitial] | None = None,
        continuation: bool = False,
    ) -> tuple[str | None, dict[str, bytes]]:
        """Validate once and return the exact sealed bytes execution may use."""
        refusal = self.validate_workflow(workflow)
        if refusal is not None:
            return refusal, {}
        assert workflow.manifest is not None

        declarations = [*workflow.interstitials, *(interstitials or [])]
        validated_interstitials: list[Interstitial] = []
        try:
            for declaration in declarations:
                validated_interstitials.append(
                    Interstitial.model_validate(declaration.model_dump(mode="python"))
                )
        except Exception as exc:
            return (
                "governed run authorization contains an invalid interstitial "
                f"declaration ({type(exc).__name__})",
                {},
            )

        from openadapt_flow.bundle_validation import interstitial_asset_paths

        unsealed_interstitial_assets = sorted(
            interstitial_asset_paths(validated_interstitials)
            - set(workflow.manifest.file_hashes)
        )
        if unsealed_interstitial_assets:
            return (
                "governed run authorization interstitial declaration references "
                "asset(s) that are not sealed in the bundle manifest: "
                + ", ".join(unsealed_interstitial_assets),
                {},
            )

        from openadapt_flow.bundle_validation import (
            BundleIntegrityError,
            verify_integrity,
        )

        try:
            verify_integrity(
                workflow,
                bundle_dir,
                workflow.manifest,
                decrypted_assets=(
                    workflow.decrypted_templates() if workflow.encrypted else None
                ),
            )
        except BundleIntegrityError as exc:
            return f"governed run authorization bundle integrity failed: {exc}", {}

        assets: dict[str, bytes] = {}
        decrypted = workflow.decrypted_templates() if workflow.encrypted else None
        try:
            for rel, expected in workflow.manifest.file_hashes.items():
                data = (
                    decrypted.get(rel)
                    if decrypted is not None
                    else (Path(bundle_dir) / rel).read_bytes()
                )
                if data is None or hashlib.sha256(data).hexdigest() != expected:
                    return (
                        f"governed run authorization asset {rel!r} changed "
                        "while its verified snapshot was created",
                        {},
                    )
                assets[rel] = data
        except OSError as exc:
            return f"governed run authorization could not snapshot assets: {exc}", {}

        actual_inputs = runtime_inputs_digest(
            workflow,
            params,
            worklists,
            interstitials=interstitials,
        )
        if actual_inputs != self.runtime_inputs_digest:
            return (
                "governed run authorization is bound to different runtime "
                "parameters or worklists, or interstitial declarations",
                {},
            )

        if not continuation:
            with _CONSUMED_LOCK:
                if self.authorization_id in _CONSUMED_IDS:
                    return (
                        "governed run authorization was already consumed by a "
                        "different execution",
                        {},
                    )
                _CONSUMED_IDS.add(self.authorization_id)
        return None, assets

    def requires_verified_identity(self, step_id: str) -> bool:
        return step_id in self.required_identity_step_ids

    def approves_unverified_write(self, step: Step) -> bool:
        """Whether this capability exactly approves ``step``'s current effects."""
        expected = sorted(effect.contract_hash() for effect in step.effects)
        return any(
            approval.step_id == step.id
            and sorted(approval.effect_contract_hashes) == expected
            for approval in self.unverified_write_approvals
        )

    def effect_requirements(
        self,
        step_id: str,
        actuation_path: Literal["gui", "api"],
    ) -> tuple[QualifiedEffectRequirement, ...]:
        """Return the exact ordered requirements admitted for one path."""

        return tuple(
            item
            for item in self.qualified_effect_requirements
            if item.step_id == step_id and item.actuation_path == actuation_path
        )
