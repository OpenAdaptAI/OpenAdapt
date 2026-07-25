#!/usr/bin/env python3
"""Coordinate replay of a PowerChart recording with dense screenshot capture.

Usage:
  python app-run/powerchart/scripts/coord_replay.py \\
      --mrn 080-975-37 \\
      --events app-run/powerchart/template/rec5/events.jsonl \\
      --out app-run/powerchart/out/080-975-37
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import Quartz
from PIL import Image
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventCreateScrollWheelEvent,
    CGEventPost,
    CGEventSetIntegerValueField,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGMouseEventClickState,
    kCGScrollEventUnitLine,
)
from openadapt_flow.backends.remote_display import MacWindowClient

# Site/layout config (labels + fractions — no per-pixel Clinic Workflow coords).
_scripts = Path(__file__).resolve().parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))
from pc_config import (  # noqa: E402
    LAYOUT,
    PANEL_FALLBACK_XY,
    PANEL_LABELS,
    PREFLIGHT_READY_MARKERS,
    PREFLIGHT_WRONG_VIEW_MARKERS,
    TAB_FALLBACK_XY,
    TAB_LABELS,
    is_chart_title,
    is_organizer_title,
    looks_like_mrn,
)

# Active MRN for this process (set via --mrn / set_mrn).
MRN = "080-975-37"


def set_mrn(mrn: str) -> None:
    global MRN
    MRN = mrn.strip()


def log(msg: str, lines: list[str]) -> None:
    print(msg, flush=True)
    lines.append(msg)


def wins(client: MacWindowClient):
    return [
        w
        for w in client.find_windows("Citrix Viewer", None)
        if getattr(w, "on_screen", False) and w.title
    ]


def find(client: MacWindowClient, substr: str):
    for w in wins(client):
        if substr in (w.title or ""):
            return w
    return None


def raise_title(part: str) -> None:
    subprocess.run(
        [
            "osascript",
            "-e",
            f'''
tell application "System Events"
  tell process "Citrix Viewer"
    set frontmost to true
    try
      set w to first window whose name contains "{part}"
      perform action "AXRaise" of w
    end try
  end tell
end tell
''',
        ],
        check=False,
    )


def abs_click(ax: float, ay: float, clicks: int = 1) -> None:
    move = CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (ax, ay), kCGMouseButtonLeft
    )
    CGEventPost(kCGHIDEventTap, move)
    time.sleep(0.05)
    for i in range(clicks):
        down = CGEventCreateMouseEvent(
            None, kCGEventLeftMouseDown, (ax, ay), kCGMouseButtonLeft
        )
        up = CGEventCreateMouseEvent(
            None, kCGEventLeftMouseUp, (ax, ay), kCGMouseButtonLeft
        )
        CGEventSetIntegerValueField(down, kCGMouseEventClickState, i + 1)
        CGEventSetIntegerValueField(up, kCGMouseEventClickState, i + 1)
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)
        time.sleep(0.07)


def scroll_at(ax: float, ay: float, lines: int = -4) -> None:
    move = CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (ax, ay), kCGMouseButtonLeft
    )
    CGEventPost(kCGHIDEventTap, move)
    time.sleep(0.04)
    CGEventPost(
        kCGHIDEventTap,
        CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, lines),
    )


def window_scale(w, im: Image.Image | None) -> float:
    """Screenshot pixels per window point (≈2.0 on Retina, 1.0 on this Citrix)."""
    bw = float(w.bounds[2]) or 1.0
    if im is None or im.width <= 0:
        return 1.0
    return im.width / bw


def px_to_screen(w, im: Image.Image | None, px: float, py: float) -> tuple[float, float]:
    """Convert screenshot-pixel coords → absolute screen (Quartz) coords."""
    scale = window_scale(w, im)
    bx, by = w.bounds[0], w.bounds[1]
    return bx + px / scale, by + py / scale


def frac_to_screen(w, fx: float, fy: float) -> tuple[float, float]:
    """Convert layout fractions of the window → absolute screen coords."""
    bx, by, bw, bh = w.bounds
    return bx + fx * bw, by + fy * bh


def park_mouse(w) -> None:
    """Move the cursor off the table rows so hover tooltips don't cover them."""
    ax, ay = frac_to_screen(w, LAYOUT.park_x, LAYOUT.park_y)
    move = CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (ax, ay), kCGMouseButtonLeft
    )
    CGEventPost(kCGHIDEventTap, move)
    time.sleep(0.12)


_WAIT_PATH = Path("/tmp/openadapt_pc_wait.png")


def wait_changed(
    w, prev_hash: str, timeout: float = 1.2, poll: float = 0.1
) -> str:
    """Poll content region until hash differs from prev_hash (or timeout)."""
    deadline = time.monotonic() + timeout
    last = prev_hash
    while time.monotonic() < deadline:
        im = capture(_WAIT_PATH, w, make_small=False)
        if im is not None:
            last = region_hash(im)
            if last != prev_hash:
                return last
        time.sleep(poll)
    return last


def wait_stable(w, timeout: float = 0.9, poll: float = 0.12) -> str | None:
    """Poll until two consecutive region hashes match (or timeout)."""
    deadline = time.monotonic() + timeout
    prev: str | None = None
    while time.monotonic() < deadline:
        im = capture(_WAIT_PATH, w, make_small=False)
        if im is not None:
            h = region_hash(im)
            if prev is not None and h == prev:
                return h
            prev = h
        time.sleep(poll)
    return prev


def after_action(w, prev_hash: str | None = None) -> str | None:
    """Adaptive post-click/scroll wait: change (if known) then brief settle."""
    if prev_hash is not None:
        wait_changed(w, prev_hash, timeout=1.2)
    return wait_stable(w, timeout=0.7)


def key(code: int) -> None:
    CGEventPost(kCGHIDEventTap, CGEventCreateKeyboardEvent(None, code, True))
    CGEventPost(kCGHIDEventTap, CGEventCreateKeyboardEvent(None, code, False))


def phash(im: Image.Image) -> str:
    a = np.asarray(im.convert("L").resize((48, 48)), dtype=np.float32)
    return hashlib.md5(a.tobytes()).hexdigest()[:12]


def region_hash(im: Image.Image, box: tuple[int, int, int, int] | None = None) -> str:
    """Hash the content-pane region (excludes status-bar clock)."""
    if box is None:
        box = (
            int(LAYOUT.content_x0 * im.width),
            int(LAYOUT.content_y0 * im.height),
            int(LAYOUT.content_x1 * im.width * 0.55),  # name cols only
            int(LAYOUT.content_y1 * im.height),
        )
    x0, y0, x1, y1 = box
    crop = im.crop((x0, y0, min(x1, im.width), min(y1, im.height)))
    a = np.asarray(crop.convert("L").resize((160, 120)), dtype=np.uint8)
    return hashlib.md5(a.tobytes()).hexdigest()[:12]


def capture(path: Path, w, *, make_small: bool = True) -> Image.Image | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["screencapture", "-x", f"-l{w.window_id}", str(path)], check=False)
    if not path.exists():
        return None
    im = Image.open(path)
    if float(np.asarray(im).mean()) > 235:
        fullp = path.with_suffix(".full.png")
        subprocess.run(["screencapture", "-x", str(fullp)], check=False)
        if fullp.exists():
            bx, by, bw, bh = w.bounds
            full = Image.open(fullp)
            im = full.crop(
                (
                    int(bx),
                    int(by),
                    int(bx + min(bw, full.width - bx)),
                    int(by + min(bh, full.height - by)),
                )
            )
            im.save(path)
            fullp.unlink(missing_ok=True)
    if make_small:
        small = path.with_name(path.stem + "_small.png")
        im.resize((960, max(1, int(960 * im.height / im.width)))).save(small)
    return im


