"""A modal scopes identity to its contents without merging its record rows."""

from __future__ import annotations

import json
from dataclasses import dataclass

import cv2
import numpy as np
import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityCheck,
    Resolution,
    Step,
    Workflow,
)
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.replayer import Replayer

DIALOG = (200, 50, 600, 300)


@dataclass
class Line:
    text: str
    region: tuple[int, int, int, int]
    confidence: float = 0.99


def frame():
    ok, encoded = cv2.imencode(".png", np.zeros((400, 1000, 3), np.uint8))
    assert ok
    return encoded.tobytes()


def resolution(y):
    return Resolution(rung="template", point=(730, y), confidence=1, elapsed_ms=1)


def step(context, *, dialog_row=None):
    return Step(
        id="select",
        intent="select the recorded record",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="unused.png",
            region=(650, 118, 160, 64),
            click_point=(730, 150),
            context_text=context,
            dialog_region=DIALOG,
            **({"dialog_row": dialog_row} if dialog_row is not None else {}),
        ),
    )


class Backend:
    viewport = (1000, 400)

    def __init__(self, census=None):
        self.census = census

    def dialog_region_at(self, x, y):
        return "dialog", DIALOG

    def identity_candidates_at(self, x, y):
        return self.census

    def screenshot(self):
        return frame()


class Vision:
    def __init__(self, lines):
        self.lines = lines

    def ocr(self, png, region=None):
        return self.lines


def check(lines, anchor_step, *, y=150, census=None):
    replayer = Replayer(Backend(census), vision=Vision(lines), poll_interval_s=0)
    return replayer._verify_identity(
        anchor_step, resolution(y), frame(), {}, Workflow(name="dialog")
    )


def test_grok_wrong_row_probe_keeps_the_clicked_row_inside_dialog():
    # Independent regression for the review probe: every visible token is the
    # same when resolving Jane's button one row down to Ann's button.
    lines = [
        Line("Select patient", (220, 60, 160, 20)),
        Line("Jane Lindqvist", (220, 140, 160, 20)),
        Line("Room 7 East", (450, 140, 120, 20)),
        Line("Select", (700, 140, 60, 20)),
        Line("Ann Wuttke", (220, 240, 160, 20)),
        Line("Room 9 West", (450, 240, 120, 20)),
        Line("Select", (700, 240, 60, 20)),
        Line("Cancel", (700, 310, 60, 20)),
    ]
    recorded = identity_mod.context_from_lines(
        lines,
        exclude_region=(650, 118, 160, 64),
        band=DIALOG,
        point=(730, 150),
        scope=DIALOG,
    )
    assert recorded == "Jane Lindqvist Room 7 East"
    assert check(lines, step(recorded)).status == "verified"
    wrong = check(lines, step(recorded), y=250)
    assert wrong.status == "mismatch", wrong
    assert "Ann Wuttke" in wrong.observed
    assert "Jane Lindqvist" not in wrong.observed


def test_legacy_whole_dialog_context_cannot_verify_the_wrong_row():
    # Bundles compiled before row refinement have the entire modal's text.
    # Replaying one must fail closed rather than accept another row's button.
    lines = [
        Line("Select patient", (220, 60, 160, 20)),
        Line("Jane Lindqvist Room 7 East", (220, 140, 350, 20)),
        Line("Select", (700, 140, 60, 20)),
        Line("Ann Wuttke Room 9 West", (220, 240, 350, 20)),
        Line("Select", (700, 240, 60, 20)),
    ]
    legacy_context = identity_mod.context_from_lines(
        lines, exclude_region=(650, 118, 160, 64), band=DIALOG, scope=DIALOG
    )
    assert legacy_context is not None
    wrong = check(lines, step(legacy_context), y=250)
    assert wrong.status != "verified", wrong


def test_dialog_census_stops_identical_rows():
    lines = [Line("Jane Lindqvist Room 7 East", (220, y, 350, 20)) for y in (140, 240)]
    result = check(lines, step("Jane Lindqvist Room 7 East"))
    assert result.status == "abstain"
    assert result.ambiguity_halt == "identical_candidates"
    assert result.candidate_count == 2


