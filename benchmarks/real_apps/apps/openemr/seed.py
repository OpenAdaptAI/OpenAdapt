"""Deterministic synthetic seed and bridge to ``packages/flow/benchmark/openemr_local``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

OPENEMR_LOCAL_DIR = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "flow"
    / "benchmark"
    / "openemr_local"
)


@dataclass(frozen=True)
class SyntheticReferralFixture:
    """Synthetic patient + referral payload for OpenEMR verification."""

    patient_id: str
    family_name: str
    given_name: str
    birth_date: str
    referral_reason: str
    external_ref: str

    def to_fhir_service_request(self) -> dict[str, Any]:
        return {
            "resourceType": "ServiceRequest",
            "id": self.external_ref,
            "status": "active",
            "intent": "order",
            "subject": {"reference": f"Patient/{self.patient_id}"},
            "reasonCode": [{"text": self.referral_reason}],
        }


def default_synthetic_referral() -> SyntheticReferralFixture:
    """Deterministic synthetic referral fixture (no real PHI)."""
    return SyntheticReferralFixture(
        patient_id="syn-pat-1001",
        family_name="Lovelace",
        given_name="Ada",
        birth_date="1985-12-10",
        referral_reason="Synthetic cardiology follow-up",
        external_ref="ref-syn-1001",
    )


def openemr_local_bridge_status() -> dict[str, Any]:
    """Report availability of the existing ``benchmark/openemr_local`` harness."""
    return {
        "bridge_dir": str(OPENEMR_LOCAL_DIR),
        "exists": OPENEMR_LOCAL_DIR.is_dir(),
        "has_fixture_py": (OPENEMR_LOCAL_DIR / "fixture.py").is_file(),
        "has_verify_yaml": (OPENEMR_LOCAL_DIR / "verify.yaml").is_file(),
    }
