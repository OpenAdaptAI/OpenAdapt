"""Fleet drift patches: share a governed UI repair across tenants.

When a common application (a payer portal, an EHR) changes its UI, one
tenant's approved repair can become a signed structural patch that other
tenants on the same application version receive. A patch never activates
itself on arrival:

1. :mod:`~openadapt_flow.fleet.export` turns a repair a person approved into
   a :class:`~openadapt_flow.fleet.patch.SignedStructuralPatch`: old
   structural anchors mapped to new ones, the source bundle digests, and the
   application-version fingerprints (:mod:`~openadapt_flow.fleet.fingerprint`),
   with no text value or PHI.
2. :mod:`~openadapt_flow.fleet.intake` verifies the signature chain on the
   receiving tenant and registers the patched copy of that tenant's own bundle
   as a ``fleet_patch`` candidate in the existing repair lifecycle.
3. :mod:`~openadapt_flow.fleet.canary` runs it on that tenant's own
   qualification cases.
4. :mod:`~openadapt_flow.fleet.policy` promotes it only under that tenant's
   policy; otherwise it waits for a person.

:mod:`~openadapt_flow.fleet.feed` moves patches (a file directory today; a
Cloud feed is an unimplemented interface). :mod:`~openadapt_flow.fleet.simulator`
runs three tenants end to end. See ``docs/FLEET_DRIFT_PATCHES.md``.
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
from openadapt_flow.fleet.intake import (  # noqa: F401
    ImportResult,
    PatchImportError,
    import_patch,
)
from openadapt_flow.fleet.patch import (  # noqa: F401
    AnchorMap,
    PatchRedactionError,
    SignedStructuralPatch,
    StructuralAnchorRef,
    StructuralPatchPayload,
    verify_patch,
)
from openadapt_flow.fleet.policy import (  # noqa: F401
    FleetPromotionPolicy,
    PromotionDecision,
    process_imported_patch,
)
from openadapt_flow.fleet.signing import (  # noqa: F401
    FleetTrust,
    PatchSignatureError,
    SignerCertificate,
    issue_certificate,
)

__all__ = [
    "AnchorMap",
    "AppVersionFingerprint",
    "CloudPatchFeed",
    "ExportResult",
    "FilePatchFeed",
    "FleetPromotionPolicy",
    "FleetTrust",
    "ImportResult",
    "PatchExportError",
    "PatchFeed",
    "PatchImportError",
    "PatchRedactionError",
    "PatchSignatureError",
    "PromotionDecision",
    "QualificationCase",
    "SignedStructuralPatch",
    "SignerCertificate",
    "StructuralAnchorRef",
    "StructuralPatchPayload",
    "TenantCanaryRunner",
    "canary_patch",
    "export_patch",
    "fingerprint_from_observation",
    "import_patch",
    "issue_certificate",
    "process_imported_patch",
    "run_qualification_canary",
    "verify_patch",
]
