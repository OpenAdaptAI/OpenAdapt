"""Tests for PixelSurfaceFaultDriver (Windows/RDP/Citrix pixel fault staging)."""

from __future__ import annotations

import io
from typing import Any, Optional

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.qualification_faults import (
    QualificationFaultContext,
    QualificationFaultDriver,
    effect_verifier_input_sha256,
    expected_fault_detector,
    sha256_bytes,
    verify_fault_mutation_receipt,
)
from openadapt_flow.qualify.faults import (
    PIXEL_SURFACE_FAULT_DRIVER_CONTRACT_SHA256,
    PIXEL_SURFACE_FAULT_DRIVER_ID,
    PixelSurfaceFaultDriver,
    ScreenOnlyRecordCheck,
    mutate_pixel_frame,
)
from openadapt_flow.verification import VerificationTier


def _make_sample_frame() -> bytes:
    img = Image.new("RGB", (320, 200), (245, 247, 250))
    draw = ImageDraw.Draw(img)
    # Identifier region at (20, 20, 140, 30)
    draw.rectangle((20, 20, 160, 50), fill=(255, 255, 255), outline=(30, 41, 59))
    draw.text((28, 28), "MRN-1001", fill=(15, 23, 42))
    # Save button region at (40, 100, 80, 32)
    draw.rectangle((40, 100, 120, 132), fill=(37, 99, 235), outline=(29, 78, 216))
    draw.text((58, 110), "Save", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_workflow() -> Workflow:
    return Workflow(
        name="rdp-save",
        steps=[
            Step(
                id="save_step",
                intent="Click Save on patient chart",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(40, 100, 80, 32),
                    click_point=(80, 116),
                    identifier_region=(20, 20, 140, 30),
                ),
            ),
            Step(
                id="no_anchor_step",
                intent="Press Enter",
                action=ActionKind.KEY,
                key="Enter",
            ),
        ],
    )


class _DummyPixelBackend:
    def __init__(self, frame: bytes) -> None:
        self._base = frame
        self._override: Optional[bytes] = None

    def screenshot(self) -> bytes:
        return self._override if self._override is not None else self._base

    def set_pixel_fault_frame(self, frame: bytes) -> None:
        self._override = frame

    def clear_pixel_fault_frame(self) -> None:
        self._override = None


class _PlainScreenshotBackend:
    def __init__(self, frame: bytes) -> None:
        self._frame = frame

    def screenshot(self) -> bytes:
        return self._frame


class _StrongVerifier:
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM


def _make_context(
    *,
    fault_kind: Any,
    backend: Any,
    before_sha256: str,
    step_id: str = "save_step",
    actuation_path: Any = "gui",
    effect_verifier: Any = None,
) -> QualificationFaultContext:
    gate, _code = expected_fault_detector(fault_kind)
    zero_hex = "0" * 64
    return QualificationFaultContext(
        project_id="proj-rdp",
        project_revision=1,
        project_contract_sha256=zero_hex,
        campaign_id_sha256=zero_hex,
        case_id_sha256=zero_hex,
        case_input_sha256=zero_hex,
        run_id_sha256=zero_hex,
        step_id=step_id,
        actuation_path=actuation_path,
        fault_kind=fault_kind,
        gate=gate,
        before_input_sha256=before_sha256,
        backend=backend,
        vision=None,
        effect_verifier=effect_verifier,
    )


