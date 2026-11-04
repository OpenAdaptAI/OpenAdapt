"""Identity evidence must be verifiable by the runtime that runs the step.

An irreversible step whose identity check can't reach ``verified`` halts. These
tests pin the capability table the compiler consults, and the runtime verdicts
for a step whose only identity evidence is a pixel crop: no surrounding text,
so neither the structured tier nor the OCR tier can judge it.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Resolution,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.replayer import Replayer

_H, _W = I.PIXEL_CANON


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def _button(val: int = 255) -> np.ndarray:
    """A plain control render with one dark bar standing in for its glyphs."""
    img = np.full((_H, _W), val, np.uint8)
    img[:, 60:66] = 0
    return img


def _id_crop() -> np.ndarray:
    """An identifier crop with real structure (text), unlike ``_button``'s
    full-height bar, which matches itself shifted and so is featureless."""
    img = np.full((_H, _W), 255, np.uint8)
    cv2.putText(
        img, "MG4408 Okafor", (6, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 0, 2, cv2.LINE_AA
    )
    return img


def _with_extra_bar(img: np.ndarray) -> np.ndarray:
    other = img.copy()
    other[:, 140:145] = 0
    return other


class _Backend:
    viewport = (_W, _H)

    def screenshot(self):
        return _png(_button())

    def click(self, x, y, *, double=False): ...

    def wait_settled(self, **kw):
        return self.screenshot()


def _crop_only_step(tmp_path: Path, recorded: np.ndarray) -> tuple[Step, Resolution]:
    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "templates" / "id.png").write_bytes(_png(recorded))
    (tmp_path / "templates" / "t.png").write_bytes(_png(recorded))
    step = Step(
        id="s_open",
        intent="click 'Open next referral'",
        action=ActionKind.CLICK,
        risk="irreversible",
        identity_armed=True,
        anchor=Anchor(
            template="templates/t.png",
            region=(0, 0, _W, _H),
            click_point=(0, 0),
            identifier_crop="templates/id.png",
            identifier_region=(0, 0, _W, _H),
        ),
    )
    assert not step.anchor.context_text and not step.anchor.structured_identity
    res = Resolution(rung="structural", point=(0, 0), confidence=1.0, elapsed_ms=1.0)
    return step, res


def _gate(tmp_path: Path, step: Step, res: Resolution, live: np.ndarray, **kw):
    replayer = Replayer(_Backend(), vision=object(), poll_interval_s=0.01, **kw)
    result = StepResult(
        step_id=step.id, intent=step.intent, ok=False, risk="irreversible"
    )
    error = replayer._identity_gate_error(
        step, res, _png(live), {}, Workflow(name="wf"), tmp_path, result
    )
    return error, result.identity


def test_capability_table() -> None:
    default = I.verifiable_identity_evidence("web")
    assert "identifier_crop" not in default
    assert default == {"structured_text", "context_band"}
    assert I.verifiable_identity_evidence(None) == default
    assert I.verifiable_identity_evidence("rdp") == {"context_band"}
    assert I.verifiable_identity_evidence("citrix") == {"context_band"}
    for kind in ("web", "windows", "macos", "linux", "rdp", "citrix"):
        assert "identifier_crop" in I.verifiable_identity_evidence(
            kind, pixel_verify_enabled=True
        )


def test_no_surrounding_text_valid_crop_verifies_where_crops_are_verifiable(
    tmp_path: Path,
) -> None:
    step, res = _crop_only_step(tmp_path, _button())
    # A read-only step: nothing consequential follows it.
    step.risk = "reversible"
    step.intent = "click 'View referral'"
    error, check = _gate(tmp_path, step, res, _button(), pixel_verify_enabled=True)
    assert error is None
    assert check is not None
    assert check.status == "verified" and check.mode == "pixel"
    # A full-height bar matches itself shifted, so it cannot count rows; with
    # nothing consequential after the step it runs and records that.
    assert check.census_status == "cannot_count"


def test_a_crop_found_once_on_the_frame_runs_before_a_write(
    tmp_path: Path,
) -> None:
    """No OCR band: the census counts where the crop verifies. Once: it runs."""

    step, res = _crop_only_step(tmp_path, _id_crop())
    error, check = _gate(tmp_path, step, res, _id_crop(), pixel_verify_enabled=True)
    assert error is None
    assert check is not None and check.status == "verified"
    assert check.candidate_count == 1


def test_a_crop_found_twice_on_the_frame_halts(tmp_path: Path) -> None:
    """Another row shows the same identifier pixels: the step stops."""

    step, res = _crop_only_step(tmp_path, _id_crop())
    frame = np.vstack([_id_crop(), np.full((8, _W), 255, np.uint8), _id_crop()])
    error, check = _gate(tmp_path, step, res, frame, pixel_verify_enabled=True)
    assert error is not None and "2 shown rows" in error
    assert check is not None
    assert check.status == "abstain" and check.ambiguity_halt == "identical_candidates"
    assert check.candidate_count == 2


def test_crop_that_does_not_match_halts(tmp_path: Path) -> None:
    # A different control under the point: the pixel tier MISMATCHES whether
    # or not its VERIFY path is on, and the step halts before acting.
    step, res = _crop_only_step(tmp_path, _button())
    for enabled in (True, False):
        error, check = _gate(
            tmp_path,
            step,
            res,
            _with_extra_bar(_button()),
            pixel_verify_enabled=enabled,
        )
        assert error is not None and "refusing to act" in error, enabled
        assert check is not None and check.status == "mismatch", enabled


def test_crop_the_runtime_cannot_verify_still_halts(tmp_path: Path) -> None:
    # The runtime never accepts without evidence it can verify: with pixel
    # VERIFY off, a matching crop abstains, nothing else can judge the
    # target, and the irreversible step halts. This is why the compiler
    # doesn't attach such a crop.
    step, res = _crop_only_step(tmp_path, _button())
    error, check = _gate(tmp_path, step, res, _button())
    assert error is not None
    assert check is not None and check.status == "unreadable"
