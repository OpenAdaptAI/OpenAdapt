"""Effective-config wiring for the pixel-compare identity VERIFY branch.

The pixel tier's VERIFY path is gated by ``verify_pixel_identity``'s
``enable_verify`` argument, which defaults to the module constant
``PIXEL_VERIFY_ENABLED`` (False). The engine now resolves that gate from the
deployment's runtime posture (``deployment.yaml`` -> ``runtime.pixel_verify_enabled``),
threaded through ``build_replayer`` -> ``Replayer`` -> the identity ladder's
pixel tier (``enable_verify=self.pixel_verify_enabled``), instead of leaving it
pinned to the module default. These tests pin the SAFE DEFAULT (OFF, exactly
today's behavior) and the seam that lets an operator arm it explicitly.

The default suite stays browser-free; the pixel crops are decoded from tiny
in-memory PNGs, no OCR/network.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from openadapt_flow.deployment import (
    DeploymentConfig,
    RuntimeSection,
    load_deployment,
)
from openadapt_flow.runtime import identity as I

# ---------------------------------------------------------------------------
# tiny synthetic identifier crops (see tests/test_identity_ladder.py)
# ---------------------------------------------------------------------------

_H, _W = 40, 260


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def _blank(val: int = 255) -> np.ndarray:
    return np.full((_H, _W), val, np.uint8)


def _with_bar(cols: int, x0: int = 100, val: int = 0) -> np.ndarray:
    img = _blank()
    img[:, x0 : x0 + cols] = val
    return img


# ---------------------------------------------------------------------------
# config schema: safe default + load path
# ---------------------------------------------------------------------------


def test_runtime_section_default_is_off() -> None:
    # SAFE DEFAULT: an unconfigured runtime never arms the experimental VERIFY
    # branch, so replay behaves exactly as it does today.
    assert RuntimeSection().pixel_verify_enabled is False
    assert DeploymentConfig().runtime.pixel_verify_enabled is False


def test_load_deployment_arms_flag_when_set(tmp_path: Path) -> None:
    cfg_path = tmp_path / "deployment.yaml"
    cfg_path.write_text("runtime:\n  pixel_verify_enabled: true\n")
    cfg = load_deployment(cfg_path)
    assert cfg.runtime.pixel_verify_enabled is True


def test_load_deployment_absent_flag_defaults_off(tmp_path: Path) -> None:
    cfg_path = tmp_path / "deployment.yaml"
    # A runtime section that sets something ELSE must leave the flag at its
    # safe default (absent -> False), not flip it on.
    cfg_path.write_text("runtime:\n  durable: true\n")
    cfg = load_deployment(cfg_path)
    assert cfg.runtime.pixel_verify_enabled is False


# ---------------------------------------------------------------------------
# behavior: the parameter is what gates the VERIFY branch
# ---------------------------------------------------------------------------


def test_verify_branch_is_gated_by_the_parameter() -> None:
    # A byte-identical crop aligns cleanly (zero drift, worst window ~0), so it
    # lands squarely in the gated VERIFY branch. With the gate OFF it must
    # ABSTAIN (None); with the gate ON it returns a pixel `verified`.
    png = _png(_with_bar(6))

    assert I.verify_pixel_identity(png, png, enable_verify=False) is None

    armed = I.verify_pixel_identity(png, png, enable_verify=True)
    assert armed is not None
    assert armed.status == "verified"
    assert armed.mode == "pixel"


def test_safe_default_never_verifies_without_the_arg() -> None:
    # Called with NO enable_verify argument, the module constant (False) is the
    # default, so the same otherwise-matching crop still ABSTAINS -- the
    # load-bearing "never false-accepts by default" guarantee, unchanged by the
    # config seam.
    assert I.PIXEL_VERIFY_ENABLED is False
    png = _png(_with_bar(6))
    assert I.verify_pixel_identity(png, png) is None


def test_arming_the_flag_does_not_defeat_a_real_mismatch() -> None:
    # Arming VERIFY must NOT weaken the HALT half: a localized glyph change is
    # still a mismatch regardless of the gate.
    rec = _png(_blank())
    live = _png(_with_bar(5))
    for enabled in (False, True):
        check = I.verify_pixel_identity(rec, live, enable_verify=enabled)
        assert check is not None
        assert check.status == "mismatch"
        assert check.mode == "pixel"


# ---------------------------------------------------------------------------
# engine wiring: the resolved config reaches the Replayer
# ---------------------------------------------------------------------------


def test_replayer_stores_pixel_verify_enabled() -> None:
    from openadapt_flow.runtime import Replayer

    class _StubBackend:
        pass

    default = Replayer(_StubBackend(), vision=object())
    assert default.pixel_verify_enabled is False

    armed = Replayer(_StubBackend(), vision=object(), pixel_verify_enabled=True)
    assert armed.pixel_verify_enabled is True