def capture_settled(
    path: Path,
    w,
    tries: int = 2,
    delay: float = 0.2,
    *,
    prev_region: str | None = None,
) -> tuple[Image.Image | None, int]:
    """Capture until the table region stops changing.

    A frame grabbed mid-paint OCRs as garbage ("Umbilical hernia" →
    "Imhiliaal hauni-"), which then survives as a bogus extra row. Re-grabbing
    the same path until two consecutive frames match kills that at the source.

    Cheap path: one grab is enough when it already differs from the previous
    scroll frame's region hash (content moved) and a quick re-check matches.
    Returns (image, retries_used).
    """
    im = capture(path, w, make_small=False)
    if im is None:
        return None, 0
    prev = region_hash(im)
    # If the first grab still matches the pre-scroll region, the UI may not
    # have painted yet — allow an extra settle try. Otherwise two matching
    # grabs (tries=2) is enough.
    max_tries = (
        max(tries, 3)
        if (prev_region is not None and prev == prev_region)
        else tries
    )
    for i in range(1, max_tries):
        time.sleep(delay)
        im2 = capture(path, w, make_small=False)
        if im2 is None:
            _write_small(path, im)
            return im, i
        h2 = region_hash(im2)
        if h2 == prev:
            _write_small(path, im2)
            return im2, i
        prev, im = h2, im2
    _write_small(path, im)
    return im, max(0, max_tries - 1)


def _write_small(path: Path, im: Image.Image) -> None:
    small = path.with_name(path.stem + "_small.png")
    im.resize((960, max(1, int(960 * im.height / im.width)))).save(small)


def chart_window(client: MacWindowClient):
    """Find the open patient chart — by MRN in title, else '… - MRN Opened by'."""
    for w in wins(client):
        if is_chart_title(w.title or "", mrn=MRN):
            return w
    return None


def organizer(client: MacWindowClient):
    for w in wins(client):
        if is_organizer_title(w.title or "") and not is_chart_title(
            w.title or "", mrn=MRN
        ):
            return w
    return find(client, "Organizer") or find(client, "PowerChart")


def front_app_name() -> str:
    try:
        from AppKit import NSWorkspace  # noqa: WPS433

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else ""
    except Exception:
        return ""


def citrix_is_front() -> bool:
    return "citrix" in front_app_name().lower()


def focus(client: MacWindowClient, w) -> None:
    part = MRN if MRN in (w.title or "") else (w.title or "Citrix")[:40]
    raise_title(part.replace('"', ""))
    client.activate(w.pid)
    time.sleep(0.25)
    if not citrix_is_front():
        # Another app (browser, etc.) is covering the screen; try once more.
        client.activate(w.pid)
        raise_title(part.replace('"', ""))
        time.sleep(0.5)


def _ensure_vision() -> None:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def _vision_lines(path: Path, *, fast: bool = True) -> list[dict]:
    """Mid-capture Vision lines (Fast by default; Accurate only for final extract)."""
    try:
        _ensure_vision()
        from vision_ocr import ocr_lines  # noqa: WPS433

        return ocr_lines(path, fast=fast)
    except Exception:
        return []


def _ocr_snippet(
    path: Path, crop: tuple[int, int, int, int] | None = None, *, fast: bool = True
) -> str:
    """Mid-capture OCR via Apple Vision (bbox-filtered crop, no temp PNG)."""
    try:
        _ensure_vision()
        from vision_ocr import ocr_text  # noqa: WPS433

        return ocr_text(path, crop=crop, fast=fast)
    except Exception:
        return ""


def _peek(path: Path, kind: str, carry: str | None = None, *, fast: bool = True):
    try:
        _ensure_vision()
        from vision_ocr import peek_progress  # noqa: WPS433

        return peek_progress(path, kind, carry, fast=fast)
    except Exception:
        return set(), None, carry


def find_label(
    vlines: list[dict],
    aliases: tuple[str, ...],
    *,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    min_w: float = 50.0,
    allow_substring: bool = False,
) -> dict | None:
    """Best Vision box whose text matches any alias inside a pixel region.

    Prefers wider boxes: Vision often emits a junk 40–60 px fragment at the
    left edge (seen matching "Allergies" at x=0) alongside the real nav label.

    When allow_substring=True (Histories tab strip), Vision often merges
    "Problem History • Procedure History • …" into one line — we still match
    and set box['_click_frac'] so the click lands on the right sub-label.
    """
    best = None
    best_score = -1.0
    for l in vlines:
        if l["w"] < min_w:
            continue
        cx, cy = l["x"] + l["w"] / 2, l["y"] + l["h"] / 2
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        low = l["text"].strip().lower()
        matched = None
        for a in aliases:
            if low == a or low.startswith(a + " ") or low.startswith(a):
                matched = ("exact", a, 0)
                break
            if allow_substring and a in low:
                matched = ("sub", a, low.index(a))
                break
        if matched is None:
            continue
        kind, alias, idx = matched
        # Score: prefer exact short labels, then width, then confidence.
        exact = 1.0 if kind == "exact" else 0.0
        score = exact * 1000 + (0 if kind == "sub" else l["w"]) + l["conf"]
        if score > best_score:
            box = dict(l)
            if kind == "sub":
                # Click at the horizontal center of the alias within the line.
                frac = (idx + len(alias) / 2) / max(len(low), 1)
                box["_click_frac"] = max(0.05, min(0.95, frac))
                box["_alias"] = alias
            best, best_score = box, score
    return best


def click_vision_box(
    w, im: Image.Image | None, box: dict, lines: list[str], label: str
) -> tuple[float, float]:
    frac = float(box.get("_click_frac", 0.5))
    ax, ay = px_to_screen(
        w, im, box["x"] + box["w"] * frac, box["y"] + box["h"] / 2
    )
    abs_click(ax, ay)
    log(f"  click '{label}' via Vision @({ax:.0f},{ay:.0f})", lines)
    return ax, ay


def ensure_clinic_workflow(
    client: MacWindowClient, w, lines: list[str], proof_dir: Path | None = None
) -> tuple[Path | None, bool]:
    """If the Clinic Workflow tab is visible but not active, click it.

    Returns (proof_path, did_click). Caller may reuse proof_path for nav OCR.
    """
    focus(client, w)
    proof = (proof_dir or Path("/tmp")) / "clinic_workflow.png"
    if proof_dir:
        proof_dir.mkdir(parents=True, exist_ok=True)
    im = capture(proof, w)
    if im is None:
        return None, False
    text = _ocr_snippet(proof).lower()
    if "verified local" in text or (
        "clinic workflow" in text and "allergies" in text and "histories" in text
    ):
        return proof, False  # already on the workflow pane
    vlines = _vision_lines(proof)
    iw, ih = im.size
    hit = find_label(
        vlines,
        ("clinic workflow",),
        x0=0.15 * iw,
        x1=0.55 * iw,
        y0=0.15 * ih,
        y1=0.35 * ih,
        min_w=80,
    )
    if hit is not None:
        prev = region_hash(im)
        click_vision_box(w, im, hit, lines, "clinic workflow")
        after_action(w, prev)
        return proof, True
    return proof, False


