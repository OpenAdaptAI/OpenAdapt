"""Fleet drift patches: share a governed UI repair across tenants.

When a common application (a payer portal, an EHR) changes its UI, one
tenant's approved repair can become a structural patch that other tenants on
the same application version receive. Every step is off until a tenant's
admin allows the application family (:mod:`~openadapt_flow.fleet.optin`), and
a patch never activates itself on arrival:

1. :mod:`~openadapt_flow.fleet.export` turns a repair a person approved into
   a :class:`~openadapt_flow.fleet.patch.PatchContribution`: old structural
   anchors mapped to new ones and the application-version fingerprints
   (:mod:`~openadapt_flow.fleet.fingerprint`), with no text value or PHI,
   signed with the tenant's short-lived contributor certificate.
2. The :mod:`~openadapt_flow.fleet.hub` verifies it, records the contributor
   in its internal audit log, waits until at least three tenants registered
   the base version, and re-signs the patch with its distribution key.
3. :mod:`~openadapt_flow.fleet.intake` verifies the chain on the receiving
   tenant and registers the patched copy of that tenant's own bundle as a
   ``fleet_patch`` candidate in the existing repair lifecycle.
4. :mod:`~openadapt_flow.fleet.canary` runs it on that tenant's own
   qualification cases.
5. :mod:`~openadapt_flow.fleet.policy` promotes it only under that tenant's
   signed policy and a fresh, verified revocation list
   (:mod:`~openadapt_flow.fleet.status`); otherwise it waits for a person.

Keys follow :mod:`~openadapt_flow.fleet.signing`: an offline root under M-of-N
custody, an intermediate, and signer certificates valid for at most seven
days. :mod:`~openadapt_flow.fleet.feed` moves patches and status (a file
directory today; a Cloud feed is an unimplemented interface).
:mod:`~openadapt_flow.fleet.simulator` runs three tenants end to end. See
``docs/FLEET_DRIFT_PATCHES.md``.
"""

from openadapt_flow.fleet.canary import (  # noqa: F401
    QualificationCase,
    TenantCanaryRunner,
    canary_patch,
    run_qualification_canary,
)
from openadapt_flow.fleet.export import (  # noqa: F401
    ExportResult,
    PatchExportError,
    export_patch,
)
from openadapt_flow.fleet.feed import CloudPatchFeed, FilePatchFeed, PatchFeed  # noqa: F401
from openadapt_flow.fleet.fingerprint import (  # noqa: F401
    AppVersionFingerprint,
    fingerprint_from_observation,
)
from openadapt_flow.fleet.hub import (  # noqa: F401
    MIN_INDEPENDENT_TENANTS,
    FleetHub,
    HubDecision,
    HubRefused,
    register_fingerprint,
)
from openadapt_flow.fleet.intake import (  # noqa: F401
    ImportResult,
    PatchImportError,
    import_patch,
)
from openadapt_flow.fleet.optin import FleetOptIn, FleetOptInError  # noqa: F401
from openadapt_flow.fleet.patch import (  # noqa: F401
    AnchorMap,
    PatchContribution,
    PatchRedactionError,
    SignedStructuralPatch,
    StructuralAnchorRef,
    StructuralPatchPayload,
    verify_patch,
)
from openadapt_flow.fleet.policy import (  # noqa: F401
    FleetPromotionPolicy,
    PolicyVerificationError,
    PromotionDecision,
    RoleRegistry,
    SignedPromotionPolicy,
    apply_fleet_kill_switches,
    process_imported_patch,
    sign_policy,
    verify_policy,
)
from openadapt_flow.fleet.signing import (  # noqa: F401
    CertificateChain,
    CustodyCeremonyError,
    FleetTrust,
    OfflineRootCustody,
    PatchSignatureError,
    SoftwareKeyCustody,
    issue_signer_certificate,
)
from openadapt_flow.fleet.status import (  # noqa: F401
    FleetStatus,
    read_fleet_status,
)

__all__ = [
    "AnchorMap",
    "AppVersionFingerprint",
    "CertificateChain",
    "CloudPatchFeed",
    "CustodyCeremonyError",
    "ExportResult",
    "FilePatchFeed",
    "FleetHub",
    "FleetOptIn",
    "FleetOptInError",
    "FleetPromotionPolicy",
    "FleetStatus",
    "FleetTrust",
    "HubDecision",
    "HubRefused",
    "ImportResult",
    "MIN_INDEPENDENT_TENANTS",
    "OfflineRootCustody",
    "PatchContribution",
    "PatchExportError",
    "PatchFeed",
    "PatchImportError",
    "PatchRedactionError",
    "PatchSignatureError",
    "PolicyVerificationError",
    "PromotionDecision",
    "QualificationCase",
    "RoleRegistry",
    "SignedPromotionPolicy",
    "SignedStructuralPatch",
    "SoftwareKeyCustody",
    "StructuralAnchorRef",
    "StructuralPatchPayload",
    "TenantCanaryRunner",
    "apply_fleet_kill_switches",
    "canary_patch",
    "export_patch",
    "fingerprint_from_observation",
    "import_patch",
    "issue_signer_certificate",
    "process_imported_patch",
    "read_fleet_status",
    "register_fingerprint",
    "run_qualification_canary",
    "sign_policy",
    "verify_patch",
    "verify_policy",
]
