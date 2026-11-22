"""Typed models of the BYOC (bring-your-own-cloud) job wire contract.

Field-for-field against the control plane's ``ByocJobPayload``
(openadapt-cloud ``src/lib/byoc.ts``): the PHI-free descriptor a customer-hosted
Connector leases from ``POST /api/connector/poll`` and executes locally.

The descriptor is metadata only. The PHI-bearing bytes (the compiled bundle IN,
the run report OUT) live in the CUSTOMER'S OWN storage — the payload carries only
opaque relative KEYS (:class:`ByocStorage`) the Connector resolves against the
storage it is configured with. The control plane holds no URL to those bytes and
signs no access to them.

Legacy payloads can still be parsed for inspection. Unknown outer fields are
retained, never silently dropped; live execution rejects them. The governance
posture is enforced by :meth:`ByocJob.ensure_governed`, which
fail-closes when the dispatch is missing the governed policy, the run-scoped
callback capability, or the immutable bundle binding — or when it carries an
our-owned signed URL that has no place on the byoc path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openadapt_flow.ir import ExecutionTargetKind
from openadapt_flow.runner.dispatch_envelope import ManagedDispatchEnvelope
from openadapt_flow.runner.protocol import DISPATCH_BINDING_LOCAL_FIELDS

if TYPE_CHECKING:
    from openadapt_flow.qualification_admission_v2 import (
        QualificationAdmissionEnvelope as QualificationAdmissionEnvelopeV2,
    )

#: The exact keys Flow's ManagedDispatchEnvelope reads (Cloud adds others).
FLOW_DISPATCH_WIRE_KEYS = (
    "schema_version",
    "run_id",
    "bundle_content_digest",
    "runtime_inputs_digest",
    "authorization",
    "dispatch_binding_sha256",
)
MANAGED_DELIVERY_AUTHORITY_CAPABILITY = "managed_delivery_authority_v3"
PROGRAM_ADMISSION_SCHEMA = "openadapt.program-admission-reference/v1"
PROGRAM_ADMISSION_ADMITTER_SCHEMA = "openadapt.program-admission-reference/v2"
PROGRAM_ADMISSION_APPROVAL_PREFIX = "hosted:program-admission:"
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")


class ByocJobParseError(ValueError):
    """A leased job payload could not be parsed as a BYOC dispatch."""


class ByocGovernanceError(ValueError):
    """A dispatch is missing a fail-closed governance requirement.

    Raised by :meth:`ByocJob.ensure_governed`. The Connector reports the run as
    ``failed`` with this PHI-free reason and NEVER touches a GUI — a dispatch we
    cannot fully govern is refused, not best-effort executed.
    """


class ByocStorage(BaseModel):
    """Customer-owned storage reference (opaque relative keys)."""

    model_config = ConfigDict(extra="allow")

    #: Hint only; the Connector's own config is authoritative.
    backend: Optional[str] = None
    #: Relative key of the compiled bundle in the CUSTOMER'S store. None when the
    #: workflow has no compiled bundle yet (the run cannot proceed).
    bundle_ref: Optional[str] = None
    #: Relative key the Connector WRITES the run report to, in the CUSTOMER'S
    #: store. Echoed back (as report_path) on the PHI-free callback.
    report_ref: str


class GroundingModel(BaseModel):
    """The org's resolved grounding-model config, delivered by the control plane.

    Structurally parallel to the engine's ``DeploymentConfig.runtime.grounding_model``
    and the cloud ``GroundingModelConfig``. The raw API key is NEVER carried —
    only ``api_key_env`` names the env var the Connector reads from ITS OWN
    environment, inside the customer perimeter.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    provider: Optional[str] = None
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    phi_egress_attested: bool = False
    #: Hosts allowed to receive PHI-bearing grounding requests. Cloud always
    #: sends it (empty by default). The customer's deployment profile stays
    #: authoritative: the engine reads its own runtime.phi_grounding_allowlist.
    phi_grounding_allowlist: list[str] = Field(default_factory=list)