def panel_click(
    client: MacWindowClient,
    w,
    key: str,
    lines: list[str],
    proof_dir: Path | None = None,
) -> Path | None:
    """Click a left-nav item by its OCR label; fall back to layout coords.

    Returns the best post-click proof path (for expand/tab reuse), or None.
    """
    cw_path, cw_clicked = ensure_clinic_workflow(
        client, w, lines, proof_dir=proof_dir
    )
    focus(client, w)
    proof = (proof_dir or Path("/tmp")) / f"panel_{key}.png"
    if proof_dir:
        proof_dir.mkdir(parents=True, exist_ok=True)
    # Reuse clinic_workflow frame when we didn't just navigate away from it.
    if cw_path is not None and cw_path.exists() and not cw_clicked:
        im = Image.open(cw_path)
        proof = cw_path
    else:
        im = capture(proof, w)
    aliases = PANEL_LABELS.get(key) or (key.replace("_", " "),)
    hit = None
    if im is not None:
        vlines = _vision_lines(proof)
        iw, ih = im.size
        hit = find_label(
            vlines,
            aliases,
            x0=LAYOUT.nav_x0 * iw,
            x1=LAYOUT.nav_x1 * iw,
            y0=LAYOUT.nav_y0 * ih,
            y1=LAYOUT.nav_y1 * ih,
        )
    if hit is not None:
        prev = region_hash(im) if im is not None else None
        click_vision_box(w, im, hit, lines, aliases[0])
        after_action(w, prev)
        # Guard: standalone Allergies/History modules leave Clinic Workflow.
        # If that happened, fall through to the recorded Clinic Workflow coords.
        check = (proof_dir or Path("/tmp")) / f"panel_{key}_after.png"
        after = capture(check, w)
        if after is not None:
            at = _ocr_snippet(check).lower()
            still_workflow = "clinic workflow" in at or "verified local" in at
            if not still_workflow:
                # Transition frames OCR empty — wait once more before falling back.
                wait_stable(w, timeout=0.9)
                after = capture(check, w)
                if after is not None:
                    at = _ocr_snippet(check).lower()
                    still_workflow = (
                        "clinic workflow" in at or "verified local" in at
                    )
            if still_workflow:
                return check
            log(
                f"  WARN: Vision '{aliases[0]}' left Clinic Workflow — "
                "retrying fallback coords",
                lines,
            )
            # Get back on Clinic Workflow before the pixel fallback.
            ensure_clinic_workflow(client, w, lines, proof_dir=proof_dir)
        else:
            return check if check.exists() else proof
    # Fallback: recorded window-relative coords for this site's layout.
    fx, fy = PANEL_FALLBACK_XY.get(key, (400, 500))
    bx, by = w.bounds[0], w.bounds[1]
    prev = None
    if im is not None:
        prev = region_hash(im)
    abs_click(bx + fx, by + fy)
    after_action(w, prev)
    log(f"  panel → {key} fallback @({fx},{fy})", lines)
    check = (proof_dir or Path("/tmp")) / f"panel_{key}_after.png"
    capture(check, w)
    return check if check.exists() else None


def tab_looks_right(path: Path, name: str, *, fast: bool = True) -> bool:
    """Patient-agnostic check: tab strip label + Verified Local present.

    The Histories strip OCRs both tab names on one line, so label presence
    alone cannot tell which is selected — callers should also require a
    content change via tab_content_changed().
    """
    vlines = _vision_lines(path, fast=fast)
    try:
        im = Image.open(path)
        iw, ih = im.size
    except Exception:
        iw, ih = 3440, 1313
    strip = " ".join(
        l["text"]
        for l in vlines
        if LAYOUT.tab_x0 * iw
        <= l["x"] + l["w"] / 2
        <= LAYOUT.tab_x1 * iw
        and LAYOUT.tab_y0 * ih
        <= l["y"] + l["h"] / 2
        <= LAYOUT.tab_y1 * ih
    ).lower()
    content = " ".join(
        l["text"]
        for l in vlines
        if LAYOUT.content_x0 * iw
        <= l["x"] + l["w"] / 2
        <= LAYOUT.content_x1 * iw
        and LAYOUT.content_y0 * ih
        <= l["y"] + l["h"] / 2
        <= LAYOUT.content_y1 * ih
    ).lower()
    has_verified = "verified local" in content
    if name == "problem_history":
        if "procedure history" in strip and "problem history" not in strip:
            return False
        return has_verified or "problem history" in strip
    if name == "procedure_history":
        if "problem history" in strip and "procedure history" not in strip:
            return False
        return has_verified or "procedure history" in strip
    return True


def tab_content_changed(
    before: Path | None, after: Path, kind: str
) -> bool:
    """True when verified row names differ enough from a prior Histories tab."""
    if before is None or not before.exists():
        return True
    before_keys, _, _ = _peek(before, kind, "verified", fast=True)
    after_keys, _, _ = _peek(after, kind, "verified", fast=True)
    if not before_keys and not after_keys:
        # Fall back to region hash when both peeks are empty.
        try:
            return region_hash(Image.open(before)) != region_hash(Image.open(after))
        except Exception:
            return True
    if not after_keys:
        return False
    overlap = len(before_keys & after_keys) / max(len(before_keys), 1)
    # Same table still showing → overlap stays high (~0.8–1.0).
    return overlap < 0.55


def tab_click(
    client: MacWindowClient,
    w,
    name: str,
    lines: list[str],
    proof_dir: Path,
    reuse: Path | None = None,
    prior_tab_proof: Path | None = None,
) -> Path | None:
    """Click a Histories sub-tab by OCR label; fall back to candidate coords.

    Returns a proof path on the correct tab (for expand reuse), or None.
    prior_tab_proof: frame from the previous Histories tab — used to reject
    clicks that did not actually change the verified table.
    """
    focus(client, w)
    proof_dir.mkdir(parents=True, exist_ok=True)
    probe = proof_dir / f"before_tab_{name}.png"
    if reuse is not None and reuse.exists():
        im = Image.open(reuse)
        probe = reuse
    else:
        im = capture(probe, w)
    aliases = TAB_LABELS.get(name) or (name.replace("_", " "),)

    def _accepted(proof: Path) -> bool:
        ok = tab_looks_right(proof, name)
        if not ok:
            wait_stable(w, timeout=0.5)
            capture(proof, w)
            ok = tab_looks_right(proof, name, fast=False)
        # Only when switching away from a known prior Histories tab — the
        # strip always OCRs both labels, so unchanged row names mean the
        # click missed (seen: procedure capture re-reading problems).
        if (
            ok
            and prior_tab_proof is not None
            and prior_tab_proof.exists()
            and name in ("problem_history", "procedure_history")
        ):
            changed = tab_content_changed(prior_tab_proof, proof, name)
            if not changed:
                log(f"  tab → {name} labels ok but table unchanged — reject", lines)
                return False
        return ok

    if im is not None:
        iw, ih = im.size
        hit = None
        for use_fast in (True, False):
            vlines = _vision_lines(probe, fast=use_fast)
            hit = find_label(
                vlines,
                aliases,
                x0=LAYOUT.tab_x0 * iw,
                x1=LAYOUT.tab_x1 * iw,
                y0=LAYOUT.tab_y0 * ih,
                y1=LAYOUT.tab_y1 * ih,
                allow_substring=True,  # strip often OCR'd as one long line
            )
            if hit is not None:
                break
        if hit is not None:
            prev = region_hash(im)
            click_vision_box(w, im, hit, lines, aliases[0])
            after_action(w, prev)
            proof = proof_dir / f"after_tab_{name}_vision.png"
            capture(proof, w)
            ok = _accepted(proof)
            log(f"  tab → {name} via Vision ok={ok}", lines)
            if ok:
                return proof

    bx, by = w.bounds[0], w.bounds[1]
    last_proof: Path | None = None
    for tx, ty in TAB_FALLBACK_XY.get(name, []):
        prev = region_hash(im) if im is not None else None
        abs_click(bx + tx, by + ty)
        after_action(w, prev)
        proof = proof_dir / f"after_tab_{name}_{tx}_{ty}.png"
        capture(proof, w)
        last_proof = proof
        ok = _accepted(proof)
        log(f"  tab → {name} fallback @({tx},{ty}) ok={ok}", lines)
        if ok:
            return proof
    log(f"  WARN: tab {name} not confirmed — refusing stale candidate", lines)
    return None


