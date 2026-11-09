"""Typed models of the BYOC (bring-your-own-cloud) job wire contract.

Field-for-field against the control plane's ``ByocJobPayload``
(openadapt-cloud ``src/lib/byoc.ts``): the PHI-free descriptor a customer-hosted
Connector leases from ``POST /api/connector/poll`` and executes locally.

The descriptor is metadata only. The PHI-bearing bytes (the compiled bundle IN,
the run report OUT) live in the CUSTOMER'S OWN storage — the payload carries only
opaque relative KEYS (:class:`ByocStorage`) the Connector resolves against the
storage it is configured with. The control plane holds no URL to those bytes and
signs no access to them.

The model is intentionally ``extra="ignore"`` (the control plane may add PHI-free
fields; a Connector must not brittle-fail on an additive change). The governance
posture is instead enforced EXPLICITLY by :meth:`ByocJob.ensure_governed`, which
fail-closes when the dispatch is missing the governed policy, the run-scoped
callback capability, or the immutable bundle binding — or when it carries an
our-owned signed URL that has no place on the byoc path.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openadapt_flow.ir import ExecutionTargetKind
from openadapt_flow.runner.dispatch_envelope import ManagedDispatchEnvelope


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

    model_config = ConfigDict(extra="ignore")

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

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    provider: Optional[str] = None
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    phi_egress_attested: bool = False


class ByocJob(BaseModel):
    """One PHI-free BYOC dispatch descriptor."""

    model_config = ConfigDict(extra="ignore")

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
    #: Exact Cloud-issued, one-run authority. It stays PHI-free and is written
    #: to a private local file before the governed child starts.
    managed_dispatch: Optional[ManagedDispatchEnvelope] = None
    #: HTTPS endpoint that issues monotonic per-action delivery permits for the
    #: exact managed dispatch. The run-scoped ``run_token`` authenticates it.
    managed_delivery_authority_url: Optional[str] = None
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
            require_run_token: when True (the live default) a dispatch without a
                run-scoped callback token is refused — we would be unable to
                report its outcome, so we must not run it. Set False only for a
                mock/dev control plane that bypasses callback auth.

        Raises:
            ByocGovernanceError: on the first missing requirement.
        """
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
        if self.managed_dispatch is not None:
            try:
                authorization = self.managed_dispatch.exact_authorization()
            except ValueError as exc:
                raise ByocGovernanceError(
                    "byoc managed dispatch has an invalid authorization binding"
                ) from exc
            if self.managed_dispatch.run_id != self.run_id:
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
    try:
        job = ByocJob.model_validate(payload)
    except ValidationError as exc:
        raise ByocJobParseError(
            f"byoc job payload does not match the connector contract: {exc}"
        ) from exc
    if lease_job_id is not None:
        job = job.model_copy(update={"lease_job_id": lease_job_id})
    return job
