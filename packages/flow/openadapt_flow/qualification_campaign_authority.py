"""Fail-closed authority for one signed non-production qualification trial."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openadapt_flow.ir import Workflow
from openadapt_flow.production_qualification import (
    ProductionQualificationAuthorityError,
    _read_private_json,
)
from openadapt_flow.qualification import (
    qualification_campaign_id_sha256,
    qualification_run_id_sha256,
)
from openadapt_flow.qualification_admission_v2 import (
    QualificationSignerRegistry,
    contract_sha256,
)
from openadapt_flow.qualification_campaign_permit import (
    QualificationCampaignPermitEnvelope,
    QualificationCampaignPermitError,
    QualificationCampaignPermitExpected,
    verify_qualification_campaign_permit,
)
from openadapt_flow.runtime.authorization import GovernedRunAuthorization

AUTHORITY_SCHEMA: Final[Literal["openadapt.qualification-campaign-authority/v1"]] = (
    "openadapt.qualification-campaign-authority/v1"
)


class QualificationCampaignAuthorityError(ValueError):
    """The non-production qualification authority is unsafe or invalid."""


class QualificationCampaignAuthority(BaseModel):
    """Closed handoff for one signed, isolated qualification trial."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.qualification-campaign-authority/v1"] = (
        AUTHORITY_SCHEMA
    )
    qualification_campaign_permit: QualificationCampaignPermitEnvelope
    qualification_campaign_permit_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected: QualificationCampaignPermitExpected
    qualification_signer_registry: QualificationSignerRegistry
    qualification_signer_registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    consumed_permit_ids: tuple[str, ...] = ()

    @field_validator("consumed_permit_ids")
    @classmethod
    def _ordered_consumed_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("consumed qualification permit ids must be ordered")
        for item in value:
            try:
                parsed = UUID(item)
            except ValueError as exc:
                raise ValueError(
                    "consumed qualification permit id must be a canonical UUID"
                ) from exc
            if str(parsed) != item:
                raise ValueError(
                    "consumed qualification permit id must be a canonical UUID"
                )
        return value

    def model_post_init(self, __context: object) -> None:
        if (
            self.qualification_campaign_permit.artifact_sha256()
            != self.qualification_campaign_permit_sha256
        ):
            raise ValueError("qualification campaign permit digest does not match")
        if (
            self.qualification_signer_registry.artifact_sha256()
            != self.qualification_signer_registry_sha256
        ):
            raise ValueError("qualification signer registry digest does not match")

    def immutable_binding_sha256(self) -> str:
        return contract_sha256(
            {
                "schema_version": self.schema_version,
                "qualification_campaign_permit_sha256": (
                    self.qualification_campaign_permit_sha256
                ),
                "expected": self.expected.model_dump(mode="json"),
                "qualification_signer_registry_sha256": (
                    self.qualification_signer_registry_sha256
                ),
            }
        )


def load_qualification_campaign_authority(
    path: Path | str,
) -> QualificationCampaignAuthority:
    """Load one exact private non-production campaign authority."""

    try:
        raw = _read_private_json(Path(path))
    except ProductionQualificationAuthorityError as exc:
        raise QualificationCampaignAuthorityError(str(exc)) from exc
    try:
        authority = QualificationCampaignAuthority.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise QualificationCampaignAuthorityError(
            "qualification campaign authority has an invalid exact binding"
        ) from exc
    if authority.model_dump(mode="json") != raw:
        raise QualificationCampaignAuthorityError(
            "qualification campaign authority is not in canonical schema form"
        )
    return authority


def _workflow_binding_refusal(
    authority: QualificationCampaignAuthority,
    workflow: Workflow,
    *,
    case_id: str,
    campaign_id: str,
    run_id: str,
    input_digest: str,
) -> str | None:
    manifest = workflow.manifest
    project = workflow.qualification
    if manifest is None or not manifest.content_digest:
        return "qualification campaign requires a sealed bundle"
    if project is None:
        return "qualification campaign requires a qualification project"
    template = manifest.provenance.governed_authorization_template
    if template is None:
        return "qualification campaign requires a governed template"
    case = next((item for item in project.cases if item.id == case_id), None)
    if case is None:
        return "qualification campaign references an unknown case"
    payload = authority.qualification_campaign_permit.payload
    trial = payload.trial
    if (
        payload.bundle_content_digest != manifest.content_digest
        or payload.environment_digest != project.environment.environment_digest
        or payload.environment_contract_sha256
        != template.qualification_environment_contract_sha256
        or trial.campaign_id != campaign_id
        or trial.qualification_run_id != run_id
        or trial.input_digest != input_digest
        or trial.campaign_contract_sha256 != project.contract_sha256()
        or trial.task != case.id
        or trial.condition != case.kind.value
    ):
        return "qualification campaign permit does not bind this exact trial"
    return None


