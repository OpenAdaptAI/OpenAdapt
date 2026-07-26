"""Ergonomic parameter identification: field-label capture -> flagged
proposals -> one-shot operator confirm.

Covers the full ladder:

- record-time capture: ``Recorder.type_text`` passively reads the backend's
  ``focused_field_label`` seam into the TYPE event (no behavior change);
- compile-time evidence: ``field_label`` lands on the compiled Step, with the
  nearby-OCR fallback when only a ``field_rect`` was recorded;
- deterministic proposals: ``FieldLabelAnnotator`` slugifies labels into
  parameter-name proposals emitted through the existing annotate pipeline and
  GATED (consequential -> flagged, never applied); explicit ``param=`` wins;
- the one-shot confirm pass: confirm / rename / mark-secret / keep-constant,
  interactive and non-interactive, applied by recompiling with
  ``param_overrides``; unconfirmed proposals stay demonstrated constants.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from openadapt_flow.compiler import compile_recording
from openadapt_flow.compiler import param_confirm as pc
from openadapt_flow.compiler.annotate import (
    FieldLabelAnnotator,
    apply_annotations,
    mask_value,
    slugify_label,
)
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.recorder import Recorder

VIEWPORT = (1280, 800)
INSURANCE_VALUE = "AB-1234-XY-99887766"


# -- helpers ------------------------------------------------------------------


def _blank() -> np.ndarray:
    return np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, dtype=np.uint8)


def _draw_text(img: np.ndarray, x: int, y: int, text: str) -> None:
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


def _write_recording(
    tmp_path: Path,
    events: list[dict],
    frames: dict[int, np.ndarray],
    *,
    params: dict[str, str] | None = None,
) -> Path:
    recording = tmp_path / "recording"
    (recording / "frames").mkdir(parents=True)
    for i in sorted(frames):
        ok, buf = cv2.imencode(".png", frames[i])
        assert ok
        for suffix in ("before", "after"):
            (recording / "frames" / f"{i:04d}_{suffix}.png").write_bytes(buf.tobytes())
    (recording / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-field-label-001",
                "created_at": "2026-07-26T00:00:00+00:00",
                "viewport": list(VIEWPORT),
                "app_url": "http://localhost:0/",
                "params": params or {},
            }
        )
    )
    return recording


def _labeled_recording(tmp_path: Path) -> Path:
    """One TYPE event carrying DOM field-label evidence, no explicit param."""
    return _write_recording(
        tmp_path,
        [
            {
                "i": 0,
                "kind": "type",
                "text": INSURANCE_VALUE,
                "field_label": "Insurance No.",
                "t": 1.0,
            }
        ],
        {0: _blank()},
    )


def _type_step(
    step_id: str = "step_000",
    *,
    text: str | None = INSURANCE_VALUE,
    param: str | None = None,
    secret: bool = False,
    field_label: str | None = "Insurance No.",
) -> Step:
    return Step(
        id=step_id,
        intent="type",
        action=ActionKind.TYPE,
        text=text,
        param=param,
        secret=secret,
        field_label=field_label,
    )


# -- slugification ------------------------------------------------------------


class TestSlugifyLabel:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Insurance No.", "insurance_no"),
            ("Patient's Date of Birth", "patients_date_of_birth"),
            ("  Member   ID ", "member_id"),
            ("Café Número", "cafe_numero"),
            ("note", "note"),
            ("Ward (press Enter to submit)", "ward_press_enter_to_submit"),
        ],
    )
    def test_slug_rules(self, label: str, expected: str) -> None:
        assert slugify_label(label) == expected

    def test_leading_digit_prefixed(self) -> None:
        assert slugify_label("42nd Street") == "f_42nd_street"

    def test_symbol_only_label_yields_none(self) -> None:
        assert slugify_label("!!! ***") is None
        assert slugify_label("") is None

    def test_truncated_to_max_len(self) -> None:
        slug = slugify_label("word " * 40)
        assert slug is not None and len(slug) <= 64


class TestMaskValue:
    def test_short_values_fully_masked(self) -> None:
        assert mask_value("ab") == "**"
        assert mask_value("x") == "*"

    def test_middle_masked(self) -> None:
        masked = mask_value(INSURANCE_VALUE)
        assert masked != INSURANCE_VALUE
        assert masked.startswith(INSURANCE_VALUE[:2])
        assert masked.endswith(INSURANCE_VALUE[-2:])
        assert "*" in masked
        # The bulk of the value never appears.
        assert INSURANCE_VALUE[3:-3] not in masked


# -- deterministic proposal source + gate -------------------------------------


class TestFieldLabelAnnotator:
    def test_proposal_flagged_never_applied(self) -> None:
        wf = Workflow(name="w", steps=[_type_step()])
        result = apply_annotations(wf, FieldLabelAnnotator())
        assert not result.applied
        assert len(result.flagged) == 1
        flag = result.flagged[0]
        assert flag.kind == "consequential_param"
        assert flag.needs_operator_confirmation
        assert "field label" in flag.detail
        # NOTHING changed on the workflow: the constant stays a constant.
        assert result.workflow.param_specs == {}
        assert result.workflow.params == {}
        assert result.workflow.steps[0].param is None
        # The raw proposal carries the slug + provenance for the confirm pass.
        prop = result.proposals.steps[0].params[0]
        assert prop.name == "insurance_no"
        assert prop.consequential is True
        assert prop.source_label == "Insurance No."

    def test_explicit_param_suppresses_proposal(self) -> None:
        wf = Workflow(
            name="w",
            params={"policy": INSURANCE_VALUE},
            steps=[_type_step(param="policy")],
        )
        result = apply_annotations(wf, FieldLabelAnnotator())
        assert result.proposals.steps == []
        assert not result.flagged

    def test_secret_step_suppresses_proposal(self) -> None:
        wf = Workflow(
            name="w",
            steps=[_type_step(text=None, param="password", secret=True)],
        )
        assert not apply_annotations(wf, FieldLabelAnnotator()).flagged

    def test_no_label_or_no_text_suppresses_proposal(self) -> None:
        wf = Workflow(
            name="w",
            steps=[
                _type_step("step_000", field_label=None),
                _type_step("step_001", text=""),
            ],
        )
        assert not apply_annotations(wf, FieldLabelAnnotator()).flagged

    def test_collision_with_declared_param_skipped(self) -> None:
        wf = Workflow(
            name="w",
            params={"insurance_no": "other"},
            steps=[_type_step()],
        )
        assert not apply_annotations(wf, FieldLabelAnnotator()).flagged

    def test_duplicate_labels_deduped_deterministically(self) -> None:
        wf = Workflow(
            name="w",
            steps=[
                _type_step("step_000"),
                _type_step("step_001", text="second value"),
            ],
        )
        result = apply_annotations(wf, FieldLabelAnnotator())
        names = [sa.params[0].name for sa in result.proposals.steps]
        assert names == ["insurance_no", "insurance_no_2"]


# -- compile: evidence capture + sidecar --------------------------------------


class TestCompileFieldLabel:
    def test_field_label_lands_on_step_and_sidecar(self, tmp_path: Path) -> None:
        recording = _labeled_recording(tmp_path)
        bundle = tmp_path / "bundle"
        wf = compile_recording(recording, bundle, name="labels")

        step = wf.steps[0]
        assert step.field_label == "Insurance No."
        # Fail-closed: no parameter without confirmation.
        assert step.param is None and step.text == INSURANCE_VALUE
        assert wf.params == {} and wf.param_specs == {}

        proposals = pc.load_proposals(bundle)
        assert [p.name for p in proposals] == ["insurance_no"]
        assert proposals[0].step_id == "step_000"
        assert proposals[0].field_label == "Insurance No."
        # The sidecar masks the demonstrated value.
        sidecar = (bundle / pc.PROPOSALS_FILENAME).read_text()
        assert INSURANCE_VALUE not in sidecar
        assert proposals[0].masked_example
        # The persisted bundle carries the evidence too.
        saved = Workflow.load(bundle)
        assert saved.steps[0].field_label == "Insurance No."

    def test_explicit_param_yields_no_sidecar(self, tmp_path: Path) -> None:
        recording = _write_recording(
            tmp_path,
            [
                {
                    "i": 0,
                    "kind": "type",
                    "text": INSURANCE_VALUE,
                    "param": "policy",
                    "field_label": "Insurance No.",
                    "t": 1.0,
                }
            ],
            {0: _blank()},
            params={"policy": INSURANCE_VALUE},
        )
        bundle = tmp_path / "bundle"
        wf = compile_recording(recording, bundle, name="explicit")
        assert wf.steps[0].param == "policy"
        assert not (bundle / pc.PROPOSALS_FILENAME).is_file()

    def test_ocr_fallback_from_field_rect(self, tmp_path: Path) -> None:
        frame = _blank()
        # Form-label geometry: "Member ID" immediately LEFT of the field rect.
        _draw_text(frame, 100, 300, "Member ID")
        field_rect = [280, 270, 300, 44]
        recording = _write_recording(
            tmp_path,
            [
                {
                    "i": 0,
                    "kind": "type",
                    "text": "M-778899",
                    "field_rect": field_rect,
                    "t": 1.0,
                }
            ],
            {0: frame},
        )
        bundle = tmp_path / "bundle"
        wf = compile_recording(recording, bundle, name="ocr-fallback")
        label = wf.steps[0].field_label
        assert label, "nearby-OCR fallback should have found the form label"
        assert "member" in label.lower()
        proposals = pc.load_proposals(bundle)
        assert proposals and "member" in proposals[0].name


# -- the one-shot confirm pass ------------------------------------------------


class TestConfirmPass:
    def _compiled(self, tmp_path: Path) -> tuple[Path, Path]:
        recording = _labeled_recording(tmp_path)
        bundle = tmp_path / "bundle"
        compile_recording(recording, bundle, name="labels")
        return recording, bundle

    def test_accept_list_confirms_parameter(self, tmp_path: Path) -> None:
        recording, bundle = self._compiled(tmp_path)
        decisions = pc.decisions_from_accept_list(
            pc.load_proposals(bundle), ["insurance_no"]
        )
        wf = pc.apply_decisions(recording, bundle, name="labels", decisions=decisions)
        assert wf.steps[0].param == "insurance_no"
        assert wf.params == {"insurance_no": INSURANCE_VALUE}
        assert wf.param_specs["insurance_no"].example == INSURANCE_VALUE
        # Confirmed -> no outstanding proposals; stale sidecar removed.
        assert not (bundle / pc.PROPOSALS_FILENAME).is_file()
        # The persisted bundle agrees.
        saved = Workflow.load(bundle)
        assert saved.steps[0].param == "insurance_no"

    def test_accept_list_unknown_name_fails_loud(self, tmp_path: Path) -> None:
        _, bundle = self._compiled(tmp_path)
        with pytest.raises(ValueError, match="no flagged proposal"):
            pc.decisions_from_accept_list(pc.load_proposals(bundle), ["nope"])

    def test_decision_file_rename(self, tmp_path: Path) -> None:
        recording, bundle = self._compiled(tmp_path)
        decisions = pc.decisions_from_file(
            pc.load_proposals(bundle),
            {"insurance_no": {"action": "rename", "name": "policy_number"}},
        )
        wf = pc.apply_decisions(recording, bundle, name="labels", decisions=decisions)
        assert wf.steps[0].param == "policy_number"
        assert wf.params == {"policy_number": INSURANCE_VALUE}

    def test_decision_file_secret(self, tmp_path: Path) -> None:
        recording, bundle = self._compiled(tmp_path)
        decisions = pc.decisions_from_file(
            pc.load_proposals(bundle),
            {"insurance_no": {"action": "secret"}},
        )
        wf = pc.apply_decisions(recording, bundle, name="labels", decisions=decisions)
        step = wf.steps[0]
        assert step.secret is True and step.text is None
        assert step.param == "insurance_no"
        assert wf.secret_params == ["insurance_no"]
        assert "insurance_no" not in wf.params  # a secret has no stored value
        # The literal never lands in any persisted bundle text artifact.
        for path in Path(bundle).rglob("*"):
            if path.suffix in (".json", ".py", ".txt", ".md"):
                assert INSURANCE_VALUE not in path.read_text()

    def test_decision_file_constant_and_unknown(self, tmp_path: Path) -> None:
        recording, bundle = self._compiled(tmp_path)
        proposals = pc.load_proposals(bundle)
        assert (
            pc.decisions_from_file(proposals, {"insurance_no": {"action": "constant"}})
            == []
        )
        with pytest.raises(ValueError, match="no flagged proposal"):
            pc.decisions_from_file(proposals, {"nope": {"action": "confirm"}})
        with pytest.raises(ValueError, match="invalid action"):
            pc.decisions_from_file(proposals, {"insurance_no": {"action": "promote"}})
        # No accepted decisions -> nothing recompiled (None), bundle unchanged.
        assert (
            pc.apply_decisions(recording, bundle, name="labels", decisions=[]) is None
        )
        assert Workflow.load(bundle).steps[0].param is None

    def test_interactive_choices(self, tmp_path: Path) -> None:
        _, bundle = self._compiled(tmp_path)
        proposals = pc.load_proposals(bundle)
        outputs: list[str] = []

        # Keep-constant is the DEFAULT (empty input).
        assert (
            pc.decisions_interactive(
                proposals, input_fn=lambda _: "", output_fn=outputs.append
            )
            == []
        )
        # Confirm.
        decisions = pc.decisions_interactive(
            proposals, input_fn=lambda _: "c", output_fn=outputs.append
        )
        assert decisions == [pc.ParamDecision(step_id="step_000", name="insurance_no")]
        # Rename.
        answers = iter(["r", "member_policy"])
        decisions = pc.decisions_interactive(
            proposals, input_fn=lambda _: next(answers), output_fn=outputs.append
        )
        assert decisions == [pc.ParamDecision(step_id="step_000", name="member_policy")]
        # Secret.
        decisions = pc.decisions_interactive(
            proposals, input_fn=lambda _: "s", output_fn=outputs.append
        )
        assert decisions == [
            pc.ParamDecision(step_id="step_000", name="insurance_no", secret=True)
        ]
        # The masked value is displayed, never the literal.
        blob = "\n".join(outputs)
        assert INSURANCE_VALUE not in blob
        assert "Insurance No." in blob

    def test_unconfirmed_stays_constant_end_to_end(self, tmp_path: Path) -> None:
        """Fail-closed: with no confirmation channel, the first compile IS the
        final bundle -- the demonstrated constant replays verbatim."""
        _, bundle = self._compiled(tmp_path)
        wf = Workflow.load(bundle)
        assert wf.steps[0].param is None
        assert wf.steps[0].text == INSURANCE_VALUE
        assert wf.params == {} and wf.param_specs == {} and wf.secret_params == []

    def test_cli_accept_params_non_interactive(self, tmp_path: Path) -> None:
        from openadapt_flow.__main__ import main

        recording = _labeled_recording(tmp_path)
        bundle = tmp_path / "bundle"
        rc = main(
            [
                "compile",
                str(recording),
                "--out",
                str(bundle),
                "--name",
                "labels",
                "--accept-params",
                "insurance_no",
            ]
        )
        assert rc == 0
        wf = Workflow.load(bundle)
        assert wf.steps[0].param == "insurance_no"
        assert wf.params == {"insurance_no": INSURANCE_VALUE}

    def test_cli_params_from_file(self, tmp_path: Path) -> None:
        from openadapt_flow.__main__ import main

        recording = _labeled_recording(tmp_path)
        bundle = tmp_path / "bundle"
        decisions_file = tmp_path / "decisions.json"
        decisions_file.write_text(
            json.dumps({"insurance_no": {"action": "rename", "name": "policy_number"}})
        )
        rc = main(
            [
                "compile",
                str(recording),
                "--out",
                str(bundle),
                "--name",
                "labels",
                "--params-from",
                str(decisions_file),
            ]
        )
        assert rc == 0
        assert Workflow.load(bundle).steps[0].param == "policy_number"

    def test_cli_no_channel_keeps_constants(self, tmp_path: Path) -> None:
        """Non-TTY, no flags: proposals stay unapplied (CI-safe default)."""
        from openadapt_flow.__main__ import main

        recording = _labeled_recording(tmp_path)
        bundle = tmp_path / "bundle"
        rc = main(["compile", str(recording), "--out", str(bundle), "--name", "labels"])
        assert rc == 0
        wf = Workflow.load(bundle)
        assert wf.steps[0].param is None and wf.params == {}
        assert (bundle / pc.PROPOSALS_FILENAME).is_file()


# -- record-time capture (real browser: PlaywrightBackend seam) ---------------


class TestPlaywrightFocusedFieldLabel:
    def test_label_ladder_and_value_exclusion(self, tmp_path: Path) -> None:
        """The DOM label ladder resolves best-first, and a control nested
        inside a wrapping <label> never leaks its typed VALUE into the
        captured label."""
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import ensure_chromium_installed
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend

        ensure_chromium_installed()
        html = (
            "<label for='a'>Insurance No.</label><input id='a'>"
            "<label>Ward<textarea id='w'></textarea></label>"
            "<input id='p' placeholder='Policy #'>"
            "<input id='n' name='member_id'>"
        )
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                backend = PlaywrightBackend(page)

                page.click("#a")
                assert backend.focused_field_label() == "Insurance No."

                page.click("#w")
                page.keyboard.type("WARD-VALUE-NEVER-A-LABEL")
                label = backend.focused_field_label()
                assert label == "Ward"
                assert "WARD-VALUE" not in (label or "")

                page.click("#p")
                assert backend.focused_field_label() == "Policy #"

                page.click("#n")
                assert backend.focused_field_label() == "member_id"
            finally:
                browser.close()


# -- record-time capture (driven Recorder, fake backend) ----------------------


def _png(size: tuple[int, int] = (64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (250, 250, 250)).save(buf, format="PNG")
    return buf.getvalue()


class _LabelBackend:
    """Minimal Backend exposing the optional focused_field_label seam."""

    def __init__(self, label: str | None) -> None:
        self._label = label
        self._png = _png()

    @property
    def viewport(self) -> tuple[int, int]:
        return (64, 48)

    def screenshot(self) -> bytes:
        return self._png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        pass

    def type_text(self, text: str) -> None:
        pass

    def press(self, key: str) -> None:
        pass

    def scroll(self, dx: int, dy: int) -> None:
        pass

    def focused_field_label(self) -> str | None:
        return self._label


class _RaisingLabelBackend(_LabelBackend):
    def focused_field_label(self) -> str | None:
        raise RuntimeError("momentary a11y failure")


class TestRecorderFieldLabelCapture:
    def _events(self, rec_dir: Path) -> list[dict]:
        lines = (rec_dir / "events.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines]

    def test_label_captured_on_type_event(self, tmp_path: Path) -> None:
        rec = Recorder(_LabelBackend("Insurance  No."), tmp_path / "rec")
        rec.type_text(INSURANCE_VALUE)
        events = self._events(rec.finish())
        # Whitespace-collapsed, passive evidence on the event.
        assert events[0]["field_label"] == "Insurance No."

    def test_no_seam_and_failure_are_silent(self, tmp_path: Path) -> None:
        rec = Recorder(_LabelBackend(None), tmp_path / "rec_none")
        rec.type_text("x")
        assert "field_label" not in self._events(rec.finish())[0]

        rec = Recorder(_RaisingLabelBackend(None), tmp_path / "rec_raise")
        rec.type_text("x")
        assert "field_label" not in self._events(rec.finish())[0]
