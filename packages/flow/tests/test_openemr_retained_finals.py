"""Replay the local-only retained OpenEMR benchmark final frames.

The frames contain synthetic public-demo data and stay outside the repository.
Set ``OPENADAPT_OPENEMR_FINALS_DIR`` when they are mounted somewhere other than
``benchmark/openemr/finals``. A checkout without the retained frames skips this
field-evidence guard; a checkout with them must supply the complete 30-frame
set and reproduce the corrected 19/20 compiled and 10/10 agent result.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from openadapt_flow.benchmark.verify import verify_note_saved

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_RETAINED_FINALS = os.environ.get("OPENADAPT_OPENEMR_FINALS_DIR")
FINALS_DIR = Path(RUN_RETAINED_FINALS or REPO_ROOT / "benchmark" / "openemr" / "finals")
RESULTS_PATH = REPO_ROOT / "benchmark" / "openemr" / "results.json"
RETAINED_FALSE_SUCCESS_SHA256 = (
    "8c504ba15bab9cdca8b5987dd1d1ab7b0ba7ae77f67fac5e93ba8481492ae18f"
)
pytestmark = pytest.mark.skipif(
    not RUN_RETAINED_FINALS,
    reason="set OPENADAPT_OPENEMR_FINALS_DIR to replay retained final frames",
)


def test_retained_final_frames_reproduce_corrected_result() -> None:
    results = json.loads(RESULTS_PATH.read_text())
    rows = [row for arm in ("compiled", "agent") for row in results["runs"][arm]]
    assert len(results["runs"]["compiled"]) == 20
    assert len(results["runs"]["agent"]) == 10
    assert len(rows) == 30

    retained_false_success = FINALS_DIR / "compiled_019.png"
    assert hashlib.sha256(retained_false_success.read_bytes()).hexdigest() == (
        RETAINED_FALSE_SUCCESS_SHA256
    )

    replayed: dict[str, list[bool]] = {"compiled": [], "agent": []}
    changed_from_legacy: list[tuple[str, int]] = []
    for row in rows:
        frame = FINALS_DIR / f"{row['arm']}_{row['i']:03d}.png"
        assert frame.is_file(), f"missing retained final frame: {frame.name}"
        verdict = verify_note_saved(frame.read_bytes(), row["note"])
        replayed[row["arm"]].append(verdict.success)
        assert verdict.success is row["success"], (row["arm"], row["i"], verdict)
        if row.get("legacy_screen_success") is not None:
            assert row["legacy_screen_success"] is not verdict.success
            changed_from_legacy.append((row["arm"], row["i"]))

    assert sum(replayed["compiled"]) == 19
    assert sum(replayed["agent"]) == 10
    assert changed_from_legacy == [("compiled", 19)]
    assert results["arms"]["compiled"]["n"] == 20
    assert results["arms"]["compiled"]["success_count"] == 19
    assert results["arms"]["agent"]["n"] == 10
    assert results["arms"]["agent"]["success_count"] == 10