class QualificationCampaignGuard:
    """Re-read one signed non-production trial permit at every input edge."""

    def __init__(
        self,
        path: Path | str,
        *,
        workflow: Workflow,
        case_id: str,
        input_digest: str,
        campaign_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        initial = load_qualification_campaign_authority(self.path)
        trial = initial.qualification_campaign_permit.payload.trial
        self.case_id = case_id
        self.input_digest = input_digest
        self.campaign_id = campaign_id or trial.campaign_id
        self.run_id = run_id or trial.qualification_run_id
        self._permit_sha256 = initial.qualification_campaign_permit_sha256
        self._expected = initial.expected
        self._registry_sha256 = initial.qualification_signer_registry_sha256
        self._consumed_ids = frozenset(initial.consumed_permit_ids)
        refusal = _workflow_binding_refusal(
            initial,
            workflow,
            case_id=self.case_id,
            campaign_id=self.campaign_id,
            run_id=self.run_id,
            input_digest=self.input_digest,
        )
        if refusal is not None:
            raise QualificationCampaignAuthorityError(refusal)

    def _load_current(self) -> QualificationCampaignAuthority:
        current = load_qualification_campaign_authority(self.path)
        if (
            current.qualification_campaign_permit_sha256 != self._permit_sha256
            or current.expected != self._expected
            or current.qualification_signer_registry_sha256 != self._registry_sha256
        ):
            raise QualificationCampaignAuthorityError(
                "qualification campaign authority changed after run admission"
            )
        consumed = frozenset(current.consumed_permit_ids)
        if not self._consumed_ids.issubset(consumed):
            raise QualificationCampaignAuthorityError(
                "qualification campaign consumption state rolled back"
            )
        self._consumed_ids = consumed
        return current

    def verify(self, workflow: Workflow) -> str:
        current = self._load_current()
        refusal = _workflow_binding_refusal(
            current,
            workflow,
            case_id=self.case_id,
            campaign_id=self.campaign_id,
            run_id=self.run_id,
            input_digest=self.input_digest,
        )
        if refusal is not None:
            raise QualificationCampaignAuthorityError(refusal)
        try:
            return verify_qualification_campaign_permit(
                current.qualification_campaign_permit,
                registry=current.qualification_signer_registry,
                expected=current.expected,
                consumed_permit_ids=frozenset(current.consumed_permit_ids),
            )
        except QualificationCampaignPermitError as exc:
            raise QualificationCampaignAuthorityError(
                "qualification campaign permit is not active"
            ) from exc

    def authorization_binding(self, workflow: Workflow) -> dict[str, object]:
        digest = self.verify(workflow)
        current = self._load_current()
        payload = current.qualification_campaign_permit.payload
        return {
            "qualification_campaign_permit_id": payload.permit_id,
            "qualification_campaign_permit_sha256": digest,
            "qualification_campaign_signer_registry_sha256": (
                current.qualification_signer_registry_sha256
            ),
            "qualification_campaign_signer_registry_revision": (
                current.qualification_signer_registry.revision
            ),
            "qualification_campaign_signer_registry_expires_at": (
                current.qualification_signer_registry.expires_at
            ),
            "qualification_campaign_authority_sha256": (
                current.immutable_binding_sha256()
            ),
        }

    def authorization_refusal(
        self,
        workflow: Workflow,
        authorization: GovernedRunAuthorization,
    ) -> str | None:
        try:
            expected = self.authorization_binding(workflow)
        except QualificationCampaignAuthorityError as exc:
            return str(exc)
        if any(
            getattr(authorization, field, None) != value
            for field, value in expected.items()
        ):
            return "qualification campaign permit differs from run authorization"
        if (
            authorization.qualification_case_id != self.case_id
            or authorization.runtime_inputs_digest != self.input_digest
            or authorization.qualification_campaign_id_sha256
            != qualification_campaign_id_sha256(self.campaign_id)
            or authorization.qualification_run_id_sha256
            != qualification_run_id_sha256(self.run_id)
        ):
            return "qualification campaign permit differs from the exact trial"
        return None

    def refusal(self, workflow: Workflow) -> str | None:
        try:
            self.verify(workflow)
        except QualificationCampaignAuthorityError as exc:
            return str(exc)
        return None


__all__ = [
    "AUTHORITY_SCHEMA",
    "QualificationCampaignAuthority",
    "QualificationCampaignAuthorityError",
    "QualificationCampaignGuard",
    "load_qualification_campaign_authority",
]
