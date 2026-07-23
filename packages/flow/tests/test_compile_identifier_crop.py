"""Compile-time identifier-crop emission (identity-on-pixels arming).

The pixel-compare identity tier (``runtime.identity.verify_pixel_identity``)
is production-reachable only when the compiler persists a recorded identifier
crop. These tests pin that emission and the guarantees it is built to
preserve:

* a pixel-only recording (an armed OCR identity band, NO structured identity —
  UIA/DOM does not cross the ICA/RDP boundary) compiles WITH an
  ``anchor.identifier_crop`` + ``identifier_region`` and the crop file on
  disk, under ``templates/identifiers/`` so it rides the SAME sealed-asset
  handling as every other image crop;
* a recording that DID capture structured identity (browser DOM / Windows
  UIA) gets NO crop — the structured tier owns identity and no identity
  pixels are written at rest — and the step records WHY
  (``Step.identifier_crop_missing_reason``, the explicit degrade);
* an explicitly MARKED identifier region (record-time ``--identifier``) wins
  over the automatic band box and forces a crop even alongside structured
  identity;
* an encrypted save seals the crop (``.enc``, plaintext removed) and an
  encrypted load exposes it via ``decrypted_template`` — no cleartext
  identifier pixels at rest, and none needed at replay;
* the captured crop drives a WRONG-identifier HALT through the REAL
  ``Replayer._verify_identity`` ladder: the pixel tier MISMATCHES a one-glyph
  -different live crop, ABSTAINS (not VERIFY) on the same value, and the
  VERIFY path stays hard-gated off — arming the tier can only add a safe
  halt, never a pixel false-accept (zero-false-accept preserved).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytest

import openadapt_flow.vision as vision
from openadapt_flow.compiler import compile_recording
from openadapt_flow.compiler.compile import (
    IDCROP_REASON_STRUCTURED,
    IDCROP_REASON_UNARMED,
    IDENTIFIER_CROP_DIR,
)
from openadapt_flow.ir import ActionKind, Resolution, Workflow
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.replayer import Replayer

VIEWPORT = (1280, 800)
# A patient identity row: name + DOB (far from the recording date => kept as
# identity, not chronology) + MRN, drawn to the LEFT of the click target so it
# lands in the click row's identity band but OUTSIDE the target's own template
# crop.
ROW_Y = 430
IDENTITY_TEXT = "MARIA GARCIA  DOB 1974-08-21  MRN AC50061"
# One-glyph-different MRN (AC50061 -> AC58061): a different patient whose only
# difference is a single digit — the wrong-patient the pixel tier must catch.
IDENTITY_TEXT_WRONG = "MARIA GARCIA  DOB 1974-08-21  MRN AC58061"


def _blank() -> np.ndarray:
    return np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, dtype=np.uint8)


def _draw_text(img: np.ndarray, x: int, y: int, text: str) -> None:
    cv2.putText(
        img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2, cv2.LINE_AA
    )


def _draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
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


def _before_frame(identity_text: str = IDENTITY_TEXT) -> np.ndarray:
    img = _blank()
    _draw_text(img, 80, ROW_Y + 8, identity_text)
    # click target ("Open") on the same row, to the right of the identity text
    _draw_button(img, 980, ROW_Y - 22, 120, 44, "Open")
    return img


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _write_frame(recording: Path, i: int, suffix: str, img: np.ndarray) -> None:
    (recording / "frames" / f"{i:04d}_{suffix}.png").write_bytes(_png(img))


def _build_recording(
    tmp_path: Path,
    *,
    with_structured: bool,
    event_identifier_region: Optional[list[int]] = None,
    meta_identifier_region: Optional[list[int]] = None,
) -> tuple[Path, Path]:
    recording = tmp_path / "recording"
    bundle = tmp_path / "bundle"
    (recording / "frames").mkdir(parents=True)

    before = _before_frame()
    after = before.copy()
    _draw_text(after, 420, 620, "Chart Opened")  # a localized postcondition change

    click: dict = {"i": 0, "kind": "click", "x": 1040, "y": ROW_Y, "t": 1.0}
    if with_structured:
        click["structured_identity"] = IDENTITY_TEXT
    if event_identifier_region is not None:
        click["identifier_region"] = event_identifier_region
    _write_frame(recording, 0, "before", before)
    _write_frame(recording, 0, "after", after)
    (recording / "events.jsonl").write_text(json.dumps(click) + "\n")
    meta: dict = {
        "id": "rec-idcrop-001",
        "created_at": "2026-07-06T00:00:00+00:00",
        "viewport": list(VIEWPORT),
        "app_url": "http://localhost:0/",
        "params": {},
    }
    if meta_identifier_region is not None:
        meta["identifier_region"] = meta_identifier_region
    (recording / "meta.json").write_text(json.dumps(meta))
    return recording, bundle


def _click_step(workflow):
    return next(s for s in workflow.steps if s.action is ActionKind.CLICK)


def test_pixel_only_recording_emits_identifier_crop(tmp_path: Path) -> None:
    recording, bundle = _build_recording(tmp_path, with_structured=False)
    workflow = compile_recording(recording, bundle, name="idcrop-pixel")

    step = _click_step(workflow)
    anchor = step.anchor
    assert anchor is not None
    # Identity is armed on the pixel substrate (OCR band read the row) ...
    assert anchor.context_text is not None or anchor.identity_template is not None
    # ... so the identifier crop was emitted, with no degrade reason.
    assert anchor.identifier_crop is not None
    assert anchor.identifier_region is not None
    assert step.identity_armed is True
    assert step.identifier_crop_missing_reason is None
    # Sealed-handling contract: the crop lives UNDER templates/ so encrypted
    # saves, the integrity manifest, and the run gate's cleartext check all
    # cover it exactly like the template crops.
    assert anchor.identifier_crop.startswith(f"{IDENTIFIER_CROP_DIR}/")
    crop_path = bundle / anchor.identifier_crop
    assert crop_path.is_file()
    # A real crop (decodable, non-empty) inside the frame.
    rx, ry, rw, rh = anchor.identifier_region
    assert rw > 0 and rh > 0
    assert 0 <= rx and 0 <= ry
    assert rx + rw <= VIEWPORT[0] and ry + rh <= VIEWPORT[1]
    img = cv2.imdecode(
        np.frombuffer(crop_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR
    )
    assert img is not None and img.size > 0
    # The crop is hashed into the sealed integrity manifest like any template.
    assert workflow.manifest is not None
    assert anchor.identifier_crop in workflow.manifest.file_hashes


def test_structured_recording_writes_no_crop_and_records_why(
    tmp_path: Path,
) -> None:
    recording, bundle = _build_recording(tmp_path, with_structured=True)
    workflow = compile_recording(recording, bundle, name="idcrop-structured")

    step = _click_step(workflow)
    anchor = step.anchor
    assert anchor is not None
    # Structured identity present => the structured tier owns identity; no
    # identity pixels are persisted at rest — and the degrade is EXPLICIT.
    assert anchor.identifier_crop is None
    assert anchor.identifier_region is None
    assert step.identifier_crop_missing_reason == IDCROP_REASON_STRUCTURED
    assert not (bundle / IDENTIFIER_CROP_DIR).exists()


def test_event_marked_region_wins_and_forces_crop_despite_structured(
    tmp_path: Path,
) -> None:
    """A record-time ``--identifier`` marking is the operator's stated intent:
    honor it even when structured identity was captured (a structural
    recording replayed over Citrix/RDP still needs the pixel tier)."""
    marked = [60, ROW_Y - 24, 560, 40]  # the identity text's cell
    recording, bundle = _build_recording(
        tmp_path, with_structured=True, event_identifier_region=marked
    )
    workflow = compile_recording(recording, bundle, name="idcrop-marked")

    step = _click_step(workflow)
    anchor = step.anchor
    assert anchor is not None
    assert anchor.identifier_crop is not None
    assert step.identity_armed is True
    assert step.identifier_crop_missing_reason is None
    assert anchor.identifier_region == tuple(marked)
    assert (bundle / anchor.identifier_crop).is_file()


def test_meta_marked_region_applies_to_pixel_recording(tmp_path: Path) -> None:
    """Desktop marking (``record --identifier X,Y,W,H`` -> meta.json) scopes
    the crop to the operator-designated region instead of the band box."""
    marked = [60, ROW_Y - 24, 560, 40]
    recording, bundle = _build_recording(
        tmp_path, with_structured=False, meta_identifier_region=marked
    )
    workflow = compile_recording(recording, bundle, name="idcrop-meta-marked")

    anchor = _click_step(workflow).anchor
    assert anchor is not None
    assert anchor.identifier_region == tuple(marked)
    assert anchor.identifier_crop is not None


def test_invalid_marked_region_degrades_to_band_with_warning(
    tmp_path: Path,
) -> None:
    """A marked region fully outside the frame is not honored silently: the
    compiler falls back to the automatic band box (crop still emitted on a
    pixel recording) rather than emitting an empty crop."""
    recording, bundle = _build_recording(
        tmp_path,
        with_structured=False,
        event_identifier_region=[5000, 5000, 40, 40],
    )
    workflow = compile_recording(recording, bundle, name="idcrop-bad-mark")
    anchor = _click_step(workflow).anchor
    assert anchor is not None
    assert anchor.identifier_crop is not None  # band fallback
    assert anchor.identifier_region != (5000, 5000, 40, 40)


def test_malformed_marked_region_fails_loud(tmp_path: Path) -> None:
    recording, bundle = _build_recording(
        tmp_path, with_structured=False, meta_identifier_region=[1, 2, 3]
    )
    with pytest.raises(ValueError, match="malformed identifier_region"):
        compile_recording(recording, bundle, name="idcrop-malformed")


def test_unarmed_click_records_unarmed_reason(tmp_path: Path) -> None:
    """A click with NO identity evidence at all (blank row) gets no crop and
    says so via the unarmed degrade reason."""
    recording = tmp_path / "recording"
    bundle = tmp_path / "bundle"
    (recording / "frames").mkdir(parents=True)
    before = _blank()
    _draw_button(before, 980, ROW_Y - 22, 120, 44, "Open")
    after = before.copy()
    _draw_text(after, 420, 620, "Chart Opened")
    _write_frame(recording, 0, "before", before)
    _write_frame(recording, 0, "after", after)
    (recording / "events.jsonl").write_text(
        json.dumps({"i": 0, "kind": "click", "x": 1040, "y": ROW_Y, "t": 1.0}) + "\n"
    )
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-idcrop-002",
                "created_at": "2026-07-06T00:00:00+00:00",
                "viewport": list(VIEWPORT),
                "app_url": "http://localhost:0/",
                "params": {},
            }
        )
    )
    workflow = compile_recording(recording, bundle, name="idcrop-unarmed")
    step = _click_step(workflow)
    assert step.identity_armed is False
    assert step.anchor is not None and step.anchor.identifier_crop is None
    assert step.identifier_crop_missing_reason == IDCROP_REASON_UNARMED


def test_encrypted_save_seals_the_identifier_crop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PHI-at-rest: the identifier crop IS identity pixels, so an encrypted
    bundle must leave no cleartext copy on disk — and an encrypted load must
    still hand the replayer the plaintext crop in memory."""
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "idcrop-test-passphrase")
    recording, bundle = _build_recording(tmp_path, with_structured=False)
    workflow = compile_recording(recording, bundle, name="idcrop-sealed")
    anchor = _click_step(workflow).anchor
    assert anchor is not None and anchor.identifier_crop is not None
    plaintext = (bundle / anchor.identifier_crop).read_bytes()

    workflow.save(bundle, encrypt=True)
    assert not (bundle / anchor.identifier_crop).exists()
    assert (bundle / f"{anchor.identifier_crop}.enc").is_file()

    loaded = Workflow.load(bundle)
    assert loaded.encrypted
    assert loaded.decrypted_template(anchor.identifier_crop) == plaintext


