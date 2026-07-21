"""Opt-in no-DOM HTML5-canvas vision-ladder e2e (noVNC/TigerVNC + Playwright).

Gated by ``OAFLOW_CANVAS_LADDER_E2E=1`` (needs Docker + the flow stack with
cv2/rapidocr + a Playwright chromium on PATH). It builds the
``benchmark/canvas_ladder/fixture`` image, starts the no-DOM canvas surface,
runs the qualification harness, and asserts the validation contract over a
genuine no-accessible-DOM HTML5 ``<canvas>`` (the Citrix Workspace-WEB class,
NOT ICA/HDX -- see ``benchmark/canvas_ladder/README.md``):

  * healthy record->compile->replay succeeds with ZERO model calls, VISUAL
    rungs only (structural never used), the write EFFECT independently confirmed;
  * moderate (legible) drift resolves to the CORRECT target -- never a silent
    WRONG write;
  * severe (illegible) drift HALTS with no write and no model call -- no blind
    coordinate replay.
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
    os.environ.get("OAFLOW_CANVAS_LADDER_E2E") != "1",
    reason="set OAFLOW_CANVAS_LADDER_E2E=1 to run the real no-DOM canvas e2e",
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "benchmark" / "canvas_ladder" / "fixture"
HARNESS = REPO / "benchmark" / "canvas_ladder" / "run_canvas_ladder_qualification.py"
IMAGE = "oaflow-canvas-fixture:test"
CONTAINER = "oaflow-canvas-ladder-test"
PORT = 6080


def _docker(*args: str, check: bool = True, timeout: int = 900):
    return subprocess.run(
        ["docker", *args], check=check, timeout=timeout, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def canvas_fixture():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker not available")
    _docker("build", "-t", IMAGE, str(FIXTURE_DIR))
    _docker("rm", "-f", CONTAINER, check=False)
    _docker(
        "run", "-d", "--name", CONTAINER, "--shm-size=1g", "-p", f"{PORT}:6080", IMAGE
    )
    time.sleep(18)  # TigerVNC + kiosk + noVNC come up
    try:
        yield CONTAINER
    finally:
        _docker("rm", "-f", CONTAINER, check=False)


def test_no_dom_canvas_vision_ladder_contract(canvas_fixture, tmp_path):
    spec = importlib.util.spec_from_file_location("canvas_ladder_harness", HARNESS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["canvas_ladder_harness"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    evidence = mod.run_qualification(
        canvas_fixture, out_dir=tmp_path, base_url="http://localhost", port=PORT
    )

    healthy, moderate, severe = evidence["trials"]
    assert healthy["success"], healthy
    assert healthy["model_calls"] == 0, healthy
    assert healthy["structural_rung_used"] == 0, healthy
    assert healthy["visual_rungs_used"], healthy
    assert healthy["effect_confirmed"], healthy

    assert not moderate["wrong_write"], moderate
    assert moderate["model_calls"] == 0, moderate

    assert severe["halted"], severe
    assert not severe["silent_write"], severe
    assert severe["model_calls"] == 0, severe

    assert evidence["accepted"], evidence
