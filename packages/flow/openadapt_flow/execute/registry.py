"""Directory-backed admitted-bundle registry, pinned by workflow digest."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from openadapt_flow.execute.models import AdmittedBundle


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


def seed_admissions(
    data_dir: Path, admissions: Iterable[AdmittedBundle]
) -> tuple[AdmittedBundle, ...]:
    """Write caller-supplied admissions into the registry directory."""

    seeded = tuple(admissions)
    for admission in seeded:
        write_admission(data_dir, admission)
    return seeded