# When these appear in OCR, we've scrolled past the target section.
STOP_MARKERS = {
    # Content-pane only (see hit_stop_marker crop). Avoid tab labels.
    "allergies": ("home medications", "home medication"),
    "problem_history": ("important links", "recommendations"),
    "procedure_history": ("recommendations", "important links"),
}


def hit_stop_marker(path: Path, prefix: str) -> str | None:
    markers = STOP_MARKERS.get(prefix) or ()
    if not markers:
        return None
    try:
        im = Image.open(path)
        iw, ih = im.size
    except Exception:
        iw, ih = 3440, 1313
    crop = (
        int(LAYOUT.content_x0 * iw),
        int(LAYOUT.content_y0 * ih),
        int(LAYOUT.content_x1 * iw),
        int(LAYOUT.content_y1 * ih),
    )
    text = _ocr_snippet(path, crop=crop).lower()
    for m in markers:
        if m in text:
            return m
    return None


# Dense charts paginate the embedded list: "Page X of Y" + First/Prev/Next/Last.
PAGER_RE = re.compile(r"page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)


def detect_pager(path: Path) -> dict | None:
    """Find the embedded list pager 'Page X of Y' and its Next button.

    Coordinates are window-relative (screenshot is 1:1 with the window).
    Returns {cur, total, y, h, next: box|None} or None.
    """
    vlines = _vision_lines(path)
    try:
        im = Image.open(path)
        iw, ih = im.size
    except Exception:
        iw, ih = 3440, 1313
    x0, x1 = LAYOUT.content_x0 * iw, LAYOUT.content_x1 * iw
    pager = None
    for l in vlines:
        # Content pane only; skip left nav and right detail flyout.
        if not (x0 <= l["x"] + l["w"] / 2 <= x1):
            continue
        m = PAGER_RE.search(l["text"])
        if m:
            pager = {
                "cur": int(m.group(1)),
                "total": int(m.group(2)),
                "y": l["y"],
                "h": l["h"],
                "next": None,
            }
            break
    if pager is None:
        return None
    for l in vlines:
        t = l["text"].strip().lower().rstrip(" >›")
        if t == "next" and abs(l["y"] - pager["y"]) < 45:
            pager["next"] = {k: l[k] for k in ("x", "y", "w", "h")}
            break
    return pager


def _pager_visible_quick(path: Path) -> bool:
    """Vision check for 'Page N of M' in the content pane."""
    try:
        im = Image.open(path)
        iw, ih = im.size
    except Exception:
        iw, ih = 3440, 1313
    crop = (
        int(LAYOUT.content_x0 * iw),
        int(LAYOUT.content_y0 * ih),
        int(LAYOUT.content_x1 * iw),
        int(LAYOUT.content_y1 * ih),
    )
    text = _ocr_snippet(path, crop=crop).lower()
    return bool(PAGER_RE.search(text))


def capture_page_frames(
    client: MacWindowClient,
    w,
    outdir: Path,
    prefix: str,
    page_no: int,
    lines: list[str],
    max_frames: int = 6,
    *,
    seen_keys: set[str] | None = None,
    ui_count: int | None = None,
) -> tuple[list[dict], set[str], int | None]:
    """Capture one pager page: scroll up to the widget top, then walk down.

    A page's top rows can sit above the viewport when the outer scroll is
    deep, so one screenshot is not enough.
    """
    sx, sy = frac_to_screen(w, LAYOUT.scroll_x, LAYOUT.scroll_y)
    prev_region: str | None = None
    for _ in range(2):
        scroll_at(sx, sy, lines=4)
        prev_region = after_action(w, prev_region)

    frames: list[dict] = []
    prev_h = None
    keys = set(seen_keys or ())
    count = ui_count
    carry: str | None = "verified"
    for j in range(max_frames):
        park_mouse(w)
        path = outdir / f"{prefix}_p{page_no}_{j:02d}.png"
        im, _ = capture_settled(path, w, prev_region=prev_region)
        if im is None:
            break
        prev_region = region_hash(im)
        h = phash(im)
        if h == prev_h:
            path.unlink(missing_ok=True)
            path.with_name(path.stem + "_small.png").unlink(missing_ok=True)
            break
        frames.append(
            {
                "file": path.name,
                "hash": h,
                "mean": float(np.asarray(im).mean()),
                "stop_marker": None,
                "page": page_no,
            }
        )
        prev_h = h
        new_keys, expected, carry = _peek(path, prefix, carry)
        keys |= new_keys
        if expected is not None:
            count = expected if count is None else max(count, expected)
        if count is not None and len(keys) >= count and not new_keys:
            acc_keys, acc_exp, _ = _peek(path, prefix, carry, fast=False)
            keys |= acc_keys
            if acc_exp is not None:
                count = max(count, acc_exp)
            if len(keys) >= count:
                log(
                    f"  [{prefix}] page {page_no}: count met "
                    f"({len(keys)}/{count}) — stop page walk",
                    lines,
                )
                break
        # The pager row marks the widget bottom — page fully captured.
        if _pager_visible_quick(path):
            break
        scroll_at(sx, sy, lines=-3)
        prev_region = after_action(w, prev_region)
    return frames, keys, count


def verified_names_on_page(outdir: Path, frames: list[dict], kind: str) -> set[str]:
    """Verified-table row names visible in a pager page's frames.

    Continuation pages rarely repeat the "Verified Local Record Data" header,
    so the peek is seeded as if inside that group.
    """
    names: set[str] = set()
    for fr in frames:
        keys, _, _ = _peek(outdir / fr["file"], kind, "verified")
        names |= keys
    return names


def paginate_capture(
    client: MacWindowClient,
    w,
    outdir: Path,
    prefix: str,
    saved: list[dict],
    lines: list[str],
    max_pages: int = 6,
    *,
    seen_keys: set[str] | None = None,
    ui_count: int | None = None,
) -> list[dict]:
    """If the last frame shows 'Page X of Y' with X<Y, click Next per page.

    Every page gets a scroll-up + walk-down capture; the OCR parser decides
    later which rows belong to the verified table.
    """
    if not saved:
        return []
    if ui_count is not None and seen_keys is not None and len(seen_keys) >= ui_count:
        return []
    pager = detect_pager(outdir / saved[-1]["file"])
    if not pager or pager["total"] <= pager["cur"]:
        return []

    extra: list[dict] = []
    seen_names: set[str] = set(seen_keys or ())
    count = ui_count
    # Last saved frame gives pixel→screen scale for Vision pager boxes.
    last_im = None
    try:
        last_im = Image.open(outdir / saved[-1]["file"])
    except Exception:
        pass
    for _ in range(max_pages):
        if count is not None and len(seen_names) >= count:
            log(
                f"  [{prefix}] skip paging — verified count already met "
                f"({len(seen_names)}/{count})",
                lines,
            )
            break
        if pager["cur"] >= pager["total"]:
            break
        nb = pager.get("next")
        if nb:
            ax, ay = px_to_screen(
                w, last_im, nb["x"] + nb["w"] / 2, nb["y"] + nb["h"] / 2
            )
        else:
            # Fraction fallback: Next sits near the right of the content pane.
            ax, ay = frac_to_screen(w, 0.70, 0.72)
        log(
            f"  [{prefix}] pager {pager['cur']}/{pager['total']} — "
            f"click Next @({ax:.0f},{ay:.0f})",
            lines,
        )
        focus(client, w)
        prev = region_hash(last_im) if last_im is not None else None
        abs_click(ax, ay)
        after_action(w, prev)

        page_no = pager["cur"] + 1
        before = set(seen_names)
        frames, seen_names, count = capture_page_frames(
            client,
            w,
            outdir,
            prefix,
            page_no,
            lines,
            seen_keys=seen_names,
            ui_count=count,
        )
        if not frames:
            log(f"  [{prefix}] page {page_no} capture failed", lines)
            break
        extra.extend(frames)
        try:
            last_im = Image.open(outdir / frames[-1]["file"])
        except Exception:
            pass

        # Some pagers drive secondary lists (Population Health Record,
        # encounter diagnoses) rather than the verified table. Rather than
        # guess from headings, ask the parser: a page that adds no new verified
        # row is not worth following.
        if not (seen_names - before):
            # Double-check with a fresh peek in case mid-capture miss.
            page_names = verified_names_on_page(outdir, frames, prefix)
            seen_names |= page_names
            if not (page_names - before):
                log(
                    f"  [{prefix}] page {page_no} adds no verified rows — stop paging",
                    lines,
                )
                break

        newp = None
        for fr in reversed(frames):
            newp = detect_pager(outdir / fr["file"])
            if newp:
                break
        if not newp or newp["cur"] <= pager["cur"]:
            log(f"  [{prefix}] pager did not advance — stop paging", lines)
            break
        log(
            f"  [{prefix}] page {newp['cur']}/{newp['total']} captured "
            f"({len(frames)} frames)",
            lines,
        )
        pager = newp
    return extra


def scroll_capture_section(
    client: MacWindowClient,
    w,
    outdir: Path,
    prefix: str,
    lines: list[str],
    max_scrolls: int = 12,
    scroll_lines: int = -3,
) -> list[dict]:
    """Screenshot densely, scroll until stable, stop marker, or UI count met."""
    outdir.mkdir(parents=True, exist_ok=True)
    focus(client, w)
    # Scroll follows the cursor — no click needed (a click would select a row
    # and open the detail flyout).
    sx, sy = frac_to_screen(w, LAYOUT.scroll_x, LAYOUT.scroll_y)

    saved: list[dict] = []
    prev = None
    stable = 0
    prev_region: str | None = None
    seen_keys: set[str] = set()  # Fast — progress logging only
    confirmed: set[str] = set()  # Accurate — early-stop gate
    ui_count: int | None = None
    carry: str | None = None
    count_met = False
    for i in range(max_scrolls + 1):
        if i > 0:
            scroll_at(sx, sy, lines=scroll_lines)
            prev_region = after_action(w, prev_region)
        park_mouse(w)
        path = outdir / f"{prefix}_{i:02d}.png"
        im, retries = capture_settled(path, w, prev_region=prev_region)
        if im is None:
            log(f"  [{prefix} {i}] capture failed", lines)
            break
        prev_region = region_hash(im)
        h = phash(im)
        mean = float(np.asarray(im).mean())
        marker = hit_stop_marker(path, prefix)
        new_keys, expected, carry = _peek(path, prefix, carry)
        seen_keys |= new_keys
        if expected is not None:
            ui_count = expected if ui_count is None else max(ui_count, expected)
        # Near the UI target, accumulate Accurate names for a trustworthy stop.
        if ui_count is not None and len(seen_keys) >= max(1, ui_count - 3):
            acc_keys, acc_exp, _ = _peek(path, prefix, carry, fast=False)
            confirmed |= acc_keys
            if acc_exp is not None:
                ui_count = max(ui_count, acc_exp)
        log(
            f"  [{prefix} {i}] hash={h} mean={mean:.1f}"
            + (f" settle={retries}" if retries else "")
            + (
                f" rows~{len(seen_keys)}"
                + (f"/{ui_count}" if ui_count else "")
                + (f" conf={len(confirmed)}" if confirmed else "")
            )
            + (f" marker={marker!r}" if marker else ""),
            lines,
        )
        saved.append(
            {"file": path.name, "hash": h, "mean": mean, "stop_marker": marker}
        )
        # Stop only on Accurate cumulative count (Fast overcounts garbles).
        if (
            ui_count is not None
            and len(confirmed) >= ui_count
            and (marker or (i > 0 and not new_keys))
        ):
            log(
                f"  [{prefix}] stop — verified count met "
                f"({len(confirmed)}/{ui_count}, accurate)",
                lines,
            )
            count_met = True
            break
        # Keep the frame that first shows the next section (context), then stop.
        if marker and i > 0:
            # One frame past the marker: the bottom row of this frame may be
            # clipped in half, and the parser discards those partial reads.
            # Scrolling once more renders it whole.
            scroll_at(sx, sy, lines=scroll_lines)
            prev_region = after_action(w, prev_region)
            park_mouse(w)
            tail = outdir / f"{prefix}_{i + 1:02d}.png"
            tail_im, _ = capture_settled(tail, w, prev_region=prev_region)
            if tail_im is not None:
                saved.append(
                    {
                        "file": tail.name,
                        "hash": phash(tail_im),
                        "mean": float(np.asarray(tail_im).mean()),
                        "stop_marker": marker,
                    }
                )
            log(f"  [{prefix}] stop — reached {marker!r} (+1 tail frame)", lines)
            break
        if prev is not None and h == prev:
            stable += 1
            if stable >= 2:
                # Drop duplicate trailing frames
                for _ in range(stable):
                    last = saved.pop()
                    (outdir / last["file"]).unlink(missing_ok=True)
                    (outdir / last["file"].replace(".png", "_small.png")).unlink(
                        missing_ok=True
                    )
                log(f"  [{prefix}] stop — no visual change", lines)
                break
        else:
            stable = 0
        prev = h

    # Dense charts: page through the embedded list ("Page X of Y" → Next).
    if not count_met:
        saved.extend(
            paginate_capture(
                client,
                w,
                outdir,
                prefix,
                saved,
                lines,
                seen_keys=confirmed or seen_keys,
                ui_count=ui_count,
            )
        )

    (outdir / "index.json").write_text(
        json.dumps({"section": prefix, "shots": saved}, indent=2)
    )
    return saved


def dismiss_center_dialog(
    client: MacWindowClient,
    w,
    out: Path,
    lines: list[str],
) -> dict | None:
    """Detect PowerChart auth/deny dialog, click OK (or center), return reason.

    Returns None if no dialog; otherwise a dict with reason + click target.
    Typical modal title: "No authorized lifetime relationships."
    """
    proof = out / "dialog"
    proof.mkdir(parents=True, exist_ok=True)
    im = capture(proof / "before.png", w)
    if im is None:
        return None

    # Apple Vision only — mid-capture OCR is fully on-device now.
    vlines = _vision_lines(proof / "before.png")
    text = " ".join(l["text"] for l in vlines).lower()
    ok_box = None
    title_box = None
    for l in vlines:
        low = l["text"].strip().lower()
        if low in ("ok", "o.k.", "okay") or low == "ok.":
            ok_box = l
        if "no authorized" in low or "authorized lifetime" in low:
            title_box = l

    markers = (
        "no authorized lifetime",
        "not authorized for any lifetime",
        "privilege database",
        "no active encounters",
        "authorized lifetime relationships",
    )
    if not any(m in text for m in markers):
        return None

    bx, by, bw, bh = w.bounds
    # Prefer Vision "OK" box; else under dialog title; else window center.
    # Vision boxes are screenshot pixels — convert via window_scale.
    if ok_box is not None:
        ax, ay = px_to_screen(
            w, im, ok_box["x"] + ok_box["w"] / 2, ok_box["y"] + ok_box["h"] / 2
        )
        how = "vision-ok"
    elif title_box is not None:
        # Dialog OK sits bottom-right of the modal under the title bar.
        ax, ay = px_to_screen(
            w,
            im,
            title_box["x"] + max(title_box["w"], 220) - 40,
            title_box["y"] + 130,
        )
        how = "title-relative-ok"
    else:
        ax = bx + bw / 2
        ay = by + bh / 2
        how = "window-center"

    log(f"  AUTH DIALOG detected — dismiss via {how} @({ax:.0f},{ay:.0f})", lines)
    abs_click(ax, ay)
    time.sleep(0.4)
    key(36)  # Enter also accepts default OK
    time.sleep(0.6)
    capture(proof / "after.png", organizer(client) or w)
    return {
        "reason": "unauthorized",
        "message": "No authorized lifetime relationships",
        "dismiss": how,
        "click": [round(ax), round(ay)],
    }


def replay_events(
    client: MacWindowClient,
    events: list[dict],
    out: Path,
    lines: list[str],
) -> tuple[list[dict], dict | None]:
    """Replay recorded events with before/after screenshots.

    Returns (event_results, skip_info). skip_info is set when an auth dialog
    is dismissed and remaining events are aborted.
    """
    ev_dir = out / "events"
    ev_dir.mkdir(parents=True, exist_ok=True)
    results = []
    skip_info: dict | None = None
    saw_unauthorized = False
    last_xy = (900.0, 700.0)

    # Start clean: close leftover Patient Search
    ps = find(client, "Patient Search")
    if ps:
        log("Closing leftover Patient Search", lines)
        raise_title("Patient Search")
        key(53)
        time.sleep(0.5)
        if find(client, "Patient Search"):
            bx, by, bw, bh = ps.bounds
            abs_click(bx + bw - 40, by + 15)
            time.sleep(0.5)

    org = organizer(client)
    if not org:
        raise SystemExit("No PowerChart Organizer window")
    focus(client, org)

    # Hard gate: CGEvent clicks go to whatever app is frontmost. If another
    # app (e.g. a browser) covers the screen, our clicks would land there.
    for _ in range(6):
        if citrix_is_front():
            break
        log(f"  waiting for Citrix front (front={front_app_name()!r})", lines)
        focus(client, org)
        time.sleep(1.0)
    if not citrix_is_front():
        skip_info = {
            "reason": "screen_busy",
            "message": f"Front app is {front_app_name()!r}, not Citrix Viewer",
        }
        log("ABORT screen_busy — another app is covering the screen", lines)
        return results, skip_info

    # Clear leftover auth dialog from a previous patient before starting.
    leftover = dismiss_center_dialog(client, org, out, lines)
    if leftover:
        log("  cleared leftover auth dialog from prior patient", lines)

    for ev in events:
        i = int(ev.get("i", 0))
        kind = ev.get("kind")
        w = chart_window(client) or organizer(client)
        if not w:
            results.append({"i": i, "kind": kind, "ok": False, "err": "no window"})
            break
        focus(client, w)
        capture(ev_dir / f"{i:02d}_{kind}_before.png", w)

        try:
            if kind in ("click", "double_click"):
                x, y = float(ev["x"]), float(ev["y"])
                # Recording is Organizer/window-scoped; map via Organizer origin
                # until chart is open (same fullscreen bounds in practice).
                base = organizer(client) or w
                ax, ay = base.bounds[0] + x, base.bounds[1] + y
                last_xy = (ax, ay)
                clicks = 2 if kind == "double_click" else 1
                log(f"[{i}] {kind} ({x:.0f},{y:.0f}) → screen ({ax:.0f},{ay:.0f})", lines)
                abs_click(ax, ay, clicks=clicks)
                time.sleep(1.15 if kind == "double_click" else 0.55)
                if kind == "double_click":
                    # Dense charts can take ~10s+ to open; poll before any
                    # not-open fallback fires.
                    for _ in range(20):
                        if chart_window(client):
                            break
                        time.sleep(0.6)
            elif kind == "type":
                text = ev.get("text") or ""
                log(f"[{i}] type {text!r}", lines)
                try:
                    client.type_chars(text)
                except Exception as e:
                    log(f"  type_chars failed: {e}; osascript", lines)
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            f'tell application "System Events" to keystroke "{text}"',
                        ],
                        check=False,
                    )
                time.sleep(0.5)
                if re_looks_like_mrn(text):
                    key(36)  # Enter to submit search
                    time.sleep(1.2)
            elif kind == "scroll":
                base = chart_window(client) or organizer(client)
                ax, ay = frac_to_screen(base, LAYOUT.scroll_x, LAYOUT.scroll_y)
                dy = ev.get("dy") or 100
                log(f"[{i}] scroll dy={dy}", lines)
                scroll_at(ax, ay, lines=-4 if dy > 0 else 4)
                time.sleep(0.7)
            else:
                log(f"[{i}] skip unknown {kind}", lines)

            w2 = chart_window(client) or organizer(client)
            if w2:
                capture(ev_dir / f"{i:02d}_{kind}_after.png", w2)
            results.append({"i": i, "kind": kind, "ok": True})

            # After open-related actions, dismiss auth modal and abort so the
            # batch can move on. Also check early clicks — leftover dialogs
            # sometimes appear before the double-click (seen on 103-339-64).
            if kind in ("double_click", "type", "click") and not chart_window(client):
                check_w = organizer(client) or w2 or w
                if check_w:
                    info = dismiss_center_dialog(client, check_w, out, lines)
                    if info:
                        saw_unauthorized = True
                        # Auth dialog during open sequence → dismiss and move on.
                        # (Seen after MRN type/Enter as well as after double-click.)
                        if kind in ("double_click", "type") or i >= 2:
                            skip_info = info
                            log(
                                "  aborting remaining events — unauthorized, moving on",
                                lines,
                            )
                            break
                        log("  dismissed dialog; continuing open sequence", lines)
                    elif kind == "double_click":
                        # OCR miss fallback: click window center + Enter and move on.
                        bx, by, bw, bh = check_w.bounds
                        ax, ay = bx + bw / 2, by + bh / 2
                        log(
                            f"  no chart after double-click — center fallback "
                            f"@({ax:.0f},{ay:.0f}) then abort",
                            lines,
                        )
                        abs_click(ax, ay)
                        time.sleep(0.35)
                        key(36)
                        time.sleep(0.45)
                        # Re-check: dialog text may OCR better after focus nudge.
                        info2 = dismiss_center_dialog(client, check_w, out, lines)
                        if info2:
                            saw_unauthorized = True
                            skip_info = info2
                        elif saw_unauthorized:
                            skip_info = {
                                "reason": "unauthorized",
                                "message": "No authorized lifetime relationships",
                                "dismiss": "window-center-fallback",
                                "click": [round(ax), round(ay)],
                                "note": "auth dialog seen earlier in open sequence",
                            }
                        else:
                            skip_info = {
                                "reason": "chart_not_open",
                                "message": "Chart did not open after double-click; dismissed center box",
                                "dismiss": "window-center-fallback",
                                "click": [round(ax), round(ay)],
                            }
                        break
        except Exception as e:
            log(f"[{i}] ERROR {e}", lines)
            results.append({"i": i, "kind": kind, "ok": False, "err": str(e)})
            # Still try to clear a blocking dialog so the next MRN isn't stuck.
            org2 = organizer(client)
            if org2:
                info = dismiss_center_dialog(client, org2, out, lines)
                if info:
                    skip_info = info
                    break

    return results, skip_info


