"""Known-ID cross-check on the real replayer path, real render and real OCR.

Same pixel-only setup as ``test_identity_homonym_8th.py``: a record list row
whose identifier carries look-alike glyphs (``AC50061``). Without a known-ID
list the OCR tier abstains on the correct row. With a complete list that has
no look-alike twin it verifies, using a second read from a fresh screenshot.
With the twin ``AC5OO61`` listed, the wrong row still abstains. A confirmed
list that went stale or changed halts the step with a named reason.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")
pytest.importorskip("rapidocr_onnxruntime")

from datetime import datetime, timedelta, timezone  # noqa: E402

import openadapt_flow.vision as vision  # noqa: E402
from openadapt_flow.ir import Anchor, Resolution, Step, Workflow  # noqa: E402
from openadapt_flow.runtime.known_ids import (  # noqa: E402
    KnownIdAdmission,
    KnownIdList,
    confirm_export,
)
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from tests.test_identity_homonym_8th import (  # noqa: E402
    _ROW_K,
    _PixelOnlyBackend,
    _processed_context,
    _render,
)


class _CountingBackend(_PixelOnlyBackend):
    """Counts captures; can serve a different frame on the fresh capture."""

    def __init__(self, viewport, live_png: bytes, fresh_png=None) -> None:
        super().__init__(viewport, live_png)
        self._fresh = fresh_png
        self.captures = 0

    def screenshot(self) -> bytes:
        self.captures += 1
        return self._fresh if self._fresh is not None else self._live_png


def _check(recorded_mrn: str, live_mrn: str, known_ids, fresh_mrn=None, out=None):
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
    backend = _CountingBackend(
        rec.viewport, live.png, _render(fresh_mrn).png if fresh_mrn else None
    )
    replayer = Replayer(backend, vision=vision, known_ids=known_ids)
    check = replayer._verify_identity(step, res, live.png, {}, wf, bundle_dir=None)
    if out is not None:
        out["captures"] = backend.captures
    return check


def _ids(*extra: str) -> KnownIdList:
    fillers = [f"ZZ{1000 + i}" for i in range(4)]
    return KnownIdList(ids=("AC50061", *fillers, *extra), source="test", complete=True)


def test_correct_row_abstains_without_list():
    assert _check("AC50061", "AC50061", None).status == "abstain"


def test_correct_row_verifies_with_complete_list_using_a_fresh_capture():
    out: dict = {}
    check = _check("AC50061", "AC50061", _ids(), out=out)
    assert check.status == "verified"
    assert check.known_id_evidence is not None
    assert check.known_id_evidence.reads == 2
    assert check.known_id_evidence.frames == 2
    assert out["captures"] == 1  # the second read took its own screenshot


def test_fresh_capture_showing_another_record_blocks_the_lift():
    # The screen changed between the two captures (a different row is now
    # under the target). The fresh read names another record, and a mismatch
    # is final: nothing lifts it.
    check = _check("AC50061", "AC50061", _ids(), fresh_mrn="ZZ9999")
    assert check.status == "mismatch"
    assert check.known_id_evidence is None


def test_wrong_row_still_abstains_when_twin_listed():
    check = _check("AC50061", "AC5OO61", _ids("AC5OO61"))
    assert check.status == "abstain"
    assert check.known_id_evidence is None


def _admission(tmp_path, *, exported_at, max_age_s=24 * 3600.0):
    export = tmp_path / "ids.txt"
    export.write_text("\n".join(["AC50061", "ZZ1000", "ZZ1001"]) + "\n")
    confirm_export(
        export, source="test export", confirmed_by="op", exported_at=exported_at
    )
    return export, KnownIdAdmission.load(export, max_age_s=max_age_s)


def test_confirmed_admission_lifts_and_records_the_confirmation(tmp_path):
    now = datetime.now(timezone.utc)
    _, adm = _admission(tmp_path, exported_at=now)
    check = _check("AC50061", "AC50061", adm)
    assert check.status == "verified"
    ev = check.known_id_evidence
    assert ev is not None and ev.export_sha256 == adm.confirmation.sha256
    assert ev.confirmed_at == adm.confirmation.confirmed_at


def test_list_changed_mid_run_halts_with_named_reason(tmp_path):
    now = datetime.now(timezone.utc)
    export, adm = _admission(tmp_path, exported_at=now)
    export.write_text("AC50061\nAC5OO61\n")
    check = _check("AC50061", "AC50061", adm)
    assert check.status == "abstain"
    assert check.known_id_halt == "known_ids_changed"


def test_list_gone_stale_mid_run_halts_with_named_reason(tmp_path):
    now = datetime.now(timezone.utc)
    _, adm = _admission(tmp_path, exported_at=now - timedelta(hours=2))
    adm.max_age_s = 3600.0
    check = _check("AC50061", "AC50061", adm)
    assert check.known_id_halt == "known_ids_stale"


def test_identity_gate_halts_reversible_step_with_named_reason(monkeypatch, tmp_path):
    from openadapt_flow.ir import IdentityCheck, StepResult

    anchor = Anchor(
        template="t.png",
        region=(0, 0, 10, 10),
        click_point=(5, 5),
        context_text="Smith AC50061",
    )
    step = Step(id="open_record", intent="open record", action="click", anchor=anchor)
    wf = Workflow(name="known_ids", params={}, steps=[step])
    res = Resolution(rung="ocr", point=(5, 5), confidence=0.9, elapsed_ms=1.0)
    replayer = Replayer(_PixelOnlyBackend((100, 100), b""), vision=vision)
    monkeypatch.setattr(
        replayer,
        "_verify_identity",
        lambda *a, **k: IdentityCheck(
            status="abstain",
            abstain_reason="glyph_confusable_identifier",
            known_id_halt="known_ids_stale",
        ),
    )
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    error = replayer._identity_gate_error(step, res, b"", {}, wf, tmp_path, result)
    assert error is not None and "(known_ids_stale)" in error
    assert result.safety_halt is True