def test_dialog_census_ignores_rows_behind_the_modal():
    lines = [
        Line("Jane Lindqvist Room 7 East", (220, 140, 350, 20)),
        Line("Jane Lindqvist Room 7 East", (10, 365, 350, 20)),
    ]
    result = check(lines, step("Jane Lindqvist Room 7 East"))
    assert result.status == "verified"
    assert result.candidate_count == 1


@pytest.mark.parametrize(
    "census",
    [None, {"row": True, "uncountable": True}, {"candidates": [{"self": True}]}],
)
def test_whole_dialog_requires_positive_live_nonrow_census(census):
    result = check(
        [Line("No matching records were found", (220, 90, 350, 20))],
        step("No matching records were found", dialog_row=False),
        census=census,
    )
    assert result.status == "unreadable"


def test_standalone_confirmation_can_read_dialog_result():
    # The dialog as the Chromium test below draws it: its title and its
    # message. The message alone has no identity-bearing token (all lowercase
    # words), and since #1773 such a band abstains wherever it is read.
    lines = [
        Line("Check existing records", (220, 60, 300, 20)),
        Line("No matching records were found", (220, 90, 350, 20)),
    ]
    result = check(
        lines,
        step("Check existing records No matching records were found", dialog_row=False),
        census={"row": False},
    )
    assert result.status == "verified"
    assert result.census_status == "not_a_row"


def test_fresh_look_mask_failure_never_reads_unscoped_frame(monkeypatch):
    replayer = Replayer(Backend(), vision=Vision([]), poll_interval_s=0)
    replayer._identity_dialog_box = DIALOG
    monkeypatch.setattr(identity_mod, "mask_outside", lambda *args: None)
    reads = []
    first = IdentityCheck(
        status="abstain", abstain_reason="glyph_confusable_identifier"
    )
    result = replayer._look_again(
        step("C0X1234 Jane Lindqvist"),
        first,
        None,
        lambda png: reads.append(png) or IdentityCheck(status="verified"),
        {},
        Workflow(name="dialog"),
        None,
        frame(),
    )
    assert reads == []
    assert result.status == "unreadable"
    assert result.look_halt == "identity_dialog_mask_failed"


def test_unset_dialog_row_preserves_old_anchor_serialization():
    assert "dialog_row" not in step("Jane Lindqvist").anchor.model_dump()


@pytest.fixture
def live_dialog_page():
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 500})
            yield page, PlaywrightBackend(page)
        finally:
            browser.close()


def _compile_page(page, backend, target, tmp_path, *, known_nonrow=False):
    box = page.locator(target).bounding_box()
    point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
    status, dialog = backend.dialog_region_at(*point)
    assert status == "dialog"
    png = backend.screenshot()
    recording = tmp_path / "recording"
    (recording / "frames").mkdir(parents=True)
    for suffix in ("before", "after"):
        (recording / "frames" / f"0000_{suffix}.png").write_bytes(png)
    event = {
        "i": 0,
        "kind": "click",
        "x": point[0],
        "y": point[1],
        "t": 1.0,
        "dialog_region": list(dialog),
    }
    if known_nonrow:
        census = backend.identity_candidates_at(*point)
        assert census == {"row": False, "top_level": True}
        event["dialog_row"] = False
    (recording / "events.jsonl").write_text(json.dumps(event) + "\n")
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "dialog-row-regression",
                "created_at": "2026-11-04T00:00:00+00:00",
                "viewport": [1000, 500],
                "params": {},
            }
        )
    )
    workflow = compile_recording(recording, tmp_path / "bundle", name="dialog-row")
    anchor_step = next(s for s in workflow.steps if s.anchor is not None)
    return workflow, anchor_step, point


