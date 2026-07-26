#!/usr/bin/env python3
"""No-DOM canvas vision-ladder qualification: record -> compile -> replay over a
genuine no-accessible-DOM HTML5 <canvas>, asserting the validation contract.

This is the CANVAS-CLASS analog of the Citrix Workspace-*web* surface: a remote
session painted into an HTML5 <canvas> over a WebSocket, with NO accessible
content inside the canvas. It drives the UNMODIFIED Recorder -> compiler ->
Replayer over that canvas with NO structural (a11y/DOM/UIA) backend, so
resolution can only go through the VISUAL rungs (template -> template_global ->
ocr -> geometry). It asserts:

  * record -> compile -> replay succeeds on a healthy run,
  * ZERO model calls on the healthy run (the $0 deterministic guarantee),
  * resolution used the VISUAL rungs and NEVER the structural rung,
  * the write EFFECT is independently confirmed (document oracle: the note the
    kiosk persisted equals the intended value),
  * the ladder HALTS (never silently mis-clicks) when the frame is degraded by
    injected DPI/theme/JPEG(+optional codec) drift (simulated drift on a real
    canvas session).

The surface is the `benchmark/canvas_ladder/fixture` container: a deterministic
Tk kiosk served over VNC by TigerVNC and rendered into an HTML5 <canvas> by
noVNC. The harness reads the canvas pixels via a browser backend (Playwright
`canvas.screenshot()`) and injects clicks/keys at canvas-relative pixel
coordinates that noVNC forwards over the VNC wire to the kiosk. Both directions
cross a real remote-display exchange rendered onto a no-DOM canvas.

HONEST SCOPE / LABEL: this qualifies the VISUAL RESOLUTION LADDER + CONTRACT over
a **no-DOM HTML5-canvas class** surface (the class Citrix Workspace-web presents).
It is **NOT Citrix ICA/HDX** (no HDX codecs, no ICA compression, no Workspace-
client input path), NOT an aardwolf/RDP transport proof, and the injected drift
is simulated-drift-on-a-real-session, not a WAN/HDX capture. See ./README.md and
~/oa/src/.private/rdp_citrix_validation_2026_07_20.md.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageFilter, ImageOps

# -- fixture geometry (kiosk_app.py renders these at fixed positions) ----------
VIEWPORT = (1280, 800)
ADA_ROW = (347, 192)  # "Ada Lovelace   MRN 24682" roster row
NOTE_FIELD = (410, 588)  # clinical-note entry
SAVE_BUTTON = (910, 648)  # "Save Note" beside the active-record banner
NOTE_PARAM = "note"
NOTE_VALUE = "followup in two weeks"
EXPECTED_MRN = "MRN 24682"  # Ada's MRN, written by the kiosk on save
SAVE_PATH = "/opt/canvas_fixture/saved_note.txt"
NOVNC_PATH = "vnc_lite.html?autoconnect=1&resize=off&reconnect=1"

# Playwright press() token map for named keys / simple chords.
_PRESS_MAP = {
    "enter": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "space": "Space",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
}


class CanvasBrowserBackend:
    """`Backend` over a no-DOM HTML5 <canvas> rendered by noVNC (Playwright).

    Implements ONLY the base :class:`openadapt_flow.backend.Backend` protocol --
    NOT ``StructuralBackend`` / ``IdentityBackend`` / ``StructuralActionBackend``.
    The <canvas> exposes no accessible content, so the resolver's structural rung
    is unavailable by construction and resolution runs on the visual floor
    (template / template_global / ocr / geometry); identity would fall back to
    the OCR name+DOB tier -- exactly the Citrix Workspace-web constraint.

    ``screenshot`` reads the canvas pixels; ``click`` / ``type_text`` / ``press``
    / ``scroll`` inject at canvas-relative pixel coordinates that noVNC forwards
    over the VNC wire to the remote session.
    """

    def __init__(self, page, *, width: int = 1280, height: int = 800) -> None:
        self._page = page
        self._w, self._h = width, height
        self._canvas = page.locator("canvas").first
        self._actuation_digest: Optional[str] = None
        self._actuation_bbox: Optional[tuple[float, float, float, float]] = None
        self._prepared_pointer: Optional[tuple[int, int]] = None

    def _bbox(self) -> tuple[float, float, float, float]:
        bb = self._canvas.bounding_box()
        if bb is None:
            raise RuntimeError("noVNC <canvas> not found / not visible")
        return bb["x"], bb["y"], bb["width"], bb["height"]

    @staticmethod
    def _frame_digest(png: bytes) -> str:
        """Hash decoded pixels, not PNG encoder metadata."""
        image = Image.open(io.BytesIO(png)).convert("RGB")
        return hashlib.sha256(
            image.width.to_bytes(4, "big")
            + image.height.to_bytes(4, "big")
            + image.tobytes()
        ).hexdigest()

    def _consume_actuation_lease(
        self,
        *,
        point: Optional[tuple[int, int]] = None,
        require_prepared_pointer: bool = False,
    ) -> None:
        """Recheck the exact canvas frame before the first input edge."""
        if self._actuation_digest is None or self._actuation_bbox is None:
            return
        try:
            if require_prepared_pointer and self._prepared_pointer != point:
                raise RuntimeError(
                    "consequential canvas click was not pre-positioned at the "
                    "freshly resolved target"
                )
            if self._bbox() != self._actuation_bbox:
                raise RuntimeError(
                    "noVNC canvas geometry changed after fresh-frame resolution"
                )
            current = self.screenshot()
            if self._frame_digest(current) != self._actuation_digest:
                raise RuntimeError(
                    "noVNC canvas content changed after fresh-frame resolution"
                )
        finally:
            # A consequential lease is one-shot even when it refuses.
            self._actuation_digest = None
            self._actuation_bbox = None

    # -- Backend protocol ----------------------------------------------------
    @property
    def viewport(self) -> tuple[int, int]:
        return (self._w, self._h)

    def screenshot(self) -> bytes:
        raw = self._canvas.screenshot()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # Playwright element capture can be off-by-one on sub-pixel bounds; pin
        # the frame to the exact framebuffer size so record/replay crops align.
        if img.size != (self._w, self._h):
            img = img.crop((0, 0, self._w, self._h))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    def prepare_pointer_actuation(self, x: int, y: int) -> None:
        """Move before the final frame so remote hover/cursor paint can settle."""
        self._actuation_digest = None
        self._actuation_bbox = None
        ox, oy, _, _ = self._bbox()
        self._page.mouse.move(ox + int(x), oy + int(y))
        previous: Optional[str] = None
        stable = 0
        for _ in range(8):
            self._page.wait_for_timeout(100)
            digest = self._frame_digest(self.screenshot())
            if digest == previous:
                stable += 1
            else:
                previous = digest
                stable = 1
            if stable >= 2:
                self._prepared_pointer = (int(x), int(y))
                return
        self._prepared_pointer = None
        raise RuntimeError("noVNC canvas did not settle after pointer positioning")

    def acquire_actuation_frame(self) -> bytes:
        """Arm a one-shot exact-frame lease for consequential remote input."""
        png = self.screenshot()
        self._actuation_bbox = self._bbox()
        self._actuation_digest = self._frame_digest(png)
        return png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        leased = self._actuation_digest is not None
        if leased:
            self._consume_actuation_lease(
                point=(int(x), int(y)), require_prepared_pointer=True
            )
            # The pointer was moved before the final frame. Emit only button
            # edges now; moving again would invalidate the frame we resolved.
            count = 2 if double else 1
            for index in range(count):
                self._page.mouse.down(button="left", click_count=index + 1)
                self._page.mouse.up(button="left", click_count=index + 1)
            self._prepared_pointer = None
        else:
            ox, oy, _, _ = self._bbox()
            self._page.mouse.click(
                ox + int(x), oy + int(y), click_count=2 if double else 1
            )
        self._page.wait_for_timeout(120)

    def type_text(self, text: str) -> None:
        # noVNC forwards keydown/keyup on the focused canvas over VNC; a small
        # per-char delay keeps the Tk Entry from dropping fast synthetic keys.
        self._consume_actuation_lease()
        self._page.keyboard.type(text, delay=35)
        self._page.wait_for_timeout(120)

    def press(self, key: str) -> None:
        parts = [p.strip() for p in key.split("+") if p.strip()]
        mapped = []
        for p in parts:
            low = p.lower()
            if low in ("ctrl", "control", "controlormeta"):
                # The browser is running on the orchestration host, but noVNC
                # forwards keys into a Linux/Windows remote session. Resolve
                # the portable local-host shortcut token to the remote
                # platform's Control modifier instead of Playwright choosing
                # Meta merely because the orchestrator happens to be macOS.
                mapped.append("Control")
            elif low in ("meta", "cmd", "super"):
                mapped.append("Meta")
            elif low in ("alt", "option"):
                mapped.append("Alt")
            elif low == "shift":
                mapped.append("Shift")
            else:
                mapped.append(_PRESS_MAP.get(low, p if len(p) > 1 else p))
        self._consume_actuation_lease()
        self._page.keyboard.press("+".join(mapped))
        self._page.wait_for_timeout(80)

    def scroll(self, dx: int, dy: int) -> None:
        self._page.mouse.wheel(int(dx), int(dy))
        self._page.wait_for_timeout(80)


# -- injected-drift wrapper (simulated drift on a real canvas session) ---------
class _DriftBackend:
    """Wraps a Backend and degrades every screenshot with DPI + theme + JPEG
    (+ optional blur) drift, so the resolver sees a realistically-degraded
    remote frame while the REAL canvas session is unchanged. The REAL session,
    inputs, and effect oracle are untouched -- only the pixels the resolver
    reads are degraded (honestly: simulated-drift-on-a-real-session).

    Two presets model the two regimes we prove:

    * MODERATE (``downscale=0.4``, no blur): a laggy/low-bandwidth but still
      LEGIBLE frame -- the visual ladder must resolve to the CORRECT target
      (robustness), never silently write the WRONG value.
    * SEVERE (``downscale=0.14``, ``blur=2.0``): a genuinely ILLEGIBLE frame
      (roster/MRN unreadable, see benchmark evidence) -- the ladder must find
      no confident target and HALT, rather than blind-click the recorded
      coordinates the way a naive coordinate-replay tool would.
    """

    def __init__(
        self,
        backend,
        *,
        downscale: float,
        invert: bool = True,
        jpeg_quality: int = 8,
        blur: float = 0.0,
    ) -> None:
        self._b = backend
        self._ds, self._invert, self._q, self._blur = (
            downscale,
            invert,
            jpeg_quality,
            blur,
        )

    @classmethod
    def moderate(cls, backend):
        return cls(backend, downscale=0.4, invert=True, jpeg_quality=12, blur=0.0)

    @classmethod
    def severe(cls, backend):
        return cls(backend, downscale=0.14, invert=True, jpeg_quality=5, blur=2.0)

    def __getattr__(self, name):
        return getattr(self._b, name)

    def _degrade(self, png_bytes: bytes) -> bytes:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        w, h = img.size
        img = img.resize(
            (max(1, int(w * self._ds)), max(1, int(h * self._ds))), Image.BILINEAR
        ).resize((w, h), Image.BILINEAR)
        if self._invert:
            img = ImageOps.invert(img)
        if self._blur:
            img = img.filter(ImageFilter.GaussianBlur(self._blur))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._q)
        out = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
        png = io.BytesIO()
        out.save(png, format="PNG")
        return png.getvalue()

    def screenshot(self) -> bytes:
        return self._degrade(self._b.screenshot())

    def prepare_pointer_actuation(self, x: int, y: int) -> None:
        self._b.prepare_pointer_actuation(x, y)

    def acquire_actuation_frame(self) -> bytes:
        # The underlying backend leases the real noVNC pixels while resolution
        # observes the deterministic degraded view. Delivery validates the raw
        # frame, so the benchmark cannot accidentally use the drift transform
        # as an actuation oracle.
        return self._degrade(self._b.acquire_actuation_frame())


def _read_saved_note(container: str) -> Optional[str]:
    res = subprocess.run(
        ["docker", "exec", container, "cat", SAVE_PATH],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if res.returncode != 0:
        return None
    return res.stdout.decode(errors="replace").strip()


def _reset_kiosk(container: str) -> None:
    """Restore the kiosk to its initial state between trials via an in-place
    SIGUSR1 reset (clears the form + deletes the saved note without destroying
    the window, so the canvas never blanks). See kiosk_app.py."""
    subprocess.run(
        ["docker", "exec", container, "pkill", "-USR1", "-f", "kiosk_app.py"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    time.sleep(1.5)


def _new_page(pw, base_url: str, container_port: int):
    browser = pw.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(
        viewport={"width": 1500, "height": 950}, device_scale_factor=1
    )
    page.goto(
        f"{base_url}:{container_port}/{NOVNC_PATH}",
        wait_until="domcontentloaded",
        timeout=20000,
    )
    # Wait for the canvas to connect + paint the kiosk (non-blank).
    page.wait_for_selector("canvas", timeout=20000)
    deadline = time.monotonic() + 20
    backend = CanvasBrowserBackend(page)
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        img = Image.open(io.BytesIO(backend.screenshot())).convert("RGB")
        if len(img.getcolors(maxcolors=1 << 24) or []) > 3:
            # A newly connected noVNC canvas consumes the first pointer gesture
            # to acquire browser/client focus on some hosts. Acquire that focus
            # on a fixture-defined inert pixel before the demonstrated action;
            # otherwise the first real target click can be silently swallowed.
            ox, oy, width, height = backend._bbox()
            page.mouse.click(ox + width - 12, oy + height - 12)
            page.wait_for_timeout(250)
            return browser, page, backend
    raise RuntimeError("noVNC canvas never painted a non-blank kiosk frame")


def run_qualification(
    container: str,
    *,
    out_dir: Path,
    base_url: str,
    port: int,
    candidate_commit: str = "",
    base_commit: str = "",
) -> dict:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.replayer import Replayer

    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    trials: list[dict] = []
    t_start = time.monotonic()

    rec_dir = work / "recording"
    bundle_dir = work / "bundle"

    with sync_playwright() as pw:
        # ---- Trial 1: healthy record -> compile -> replay through the ladder --
        _reset_kiosk(container)
        browser, page, backend = _new_page(pw, base_url, port)
        try:
            rec = Recorder(
                backend,
                rec_dir,
                settle_interval_s=0.3,
                settle_stable_frames=2,
                settle_timeout_s=6.0,
            )
            rec.click(*ADA_ROW)  # select patient (visual rung)
            rec.click(*NOTE_FIELD)  # focus note field
            rec.type_text(NOTE_VALUE, param=NOTE_PARAM)
            rec.click(*SAVE_BUTTON)  # write (irreversible)
            rec.finish()
        finally:
            browser.close()

        workflow = compile_recording(rec_dir, bundle_dir, name="canvas-vision-ladder")

        _reset_kiosk(container)
        browser, page, backend = _new_page(pw, base_url, port)
        run_dir = work / "run_healthy"
        try:
            report = Replayer(backend, poll_interval_s=0.3).run(
                workflow,
                params={NOTE_PARAM: NOTE_VALUE},
                bundle_dir=bundle_dir,
                run_dir=run_dir,
            )
        finally:
            browser.close()

        saved = _read_saved_note(container)
        expected_saved = f"{EXPECTED_MRN}\t{NOTE_VALUE}"
        effect_confirmed = saved == expected_saved
        rung_counts = dict(report.rung_counts)
        structural_used = rung_counts.get("structural", 0)
        visual_rungs = {
            k: v
            for k, v in rung_counts.items()
            if k in ("template", "template_global", "ocr", "geometry")
        }
        healthy_ok = (
            report.success
            and report.model_calls == 0
            and structural_used == 0
            and bool(visual_rungs)
            and effect_confirmed
        )
        trials.append(
            {
                "trial": 1,
                "kind": "healthy_record_compile_replay",
                "success": bool(report.success),
                "model_calls": int(report.model_calls),
                "rung_counts": rung_counts,
                "structural_rung_used": int(structural_used),
                "visual_rungs_used": visual_rungs,
                "effect_confirmed": effect_confirmed,
                "effect_expected": expected_saved,
                "effect_observed": saved,
                "passed": bool(healthy_ok),
                "failure_class": None if healthy_ok else "healthy_contract_violation",
            }
        )

        # ---- Trial 2: MODERATE drift -> resolve CORRECTLY, never wrong-write --
        # A laggy-but-legible remote frame. A safe halt prevents corruption but
        # is still an availability failure in this regime: the ladder must
        # resolve to the CORRECT target and write the CORRECT value. A wrong or
        # partial write and an over-halt both fail this qualification case.
        _reset_kiosk(container)
        browser, page, backend = _new_page(pw, base_url, port)
        run_dir_mod = work / "run_drift_moderate"
        try:
            mod_report = Replayer(
                _DriftBackend.moderate(backend), poll_interval_s=0.3
            ).run(
                workflow,
                params={NOTE_PARAM: NOTE_VALUE},
                bundle_dir=bundle_dir,
                run_dir=run_dir_mod,
            )
        finally:
            browser.close()
        saved_mod = _read_saved_note(container)
        mod_correct_write = mod_report.success and saved_mod == expected_saved
        mod_no_wrong_write = saved_mod is None or saved_mod == expected_saved
        mod_ok = (
            mod_correct_write and mod_no_wrong_write and mod_report.model_calls == 0
        )
        trials.append(
            {
                "trial": 2,
                "kind": "moderate_drift_no_silent_wrong_write",
                "drift": "downscale_0.4x + theme_invert + jpeg_q12 (legible; simulated on real session)",
                "resolved_and_correct": bool(mod_correct_write),
                "halted": bool(not mod_report.success),
                "rung_counts": dict(mod_report.rung_counts),
                "model_calls": int(mod_report.model_calls),
                "effect_observed": saved_mod,
                "effect_expected": expected_saved,
                "wrong_write": bool(
                    saved_mod is not None and saved_mod != expected_saved
                ),
                "passed": bool(mod_ok),
                "failure_class": None
                if mod_ok
                else (
                    "silent_wrong_write_or_model_call"
                    if not mod_no_wrong_write or mod_report.model_calls != 0
                    else "moderate_drift_over_halt"
                ),
            }
        )

        # ---- Trial 3: SEVERE drift -> SAFE-HALT (no blind coordinate replay) --
        # The frame is genuinely illegible (roster/MRN unreadable). The ladder
        # must find no confident target and HALT with no write and no model
        # call -- it must NOT fall back to firing the recorded pixel coordinates
        # the way a naive record/replay tool would.
        _reset_kiosk(container)
        browser, page, backend = _new_page(pw, base_url, port)
        run_dir_sev = work / "run_drift_severe"
        try:
            sev_report = Replayer(
                _DriftBackend.severe(backend), poll_interval_s=0.3
            ).run(
                workflow,
                params={NOTE_PARAM: NOTE_VALUE},
                bundle_dir=bundle_dir,
                run_dir=run_dir_sev,
            )
        finally:
            browser.close()
        saved_sev = _read_saved_note(container)
        sev_halted = not sev_report.success
        sev_no_write = saved_sev != expected_saved
        sev_no_model = sev_report.model_calls == 0
        sev_ok = sev_halted and sev_no_write and sev_no_model
        trials.append(
            {
                "trial": 3,
                "kind": "severe_drift_safe_halt",
                "drift": "downscale_0.14x + gaussian_blur_2.0 + theme_invert + jpeg_q5 (illegible; simulated on real session)",
                "halted": bool(sev_halted),
                "rung_counts": dict(sev_report.rung_counts),
                "model_calls": int(sev_report.model_calls),
                "silent_write": bool(saved_sev == expected_saved),
                "effect_after_drift": saved_sev,
                "passed": bool(sev_ok),
                "failure_class": None if sev_ok else "drift_not_safely_halted",
            }
        )

    _reset_kiosk(container)

    successes = sum(1 for t in trials if t["passed"])
    accepted = (
        len(trials) == 3
        and successes == 3
        and trials[0]["model_calls"] == 0
        and trials[0]["structural_rung_used"] == 0
        and trials[0]["effect_confirmed"]
        and trials[1]["resolved_and_correct"]
        and not trials[1]["wrong_write"]
        and trials[2]["halted"]
        and not trials[2]["silent_write"]
    )

    evidence = {
        "schema_version": "openadapt.canvas-ladder-qualification.v1",
        "substrate": "no-dom-html5-canvas-novnc-tigervnc-linux-kiosk",
        "candidate_commit": candidate_commit,
        "base_commit": base_commit,
        "task": (
            "record->compile->replay a patient-note write through the "
            "vision-only resolver ladder over a no-DOM HTML5 <canvas> "
            "(noVNC), with no structural backend; confirm the write via an "
            "independent document oracle; and halt under injected "
            "DPI/theme/JPEG drift"
        ),
        "contract": {
            "healthy_zero_model_calls": trials[0]["model_calls"] == 0,
            "healthy_structural_rung_used": trials[0]["structural_rung_used"],
            "healthy_visual_rungs_used": trials[0]["visual_rungs_used"],
            "healthy_effect_confirmed": trials[0]["effect_confirmed"],
            "moderate_drift_no_silent_wrong_write": not trials[1]["wrong_write"],
            "moderate_drift_resolved_correctly": trials[1]["resolved_and_correct"],
            "severe_drift_safely_halted": trials[2]["halted"],
            "severe_drift_no_silent_write": not trials[2]["silent_write"],
        },
        "environment": {
            "vnc_server": "TigerVNC Xvnc (X display :0 + VNC :5900)",
            "canvas_gateway": "noVNC + websockify (HTML5 <canvas> on :6080)",
            "surface": "Tk kiosk rendered into a no-accessible-DOM HTML5 canvas",
            "backend": "CanvasBrowserBackend (Playwright, pixel-only; no structural/identity)",
            "viewport": list(VIEWPORT),
            "note": (
                "The <canvas> exposes no accessible content, so the "
                "structural rung is unavailable by construction and "
                "resolution runs on the visual floor -- the Citrix "
                "Workspace-web class."
            ),
        },
        "oracle": "docker exec cat of the kiosk-persisted note file",
        "failure_taxonomy": [
            "connect_or_frame_failure",
            "healthy_contract_violation",
            "effect_not_confirmed",
            "moderate_drift_over_halt",
            "silent_wrong_write_or_model_call",
            "drift_not_safely_halted",
        ],
        "caveat": (
            "no-DOM HTML5-canvas class (the class Citrix Workspace-web "
            "presents) -- NOT Citrix ICA/HDX (no HDX codecs, no ICA "
            "compression, no Workspace-client input path), NOT an "
            "aardwolf/RDP transport proof, and the drift is "
            "simulated-on-a-real-session (not WAN/HDX capture)."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trials": trials,
        "run_count": len(trials),
        "successes": successes,
        "model_calls": trials[0]["model_calls"],
        "total_s": round(time.monotonic() - t_start, 3),
        "accepted": bool(accepted),
    }
    return evidence


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--container",
        default=os.environ.get(
            "OAFLOW_CANVAS_LADDER_CONTAINER", "oaflow-canvas-ladder"
        ),
    )
    ap.add_argument(
        "--base-url",
        default=os.environ.get("OAFLOW_CANVAS_LADDER_BASE_URL", "http://localhost"),
    )
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("OAFLOW_CANVAS_LADDER_PORT", "6080")),
    )
    ap.add_argument(
        "--output", type=Path, default=Path("benchmark/canvas_ladder/results.json")
    )
    ap.add_argument("--candidate-commit", default="")
    ap.add_argument("--base-commit", default="")
    args = ap.parse_args()

    out_dir = args.output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = run_qualification(
        args.container,
        out_dir=out_dir,
        base_url=args.base_url,
        port=args.port,
        candidate_commit=args.candidate_commit,
        base_commit=args.base_commit,
    )
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    payload = json.dumps(evidence, sort_keys=True).encode()
    print(f"evidence sha256: {hashlib.sha256(payload).hexdigest()}")
    print(f"accepted: {evidence['accepted']}  wrote: {args.output}")
    return 0 if evidence["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
