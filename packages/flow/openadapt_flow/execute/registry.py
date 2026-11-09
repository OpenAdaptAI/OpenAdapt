"""Directory-backed admitted-bundle registry, pinned by workflow digest."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.execute.models import AdmittedBundle

# Well-known MockMed / tutorial admissions used by tests and ``--seed-mockmed``.
MOCKMED_QUALIFICATION_ID = "qualification_mockmed01"
MOCKMED_WORKFLOW_VERSION = "workflow_tutorial1"
MOCKMED_WORKFLOW_DIGEST = "sha256:" + "c" * 64
MOCKMED_ENVIRONMENT_OK = "environment_mockmed_ok"
MOCKMED_ENVIRONMENT_LIE = "environment_mockmed_lie"
MOCKMED_EFFECT_STRENGTH = "independent_system_of_record"


class AdmissionError(ValueError):
    """The request does not match an admitted bundle exactly."""


def admissions_dir(data_dir: Path) -> Path:
    return data_dir / "admissions"


def load_admissions(data_dir: Path) -> tuple[AdmittedBundle, ...]:
    directory = admissions_dir(data_dir)
    if not directory.is_dir():
        return ()
    loaded: list[AdmittedBundle] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        loaded.append(AdmittedBundle.model_validate(payload))
    return tuple(loaded)


def lookup_admission(
    data_dir: Path,
    *,
    qualification_id: str,
    workflow_version: str,
    workflow_digest: str,
    environment_id: str,
    minimum_effect_strength: str,
) -> AdmittedBundle:
    """Return the unique admission that matches every binding field."""

    matches = [
        item
        for item in load_admissions(data_dir)
        if item.qualification_id == qualification_id
        and item.workflow_version == workflow_version
        and item.workflow_digest == workflow_digest
        and item.environment_id == environment_id
        and item.minimum_effect_strength == minimum_effect_strength
    ]
    if not matches:
        raise AdmissionError(
            "request does not match an admitted bundle exactly "
            "(qualification_id, workflow_version, workflow_digest, "
            "environment_id, minimum_effect_strength)"
        )
    if len(matches) > 1:
        raise AdmissionError("multiple admitted bundles match this request")
    return matches[0]


def write_admission(data_dir: Path, admission: AdmittedBundle) -> Path:
    directory = admissions_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    slug = f"{admission.qualification_id}__{admission.environment_id}.json"
    path = directory / slug
    path.write_text(
        admission.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def seed_mockmed_admissions(data_dir: Path) -> tuple[AdmittedBundle, AdmittedBundle]:
    """Write the synthetic MockMed ok + banner-lie admissions."""

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
    write_admission(data_dir, honest)
    write_admission(data_dir, lie)
    return honest, lie
