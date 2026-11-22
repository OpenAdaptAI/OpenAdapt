"""Synthetic MockMed demo admissions for ``serve-execute --seed-mockmed``."""

from __future__ import annotations

from pathlib import Path

from openadapt_flow.execute.models import AdmittedBundle
from openadapt_flow.execute.registry import seed_admissions

# Well-known MockMed / tutorial admissions used by tests and ``--seed-mockmed``.
MOCKMED_QUALIFICATION_ID = "qualification_mockmed01"
MOCKMED_WORKFLOW_VERSION = "workflow_tutorial1"
MOCKMED_WORKFLOW_DIGEST = "sha256:" + "c" * 64
MOCKMED_ENVIRONMENT_OK = "environment_mockmed_ok"
MOCKMED_ENVIRONMENT_LIE = "environment_mockmed_lie"
MOCKMED_EFFECT_STRENGTH = "independent_system_of_record"


def demo_admissions() -> tuple[AdmittedBundle, AdmittedBundle]:
    """Return the synthetic MockMed ok + banner-lie admissions."""

    honest = AdmittedBundle(
        qualification_id=MOCKMED_QUALIFICATION_ID,
        workflow_version=MOCKMED_WORKFLOW_VERSION,
        workflow_digest=MOCKMED_WORKFLOW_DIGEST,
        environment_id=MOCKMED_ENVIRONMENT_OK,
        minimum_effect_strength=MOCKMED_EFFECT_STRENGTH,
        synthetic=True,
        break_it=False,
    )
    lie = AdmittedBundle(
        qualification_id=MOCKMED_QUALIFICATION_ID,
        workflow_version=MOCKMED_WORKFLOW_VERSION,
        workflow_digest=MOCKMED_WORKFLOW_DIGEST,
        environment_id=MOCKMED_ENVIRONMENT_LIE,
        minimum_effect_strength=MOCKMED_EFFECT_STRENGTH,
        synthetic=True,
        break_it=True,
    )
    return honest, lie


mockmed_admissions = demo_admissions


def seed_mockmed_admissions(data_dir: Path) -> tuple[AdmittedBundle, AdmittedBundle]:
    """Write the synthetic MockMed ok + banner-lie admissions."""

    honest, lie = mockmed_admissions()
    seed_admissions(data_dir, (honest, lie))
    return honest, lie
