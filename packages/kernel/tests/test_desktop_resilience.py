"""Tests for Closed-Loop Visual Hover-Servoing and 3-Layer Hardened Desktop Stack."""

from __future__ import annotations

import math
import sys
from typing import Tuple

from openadapt_kernel.desktop_driver import (
    CitrixBoxGraphDriver,
    InputPacingConfig,
    NormalizedWindowRect,
    check_stream_liveness,
    compute_paced_pointer_path,
    enable_windows_per_monitor_v2_dpi,
    resume_after_desktop_reconnect,
    run_desktop_preflight_diagnostics,
)
from openadapt_kernel.macro import ControlSignature
from openadapt_kernel.probe import visual_hover_servo_click
from openadapt_kernel.stateful_twin import A11yNodeSpec


def test_visual_hover_servo_converges_across_dpi_scales_and_citrix_letterbox() -> None:
    """Open-loop clicks miss on 1.25x/1.5x/2.0x + (120, 80) letterbox; visual_hover_servo_click converges <= 4 iters."""
    win_w, win_h = 1200.0, 800.0
    norm_x, norm_y = 0.65, 0.45
    letterbox_x, letterbox_y = 120.0, 80.0
    button_radius_px = 18.0

    for scale in (1.0, 1.25, 1.5, 2.0):
        # True button center in captured framebuffer coordinates:
        true_tgt_x = letterbox_x + norm_x * win_w * scale
        true_tgt_y = letterbox_y + norm_y * win_h * scale

        # Open-loop command assumes scale=1.0, offset=(0,0) -> observes at (cmd_x * scale, cmd_y * scale)
        open_cmd_x, open_cmd_y = norm_x * win_w, norm_y * win_h
        open_obs_x, open_obs_y = open_cmd_x * scale, open_cmd_y * scale
        open_err = math.hypot(true_tgt_x - open_obs_x, true_tgt_y - open_obs_y)
        assert open_err > button_radius_px, f"Expected open-loop miss at scale={scale}"

        def _move_and_observe(cx: float, cy: float, _s: float = scale, _tx: float = true_tgt_x, _ty: float = true_tgt_y) -> Tuple[float, float, float, float]:
            # Cursor rendered in same captured frame with unknown DPI scale (no letterbox on OS pointer):
            return (cx * _s, cy * _s, _tx, _ty)

        def _hover_probe(cx: float, cy: float, _s: float = scale, _tx: float = true_tgt_x, _ty: float = true_tgt_y) -> float:
            dist = math.hypot(cx * _s - _tx, cy * _s - _ty)
            return 0.08 if dist <= button_radius_px else 0.0

        res = visual_hover_servo_click(
            norm_x,
            norm_y,
            window_width=win_w,
            window_height=win_h,
            move_and_observe_fn=_move_and_observe,
            hover_probe_fn=_hover_probe,
            max_iters=6,
            tol_px=4.0,
            delta_threshold=0.01,
        )
        assert res.converged is True, f"Servo failed at scale={scale}: {res}"
        assert res.iterations <= 4, f"Expected <= 4 iters at scale={scale}, got {res.iterations}"
        assert res.hover_delta >= 0.01
        assert abs(res.estimated_scale_x - scale) < 0.05
        assert abs(res.estimated_scale_y - scale) < 0.05

    # Dead hover delta refuses click even when pointer reaches target coordinates.
    dead = visual_hover_servo_click(
        0.5,
        0.5,
        window_width=400.0,
        window_height=400.0,
        move_and_observe_fn=lambda cx, cy: (cx, cy, 200.0, 200.0),
        hover_probe_fn=lambda _cx, _cy: 0.0,
        delta_threshold=0.01,
    )
    assert dead.converged is False
    assert dead.reason == "hover_delta_below_threshold"


