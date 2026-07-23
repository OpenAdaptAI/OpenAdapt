"""Opt-in real-RDP vision-ladder e2e (Docker FreeRDP round-trip).

Gated by ``OAFLOW_DOCKER_RDP_E2E=1`` (needs Docker + the flow stack with
cv2/rapidocr on PATH). It builds the ``benchmark/rdp_ladder/fixture`` image,
starts the RDP round-trip, runs the qualification harness, and asserts the
validation contract on a real RDP pixel surface:

  * healthy record->compile->replay succeeds with ZERO model calls,
  * resolution used the VISUAL rungs and NEVER the structural rung,
  * the write EFFECT is independently confirmed,
  * the ladder HALTS (no silent write, no model call) under injected drift.

This is the CI-viable Linux analog of the RDP transport proof in
``benchmark/rdp`` (which needs a real Windows target for the aardwolf transport).
See ``benchmark/rdp_ladder/README.md`` for why FreeRDP (not aardwolf) is used.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OAFLOW_DOCKER_RDP_E2E") != "1",
    reason="set OAFLOW_DOCKER_RDP_E2E=1 to run the real Docker-RDP e2e",
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "benchmark" / "rdp_ladder" / "fixture"
HARNESS = REPO / "benchmark" / "rdp_ladder" / "run_rdp_ladder_qualification.py"
IMAGE = "oaflow-rdp-fixture:test"
CONTAINER = "oaflow-rdp-ladder-test"


def _docker(*args: str, check: bool = True, timeout: int = 900):
    return subprocess.run(
        ["docker", *args], check=check, timeout=timeout, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def rdp_fixture(tmp_path_factory):
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker not available")
    oracle_root = tmp_path_factory.mktemp("rdp-ladder-oracle")
    _docker("build", "-t", IMAGE, str(FIXTURE_DIR))
    _docker("rm", "-f", CONTAINER, check=False)
    _docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "--shm-size=1g",
        "-e",
        "RDP_FIXTURE_ORACLE_ROOT=/oracle",
        "-v",
        f"{oracle_root}:/oracle",
        IMAGE,
    )
    time.sleep(22)  # kiosk + shadow server + client come up
    try:
        yield CONTAINER, oracle_root
    finally:
        _docker("rm", "-f", CONTAINER, check=False)


def test_real_rdp_vision_ladder_contract(rdp_fixture, tmp_path):
    container, oracle_root = rdp_fixture
    spec = importlib.util.spec_from_file_location("rdp_ladder_harness", HARNESS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rdp_ladder_harness"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    candidate_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
    ).strip()
    base_commit = subprocess.check_output(
        ["git", "merge-base", candidate_commit, "origin/main"], cwd=REPO, text=True
    ).strip()

    evidence = mod.run_qualification(
        container,
        out_dir=tmp_path,
        oracle_root=oracle_root,
        work_dir=tmp_path / "work",
        candidate_commit=candidate_commit,
        base_commit=base_commit,
    )

    healthy = [
        trial
        for trial in evidence["trials"]
        if trial["kind"] == "healthy_record_compile_replay"
    ]
    drift = [
        trial
        for trial in evidence["trials"]
        if trial["kind"] == "halt_under_injected_drift"
    ]
    assert len(healthy) == 3, healthy
    assert len(drift) == 3, drift
    assert all(trial["success"] for trial in healthy), healthy
    assert all(trial["model_calls"] == 0 for trial in healthy), healthy
    assert all(trial["structural_rung_used"] == 0 for trial in healthy), healthy
    assert all(trial["visual_rungs_used"] for trial in healthy), healthy
    assert all(trial["effect_confirmed"] for trial in healthy), healthy
    assert all(trial["runtime_effect_verified"] for trial in healthy), healthy
    assert all(trial["policy_admitted"] for trial in healthy), healthy
    assert all(trial["identity_verified"] for trial in healthy), healthy
    assert not any(trial["silent_incorrect_success"] for trial in healthy)
    assert not any(trial["over_halt"] for trial in healthy)
    assert all(trial["halted"] for trial in drift), drift
    assert not any(trial["silent_write"] for trial in drift), drift
    assert not any(trial["false_completion"] for trial in drift), drift
    assert all(trial["model_calls"] == 0 for trial in drift), drift
    assert all(trial["policy_bound"] for trial in drift), drift
    assert evidence["schema_version"] == mod.RESULT_SCHEMA
    assert evidence["candidate_commit"] == candidate_commit
    assert evidence["base_commit"] == base_commit
    assert evidence["accepted"], evidence