@pytest.mark.parametrize("duplicate", [False, True])
def test_chromium_compile_and_ocr_refuse_wrong_dialog_row(
    live_dialog_page, tmp_path, duplicate
):
    page, backend = live_dialog_page
    second = "Jane Lindqvist Room 7 East" if duplicate else "Ann Wuttke Room 9 West"
    page.set_content(
        """<style>
      body {font:22px Arial} dialog {width:760px} td {padding:20px} button {font:22px Arial}
      table {width:100%} td:last-child {width:140px;text-align:right}
      </style><p>Jane Lindqvist Room 7 East</p><dialog id=d><h2>Select record</h2><table>
      <tr><td>Jane Lindqvist Room 7 East</td><td><button id=first>Select</button></td></tr>
      <tr><td>"""
        + second
        + """</td><td><button id=second>Select</button></td></tr>
      </table><button>Cancel</button></dialog><script>d.showModal()</script>"""
    )
    workflow, anchor_step, point = _compile_page(page, backend, "#first", tmp_path)
    assert anchor_step.identity_armed is True, anchor_step.identity_unarmed_reason
    assert anchor_step.anchor.dialog_row is True
    box = page.locator("#second").bounding_box()
    wrong_point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
    replayer = Replayer(backend, poll_interval_s=0)
    result = replayer._verify_identity(
        anchor_step,
        Resolution(rung="template", point=wrong_point, confidence=1, elapsed_ms=1),
        backend.screenshot(),
        {},
        workflow,
        tmp_path / "bundle",
    )
    assert result.status in ("mismatch", "abstain"), result
    if duplicate:
        assert result.ambiguity_halt == "identical_candidates"
        assert result.candidate_count == 2
    else:
        correct = replayer._verify_identity(
            anchor_step,
            Resolution(rung="template", point=point, confidence=1, elapsed_ms=1),
            backend.screenshot(),
            {},
            workflow,
            tmp_path / "bundle",
        )
        assert correct.status == "verified", correct


@pytest.mark.parametrize("target", ["#confirm", "#cancel"])
def test_chromium_compile_standalone_buttons_keep_dialog_fallback(
    live_dialog_page, tmp_path, target
):
    page, backend = live_dialog_page
    page.set_content("""<style>body {font:22px Arial} dialog {width:760px}
      button {font:20px Arial;margin:12px}</style><p>County: Emergency Contact:</p>
      <dialog id=d><h2>Check existing records</h2><p>No matching records were found.</p>
      <button id=confirm>Confirm</button><button id=cancel>Cancel</button></dialog>
      <script>d.showModal()</script>""")
    workflow, anchor_step, point = _compile_page(
        page, backend, target, tmp_path, known_nonrow=True
    )
    assert anchor_step.anchor.dialog_row is False
    result = Replayer(backend, poll_interval_s=0)._verify_identity(
        anchor_step,
        Resolution(rung="template", point=point, confidence=1, elapsed_ms=1),
        backend.screenshot(),
        {},
        workflow,
        tmp_path / "bundle",
    )
    assert result.status == "verified", result
    assert result.census_status == "not_a_row"


def test_chromium_short_record_row_never_falls_back_to_whole_dialog(
    live_dialog_page, tmp_path
):
    page, backend = live_dialog_page
    page.set_content("""<style>dialog {width:760px;font:22px Arial} td {padding:20px}
      button {font:22px Arial}</style><dialog id=d><h2>Select existing record</h2><table>
      <tr><td>Ann</td><td><button id=first>Select</button></td></tr>
      <tr><td>Bob</td><td><button>Select</button></td></tr></table></dialog>
      <script>d.showModal()</script>""")
    _, anchor_step, _ = _compile_page(page, backend, "#first", tmp_path)
    assert anchor_step.anchor.dialog_row is True
    assert anchor_step.anchor.context_text is None
    assert anchor_step.anchor.identity_template is None


def test_chromium_structured_census_counts_only_modal_rows(live_dialog_page):
    page, backend = live_dialog_page
    page.set_content("""<table><tr><td>Jane Lindqvist</td><td><button>Select</button></td></tr></table>
      <dialog id=d><table><tr><td>Jane Lindqvist</td><td><button id=first>Select</button></td></tr>
      <tr><td>Jane Lindqvist</td><td><button>Select</button></td></tr></table></dialog>
      <script>d.showModal()</script>""")
    box = page.locator("#first").bounding_box()
    census = backend.identity_candidates_at(
        int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
    )
    assert len(census["candidates"]) == 2, census
