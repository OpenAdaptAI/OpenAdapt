"""Unit tests for openadapt_flow.compiler.

Builds a synthetic recording directory programmatically (numpy/cv2 frames,
hand-written events.jsonl/meta.json — no Agent A code) and compiles it.
"""

from __future__ import annotations

import ast
import difflib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from openadapt_flow.compiler import compile_recording, render_workflow_py
from openadapt_flow.ir import ActionKind, PostconditionKind, Workflow
from openadapt_flow.runtime.identity_template import token_in_template
from openadapt_flow.vision.ocr import normalize_text

VIEWPORT = (1280, 800)
NOTE_VALUE = "confidential follow up note"
BANNER_TASKS = "Referral Tasks Loaded"
BANNER_SAVED = "Encounter Saved Successfully"


def blank() -> np.ndarray:
    return np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, dtype=np.uint8)


def draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
    cv2.rectangle(img, (x, y), (x + w, y + h), (205, 205, 205), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 70), 2)
    cv2.putText(
        img,
        label,
        (x + 12, y + h // 2 + 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def draw_text(img: np.ndarray, x: int, y: int, text: str) -> None:
    cv2.putText(
        img,
        text,
        (x, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def write_frame(recording: Path, i: int, suffix: str, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    (recording / "frames" / f"{i:04d}_{suffix}.png").write_bytes(buf.tobytes())


def fuzzy_eq(a: str, b: str, min_ratio: float = 0.7) -> bool:
    return (
        difflib.SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()
        >= min_ratio
    )


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory):
    """Build a synthetic 4-event recording, compile it, return everything."""
    recording = tmp_path_factory.mktemp("recording")
    bundle = tmp_path_factory.mktemp("bundle")
    (recording / "frames").mkdir()

    # -- screens ------------------------------------------------------------
    # login: centered title (landmark) + Sign In button (click target)
    login = blank()
    draw_text(login, 540, 84, "MockMed Portal")
    draw_button(login, 560, 400, 160, 48, "Sign In")
    # corner button for the clamping test (used by event 3's before frame)
    draw_button(login, 0, 0, 110, 40, "Menu")

    # tasks screen = login + a new banner (small localized change)
    tasks = login.copy()
    draw_text(tasks, 420, 244, BANNER_TASKS)

    # typed: tasks + the (parameterized) note text rendered
    typed = tasks.copy()
    draw_text(typed, 420, 320, NOTE_VALUE)

    # saved: typed + a saved banner
    saved = typed.copy()
    draw_text(saved, 380, 560, BANNER_SAVED)

    # -- events ---------------------------------------------------------------
    click_xy = (640, 424)  # center of the Sign In button
    events = [
        {"i": 0, "kind": "click", "x": click_xy[0], "y": click_xy[1], "t": 1.0},
        {"i": 1, "kind": "type", "text": NOTE_VALUE, "param": "note", "t": 2.0},
        {"i": 2, "kind": "key", "key": "Enter", "t": 3.0},
        {"i": 3, "kind": "click", "x": 10, "y": 12, "t": 4.0},
    ]
    frames = {
        0: (login, tasks),
        1: (tasks, typed),
        2: (typed, saved),
        3: (saved, saved),  # no visual change
    }
    for i, (before, after) in frames.items():
        write_frame(recording, i, "before", before)
        write_frame(recording, i, "after", after)
    (recording / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-synthetic-001",
                "created_at": "2026-07-06T00:00:00+00:00",
                "viewport": list(VIEWPORT),
                "app_url": "http://localhost:0/",
                "params": {"note": NOTE_VALUE},
            }
        )
    )

    workflow = compile_recording(recording, bundle, name="triage-demo")
    return {
        "workflow": workflow,
        "bundle": bundle,
        "recording": recording,
        "login": login,
        "saved": saved,
    }


class TestCompileRecording:
    def test_incomplete_frame_pair_fails_loud(self, tmp_path: Path) -> None:
        recording = tmp_path / "recording"
        (recording / "frames").mkdir(parents=True)
        write_frame(recording, 0, "before", blank())
        (recording / "events.jsonl").write_text(
            json.dumps({"i": 0, "kind": "key", "key": "Enter", "t": 1.0}) + "\n"
        )
        (recording / "meta.json").write_text(
            json.dumps(
                {
                    "id": "incomplete-recording",
                    "created_at": "2026-07-16T00:00:00+00:00",
                    "viewport": list(VIEWPORT),
                    "params": {},
                }
            )
        )

        with pytest.raises(FileNotFoundError, match="incomplete frame pair"):
            compile_recording(recording, tmp_path / "bundle", name="incomplete")

    def test_workflow_metadata(self, compiled) -> None:
        wf = compiled["workflow"]
        assert wf.name == "triage-demo"
        assert wf.recording_id == "rec-synthetic-001"
        assert wf.viewport == VIEWPORT
        assert wf.params == {"note": NOTE_VALUE}
        assert [s.action for s in wf.steps] == [
            ActionKind.CLICK,
            ActionKind.TYPE,
            ActionKind.KEY,
            ActionKind.CLICK,
        ]

    def test_click_template_cropped_correctly(self, compiled) -> None:
        wf, bundle, login = (
            compiled["workflow"],
            compiled["bundle"],
            compiled["login"],
        )
        step = wf.steps[0]
        anchor = step.anchor
        assert anchor is not None
        # 160x64 crop centered on the click, inside the frame
        assert anchor.region == (640 - 80, 424 - 32, 160, 64)
        assert anchor.click_point == (640, 424)
        template_path = bundle / anchor.template
        assert template_path.exists()
        tmpl = cv2.imdecode(
            np.frombuffer(template_path.read_bytes(), np.uint8),
            cv2.IMREAD_COLOR,
        )
        x, y, w, h = anchor.region
        assert tmpl.shape[:2] == (h, w)
        assert np.array_equal(tmpl, login[y : y + h, x : x + w])

    def test_click_ocr_text(self, compiled) -> None:
        anchor = compiled["workflow"].steps[0].anchor
        assert anchor.ocr_text is not None
        assert fuzzy_eq(anchor.ocr_text, "Sign In")

    def test_click_intent(self, compiled) -> None:
        step = compiled["workflow"].steps[0]
        assert step.intent.startswith("click '")

    def test_click_landmarks_outside_crop(self, compiled) -> None:
        anchor = compiled["workflow"].steps[0].anchor
        assert 1 <= len(anchor.landmarks) <= 2
        for lm in anchor.landmarks:
            assert lm.distance_px > 0
            assert lm.relation in {"left_of", "right_of", "above", "below"}
            # landmark text is not the button's own label
            assert not fuzzy_eq(lm.ocr_text, "Sign In")
        # the title sits above the click point -> the LANDMARK is above the
        # target (relation describes the landmark's position, see ir.Landmark)
        titles = [
            lm for lm in anchor.landmarks if fuzzy_eq(lm.ocr_text, "MockMed Portal")
        ]
        assert titles and titles[0].relation == "above"
        # exact offsets landmark-center -> click point are carried through
        for lm in anchor.landmarks:
            assert lm.dx_px is not None and lm.dy_px is not None
            assert round((lm.dx_px**2 + lm.dy_px**2) ** 0.5) == lm.distance_px

    def test_click_postconditions(self, compiled) -> None:
        step = compiled["workflow"].steps[0]
        kinds = [pc.kind for pc in step.expect]
        assert PostconditionKind.REGION_STABLE in kinds
        assert PostconditionKind.TEXT_PRESENT in kinds
        stable = next(
            pc for pc in step.expect if pc.kind is PostconditionKind.REGION_STABLE
        )
        assert stable.phash and stable.region is not None
        assert stable.phash_tolerance == 16
        # changed region covers the new banner (drawn around y≈225-245)
        x, y, w, h = stable.region
        assert y < 260 and y + h > 220 and x < 700 and x + w > 420
        text_pc = next(
            pc for pc in step.expect if pc.kind is PostconditionKind.TEXT_PRESENT
        )
        assert fuzzy_eq(text_pc.text, BANNER_TASKS)

    def test_type_step_carries_param_and_skips_typed_text(self, compiled) -> None:
        step = compiled["workflow"].steps[1]
        assert step.action is ActionKind.TYPE
        assert step.param == "note"
        assert step.text == NOTE_VALUE
        assert step.anchor is None
        assert "note" in step.intent
        # the only new text on screen is the typed (parameterized) value:
        # it must NOT be asserted as TEXT_PRESENT
        assert all(pc.kind is not PostconditionKind.TEXT_PRESENT for pc in step.expect)
        # and the diff-based REGION_STABLE is skipped too: the changed
        # region is the typed value's own pixels, which vary per run
        assert all(pc.kind is not PostconditionKind.REGION_STABLE for pc in step.expect)

    def test_key_step(self, compiled) -> None:
        step = compiled["workflow"].steps[2]
        assert step.action is ActionKind.KEY
        assert step.key == "Enter"
        text_pcs = [
            pc for pc in step.expect if pc.kind is PostconditionKind.TEXT_PRESENT
        ]
        assert text_pcs and fuzzy_eq(text_pcs[0].text, BANNER_SAVED)

    def test_corner_click_clamped_no_change(self, compiled) -> None:
        step = compiled["workflow"].steps[3]
        anchor = step.anchor
        assert anchor.region == (0, 0, 160, 64)  # clamped to frame origin
        assert anchor.click_point == (10, 12)
        # identical before/after frames -> no postconditions derived
        assert step.expect == []

    def test_bundle_roundtrip(self, compiled) -> None:
        wf, bundle = compiled["workflow"], compiled["bundle"]
        loaded = Workflow.load(bundle)
        assert loaded == wf
        # every referenced template exists in the bundle
        for step in loaded.steps:
            if step.anchor:
                assert (bundle / step.anchor.template).exists()

    def test_codegen_parses(self, compiled) -> None:
        bundle = compiled["bundle"]
        source = (bundle / "workflow.py").read_text()
        tree = ast.parse(source)  # must be valid Python
        assert isinstance(tree, ast.Module)
        assert "step_000" in source
        assert "triage-demo" in source
        # regenerating from the model matches what's on disk
        assert source == render_workflow_py(compiled["workflow"])

    def test_region_stable_carries_expected_content_template(self, compiled) -> None:
        """Every REGION_STABLE postcondition ships a crop of the expected
        region content so the replayer can tolerate small layout shifts."""
        bundle = compiled["bundle"]
        seen = 0
        for step in compiled["workflow"].steps:
            for pc in step.expect:
                if pc.kind is PostconditionKind.REGION_STABLE:
                    assert pc.template, f"{step.id} region_stable lacks crop"
                    assert (bundle / pc.template).exists()
                    seen += 1
        assert seen > 0

    def test_scroll_event_compiles(self, tmp_path: Path) -> None:
        """scroll events compile to SCROLL steps with deltas and NO
        postconditions (a scroll shifts the whole viewport; asserting the
        resulting frame would bake mutable page content into the bundle —
        the next anchored step's resolution verifies the scroll landed)."""
        recording = tmp_path / "rec"
        (recording / "frames").mkdir(parents=True)
        before = blank()
        draw_text(before, 540, 84, "MockMed Portal")
        after = blank()
        draw_text(after, 540, 700, "MockMed Portal")  # content shifted
        write_frame(recording, 0, "before", before)
        write_frame(recording, 0, "after", after)
        (recording / "events.jsonl").write_text(
            json.dumps({"i": 0, "kind": "scroll", "dx": 0, "dy": 400, "t": 1.0}) + "\n"
        )
        (recording / "meta.json").write_text(
            json.dumps(
                {
                    "id": "rec-scroll",
                    "created_at": "2026-07-06T00:00:00+00:00",
                    "viewport": list(VIEWPORT),
                    "app_url": "http://localhost:0/",
                    "params": {},
                }
            )
        )
        bundle = tmp_path / "bundle"
        workflow = compile_recording(recording, bundle, name="scrolly")
        assert len(workflow.steps) == 1
        step = workflow.steps[0]
        assert step.action is ActionKind.SCROLL
        assert step.scroll_dx == 0
        assert step.scroll_dy == 400
        assert step.anchor is None
        assert step.expect == []
        assert step.intent == "scroll by (0, 400)"
        source = (bundle / "workflow.py").read_text()
        ast.parse(source)
        assert "flow.scroll(0, 400)" in source

    def test_double_click_event_compiles(self, tmp_path: Path) -> None:
        """double_click events (Recorder.double_click) must compile."""
        recording = tmp_path / "rec"
        (recording / "frames").mkdir(parents=True)
        before = blank()
        draw_text(before, 540, 84, "MockMed Portal")
        draw_button(before, 560, 400, 160, 48, "Sign In")
        after = before.copy()
        draw_text(after, 420, 244, BANNER_TASKS)
        write_frame(recording, 0, "before", before)
        write_frame(recording, 0, "after", after)
        (recording / "events.jsonl").write_text(
            json.dumps({"i": 0, "kind": "double_click", "x": 640, "y": 424, "t": 1.0})
            + "\n"
        )
        (recording / "meta.json").write_text(
            json.dumps(
                {
                    "id": "rec-dclick",
                    "created_at": "2026-07-06T00:00:00+00:00",
                    "viewport": list(VIEWPORT),
                    "app_url": "http://localhost:0/",
                    "params": {},
                }
            )
        )
        bundle = tmp_path / "bundle"
        workflow = compile_recording(recording, bundle, name="dclick")
        assert len(workflow.steps) == 1
        step = workflow.steps[0]
        assert step.action is ActionKind.DOUBLE_CLICK
        assert step.intent.startswith("double-click")
        assert step.anchor is not None
        assert step.anchor.click_point == (640, 424)
        assert (bundle / step.anchor.template).exists()
        # Postconditions derived just like a single click.
        assert any(pc.kind is PostconditionKind.TEXT_PRESENT for pc in step.expect)

    def test_param_value_never_asserted_in_downstream_steps(
        self, tmp_path: Path
    ) -> None:
        """A later click whose after-frame embeds the typed param value
        (e.g. a save-confirmation banner) must not bake that value into its
        TEXT_PRESENT postcondition — otherwise the bundle only replays with
        the exact demo-time value."""
        recording = tmp_path / "rec"
        (recording / "frames").mkdir(parents=True)

        base = blank()
        draw_text(base, 540, 84, "Patient Chart Overview")
        draw_button(base, 560, 400, 160, 48, "Save")

        typed = base.copy()
        draw_text(typed, 200, 320, NOTE_VALUE)

        saved = typed.copy()
        # Banner embedding the typed (parameterized) note...
        draw_text(saved, 200, 560, "Saved " + NOTE_VALUE)
        # ...plus an unrelated stable new line the compiler CAN assert.
        draw_text(saved, 200, 620, "Chart synchronization complete")

        events = [
            {"i": 0, "kind": "type", "text": NOTE_VALUE, "param": "note", "t": 1.0},
            {"i": 1, "kind": "click", "x": 640, "y": 424, "t": 2.0},
        ]
        frames = {0: (base, typed), 1: (typed, saved)}
        for i, (before, after) in frames.items():
            write_frame(recording, i, "before", before)
            write_frame(recording, i, "after", after)
        (recording / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )
        (recording / "meta.json").write_text(
            json.dumps(
                {
                    "id": "rec-param-downstream",
                    "created_at": "2026-07-06T00:00:00+00:00",
                    "viewport": list(VIEWPORT),
                    "app_url": "http://localhost:0/",
                    "params": {"note": NOTE_VALUE},
                }
            )
        )

        workflow = compile_recording(
            recording, tmp_path / "bundle", name="param-downstream"
        )
        click_step = workflow.steps[1]
        assert click_step.action is ActionKind.CLICK
        text_pcs = [
            pc for pc in click_step.expect if pc.kind is PostconditionKind.TEXT_PRESENT
        ]
        # The stable new line IS asserted...
        assert text_pcs, "expected a TEXT_PRESENT for the non-param new text"
        assert fuzzy_eq(text_pcs[0].text, "Chart synchronization complete")
        # ...but the parameterized value is not, in ANY step's postconditions.
        squashed_note = "".join(normalize_text(NOTE_VALUE).split())
        for step in workflow.steps:
            for pc in step.expect:
                if pc.kind is not PostconditionKind.TEXT_PRESENT:
                    continue
                hay = "".join(normalize_text(pc.text or "").split())
                matcher = difflib.SequenceMatcher(None, squashed_note, hay)
                contained = sum(b.size for b in matcher.get_matching_blocks()) / len(
                    squashed_note
                )
                assert contained < 0.8, f"param value baked into {step.id}: {pc.text!r}"

    def test_click_target_labels_never_asserted(self, tmp_path: Path) -> None:
        """A button label that appears after a click but is itself a later
        click target must not become a TEXT_PRESENT postcondition: labels
        are mutable evidence (rename drift changes them and the ladder heals
        through it), so asserting one turns cosmetic label drift into a
        false semantic-drift abort."""
        recording = tmp_path / "rec"
        (recording / "frames").mkdir(parents=True)

        first = blank()
        draw_text(first, 500, 84, "Step One Page")
        draw_button(first, 560, 400, 160, 48, "Continue")

        # After clicking Continue: same title, a new "Finish" button (the
        # ONLY new text) which event 1 then clicks.
        second = blank()
        draw_text(second, 500, 84, "Step One Page")
        draw_button(second, 560, 400, 160, 48, "Finish")

        third = second.copy()
        draw_text(third, 400, 560, "All steps completed successfully")

        events = [
            {"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0},
            {"i": 1, "kind": "click", "x": 640, "y": 424, "t": 2.0},
        ]
        frames = {0: (first, second), 1: (second, third)}
        for i, (before, after) in frames.items():
            write_frame(recording, i, "before", before)
            write_frame(recording, i, "after", after)
        (recording / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )
        (recording / "meta.json").write_text(
            json.dumps(
                {
                    "id": "rec-labels",
                    "created_at": "2026-07-06T00:00:00+00:00",
                    "viewport": list(VIEWPORT),
                    "app_url": "http://localhost:0/",
                    "params": {},
                }
            )
        )

        workflow = compile_recording(recording, tmp_path / "bundle", name="labels")
        step0, step1 = workflow.steps
        # Sanity: the second click's anchor label is the "Finish" button.
        assert step1.anchor is not None and step1.anchor.ocr_text
        assert fuzzy_eq(step1.anchor.ocr_text, "Finish")
        # Step 0's only new text was that label -> no TEXT_PRESENT at all
        # (REGION_STABLE still asserts the visual change).
        kinds0 = [pc.kind for pc in step0.expect]
        assert PostconditionKind.TEXT_PRESENT not in kinds0, step0.expect
        assert PostconditionKind.REGION_STABLE in kinds0
        # Step 1 still asserts its genuinely new, non-label text.
        text_pcs = [
            pc for pc in step1.expect if pc.kind is PostconditionKind.TEXT_PRESENT
        ]
        assert text_pcs
        assert fuzzy_eq(text_pcs[0].text, "All steps completed successfully")

    def test_unknown_event_kind_raises(self, tmp_path: Path) -> None:
        recording = tmp_path / "rec"
        (recording / "frames").mkdir(parents=True)
        (recording / "meta.json").write_text(
            json.dumps(
                {
                    "id": "r",
                    "created_at": "t",
                    "viewport": [100, 100],
                    "app_url": "u",
                    "params": {},
                }
            )
        )
        (recording / "events.jsonl").write_text(
            json.dumps({"i": 0, "kind": "hover", "x": 1, "y": 2, "t": 0.1}) + "\n"
        )
        with pytest.raises(ValueError, match="hover"):
            compile_recording(recording, tmp_path / "bundle", name="x")


class TestIdentityContext:
    def test_row_click_captures_context_outside_crop(self, tmp_path: Path) -> None:
        """A click on a button inside a table-like row records the row's
        OTHER text (the discriminative name column) as identity context —
        excluding the button's own crop (mutable label) and any
        timestamp-bearing cell (volatile)."""
        recording = tmp_path / "rec"
        (recording / "frames").mkdir(parents=True)

        before = blank()
        # A row: name | clock time | priority | [Open] button (click target).
        draw_text(before, 40, 430, "Jane Sample")
        draw_text(before, 280, 430, "12:45")
        draw_text(before, 400, 430, "High")
        draw_button(before, 560, 400, 160, 48, "Open")
        # Text on ANOTHER row must stay out of the band.
        draw_text(before, 40, 560, "Alex Testcase")
        after = before.copy()
        draw_text(after, 420, 244, BANNER_TASKS)

        write_frame(recording, 0, "before", before)
        write_frame(recording, 0, "after", after)
        (recording / "events.jsonl").write_text(
            json.dumps({"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0}) + "\n"
        )
        (recording / "meta.json").write_text(
            json.dumps(
                {
                    "id": "rec-row",
                    "created_at": "2026-07-06T00:00:00+00:00",
                    "viewport": list(VIEWPORT),
                    "app_url": "http://localhost:0/",
                    "params": {},
                }
            )
        )
        workflow = compile_recording(recording, tmp_path / "bundle", name="row")
        anchor = workflow.steps[0].anchor
        assert anchor is not None
        # PHI-free artifact (audit REM-2): the identity band is stored as a
        # salted-hash template, not plaintext. context_text is None and no
        # patient identifier is persisted; membership is probed by hash.
        assert anchor.context_text is None
        tmpl = anchor.identity_template
        assert tmpl is not None
        assert token_in_template(tmpl, "jane") and token_in_template(tmpl, "sample")
        assert token_in_template(tmpl, "high")
        assert not token_in_template(tmpl, "12:45")  # timestamps are volatile
        assert not token_in_template(tmpl, "open")  # target's own (mutable) label
        assert not token_in_template(tmpl, "alex")  # other rows outside the band
        # No plaintext identifier anywhere in the serialized bundle.
        assert "Jane" not in tmpl.model_dump_json()
        # Armed-coverage audit trail in the bundle:
        assert workflow.steps[0].identity_armed is True
        assert workflow.steps[0].identity_unarmed_reason is None

    def test_click_with_no_row_text_has_no_context(self, compiled) -> None:
        """The synthetic Sign In button sits alone on its row: nothing
        outside the crop shares the band, so no context is recorded and the
        identity check is not armed for the step — and the bundle says so
        (identity_armed=False plus a reason), so an operator can audit
        protection coverage before running."""
        for step in compiled["workflow"].steps:
            if step.anchor is not None:
                assert step.anchor.context_text is None, step.id
                assert step.identity_armed is False, step.id
                assert step.identity_unarmed_reason, step.id


def _write_recording(
    recording: Path,
    events: list[dict],
    frames: dict[int, tuple[np.ndarray, np.ndarray]],
    *,
    params: dict[str, str] | None = None,
    created_at: str = "2026-07-06T00:00:00+00:00",
) -> None:
    """Write a synthetic recording directory (events + frames + meta)."""
    (recording / "frames").mkdir(parents=True, exist_ok=True)
    for i, (before, after) in frames.items():
        write_frame(recording, i, "before", before)
        write_frame(recording, i, "after", after)
    (recording / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-synth",
                "created_at": created_at,
                "viewport": list(VIEWPORT),
                "app_url": "http://localhost:0/",
                "params": params or {},
            }
        )
    )


class TestStabilitySelectedMining:
    """Postcondition mining selects for STABILITY, not novelty — the fix for
    the ':01'-class false halts (docs/validation/VALIDATION.md, Track D)."""

    def test_clock_fragment_never_wins_even_when_longest(self, tmp_path: Path) -> None:
        """A new timestamped log row (longer text!) must lose to shorter
        stable UI text; no mined TEXT_PRESENT may carry a clock time."""
        before = blank()
        draw_button(before, 560, 400, 160, 48, "Open")
        after = before.copy()
        # The volatile candidate is deliberately the LONGEST new text.
        draw_text(after, 120, 244, "Message received 2026-07-05 18:01:07")
        draw_text(after, 120, 560, "Inbox loaded")
        events = [{"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0}]
        recording = tmp_path / "rec"
        _write_recording(recording, events, {0: (before, after)})
        wf = compile_recording(recording, tmp_path / "bundle", name="clock")
        text_pcs = [
            pc for pc in wf.steps[0].expect if pc.kind is PostconditionKind.TEXT_PRESENT
        ]
        assert text_pcs, "expected the stable candidate to be asserted"
        assert fuzzy_eq(text_pcs[0].text, "Inbox loaded")
        for pc in text_pcs:
            assert ":0" not in pc.text and "2026" not in pc.text

    def test_ephemeral_text_not_asserted(self, tmp_path: Path) -> None:
        """Text present in a step's after frame but already GONE by the next
        step's before frame (same screen, moments later) is ephemeral —
        a toast/spinner — and must not be mined."""
        before = blank()
        draw_button(before, 560, 400, 160, 48, "Save")
        after = before.copy()
        draw_text(after, 120, 244, "Saving in progress please wait")  # toast
        draw_text(after, 120, 560, "Record updated")  # persists
        next_before = before.copy()
        draw_text(next_before, 120, 560, "Record updated")  # toast gone
        next_after = next_before.copy()
        draw_text(next_after, 120, 620, "Done reviewing")
        events = [
            {"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0},
            {"i": 1, "kind": "key", "key": "Enter", "t": 2.0},
        ]
        recording = tmp_path / "rec"
        _write_recording(
            recording,
            events,
            {0: (before, after), 1: (next_before, next_after)},
        )
        wf = compile_recording(recording, tmp_path / "bundle", name="toast")
        text_pcs = [
            pc for pc in wf.steps[0].expect if pc.kind is PostconditionKind.TEXT_PRESENT
        ]
        assert text_pcs
        assert fuzzy_eq(text_pcs[0].text, "Record updated")

    def test_self_mutating_region_not_asserted(self, tmp_path: Path) -> None:
        """A changed region that KEEPS changing between the after frame and
        the next before frame (no action in between — an animation/clock)
        must not become a REGION_STABLE postcondition."""
        before = blank()
        draw_button(before, 560, 400, 160, 48, "Save")
        after = before.copy()
        draw_text(after, 120, 244, "spinnerframeone")
        next_before = before.copy()
        draw_text(next_before, 120, 244, "otherframe")  # same spot, changed
        next_after = next_before.copy()
        draw_text(next_after, 120, 620, "Done reviewing")
        events = [
            {"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0},
            {"i": 1, "kind": "key", "key": "Enter", "t": 2.0},
        ]
        recording = tmp_path / "rec"
        _write_recording(
            recording,
            events,
            {0: (before, after), 1: (next_before, next_after)},
        )
        wf = compile_recording(recording, tmp_path / "bundle", name="spin")
        kinds = [pc.kind for pc in wf.steps[0].expect]
        assert PostconditionKind.REGION_STABLE not in kinds, wf.steps[0].expect

    def test_dob_banner_in_identity_region_is_asserted(self, tmp_path: Path) -> None:
        """FIXED: the old blanket timestamp filter dropped the patient
        banner because a DOB looks like a date, leaving only patient-
        agnostic text. A date FAR from the recording date is identity data
        and the banner must survive mining, DOB included."""
        before = blank()
        draw_button(before, 560, 400, 160, 48, "Open")
        after = blank()
        draw_button(after, 560, 400, 160, 48, "Open")
        draw_text(after, 200, 244, "Jane Sample DOB 1980-01-01")
        events = [{"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0}]
        recording = tmp_path / "rec"
        _write_recording(recording, events, {0: (before, after)})
        wf = compile_recording(recording, tmp_path / "bundle", name="dob")
        text_pcs = [
            pc for pc in wf.steps[0].expect if pc.kind is PostconditionKind.TEXT_PRESENT
        ]
        assert text_pcs, "the DOB banner must not be filtered as a timestamp"
        assert fuzzy_eq(text_pcs[0].text, "Jane Sample DOB 1980-01-01", 0.6)

    def test_dob_line_kept_in_identity_context(self, tmp_path: Path) -> None:
        """The identity context band keeps a row whose only date is a far
        DOB — that date is discriminative identity data."""
        before = blank()
        draw_text(before, 40, 430, "Jane Sample")
        draw_text(before, 260, 430, "1980-01-01")
        draw_button(before, 560, 400, 160, 48, "Open")
        after = before.copy()
        draw_text(after, 420, 244, BANNER_TASKS)
        events = [{"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0}]
        recording = tmp_path / "rec"
        _write_recording(recording, events, {0: (before, after)})
        wf = compile_recording(recording, tmp_path / "bundle", name="dobctx")
        anchor = wf.steps[0].anchor
        assert anchor is not None and anchor.context_text is None
        tmpl = anchor.identity_template
        assert tmpl is not None
        assert token_in_template(tmpl, "jane") and token_in_template(tmpl, "sample")
        # The far date is KEPT as identity data (retained in the template): the
        # DOB token verifies, and a DIFFERENT DOB does not.
        assert token_in_template(tmpl, "1980-01-01")
        from openadapt_flow.runtime.identity_template import verify_template_identity

        assert (
            verify_template_identity(tmpl, "Jane Sample 1980-01-01").status
            == "verified"
        )
        assert (
            verify_template_identity(tmpl, "Jane Sample 1990-01-01").status
            == "mismatch"
        )


class TestParameterHygiene:
    def test_param_value_never_becomes_a_landmark(self, tmp_path: Path) -> None:
        """FIXED: on OpenEMR the save step's geometry landmark was the
        recorded note text itself — a demo parameter value leaking into
        geometry evidence, silently degrading healing for every run whose
        value differs from the demo (i.e., all of them)."""
        base = blank()
        draw_text(base, 540, 84, "Patient Chart Overview")
        draw_button(base, 560, 400, 160, 48, "Save")

        typed = base.copy()
        # The (parameterized) note renders right above the Save button —
        # nearest text, i.e. the winning landmark under the old rules.
        draw_text(typed, 560, 380, NOTE_VALUE)

        saved = typed.copy()
        draw_text(saved, 200, 620, "Chart synchronization complete")

        events = [
            {"i": 0, "kind": "type", "text": NOTE_VALUE, "param": "note", "t": 1.0},
            {"i": 1, "kind": "click", "x": 640, "y": 424, "t": 2.0},
        ]
        recording = tmp_path / "rec"
        _write_recording(
            recording,
            events,
            {0: (base, typed), 1: (typed, saved)},
            params={"note": NOTE_VALUE},
        )
        wf = compile_recording(recording, tmp_path / "bundle", name="lmk")
        anchor = wf.steps[1].anchor
        assert anchor is not None
        squashed_note = "".join(normalize_text(NOTE_VALUE).split())
        for lm in anchor.landmarks:
            hay = "".join(normalize_text(lm.ocr_text).split())
            matcher = difflib.SequenceMatcher(None, squashed_note, hay)
            contained = sum(b.size for b in matcher.get_matching_blocks()) / len(
                squashed_note
            )
            assert contained < 0.8, f"param leaked into landmark {lm.ocr_text!r}"

    def test_lint_flags_param_value_in_postcondition(self) -> None:
        from openadapt_flow.compiler import lint_param_leakage
        from openadapt_flow.ir import Anchor, Landmark, Postcondition, Step

        wf = Workflow(
            name="leaky",
            params={"note": NOTE_VALUE},
            steps=[
                Step(
                    id="step_000",
                    intent="click 'Save'",
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/step_000.png",
                        region=(0, 0, 10, 10),
                        click_point=(5, 5),
                        landmarks=[
                            Landmark(
                                relation="above",
                                ocr_text=NOTE_VALUE,  # leaked
                                distance_px=10,
                            )
                        ],
                    ),
                    expect=[
                        Postcondition(
                            kind=PostconditionKind.TEXT_PRESENT,
                            text=f"Saved {NOTE_VALUE}",  # leaked
                        )
                    ],
                )
            ],
        )
        violations = lint_param_leakage(wf, (NOTE_VALUE,))
        assert len(violations) == 2
        assert any("postcondition" in v for v in violations)
        assert any("landmark" in v for v in violations)

    def test_lint_allows_designated_slots(self) -> None:
        from openadapt_flow.compiler import lint_param_leakage
        from openadapt_flow.ir import Anchor, Step

        wf = Workflow(
            name="clean",
            params={"patient": "Belford, Phil"},
            steps=[
                Step(
                    id="step_000",
                    intent="type <patient>",
                    action=ActionKind.TYPE,
                    text="Belford, Phil",  # designated: recorded example
                    param="patient",
                ),
                Step(
                    id="step_001",
                    intent="click 'Belford, Phil'",
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/step_001.png",
                        region=(0, 0, 10, 10),
                        click_point=(5, 5),
                        # Designated: resolution/identity evidence — the
                        # identity check re-anchors on the RUN's value.
                        ocr_text="Belford, Phil",
                        context_text="Belford, Phil 1948-01-01 male",
                    ),
                ),
            ],
        )
        assert lint_param_leakage(wf, ("Belford, Phil",)) == []

    def test_lint_skips_too_short_values(self) -> None:
        from openadapt_flow.compiler import lint_param_leakage
        from openadapt_flow.ir import Postcondition, Step

        wf = Workflow(
            name="short",
            params={"initial": "d"},
            steps=[
                Step(
                    id="step_000",
                    intent="press Enter",
                    action=ActionKind.KEY,
                    key="Enter",
                    expect=[
                        Postcondition(
                            kind=PostconditionKind.TEXT_PRESENT,
                            text="Species set to Dog.",
                        )
                    ],
                )
            ],
        )
        assert lint_param_leakage(wf, ("d",)) == []

    def test_compile_fails_loudly_on_injected_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: if mining ever regresses and emits a leaked value,
        compilation itself must fail — no silently demo-bound bundles."""
        from openadapt_flow.compiler import compile as compile_mod
        from openadapt_flow.ir import Postcondition

        base = blank()
        draw_button(base, 560, 400, 160, 48, "Save")
        typed = base.copy()
        draw_text(typed, 200, 320, NOTE_VALUE)
        events = [
            {"i": 0, "kind": "type", "text": NOTE_VALUE, "param": "note", "t": 1.0},
        ]
        recording = tmp_path / "rec"
        _write_recording(
            recording, events, {0: (base, typed)}, params={"note": NOTE_VALUE}
        )

        def leaky_postconditions(*args, **kwargs):
            return [
                Postcondition(
                    kind=PostconditionKind.TEXT_PRESENT,
                    text=f"Saved {NOTE_VALUE}",
                )
            ]

        monkeypatch.setattr(compile_mod, "_postconditions", leaky_postconditions)
        with pytest.raises(ValueError, match="parameter leakage"):
            compile_recording(recording, tmp_path / "bundle", name="leak")


class TestStructuralPostconditions:
    def _navigation_recording(self, tmp_path: Path, extra: dict) -> tuple[Path, Path]:
        """A click whose before/after frames are IDENTICAL (the visual
        runtime saw nothing) plus recorder-captured structural keys."""
        frame = blank()
        draw_button(frame, 560, 400, 160, 48, "Report")
        events = [{"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0, **extra}]
        recording = tmp_path / "rec"
        _write_recording(recording, events, {0: (frame, frame)})
        return recording, tmp_path / "bundle"

    def test_new_tab_click_mines_new_tab_postcondition(self, tmp_path: Path) -> None:
        recording, bundle = self._navigation_recording(
            tmp_path,
            {
                "url_before": "http://app/",
                "url_after": "http://app/",
                "pages_before": 1,
                "pages_after": 2,
            },
        )
        wf = compile_recording(recording, bundle, name="newtab")
        kinds = [pc.kind for pc in wf.steps[0].expect]
        assert kinds == [PostconditionKind.NEW_TAB_OPENED]

    def test_navigation_click_mines_url_changed(self, tmp_path: Path) -> None:
        recording, bundle = self._navigation_recording(
            tmp_path,
            {
                "url_before": "http://app/#inbox",
                "url_after": "http://app/#report",
                "pages_before": 1,
                "pages_after": 1,
            },
        )
        wf = compile_recording(recording, bundle, name="nav")
        kinds = [pc.kind for pc in wf.steps[0].expect]
        assert kinds == [PostconditionKind.URL_CHANGED]

    def test_title_only_change_mines_title_changed(self, tmp_path: Path) -> None:
        recording, bundle = self._navigation_recording(
            tmp_path,
            {
                "url_before": "http://app/",
                "url_after": "http://app/",
                "title_before": "Inbox",
                "title_after": "Report",
            },
        )
        wf = compile_recording(recording, bundle, name="title")
        kinds = [pc.kind for pc in wf.steps[0].expect]
        assert kinds == [PostconditionKind.TITLE_CHANGED]

    def test_no_structural_change_stays_honestly_vacuous(self, tmp_path: Path) -> None:
        recording, bundle = self._navigation_recording(
            tmp_path,
            {
                "url_before": "http://app/",
                "url_after": "http://app/",
                "pages_before": 1,
                "pages_after": 1,
            },
        )
        wf = compile_recording(recording, bundle, name="inert")
        assert wf.steps[0].expect == []

    def test_structural_is_fallback_only(self, tmp_path: Path) -> None:
        """A step with visual postconditions does not also get structural
        ones (fallback keeps the false-abort surface minimal)."""
        before = blank()
        draw_button(before, 560, 400, 160, 48, "Sign In")
        after = before.copy()
        draw_text(after, 420, 244, BANNER_TASKS)
        events = [
            {
                "i": 0,
                "kind": "click",
                "x": 640,
                "y": 424,
                "t": 1.0,
                "url_before": "http://app/",
                "url_after": "http://app/#tasks",
            }
        ]
        recording = tmp_path / "rec"
        _write_recording(recording, events, {0: (before, after)})
        wf = compile_recording(recording, tmp_path / "bundle", name="fb")
        kinds = {pc.kind for pc in wf.steps[0].expect}
        assert PostconditionKind.TEXT_PRESENT in kinds
        assert PostconditionKind.URL_CHANGED not in kinds


class TestRiskOverrides:
    """Risk is opt-in at compile time — never auto-assigned. Without an
    override every step is reversible, which means the irreversible
    safeguards (below-OCR-rung refusal, unreadable-identity-band refusal)
    are UNREACHABLE from a default compile; this is disclosed in
    docs/LIMITS.md. The override is the supported way to arm them."""

    def test_default_compile_marks_every_step_reversible(self, compiled):
        assert all(s.risk == "reversible" for s in compiled["workflow"].steps)

    def test_override_marks_step_irreversible(self, compiled, tmp_path):
        workflow = compile_recording(
            compiled["recording"],
            tmp_path / "bundle",
            name="risky",
            risk_overrides={"step_003": "irreversible"},
        )
        by_id = {s.id: s for s in workflow.steps}
        assert by_id["step_003"].risk == "irreversible"
        assert all(s.risk == "reversible" for s in workflow.steps if s.id != "step_003")
        # The risk survives the bundle round-trip.
        reloaded = Workflow.load(tmp_path / "bundle")
        assert {s.id: s.risk for s in reloaded.steps} == {
            s.id: s.risk for s in workflow.steps
        }

    def test_unknown_step_id_rejected(self, compiled, tmp_path):
        with pytest.raises(ValueError, match="unknown step"):
            compile_recording(
                compiled["recording"],
                tmp_path / "bundle",
                name="risky",
                risk_overrides={"step_999": "irreversible"},
            )

    def test_invalid_risk_value_rejected(self, compiled, tmp_path):
        with pytest.raises(ValueError, match="invalid risk"):
            compile_recording(
                compiled["recording"],
                tmp_path / "bundle",
                name="risky",
                risk_overrides={"step_000": "dangerous"},
            )