def ensure_verified_expanded(
    client: MacWindowClient,
    w,
    lines: list[str],
    proof_dir: Path,
    reuse: Path | None = None,
) -> bool:
    """Expand 'Verified Local Record Data' if the group header is collapsed.

    Collapsed headers show the count but no rows — OCR then reports 0/N.
    Click a few px left of the header text to hit the disclosure chevron.
    """
    proof_dir.mkdir(parents=True, exist_ok=True)
    focus(client, w)
    proof = proof_dir / "verified_expand_before.png"
    if reuse is not None and reuse.exists():
        im = Image.open(reuse)
        proof = reuse
    else:
        im = capture(proof, w)
    if im is None:
        return False
    vlines = _vision_lines(proof)
    hit = next(
        (l for l in vlines if "verified local record data" in l["text"].lower()),
        None,
    )
    if hit is None:
        return False
    iw = im.width
    # Expanded ⇒ at least one name-column cell in the 30–140 px under the header
    # that isn't another section title.
    below = [
        l
        for l in vlines
        if hit["y"] + 25 < l["y"] < hit["y"] + 140
        and LAYOUT.content_x0 * iw <= l["x"] <= LAYOUT.content_x0 * iw + 120
        and l["text"].strip().lower()
        not in ("home medications", "important links", "recommendations")
        and not l["text"].strip().lower().startswith("unverified")
        and not l["text"].strip().lower().startswith("verified")
    ]
    if below:
        log("  verified group already expanded", lines)
        return True
    prev = region_hash(im)
    ax, ay = px_to_screen(w, im, hit["x"] - 12, hit["y"] + hit["h"] / 2)
    abs_click(ax, ay)
    after_action(w, prev)
    log(f"  expand Verified Local @({ax:.0f},{ay:.0f})", lines)
    capture(proof_dir / "verified_expand_after.png", w)
    return True


