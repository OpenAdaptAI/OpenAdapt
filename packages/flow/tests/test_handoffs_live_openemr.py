"""Referral intake read back from a real OpenEMR receiver (synthetic).

Skipped unless the receiver's variables are set (see
``benchmark/handoff_receiver/README.md``); normal CI never reaches it. The
unit tests in ``test_handoffs.py`` cover the same logic with fakes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HANDOFF_RECEIVER_FHIR_BASE"),
    reason="needs the synthetic OpenEMR receiver (HANDOFF_RECEIVER_*)",
)


def test_every_referral_case_against_the_real_receiver(tmp_path: Path) -> None:
    from benchmark.handoff_receiver.referral_cases import run_cases, summarize

    repeat = int(os.environ.get("HANDOFF_RECEIVER_REPEAT", "5"))
    results = run_cases(tmp_path, repeat=repeat)
    summary = summarize(results)
    out = os.environ.get("HANDOFF_RESULTS_OUT")
    if out:
        Path(out).write_text(json.dumps(summary, indent=2) + "\n")
    wrong = [r for r in results if not r.ok]
    assert not wrong, [(r.case, r.state, r.reason, r.differing) for r in wrong]
    # No false alarm: every correctly recorded referral reads as intended.
    for case in ("intended", "benign_format"):
        assert summary["cases"][case]["as_expected"] == repeat
