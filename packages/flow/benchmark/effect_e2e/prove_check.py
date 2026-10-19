"""The effect_e2e record service as a fixture for proving a proposed check.

``openadapt-flow scaffold-verifier --prove effect-e2e`` runs a proposed record
check through the real replayer against this service, one run per fault in
:data:`benchmark.effect_e2e.run.SCENARIOS`, judged by this harness's
independent ground truth (a read-only connection to the SQLite file that never
sees the check). See :mod:`openadapt_flow.record_check_proof`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import requests

from benchmark.effect_e2e import ground_truth
from benchmark.effect_e2e.record_service import TARGET_PATIENT, TARGET_TYPE, serve
from benchmark.effect_e2e.run import SCENARIOS
from openadapt_flow.record_check_proof import Fixture


class _Handle:
    def __init__(
        self, base_url: str, db_path: Path, tmp: tempfile.TemporaryDirectory
    ) -> None:
        self.base_url = base_url
        self.db_path = db_path
        self.tmp = tmp


def _serve() -> tuple[str, Optional[Path], Callable[[], None], Any]:
    tmp = tempfile.TemporaryDirectory(prefix="record-check-proof-")
    db_path = Path(tmp.name) / "record.db"
    service = serve(db_path)
    handle = _Handle(service.base_url, db_path, tmp)

    def stop() -> None:
        service.stop()
        tmp.cleanup()

    return service.base_url, db_path, stop, handle


def _reset(handle: _Handle, seed: bool) -> None:
    requests.post(
        f"{handle.base_url}/api/reset", json={"seed_concurrent": seed}, timeout=5.0
    )


def _judge(
    before: ground_truth.Snapshot,
    after: ground_truth.Snapshot,
    params: Mapping[str, str],
) -> tuple[bool, str]:
    verdict = ground_truth.judge(before, after, intended_note=params.get("note", ""))
    return verdict.correct, verdict.fault_class


FIXTURE = Fixture(
    name="effect-e2e",
    serve=_serve,
    reset=_reset,
    snapshot=lambda handle: ground_truth.capture(handle.db_path),
    judge=_judge,
    scenarios=tuple((sc.name, sc.seed_concurrent) for sc in SCENARIOS),
    write_path="/api/encounter",
    default_body={
        "patient_id": TARGET_PATIENT,
        "type": TARGET_TYPE,
        "note": "{note}",
    },
)