def test_normalized_window_rect_dpi_helper_and_paced_pointer_path() -> None:
    """NormalizedWindowRect computes DPI scales and conversions; pacing path reaches endpoint cleanly."""
    rect = NormalizedWindowRect(
        left=100.0,
        top=50.0,
        logical_width=1000.0,
        logical_height=500.0,
        framebuffer_width=1500.0,
        framebuffer_height=750.0,
    )
    assert rect.scale_x == 1.5
    assert rect.scale_y == 1.5
    rx, ry = rect.to_normalized(600.0, 300.0)
    assert rx == 0.5 and ry == 0.5
    px, py = rect.to_physical(rx, ry)
    assert px == 900.0 and py == 450.0

    if sys.platform != "win32":
        assert enable_windows_per_monitor_v2_dpi() is False

    path = compute_paced_pointer_path(0.0, 0.0, 120.0, 60.0, config=InputPacingConfig(pointer_steps=4))
    assert len(path) == 4
    assert path[-1] == (120.0, 60.0)
    assert path[0] == (30.0, 15.0)


def test_run_desktop_preflight_diagnostics_loopback_bind() -> None:
    """run_desktop_preflight_diagnostics verifies real 127.0.0.1 loopback bind, a11y readability, and zero DLL hooks."""
    driver = CitrixBoxGraphDriver(
        nodes=[A11yNodeSpec("btn_1", "Button", "Save", bbox=(10, 10, 100, 32), platform="citrix_box")]
    )
    report = run_desktop_preflight_diagnostics(driver, proxy_host="127.0.0.1")
    assert report.ok is True
    assert report.uia_readable is True
    assert report.hover_probe_ok is True
    assert report.loopback_proxy_ok is True
    assert report.zero_dll_injection_verified is True
    assert report.diagnostics == ()


def test_check_stream_liveness_all_states() -> None:
    """check_stream_liveness detects LIVE, DISCONNECTED, LOCKED, MINIMIZED, and FROZEN stream states."""
    live = check_stream_liveness(
        window_alive=True,
        minimized=False,
        window_title="Workspace Portal",
        prev_frame_hash="aaa",
        curr_frame_hash="bbb",
        hover_probe_delta=0.05,
    )
    assert live.alive is True and live.state == "LIVE"

    disc = check_stream_liveness(window_title="Session Reconnecting...")
    assert disc.alive is False and disc.state == "DISCONNECTED" and "RECONNECT_WAIT" in disc.reason

    lock = check_stream_liveness(window_title="Windows Security - LogonUI")
    assert lock.alive is False and lock.state == "LOCKED" and "RECONNECT_WAIT" in lock.reason

    mini = check_stream_liveness(minimized=True, window_title="Workspace Portal")
    assert mini.alive is False and mini.state == "MINIMIZED"

    frozen = check_stream_liveness(
        window_title="Workspace Portal",
        prev_frame_hash="same_hash",
        curr_frame_hash="same_hash",
        hover_probe_delta=0.0,
    )
    assert frozen.alive is False and frozen.state == "FROZEN"


def test_resume_after_desktop_reconnect_idempotency_and_fingerprint_block() -> None:
    """resume_after_desktop_reconnect halts on fingerprint mismatch and prevents duplicate commits via PutGet."""
    expected = [
        ControlSignature(role="textbox", label_tokens=("name",)),
        ControlSignature(role="button", label_tokens=("save",)),
    ]

    mismatch = resume_after_desktop_reconnect(
        expected,
        capture_signatures_fn=lambda: [ControlSignature(role="button", label_tokens=("login",))],
        readback_probe_fn=lambda: False,
    )
    assert mismatch["action"] == "HALT_FINGERPRINT_MISMATCH"
    assert mismatch["fingerprint_matched"] is False
    assert mismatch["missing_count"] == 2

    already_done = resume_after_desktop_reconnect(
        expected,
        capture_signatures_fn=lambda: expected,
        readback_probe_fn=lambda: True,
    )
    assert already_done["action"] == "ALREADY_COMMITTED"
    assert already_done["already_committed"] is True

    can_resume = resume_after_desktop_reconnect(
        expected,
        capture_signatures_fn=lambda: expected,
        readback_probe_fn=lambda: False,
    )
    assert can_resume["action"] == "RESUME"
    assert can_resume["fingerprint_matched"] is True
    assert can_resume["already_committed"] is False