def scroll_content_top(w, lines: list[str], n: int = 6) -> None:
    """Scroll the content pane up until Histories tabs reappear (or n bursts)."""
    sx, sy = frac_to_screen(w, LAYOUT.scroll_x, LAYOUT.scroll_y)
    prev = None
    probe = Path("/tmp/openadapt_pc_scroll_top.png")
    for i in range(n):
        scroll_at(sx, sy, lines=5)
        prev = after_action(w, prev)
        im = capture(probe, w, make_small=False)
        if im is None:
            continue
        text = _ocr_snippet(probe).lower()
        if "procedure history" in text and "problem history" in text:
            log(f"  scroll content to top — tabs visible after {i + 1} burst(s)", lines)
            return
    log(f"  scroll content to top ({n} bursts, tabs not confirmed)", lines)


def capture_verified_sections(
    client: MacWindowClient, out: Path, lines: list[str]
) -> dict:
    """After chart is open: panel/tabs + dense scroll screenshots for OCR."""
    chart = chart_window(client)
    if not chart:
        log("WARN: chart not open — skipping verified section capture", lines)
        return {}

    sections: dict = {}
    shots = out / "verified"
    shots.mkdir(parents=True, exist_ok=True)

    nav_proof = shots / "_nav_proofs"
    tab_proofs = shots / "_tab_proofs"

    # Allergies
    log("SECTION allergies", lines)
    after = panel_click(client, chart, "allergies", lines, proof_dir=nav_proof)
    ensure_verified_expanded(
        client, chart, lines, shots / "_expand" / "allergies", reuse=after
    )
    sections["allergies"] = scroll_capture_section(
        client,
        chart,
        shots / "allergies",
        "allergies",
        lines,
        max_scrolls=10,
        scroll_lines=-2,  # denser frames so Shrimp row is not missed
    )

    # Problem History
    log("SECTION problem_history", lines)
    chart = chart_window(client) or chart
    after = panel_click(client, chart, "histories", lines, proof_dir=nav_proof)
    # Fresh capture for tabs (panel proof is fine — no scroll yet).
    problem_tab = tab_click(
        client,
        chart,
        "problem_history",
        lines,
        proof_dir=tab_proofs,
        reuse=after,
    )
    ensure_verified_expanded(
        client,
        chart,
        lines,
        shots / "_expand" / "problem_history",
        reuse=problem_tab or after,
    )
    sections["problem_history"] = scroll_capture_section(
        client,
        chart,
        shots / "problem_history",
        "problem_history",
        lines,
        max_scrolls=10,
        scroll_lines=-3,
    )

    # Procedure History — re-enter Histories so the tab strip is at the top.
    # Scrolling back up after a dense problem list is unreliable (tabs stay
    # off-screen); a fresh panel click resets the widget.
    log("SECTION procedure_history", lines)
    chart = chart_window(client) or chart
    after = panel_click(client, chart, "histories", lines, proof_dir=nav_proof)
    # Landing view is usually Problem History — snapshot as change baseline.
    landing = tab_proofs / "histories_landing.png"
    focus(client, chart)
    capture(landing, chart)
    tab_after = tab_click(
        client,
        chart,
        "procedure_history",
        lines,
        proof_dir=tab_proofs,
        reuse=landing if landing.exists() else after,
        prior_tab_proof=landing if landing.exists() else problem_tab,
    )
    if tab_after is None:
        log("  WARN: procedure tab not confirmed after Histories re-entry", lines)
    ensure_verified_expanded(
        client,
        chart,
        lines,
        shots / "_expand" / "procedure_history",
        reuse=tab_after,
    )
    sections["procedure_history"] = scroll_capture_section(
        client,
        chart,
        shots / "procedure_history",
        "procedure_history",
        lines,
        max_scrolls=16,
        scroll_lines=-2,  # denser: rows can be OCR-missed when hover-highlighted
    )

    (shots / "index.json").write_text(
        json.dumps(
            {
                "mrn": MRN,
                "sections": {k: v for k, v in sections.items()},
            },
            indent=2,
        )
    )
    return sections