class _PixelBackend:
    """Minimal pixel-only backend: screenshots, no structured_text_at."""

    def __init__(self, png: bytes, viewport: tuple[int, int]) -> None:
        self._png = png
        self.viewport = viewport

    def screenshot(self) -> bytes:
        return self._png

    def click(self, x: int, y: int, double: bool = False) -> None:  # pragma: no cover
        pass

    def type_text(self, text: str) -> None:  # pragma: no cover
        pass

    def press(self, key: str) -> None:  # pragma: no cover
        pass

    def scroll(self, dx: int, dy: int) -> None:  # pragma: no cover
        pass


def test_compiled_crop_arms_pixel_tier_in_replayer_ladder(tmp_path: Path) -> None:
    """Runtime round-trip: the crop the COMPILER emitted drives the REAL
    ``Replayer._verify_identity`` ladder to a pixel-tier MISMATCH (halt) when
    the live screen shows a one-glyph-different MRN — and never a pixel
    VERIFY (the gate stays off), so arming cannot false-accept."""
    recording, bundle = _build_recording(tmp_path, with_structured=False)
    workflow = compile_recording(recording, bundle, name="idcrop-roundtrip")
    step = _click_step(workflow)
    anchor = step.anchor
    assert anchor is not None and anchor.identifier_crop is not None

    wrong_png = _png(_before_frame(IDENTITY_TEXT_WRONG))
    resolution = Resolution(
        rung="ocr",
        point=anchor.click_point,
        confidence=0.9,
        elapsed_ms=1.0,
    )
    replayer = Replayer(_PixelBackend(wrong_png, VIEWPORT), vision=vision)
    check = replayer._verify_identity(step, resolution, wrong_png, {}, workflow, bundle)
    assert check.mode == "pixel"
    assert check.status == "mismatch"

    # Same value re-cut from the identical recorded frame: no localized spike,
    # so the pixel tier ABSTAINS and a verdict — whatever tier supplies it —
    # is never a pixel VERIFY (the gate is off).
    same_png = _png(_before_frame(IDENTITY_TEXT))
    same_replayer = Replayer(_PixelBackend(same_png, VIEWPORT), vision=vision)
    same_check = same_replayer._verify_identity(
        step, resolution, same_png, {}, workflow, bundle
    )
    assert not (same_check.mode == "pixel" and same_check.status == "verified")

    # A deployment that has explicitly qualified positive pixel verification
    # (the RDP ladder harness does so for its bounded synthetic fixture) must be
    # able to consume the exact compiler-emitted crop and verify the unchanged
    # live identifier before a governed pointer action.
    qualified_replayer = Replayer(
        _PixelBackend(same_png, VIEWPORT),
        vision=vision,
        pixel_verify_enabled=True,
    )
    qualified_check = qualified_replayer._verify_identity(
        step, resolution, same_png, {}, workflow, bundle
    )
    assert qualified_check.mode == "pixel"
    assert qualified_check.status == "verified"

    # The gate that makes the above unconditional: the pixel tier can never
    # VERIFY by default, so it can never false-accept unless an exact bounded
    # deployment has opted in after qualification.
    assert identity_mod.PIXEL_VERIFY_ENABLED is False


def test_encrypted_bundle_crop_reaches_pixel_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sealed crops must still ARM the tier: after an encrypted save + load,
    the replayer reads the crop from the in-memory decrypted store (never
    from a cleartext file) and the wrong-MRN mismatch still fires."""
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "idcrop-test-passphrase")
    recording, bundle = _build_recording(tmp_path, with_structured=False)
    workflow = compile_recording(recording, bundle, name="idcrop-sealed-arm")
    workflow.save(bundle, encrypt=True)
    loaded = Workflow.load(bundle)
    step = _click_step(loaded)
    anchor = step.anchor
    assert anchor is not None and anchor.identifier_crop is not None
    assert not (bundle / anchor.identifier_crop).exists()  # sealed, no cleartext

    wrong_png = _png(_before_frame(IDENTITY_TEXT_WRONG))
    resolution = Resolution(
        rung="ocr", point=anchor.click_point, confidence=0.9, elapsed_ms=1.0
    )
    replayer = Replayer(_PixelBackend(wrong_png, VIEWPORT), vision=vision)
    check = replayer._verify_identity(step, resolution, wrong_png, {}, loaded, bundle)
    assert check.mode == "pixel"
    assert check.status == "mismatch"
