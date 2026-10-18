"""Known-ID cross-check on the real replayer path, real render and real OCR.

Same pixel-only setup as ``test_identity_homonym_8th.py``: a record list row
whose identifier carries look-alike glyphs (``AC50061``). Without a known-ID
list the OCR tier abstains on the correct row. With a complete list that has
no look-alike twin it verifies. With the twin ``AC5OO61`` listed, the wrong
row still abstains.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")
pytest.importorskip("rapidocr_onnxruntime")

import openadapt_flow.vision as vision  # noqa: E402
from openadapt_flow.ir import Anchor, Resolution, Step, Workflow  # noqa: E402
from openadapt_flow.runtime.known_ids import KnownIdList  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from tests.test_identity_homonym_8th import (  # noqa: E402
    _ROW_K,
    _PixelOnlyBackend,
    _processed_context,
    _render,
)


def _check(recorded_mrn: str, live_mrn: str, known_ids):
    rec = _render(recorded_mrn)
    live = _render(live_mrn)
    _, open_pt, _, _ = rec.points[_ROW_K]
    _, live_pt, _, _ = live.points[_ROW_K]
    anchor = Anchor(
        template="t.png",
        region=(open_pt[0] - 30, open_pt[1] - 12, 60, 24),
        click_point=(open_pt[0], open_pt[1]),
        context_text=_processed_context(rec, _ROW_K),
        identifier_crop=None,
    )
    step = Step(
        id="open_record",
        intent="open record",
        action="click",
        anchor=anchor,
        risk="irreversible",
    )
    wf = Workflow(name="known_ids", params={}, steps=[step])
    res = Resolution(rung="ocr", point=live_pt, confidence=0.9, elapsed_ms=1.0)
    replayer = Replayer(
        _PixelOnlyBackend(rec.viewport, live.png), vision=vision, known_ids=known_ids
    )
    return replayer._verify_identity(step, res, live.png, {}, wf, bundle_dir=None)


def _ids(*extra: str) -> KnownIdList:
    fillers = [f"ZZ{1000 + i}" for i in range(4)]
    return KnownIdList(ids=("AC50061", *fillers, *extra), source="test", complete=True)


def test_correct_row_abstains_without_list():
    assert _check("AC50061", "AC50061", None).status == "abstain"


def test_correct_row_verifies_with_complete_list():
    check = _check("AC50061", "AC50061", _ids())
    assert check.status == "verified"
    assert check.known_id_evidence is not None
    assert check.known_id_evidence.reads == 2


def test_wrong_row_still_abstains_when_twin_listed():
    check = _check("AC50061", "AC5OO61", _ids("AC5OO61"))
    assert check.status == "abstain"
    assert check.known_id_evidence is None