def close_patient(client: MacWindowClient, out: Path, lines: list[str]) -> bool:
    chart = chart_window(client)
    if not chart:
        log("close: chart already closed", lines)
        return True
    focus(client, chart)
    close_dir = out / "close"
    close_dir.mkdir(parents=True, exist_ok=True)
    im = capture(close_dir / "banner_before.png", chart)
    if im:
        im.crop((0, 0, im.width, 220)).save(close_dir / "top_banner.png")

    # Prefer a Vision "×" / "x" near the chart tab that carries the MRN;
    # otherwise use layout fractions (portable across window sizes).
    clicked = False
    if im is not None:
        vlines = _vision_lines(close_dir / "banner_before.png")
        iw, ih = im.size
        for l in vlines:
            t = l["text"].strip().lower()
            if t not in ("x", "×", "✕", "close"):
                continue
            if l["y"] > 0.15 * ih or l["x"] > 0.25 * iw:
                continue
            click_vision_box(chart, im, l, lines, "close")
            clicked = True
            break
    if not clicked:
        ax, ay = frac_to_screen(chart, LAYOUT.close_x, LAYOUT.close_y)
        abs_click(ax, ay)
        log(f"  close via fraction @({ax:.0f},{ay:.0f})", lines)
    time.sleep(1.2)
    if chart_window(client):
        ax, ay = frac_to_screen(chart, LAYOUT.close_x, LAYOUT.close_y)
        for dx in (0, 7, -5, 10):
            abs_click(ax + dx, ay)
            time.sleep(0.8)
            if not chart_window(client):
                break
    closed = chart_window(client) is None
    log(f"close: closed={closed}", lines)
    org = organizer(client)
    if org:
        focus(client, org)
        capture(close_dir / "organizer_after.png", org)
    return closed