class ByocJob(BaseModel):
    """One BYOC descriptor; parsing alone never authorizes live execution."""

    model_config = ConfigDict(extra="allow")

    mode: str = "replay"
    run_id: str
    org_id: str
    workflow_id: str

    #: Required first-class execution substrate. The control plane derives this
    #: from the signed runtime-validation attestation; the connector never
    #: defaults a missing value to web.
    target_kind: ExecutionTargetKind
    storage: Optional[ByocStorage] = None
    report_path: Optional[str] = None
    target_url: Optional[str] = None
    allowed_hosts: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    secrets_ref: Optional[str] = None

    # byoc NEVER uses our signed URLs; a non-null value here is a contract
    # violation (an our-owned bundle bytes URL leaking onto the byoc path).
    bundle_download_url: Optional[str] = None

    # --- Governed callback binding (fail-closed) -------------------------------
    #: Run-scoped bearer capability presented as ``x-run-token`` on the PHI-free
    #: callback. It proves this run and must remain private.
    run_token: Optional[str] = None
    #: Exact Cloud-issued, one-run authority, as Cloud sends it: the six Flow
    #: wire keys plus the authority reference (``program_admission`` for an
    #: admitted wrap program, or ``qualification_admission`` and its digest).
    #: Kept raw here; :meth:`flow_dispatch` projects the exact Flow envelope
    #: that is written to a private local file before the governed child
    #: starts, and :meth:`program_admission` validates the reference.
    managed_dispatch: Optional[dict[str, Any]] = None
    #: HTTPS endpoint that issues monotonic per-action delivery permits for the
    #: exact managed dispatch. The run-scoped ``run_token`` authenticates it.
    managed_delivery_authority_url: Optional[str] = None
    #: Retained Cloud session, required by live execution and bound again by
    #: the signed v3 permit. Never inferred from an untrusted response.
    managed_dispatch_session_id: Optional[str] = None
    bundle_version_id: Optional[str] = None
    runtime_validation_id: Optional[str] = None
    #: SHA-256 of the exact approved sanitized derivative ZIP bytes staged at
    #: ``storage.bundle_ref``. Distinct from the bundle's semantic content
    #: digest; required so a valid but different archive cannot be executed.
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    # --- Governed policy delivery (fail-closed) --------------------------------
    #: The org's resolved (baseline-filled) Tier-3 safety block. Always fully
    #: populated on the live lane; a missing/empty block is a refusal.
    safety: dict[str, Any] = Field(default_factory=dict)
    grounding_model: GroundingModel = Field(default_factory=GroundingModel)

    # The lease id (from the poll envelope, not the payload) is stamped on so ack
    # can release exactly this lease. Not part of the wire payload itself.
    lease_job_id: Optional[str] = None

    def ensure_governed(self, *, require_run_token: bool = True) -> None:
        """Fail closed unless every governance requirement is present.

        Args:
            require_run_token: retained for legacy callers. Live managed
                execution always requires its callback token and session;
                False cannot authorize an older/mock payload.

        Raises:
            ByocGovernanceError: on the first missing requirement.
        """
        if self.mode != "replay":
            raise ByocGovernanceError(
                "byoc live connector only supports governed replay jobs"
            )
        if self.bundle_download_url:
            raise ByocGovernanceError(
                "byoc dispatch carries an our-owned bundle URL; the bundle must "
                "resolve from the customer's own storage (storage.bundle_ref)"
            )
        if not self.safety:
            raise ByocGovernanceError(
                "byoc dispatch is missing the resolved safety policy; refusing to "
                "run without the org's governed safety posture (fail closed)"
            )
        compatibility_kind = self.params.get("target_kind")
        if compatibility_kind is not None and compatibility_kind != self.target_kind:
            raise ByocGovernanceError(
                "byoc dispatch target_kind disagrees with the compatibility "
                "params.target_kind; refusing ambiguous substrate routing "
                "(fail closed)"
            )
        if self.target_kind != "web" and (self.target_url or self.allowed_hosts):
            raise ByocGovernanceError(
                "native/remote byoc dispatch carries a browser target boundary; "
                "app, window, host, and readiness hints must come from the "
                "customer-owned deployment profile (fail closed)"
            )
        # grounding_model is always present (default factory); its absence would
        # mean the whole policy block was dropped, which `safety` already catches.
        if require_run_token and not self.run_token:
            raise ByocGovernanceError(
                "byoc dispatch is missing a run-scoped callback token; refusing "
                "to run a job whose outcome we could not report (fail closed)"
            )
        if (self.managed_dispatch is None) != (
            self.managed_delivery_authority_url is None
        ):
            raise ByocGovernanceError(
                "byoc managed dispatch and delivery authority must be supplied "
                "together (fail closed)"
            )
        if (
            self.model_extra
            or (self.storage is not None and self.storage.model_extra)
            or self.grounding_model.model_extra
        ):
            raise ByocGovernanceError(
                "byoc live dispatch carries unsupported governance fields"
            )
        if self.managed_dispatch is None or not self.managed_delivery_authority_url:
            raise ByocGovernanceError(
                "byoc live execution requires exact managed delivery authority"
            )
        if not isinstance(self.managed_dispatch_session_id, str) or not _UUID.fullmatch(
            self.managed_dispatch_session_id
        ):
            raise ByocGovernanceError(
                "byoc live execution requires a canonical managed dispatch session"
            )
        if self.managed_dispatch is not None:
            if not self.run_token:
                raise ByocGovernanceError(
                    "byoc managed dispatch requires its run-scoped callback token"
                )
            envelope = self.flow_dispatch()
            try:
                authorization = envelope.exact_authorization()
            except ValueError as exc:
                raise ByocGovernanceError(
                    "byoc managed dispatch has an invalid authorization binding"
                ) from exc
            if self.program_admission() is None and not self._qualification_ok(
                envelope
            ):
                raise ByocGovernanceError(
                    "byoc managed dispatch carries neither a program admission "
                    "nor an exact qualification admission (fail closed)"
                )
            if envelope.run_id != self.run_id:
                raise ByocGovernanceError(
                    "byoc managed dispatch belongs to a different run (fail closed)"
                )
            if not authorization.approval_source.startswith("hosted:"):
                raise ByocGovernanceError(
                    "byoc managed dispatch lacks hosted authorization provenance"
                )
            try:
                parsed = urlsplit(self.managed_delivery_authority_url or "")
            except ValueError as exc:
                raise ByocGovernanceError(
                    "byoc managed delivery authority URL is invalid"
                ) from exc
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or bool(parsed.query)
                or bool(parsed.fragment)
            ):
                raise ByocGovernanceError(
                    "byoc managed delivery authority must be a credential-free "
                    "HTTPS endpoint without query or fragment"
                )
        if not self.bundle_version_id or not self.runtime_validation_id:
            raise ByocGovernanceError(
                "byoc dispatch is missing its immutable bundle-version or "
                "runtime-validation binding (fail closed)"
            )
        storage_ref = self.storage.bundle_ref if self.storage else None
        if not storage_ref:
            raise ByocGovernanceError(
                "byoc dispatch has no storage.bundle_ref; there is no bundle to "
                "resolve from the customer's storage (fail closed)"
            )

    def flow_dispatch(self) -> ManagedDispatchEnvelope:
        """Consume the closed authority union before writing its six Flow keys."""
        from openadapt_flow.runtime.authorization import GovernedRunAuthorization

        raw = self.managed_dispatch
        if not isinstance(raw, dict):
            raise ByocGovernanceError("byoc managed dispatch is malformed")
        program = "program_admission" in raw
        extension = (
            {"program_admission"}
            if program
            else {"qualification_admission", "qualification_admission_sha256"}
        )
        if set(raw) != set(FLOW_DISPATCH_WIRE_KEYS) | extension:
            raise ByocGovernanceError("byoc managed authority union is not closed")
        if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
            raise ByocGovernanceError("byoc managed dispatch version is not canonical")
        authorization = raw.get("authorization")
        if not isinstance(authorization, dict) or set(authorization) - set(
            GovernedRunAuthorization.model_fields
        ):
            raise ByocGovernanceError(
                "byoc managed authorization has unsupported fields"
            )
        if any(
            authorization.get(key) is not None for key in DISPATCH_BINDING_LOCAL_FIELDS
        ):
            raise ByocGovernanceError(
                "byoc remote dispatch cannot supply runner-local authority"
            )
        try:
            envelope = ManagedDispatchEnvelope.model_validate(
                {key: raw[key] for key in FLOW_DISPATCH_WIRE_KEYS}
            )
        except (ValidationError, ValueError) as exc:
            raise ByocGovernanceError(
                "byoc managed dispatch does not match the Flow envelope"
            ) from exc
        if program:
            if (
                self.program_admission() is None
                or envelope.authorization.qualification_admission is not None
                or envelope.authorization.qualification_admission_sha256 is not None
            ):
                raise ByocGovernanceError("byoc program admission reference is invalid")
        elif not self._qualification_ok(envelope):
            raise ByocGovernanceError(
                "byoc qualification admission does not bind this job"
            )
        return envelope

    def program_admission(self) -> Optional[dict[str, str]]:
        """The exact person-v1 or admitter-v2 reference, without downgrading."""
        raw = self.managed_dispatch
        if not isinstance(raw, dict):
            return None
        reference = raw.get("program_admission")
        authorization = raw.get("authorization")
        if (
            "qualification_admission" in raw
            or "qualification_admission_sha256" in raw
            or not isinstance(reference, dict)
            or not isinstance(authorization, dict)
            or authorization.get("execution_profile") != "standard"
        ):
            return None
        common = {"schema_version", "admission_sha256", "public_key_hex"}
        version = reference.get("schema_version")
        if version == PROGRAM_ADMISSION_SCHEMA:
            if set(reference) != common:
                return None
        elif version == PROGRAM_ADMISSION_ADMITTER_SCHEMA:
            if (
                set(reference)
                != common
                | {
                    "admission_mode",
                    "admitted_by_principal_id",
                    "admitter_public_key_hex",
                }
                or reference.get("admission_mode") != "admitter_key"
                or not isinstance(reference.get("admitted_by_principal_id"), str)
                or not _PRINCIPAL.fullmatch(reference["admitted_by_principal_id"])
                or not isinstance(reference.get("admitter_public_key_hex"), str)
                or not _HEX64.fullmatch(reference["admitter_public_key_hex"])
            ):
                return None
        else:
            return None
        if (
            not isinstance(reference.get("admission_sha256"), str)
            or not _HEX64.fullmatch(reference["admission_sha256"])
            or not isinstance(reference.get("public_key_hex"), str)
            or not _HEX64.fullmatch(reference["public_key_hex"])
            or authorization.get("approval_source")
            != PROGRAM_ADMISSION_APPROVAL_PREFIX + reference["admission_sha256"]
        ):
            return None
        return dict(reference)

    def qualification_admission(self) -> QualificationAdmissionEnvelopeV2:
        """Parse the actual outer signed v2 proof; parsing is not authority."""
        from openadapt_flow.qualification_admission_v2 import (
            QualificationAdmissionEnvelope,
        )

        raw = self.managed_dispatch or {}
        admission = raw.get("qualification_admission")
        digest = raw.get("qualification_admission_sha256")
        if (
            "program_admission" in raw
            or not isinstance(admission, dict)
            or set(admission) != {"payload", "algorithm", "signature"}
            or not isinstance(digest, str)
            or not _HEX64.fullmatch(digest)
        ):
            raise ByocGovernanceError(
                "byoc signed qualification reference is malformed"
            )
        try:
            parsed = QualificationAdmissionEnvelope.model_validate(admission)
        except (ValidationError, ValueError) as exc:
            raise ByocGovernanceError(
                "byoc qualification proof is not admission v2"
            ) from exc
        if (
            parsed.model_dump(mode="json") != admission
            or parsed.artifact_sha256() != digest
        ):
            raise ByocGovernanceError("byoc qualification proof digest does not match")
        return parsed

    def _qualification_ok(self, envelope: ManagedDispatchEnvelope) -> bool:
        # The outer v2 artifact is distinct from the runtime-local v1 field,
        # which Cloud cannot populate. The executor must verify the artifact
        # against independent operator context through the authority-file path.
        if (
            envelope.authorization.qualification_admission is not None
            or envelope.authorization.qualification_admission_sha256 is not None
        ):
            return False
        try:
            payload = self.qualification_admission().payload
        except ByocGovernanceError:
            return False
        return (
            payload.tenant_id == self.org_id
            and payload.workflow_id == self.workflow_id
            and payload.workflow_version_id == self.bundle_version_id
            and payload.bundle_version_id == self.bundle_version_id
            and payload.runtime_validation_id == self.runtime_validation_id
            and payload.bundle_artifact_sha256 == self.bundle_sha256
            and payload.bundle_content_digest == envelope.bundle_content_digest
        )

    def report_ref(self) -> Optional[str]:
        """The customer-storage key the report is written to (opaque to us)."""
        if self.storage and self.storage.report_ref:
            return self.storage.report_ref
        return self.report_path


def parse_job(
    payload: dict[str, Any], *, lease_job_id: Optional[str] = None
) -> ByocJob:
    """Parse a leased job payload into a :class:`ByocJob`.

    Raises:
        ByocJobParseError: on a payload the Connector cannot represent (missing
            required identifiers). The caller reports the refusal; it never runs
            a job it could not parse.
    """
    if "lease_job_id" in payload:
        raise ByocJobParseError(
            "byoc payload cannot replace its authenticated lease id"
        )
    try:
        job = ByocJob.model_validate(payload)
    except ValidationError as exc:
        raise ByocJobParseError(
            f"byoc job payload does not match the connector contract: {exc}"
        ) from exc
    if lease_job_id is not None:
        job = job.model_copy(update={"lease_job_id": lease_job_id})
    return job
