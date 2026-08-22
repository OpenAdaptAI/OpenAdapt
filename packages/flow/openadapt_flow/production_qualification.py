"""Fail-closed v2 qualification authority for Production actuation.

The qualification project and its persisted certification are evidence.  They
are not authority to actuate.  A Standard or Regulated run must also carry the
signed, expiring, revocable v2 admission defined in
``qualification_admission_v2``.  This module loads that authority from one
private local handoff and re-verifies it at every input edge.

The handoff keeps two trust sources separate:

* the signed admission states what the qualification authority admitted;
* ``expected`` states the values reproduced by the live runner or deployment.

Flow compares both and independently binds the fields that the sealed bundle
can reproduce.  A customer-local runner must also provide a fresh permit trust
snapshot.  A managed Cloud run obtains the equivalent current revocation and
expiry decision atomically from its remote delivery permit before each edge.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openadapt_flow.ir import Workflow
from openadapt_flow.private_file import (
    PrivateFileAclError,
    windows_descriptor_has_private_acl,
)
from openadapt_flow.qualification_admission_v2 import (
    QualificationAdmissionEnvelope,
    QualificationAdmissionError,
    QualificationAdmissionExpected,
    QualificationPermitTrustSnapshot,
    QualificationSignerRegistry,
    VerifiedQualificationAdmission,
    contract_sha256,
    verify_qualification_admission,
    verify_qualification_admission_for_actuation,
)

MAX_AUTHORITY_BYTES = 512 * 1024
AUTHORITY_SCHEMA: Final[Literal["openadapt.production-qualification-authority/v1"]] = (
    "openadapt.production-qualification-authority/v1"
)


class ProductionQualificationAuthorityError(ValueError):
    """The private Production qualification handoff is unsafe or invalid."""


class ProductionQualificationAuthority(BaseModel):
    """Closed handoff from an independent qualification and runtime authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.production-qualification-authority/v1"] = (
        AUTHORITY_SCHEMA
    )
    qualification_admission: QualificationAdmissionEnvelope
    qualification_admission_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected: QualificationAdmissionExpected
    qualification_signer_registry: QualificationSignerRegistry
    qualification_signer_registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    permit_trust_snapshot: QualificationPermitTrustSnapshot | None = None
    revoked_admission_ids: tuple[str, ...] = ()

    @field_validator("revoked_admission_ids")
    @classmethod
    def _ordered_revocations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("revoked qualification admission ids must be ordered")
        for item in value:
            try:
                parsed = UUID(item)
            except ValueError as exc:
                raise ValueError(
                    "revoked qualification admission id must be a canonical UUID"
                ) from exc
            if str(parsed) != item:
                raise ValueError(
                    "revoked qualification admission id must be a canonical UUID"
                )
        return value

    def model_post_init(self, __context: object) -> None:
        if (
            self.qualification_admission.artifact_sha256()
            != self.qualification_admission_sha256
        ):
            raise ValueError("qualification admission digest does not match")
        if (
            self.qualification_signer_registry.artifact_sha256()
            != self.qualification_signer_registry_sha256
        ):
            raise ValueError("qualification signer registry digest does not match")

    def immutable_binding_sha256(self) -> str:
        """Identify the immutable admission, expectation, and signer authority.

        The permit snapshot must be refreshed before local input delivery, and
        the revocation set can only grow.  Neither mutable safety input is part
        of the retained run identity.
        """

        return contract_sha256(
            {
                "schema_version": self.schema_version,
                "qualification_admission_sha256": (self.qualification_admission_sha256),
                "expected": self.expected.model_dump(mode="json"),
                "qualification_signer_registry_sha256": (
                    self.qualification_signer_registry_sha256
                ),
            }
        )