class TestPixelSurfaceFaultDriver:
    def test_implements_protocol_and_metadata(self) -> None:
        driver = PixelSurfaceFaultDriver(_make_workflow())
        assert isinstance(driver, QualificationFaultDriver)
        assert driver.driver_id == PIXEL_SURFACE_FAULT_DRIVER_ID
        assert driver.contract_sha256 == PIXEL_SURFACE_FAULT_DRIVER_CONTRACT_SHA256

    @pytest.mark.parametrize("kind", ["ambiguity", "wrong_identity", "stale_identity"])
    def test_screen_faults_produce_signed_verifiable_receipts(self, kind: str) -> None:
        frame = _make_sample_frame()
        backend = _DummyPixelBackend(frame)
        driver = PixelSurfaceFaultDriver(_make_workflow())
        before_sha = sha256_bytes(frame)

        ctx = _make_context(fault_kind=kind, backend=backend, before_sha256=before_sha)
        mutation = driver.mutate(ctx)
        assert mutation is not None
        assert driver.declined is None
        assert mutation.replace_effect_verifier is False

        receipt = mutation.receipt
        assert receipt.fault_kind == kind
        assert receipt.before_input_sha256 == before_sha
        assert receipt.after_input_sha256 != before_sha
        assert receipt.after_input_sha256 == sha256_bytes(backend.screenshot())
        assert verify_fault_mutation_receipt(
            receipt, trusted_public_key_base64=driver.public_key_base64
        )

        # Cleanup restores original frame.
        driver.cleanup(backend)
        assert backend.screenshot() == frame

    def test_screenshot_patch_fallback_and_cleanup(self) -> None:
        frame = _make_sample_frame()
        backend = _PlainScreenshotBackend(frame)
        driver = PixelSurfaceFaultDriver(_make_workflow())
        ctx = _make_context(
            fault_kind="stale_identity",
            backend=backend,
            before_sha256=sha256_bytes(frame),
        )
        mutation = driver.mutate(ctx)
        assert mutation is not None
        assert sha256_bytes(backend.screenshot()) == mutation.receipt.after_input_sha256

        driver.cleanup(backend)
        assert backend.screenshot() == frame

    def test_ambiguity_clones_target_pixels_and_wrong_identity_alters_id_box(
        self,
    ) -> None:
        frame = _make_sample_frame()
        amb_png = mutate_pixel_frame(
            frame,
            fault_kind="ambiguity",
            region=(40, 100, 80, 32),
        )
        with Image.open(io.BytesIO(amb_png)) as amb_img:
            orig_crop = amb_img.crop((40, 100, 120, 132)).tobytes()
            # Placed to the right with 8px pad: x = 128..208
            dup_crop = amb_img.crop((128, 100, 208, 132)).tobytes()
            assert orig_crop == dup_crop

        wrong_png = mutate_pixel_frame(
            frame,
            fault_kind="wrong_identity",
            region=(40, 100, 80, 32),
            identifier_region=(20, 20, 140, 30),
        )
        with (
            Image.open(io.BytesIO(frame)) as orig_img,
            Image.open(io.BytesIO(wrong_png)) as wrong_img,
        ):
            assert (
                orig_img.crop((20, 20, 160, 50)).tobytes()
                != wrong_img.crop((20, 20, 160, 50)).tobytes()
            )
            # Save button outside identifier_region remains unchanged.
            assert (
                orig_img.crop((40, 100, 120, 132)).tobytes()
                == wrong_img.crop((40, 100, 120, 132)).tobytes()
            )

    @pytest.mark.parametrize("kind", ["weak_effect", "missing_effect"])
    def test_effect_faults_replace_verifier_and_sign_receipt(self, kind: str) -> None:
        frame = _make_sample_frame()
        backend = _DummyPixelBackend(frame)
        driver = PixelSurfaceFaultDriver(_make_workflow())
        strong = _StrongVerifier()
        before_sha = effect_verifier_input_sha256(strong, ())

        ctx = _make_context(
            fault_kind=kind,
            backend=backend,
            before_sha256=before_sha,
            effect_verifier=strong,
        )
        mutation = driver.mutate(ctx)
        assert mutation is not None
        assert mutation.replace_effect_verifier is True
        if kind == "weak_effect":
            assert isinstance(mutation.effect_verifier, ScreenOnlyRecordCheck)
        else:
            assert mutation.effect_verifier is None
        assert verify_fault_mutation_receipt(
            mutation.receipt, trusted_public_key_base64=driver.public_key_base64
        )

    def test_declines_cleanly_when_not_gui_or_missing_anchor(self) -> None:
        frame = _make_sample_frame()
        backend = _DummyPixelBackend(frame)
        driver = PixelSurfaceFaultDriver(_make_workflow())

        api_ctx = _make_context(
            fault_kind="ambiguity",
            backend=backend,
            before_sha256=sha256_bytes(frame),
            actuation_path="api",
        )
        assert driver.mutate(api_ctx) is None
        assert driver.declined == "pixel_fault_needs_gui"

        no_anchor_ctx = _make_context(
            fault_kind="ambiguity",
            backend=backend,
            before_sha256=sha256_bytes(frame),
            step_id="no_anchor_step",
        )
        assert driver.mutate(no_anchor_ctx) is None
        assert driver.declined == "step_anchor_unavailable"
