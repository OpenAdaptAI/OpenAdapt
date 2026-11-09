"""Fresh nonclinical development cases for repeated confirmation checks.

Compile-time OCR is controlled for the rendered recording's whole frames.
Runtime evaluation uses production RapidOCR, template matching and pHash.
This is a compiler/evaluator test, not a full replay or an application run.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

import openadapt_flow.vision as vision
from openadapt_flow.compiler import compile_recording
from openadapt_flow.compiler.compile import _new_text_postcondition
from openadapt_flow.ir import PostconditionKind
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision.ocr import OcrLine

NEW_VALUES = ("BATCH-CEDAR7", "GROUP-THETA9")
OLD_VALUES = ("ITEM-LIMA3", "COHORT-MORSE5")


def _line(text, x, y, width=360, height=32):
    return OcrLine(text=text, region=(x, y, width, height), confidence=0.99)


def _row(values, y=170):
    return [_line(value, x, y) for value, x in zip(values, (80, 520))]


@pytest.mark.parametrize("label", ["stored entry:", "confirmation:"])
@pytest.mark.parametrize("prior_row_y", [170, 110])
def test_repeated_confirmation_on_existing_row_keeps_positive_text(label, prior_row_y):
    prior = _line(f"{label} {' '.join(OLD_VALUES)}", 70, 560, 1100)
    current = _line(f"{label} {' '.join(NEW_VALUES)}", 70, 560, 1100)
    before = _row(NEW_VALUES, prior_row_y) + [prior]
    after = _row(NEW_VALUES) + [current]
    check = _new_text_postcondition(before, after, click_point=(1050, 455))
    assert check is not None and check.text == current.text


def test_unknown_click_does_not_establish_automatic_selection():
    prior = _line(f"entry: {' '.join(OLD_VALUES)}", 70, 560, 1100)
    current = _line(f"entry: {' '.join(NEW_VALUES)}", 70, 560, 1100)
    check = _new_text_postcondition([prior], _row(NEW_VALUES) + [current])
    assert check is not None and check.text == current.text


def test_partial_row_match_cannot_explain_an_additional_changed_value():
    prior = _line(f"entry: {' '.join(OLD_VALUES)} TRACK-IOTA4", 70, 560, 1100)
    current = _line(f"entry: {' '.join(NEW_VALUES)} TRACK-KAPPA6", 70, 560, 1100)
    check = _new_text_postcondition(
        [prior], _row(NEW_VALUES) + [current], click_point=(1050, 455)
    )
    assert check is not None and check.text == current.text


def test_new_row_without_selection_proof_keeps_its_detail_check():
    prior = _line(f"entry: {' '.join(OLD_VALUES)}", 70, 560, 1100)
    current = _line(f"entry: {' '.join(NEW_VALUES)}", 70, 560, 1100)
    before = _row(OLD_VALUES) + [prior]
    after = _row(NEW_VALUES) + [current]
    check = _new_text_postcondition(before, after, click_point=(1050, 455))
    assert check is not None and check.text == current.text


def _render(lines):
    image = np.full((720, 1280, 3), 245, dtype=np.uint8)
    cv2.rectangle(image, (990, 425), (1160, 485), (215, 215, 215), -1)
    cv2.rectangle(image, (990, 425), (1160, 485), (50, 50, 50), 2)
    for line in lines:
        x, y, _, height = line.region
        cv2.putText(
            image,
            line.text,
            (x, y + height - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def test_full_compiler_and_actual_ocr_reject_unchanged_confirmation(
    tmp_path, monkeypatch
):
    compiler = importlib.import_module("openadapt_flow.compiler.compile")
    prior = _line(f"stored entry: {' '.join(OLD_VALUES)}", 70, 560, 1100)
    current = _line(f"stored entry: {' '.join(NEW_VALUES)}", 70, 560, 1100)
    button = _line("Store", 1015, 435, 100, 35)
    before_lines = _row(NEW_VALUES) + [prior, button]
    after_lines = _row(NEW_VALUES) + [current, button]
    stale = _render(before_lines)
    saved = _render(after_lines)
    observations = {stale: before_lines, saved: after_lines}
    real_compile_ocr = compiler.ocr

    def controlled_compile_ocr(png, region=None):
        lines = observations.get(png)
        if lines is None:
            return real_compile_ocr(png, region=region)
        if region is None:
            return lines
        x, y, width, height = region
        return [
            line
            for line in lines
            if x < line.region[0] + line.region[2]
            and line.region[0] < x + width
            and y < line.region[1] + line.region[3]
            and line.region[1] < y + height
        ]

    monkeypatch.setattr(compiler, "ocr", controlled_compile_ocr)
    recording = tmp_path / "recording"
    (recording / "frames").mkdir(parents=True)
    (recording / "frames/0000_before.png").write_bytes(stale)
    (recording / "frames/0000_after.png").write_bytes(saved)
    (recording / "events.jsonl").write_text(
        json.dumps(
            {
                "i": 0,
                "kind": "click",
                "x": 1050,
                "y": 455,
                "t": 1.0,
            }
        )
        + "\n"
    )
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "synthetic-inventory-confirmation",
                "created_at": "2026-11-05T02:00:00+00:00",
                "viewport": [1280, 720],
                "params": {},
            }
        )
    )
    bundle = tmp_path / "bundle"
    workflow = compile_recording(recording, bundle, name="inventory-confirmation")
    evaluator = Replayer.__new__(Replayer)
    evaluator.backend = object()
    evaluator.vision = vision
    evaluator.governed_authorization = None
    evaluator._pc_page_scale = None
    assert evaluator.vision.ocr is not controlled_compile_ocr
    checks = workflow.steps[0].expect
    stale_results = [
        evaluator._postcondition_passes(check, stale, bundle, {}) for check in checks
    ]
    saved_results = [
        evaluator._postcondition_passes(check, saved, bundle, {}) for check in checks
    ]
    observed_stale = [line.text for line in vision.ocr(stale)]
    observed_saved = [line.text for line in vision.ocr(saved)]
    report = {
        "before_sha256": hashlib.sha256(stale).hexdigest(),
        "after_sha256": hashlib.sha256(saved).hexdigest(),
        "checks": [check.model_dump(mode="json") for check in checks],
        "stale_results": stale_results,
        "saved_results": saved_results,
        "actual_stale_ocr": observed_stale,
        "actual_saved_ocr": observed_saved,
    }
    print(json.dumps(report, sort_keys=True))
    if destination := os.environ.get("OA1734_EVIDENCE_DIR"):
        evidence = Path(destination)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "before.png").write_bytes(stale)
        (evidence / "after.png").write_bytes(saved)
        (evidence / "runtime-observations.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        for check in checks:
            if check.template:
                target = evidence / check.template
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((bundle / check.template).read_bytes())
    assert all(saved_results), (
        "the valid confirmation must pass every unchanged runtime check"
    )
    assert any(
        check.kind is PostconditionKind.TEXT_PRESENT and check.text == current.text
        for check in checks
    ), "the repeated confirmation lost its positive text check"
    assert not all(stale_results), (
        "an unchanged screen must not satisfy the compiled save checks"
    )
