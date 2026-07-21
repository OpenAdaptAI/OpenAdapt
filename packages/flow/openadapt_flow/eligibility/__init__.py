"""Governed API-first 270/271 eligibility mechanism.

The client, exact route-binding schema, qualifier-aware parser, and atomic
practice-local artifact boundary are public.  Practice payer maps and account
credentials are deployment data.  Transient API failures can use a reviewed
portal fallback; identity, configuration, ambiguity, and forbidden-portal
outcomes enter the attended queue instead of being guessed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.eligibility.artifact import (
        ArtifactEncryption,
        EligibilityArtifact,
        PracticeArtifactPolicy,
        purge_expired_eligibility_artifacts,
        write_and_verify,
        write_eligibility_artifacts,
    )
    from openadapt_flow.eligibility.client import (
        ApplicationMode,
        BenefitSelection,
        EligibilityRequest,
        EligibilityResult,
        EligibilityStatus,
        ErrorCategory,
        QualifiedBenefit,
        RetryDisposition,
        StediAccountBoundary,
        StediEligibilityClient,
        eligibility_request_sha256,
    )
    from openadapt_flow.eligibility.waterfall import (
        EligibilityRoute,
        PayerCapability,
        RouteDecision,
        load_payer_routes,
        resolve_route,
        run_waterfall,
    )

__all__ = [
    "ApplicationMode",
    "ArtifactEncryption",
    "BenefitSelection",
    "EligibilityArtifact",
    "EligibilityRequest",
    "EligibilityResult",
    "EligibilityRoute",
    "EligibilityStatus",
    "ErrorCategory",
    "PayerCapability",
    "PracticeArtifactPolicy",
    "QualifiedBenefit",
    "RetryDisposition",
    "RouteDecision",
    "StediAccountBoundary",
    "StediEligibilityClient",
    "eligibility_request_sha256",
    "load_payer_routes",
    "purge_expired_eligibility_artifacts",
    "resolve_route",
    "run_waterfall",
    "write_and_verify",
    "write_eligibility_artifacts",
]

_CLIENT = (
    "ApplicationMode",
    "BenefitSelection",
    "EligibilityRequest",
    "EligibilityResult",
    "EligibilityStatus",
    "ErrorCategory",
    "QualifiedBenefit",
    "RetryDisposition",
    "StediAccountBoundary",
    "StediEligibilityClient",
    "eligibility_request_sha256",
)
_WATERFALL = (
    "EligibilityRoute",
    "PayerCapability",
    "RouteDecision",
    "load_payer_routes",
    "resolve_route",
    "run_waterfall",
)
_ARTIFACT = (
    "ArtifactEncryption",
    "EligibilityArtifact",
    "PracticeArtifactPolicy",
    "purge_expired_eligibility_artifacts",
    "write_and_verify",
    "write_eligibility_artifacts",
)


def __getattr__(name: str) -> object:
    """Lazy re-exports (PEP 562) -- importing the package stays cheap."""
    if name in _CLIENT:
        from openadapt_flow.eligibility import client

        return getattr(client, name)
    if name in _WATERFALL:
        from openadapt_flow.eligibility import waterfall

        return getattr(waterfall, name)
    if name in _ARTIFACT:
        from openadapt_flow.eligibility import artifact

        return getattr(artifact, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