def load_events_for_mrn(events_path: Path, mrn: str) -> list[dict]:
    """Load template events, substitute typed MRN, drop trailing close click."""
    events = [
        json.loads(l) for l in events_path.read_text().splitlines() if l.strip()
    ]
    for ev in events:
        if ev.get("kind") == "type" and ev.get("text"):
            # Replace whatever MRN the recording typed with the target.
            if looks_like_mrn(str(ev["text"])):
                ev["text"] = mrn
    if (
        events
        and events[-1].get("kind") == "click"
        and float(events[-1].get("y", 0)) < 150
        and float(events[-1].get("x", 0)) < 200
    ):
        events = events[:-1]
    return events


def re_looks_like_mrn(text: str) -> bool:
    """Backward-compatible alias — site MRN pattern lives in pc_config."""
    return looks_like_mrn(text)


def preflight_organizer(
    client: MacWindowClient, out: Path, lines: list[str]
) -> dict | None:
    """Abort early if Organizer is on the wrong view (e.g. Message Center).

    Returns skip_info dict when the MRN search UI is not available; else None.
    """
    org = organizer(client)
    if not org:
        return {
            "reason": "wrong_view",
            "message": "No PowerChart Organizer window found",
        }
    focus(client, org)
    proof = out / "preflight"
    proof.mkdir(parents=True, exist_ok=True)
    im = capture(proof / "organizer.png", org)
    if im is None:
        return None  # can't tell; let the open sequence try
    text = _ocr_snippet(proof / "organizer.png").lower()
    ready = any(m in text for m in PREFLIGHT_READY_MARKERS)
    wrong = any(m in text for m in PREFLIGHT_WRONG_VIEW_MARKERS)
    log(
        f"  preflight ready={ready} wrong_view_hint={wrong} "
        f"(front={front_app_name()!r})",
        lines,
    )
    (proof / "ocr.txt").write_text(text[:4000])
    if wrong and not ready:
        return {
            "reason": "wrong_view",
            "message": (
                "Organizer is not on the patient-search / list view "
                "(saw Message Center or similar). Switch views and re-run."
            ),
            "ocr_snippet": text[:240],
        }
    return None


def run_one(
    *,
    mrn: str,
    events_path: Path,
    out: Path,
    skip_events: bool = False,
    skip_sections: bool = False,
    skip_close: bool = False,
) -> dict:
    """Run capture for one MRN. Returns SUMMARY dict (also written to disk)."""
    import shutil

    set_mrn(mrn)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    lines: list[str] = []
    client = MacWindowClient()
    log(f"MRN={mrn} windows={[(w.window_id, w.title) for w in wins(client)]}", lines)

    results: list[dict] = []
    skip_info: dict | None = None
    if not skip_events:
        # Refuse to click into Message Center / covered screens.
        skip_info = preflight_organizer(client, out, lines)
        if skip_info:
            log(f"STATUS {skip_info['reason']} — aborting before open sequence", lines)
        else:
            events = load_events_for_mrn(events_path, mrn)
            log(f"Events loaded: {len(events)} (type→{mrn!r})", lines)
            results, skip_info = replay_events(client, events, out, lines)
            log(
                f"Events: {sum(1 for r in results if r.get('ok'))}/{len(results)} ok",
                lines,
            )

    # Final safety: clear any leftover auth dialog before leaving this patient.
    org = organizer(client)
    if org and not skip_info:
        leftover = dismiss_center_dialog(client, org, out, lines)
        if leftover:
            skip_info = leftover

    sections: dict = {}
    status = "ok"
    if skip_info and skip_info.get("reason") == "unauthorized":
        status = "unauthorized"
        log("STATUS unauthorized — skipping section capture", lines)
    elif skip_info and skip_info.get("reason") == "chart_not_open":
        status = "chart_not_open"
        log("STATUS chart_not_open — skipping section capture", lines)
    elif skip_info and skip_info.get("reason") == "screen_busy":
        status = "screen_busy"
        log("STATUS screen_busy — skipping section capture", lines)
    elif skip_info and skip_info.get("reason") == "wrong_view":
        status = "wrong_view"
        log("STATUS wrong_view — skipping section capture", lines)
    elif not skip_sections:
        if not chart_window(client):
            status = "chart_not_open"
            log("Chart not open after events — cannot capture verified sections", lines)
            # Try one more dialog dismiss in case we missed it.
            if org:
                leftover = dismiss_center_dialog(client, org, out, lines)
                if leftover:
                    skip_info = leftover
                    status = "unauthorized"
        else:
            sections = capture_verified_sections(client, out, lines)

    closed = None
    if not skip_close and status == "ok":
        closed = close_patient(client, out, lines)
    elif status != "ok":
        # Ensure Organizer is focused and clean for the next patient.
        org = organizer(client)
        if org:
            focus(client, org)

    summary = {
        "mrn": mrn,
        "status": status,
        "skip": skip_info,
        "events_file": str(events_path),
        "out": str(out),
        "event_results": results,
        "verified_sections": {
            k: [s.get("file") for s in v] for k, v in sections.items()
        },
        "closed": closed,
        "windows_end": [(w.window_id, w.title) for w in wins(client)],
    }
    (out / "SUMMARY.json").write_text(json.dumps(summary, indent=2))
    (out / "log.txt").write_text("\n".join(lines))
    log(f"Done → {out} status={status}", lines)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mrn", default="080-975-37")
    ap.add_argument(
        "--events",
        type=Path,
        default=Path("app-run/powerchart/template/rec5/events.jsonl"),
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
    )
    ap.add_argument("--skip-events", action="store_true")
    ap.add_argument("--skip-sections", action="store_true")
    ap.add_argument("--skip-close", action="store_true")
    args = ap.parse_args()
    out = args.out or Path(f"app-run/powerchart/out/{args.mrn}")
    run_one(
        mrn=args.mrn,
        events_path=args.events,
        out=out,
        skip_events=args.skip_events,
        skip_sections=args.skip_sections,
        skip_close=args.skip_close,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