def _read_private_json(path: Path) -> object:
    """Read one bounded, owner-only regular file without following a link."""

    path = Path(path)
    lstat_before = None
    if os.name == "nt":
        try:
            lstat_before = os.lstat(path)
        except OSError as exc:
            raise ProductionQualificationAuthorityError(
                "qualification authority file could not be inspected safely"
            ) from exc
        if stat.S_ISLNK(lstat_before.st_mode):
            raise ProductionQualificationAuthorityError(
                "qualification authority file must not be a link"
            )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProductionQualificationAuthorityError(
            "qualification authority file could not be opened safely"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        windows_path_changed = False
        if lstat_before is not None:
            try:
                lstat_after = os.lstat(path)
                windows_path_changed = (
                    stat.S_ISLNK(lstat_after.st_mode)
                    or lstat_after.st_ino != lstat_before.st_ino
                    or lstat_after.st_dev != lstat_before.st_dev
                )
            except OSError:
                windows_path_changed = True
        if os.name == "nt" and not windows_path_changed:
            try:
                unsafe_permissions = not windows_descriptor_has_private_acl(descriptor)
            except PrivateFileAclError as exc:
                raise ProductionQualificationAuthorityError(str(exc)) from exc
        else:
            unsafe_permissions = (
                not hasattr(os, "geteuid")
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or unsafe_permissions
            or windows_path_changed
            or metadata.st_size > MAX_AUTHORITY_BYTES
        ):
            raise ProductionQualificationAuthorityError(
                "qualification authority file is not a private regular file"
            )
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise ProductionQualificationAuthorityError(
                    "qualification authority file ended during the safe read"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ProductionQualificationAuthorityError(
                "qualification authority file changed during the safe read"
            )
        after = os.fstat(descriptor)
        if (
            after.st_dev != metadata.st_dev
            or after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise ProductionQualificationAuthorityError(
                "qualification authority file changed during the safe read"
            )
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionQualificationAuthorityError(
            "qualification authority file is not valid JSON"
        ) from exc


def load_production_qualification_authority(
    path: Path | str,
) -> ProductionQualificationAuthority:
    """Load one exact private Production qualification authority file."""

    raw = _read_private_json(Path(path))
    try:
        authority = ProductionQualificationAuthority.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ProductionQualificationAuthorityError(
            "qualification authority file has an invalid exact binding"
        ) from exc
    if authority.model_dump(mode="json") != raw:
        raise ProductionQualificationAuthorityError(
            "qualification authority file is not in canonical schema form"
        )
    return authority


def _workflow_binding_refusal(
    authority: ProductionQualificationAuthority,
    workflow: Workflow,
) -> str | None:
    """Bind the signed v2 authority to contracts reproduced from the bundle."""

    manifest = workflow.manifest
    project = workflow.qualification
    if manifest is None or not manifest.content_digest:
        return "Production qualification requires a sealed bundle"
    template = manifest.provenance.governed_authorization_template
    if template is None:
        return "Production qualification requires a governed template"
    if project is None:
        return "Production qualification requires a qualification project"
    payload = authority.qualification_admission.payload
    effect_contract_sha256 = contract_sha256(
        [
            item.model_dump(mode="json")
            for item in template.qualified_effect_requirements
        ]
    )
    if (
        payload.bundle_content_digest != manifest.content_digest
        or payload.governed_authorization_template_sha256 != template.template_sha256
        or payload.environment_digest != project.environment.environment_digest
        or payload.environment_contract_sha256
        != template.qualification_environment_contract_sha256
        or payload.input_policy_sha256 != template.parameter_contract_sha256
        or payload.action_policy_sha256
        != template.qualification_project_contract_sha256
        or payload.identity_contract_sha256 != template.identity_contract_sha256
        or payload.effect_contract_sha256 != effect_contract_sha256
    ):
        return "Production qualification does not bind the sealed workflow contracts"
    return None


class ProductionQualificationGuard:
    """Re-read and verify one v2 authority at every Production input edge."""

    def __init__(
        self,
        path: Path | str,
        *,
        remote_permit_revalidation: bool,
    ) -> None:
        self.path = Path(path)
        self.remote_permit_revalidation = bool(remote_permit_revalidation)
        initial = load_production_qualification_authority(self.path)
        self._admission_sha256 = initial.qualification_admission_sha256
        self._expected = initial.expected
        self._registry_sha256 = initial.qualification_signer_registry_sha256
        self._revoked_ids = frozenset(initial.revoked_admission_ids)

    def _load_current(self) -> ProductionQualificationAuthority:
        current = load_production_qualification_authority(self.path)
        if (
            current.qualification_admission_sha256 != self._admission_sha256
            or current.expected != self._expected
            or current.qualification_signer_registry_sha256 != self._registry_sha256
        ):
            raise ProductionQualificationAuthorityError(
                "qualification authority changed after run admission"
            )
        current_revoked = frozenset(current.revoked_admission_ids)
        if not self._revoked_ids.issubset(current_revoked):
            raise ProductionQualificationAuthorityError(
                "qualification revocation state rolled back"
            )
        self._revoked_ids = current_revoked
        return current

    def verify(
        self,
        workflow: Workflow,
        *,
        for_actuation: bool,
    ) -> VerifiedQualificationAdmission:
        """Verify signature, time, revocation, live values, and bundle binding."""

        current = self._load_current()
        refusal = _workflow_binding_refusal(current, workflow)
        if refusal is not None:
            raise ProductionQualificationAuthorityError(refusal)
        revoked = frozenset(current.revoked_admission_ids)
        try:
            if for_actuation and not self.remote_permit_revalidation:
                snapshot = current.permit_trust_snapshot
                if snapshot is None:
                    raise ProductionQualificationAuthorityError(
                        "local Production actuation requires a fresh permit trust snapshot"
                    )
                return verify_qualification_admission_for_actuation(
                    current.qualification_admission,
                    registry=current.qualification_signer_registry,
                    expected=current.expected,
                    permit_trust_snapshot=snapshot,
                    revoked_admission_ids=revoked,
                )
            return verify_qualification_admission(
                current.qualification_admission,
                registry=current.qualification_signer_registry,
                expected=current.expected,
                revoked_admission_ids=revoked,
            )
        except QualificationAdmissionError as exc:
            raise ProductionQualificationAuthorityError(
                "Production qualification admission is not active"
            ) from exc

    def refusal(self, workflow: Workflow) -> str | None:
        """Return a PHI-free refusal for the last point before input delivery."""

        try:
            self.verify(workflow, for_actuation=True)
        except ProductionQualificationAuthorityError as exc:
            return str(exc)
        return None

    def authorization_binding(self, workflow: Workflow) -> dict[str, object]:
        """Return the verified PHI-free fields retained in run authorization."""

        verified = self.verify(workflow, for_actuation=False)
        current = self._load_current()
        return {
            "production_qualification_admission_id": verified.admission_id,
            "production_qualification_admission_sha256": (
                verified.admission_artifact_sha256
            ),
            "production_qualification_evidence_identity_sha256": (
                verified.evidence_identity_sha256
            ),
            "production_qualification_runtime_validation_id": (
                verified.runtime_validation_id
            ),
            "production_qualification_signer_registry_sha256": (
                verified.registry_sha256
            ),
            "production_qualification_signer_registry_revision": (
                verified.registry_revision
            ),
            "production_qualification_signer_registry_expires_at": (
                verified.registry_expires_at
            ),
            "production_qualification_authority_sha256": (
                current.immutable_binding_sha256()
            ),
        }


__all__ = [
    "AUTHORITY_SCHEMA",
    "ProductionQualificationAuthority",
    "ProductionQualificationAuthorityError",
    "ProductionQualificationGuard",
    "load_production_qualification_authority",
]
