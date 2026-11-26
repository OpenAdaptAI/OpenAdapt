"""Reversible pre-contact occlusion and hover/focus differential probe.

Checks whether a target control is actually the topmost hit-tested element at
its center coordinates (catching transparent overlays and modal click-jacking)
and verifies a reversible hover/focus state response before committing a click.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

_HIT_TEST_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el) return { found: false, occluded: true, reason: 'not_found' };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) {
    return { found: true, occluded: true, reason: 'zero_box' };
  }
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  const stack = document.elementsFromPoint(cx, cy);
  if (!stack || stack.length === 0) {
    return { found: true, occluded: true, reason: 'empty_hit_stack' };
  }
  const top = stack[0];
  const sameOrChild = top === el || el.contains(top) || top.contains(el);
  const disabled = Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true');
  return {
    found: true,
    occluded: !sameOrChild,
    disabled,
    top_tag: top.tagName ? top.tagName.toLowerCase() : '',
    top_id: top.id || '',
    reason: !sameOrChild ? 'occluded_by_overlay' : (disabled ? 'control_disabled' : 'clear'),
  };
}"""


@dataclass
class ProbeResult:
    safe_to_click: bool
    found: bool
    occluded: bool
    disabled: bool
    focus_responded: bool
    reason: str
    top_blocker: str = ""


def probe_before_click(page: Any, selector: str) -> ProbeResult:
    """Run zero-side-effect hit-test occlusion check + reversible hover/focus probe."""
    hit = page.evaluate(_HIT_TEST_JS, selector) or {}
    found = bool(hit.get("found", False))
    occluded = bool(hit.get("occluded", True))
    disabled = bool(hit.get("disabled", False))
    reason = str(hit.get("reason") or "unknown")
    blocker = ""
    if occluded and found:
        blocker = f"{hit.get('top_tag', '')}#{hit.get('top_id', '')}".rstrip("#")

    if not found or occluded or disabled:
        return ProbeResult(False, found, occluded, disabled, False, reason, blocker)

    focus_ok = False
    try:
        loc = page.locator(selector).first
        loc.hover(timeout=800)
        page.wait_for_timeout(80)
        focus_ok = bool(page.evaluate("(sel) => { const el = document.querySelector(sel); if (!el) return false; el.focus({ preventScroll: true }); return document.activeElement === el || el.matches(':hover'); }", selector))
    except Exception:
        focus_ok = False

    return ProbeResult(True, True, False, False, focus_ok, "clear")


def compute_pixel_delta_ratio(pre_bytes: bytes, post_bytes: bytes, threshold: int = 8) -> float:
    """Compute fraction of differing bytes/samples between two equal-sized frame buffers (RDP/Canvas/Win32)."""
    if not pre_bytes or not post_bytes:
        return 0.0
    n = min(len(pre_bytes), len(post_bytes))
    diff = sum(1 for a, b in zip(pre_bytes[:n], post_bytes[:n]) if abs(a - b) > threshold)
    return round(diff / max(n, 1), 6)


def probe_hover_pixel_delta(
    page: Any,
    x: float,
    y: float,
    *,
    box_size: int = 120,
    wait_ms: int = 80,
) -> float:
    """Reversible 80ms hover differential pixel probe at (x, y) for Canvas/RDP/non-DOM controls."""
    clip = {
        "x": max(0.0, x - box_size / 2.0),
        "y": max(0.0, y - box_size / 2.0),
        "width": float(box_size),
        "height": float(box_size),
    }
    pre = bytes(page.screenshot(clip=clip))
    page.mouse.move(x, y)
    page.wait_for_timeout(wait_ms)
    post = bytes(page.screenshot(clip=clip))
    if pre == post:
        return 0.0
    return max(compute_pixel_delta_ratio(pre, post, threshold=1), 0.001)


@dataclass(frozen=True)
class VisualServoResult:
    converged: bool
    final_x: float
    final_y: float
    iterations: int
    hover_delta: float
    estimated_scale_x: float
    estimated_scale_y: float
    reason: str = ""


def visual_hover_servo_click(
    target_norm_x: float, target_norm_y: float, *, window_width: float, window_height: float,
    move_and_observe_fn: Callable[[float, float], Tuple[float, float, float, float]],
    hover_probe_fn: Callable[[float, float], float],
    max_iters: int = 6, tol_px: float = 4.0, delta_threshold: float = 0.01,
) -> VisualServoResult:
    """Closed-loop proportional hover-servoing invariant to unknown DPI scaling and letterbox offsets."""
    cmd_x, cmd_y, sx, sy = float(target_norm_x) * float(window_width), float(target_norm_y) * float(window_height), 1.0, 1.0
    prev_cmd: Optional[Tuple[float, float]] = None
    prev_obs: Optional[Tuple[float, float]] = None
    limit = max(1, int(max_iters))
    for it in range(1, limit + 1):
        obs_x, obs_y, tgt_x, tgt_y = move_and_observe_fn(cmd_x, cmd_y)
        if prev_cmd is not None and prev_obs is not None:
            dcx, dcy, dox, doy = cmd_x - prev_cmd[0], cmd_y - prev_cmd[1], obs_x - prev_obs[0], obs_y - prev_obs[1]
            if abs(dcx) >= 1e-3 and abs(dox) >= 1e-3:
                sx = max(0.25, min(4.0, dox / dcx))
            if abs(dcy) >= 1e-3 and abs(doy) >= 1e-3:
                sy = max(0.25, min(4.0, doy / dcy))
        err_x, err_y = tgt_x - obs_x, tgt_y - obs_y
        if math.hypot(err_x, err_y) <= tol_px:
            delta = float(hover_probe_fn(cmd_x, cmd_y))
            ok = delta >= delta_threshold
            return VisualServoResult(ok, cmd_x, cmd_y, it, delta, round(sx, 4), round(sy, 4), "converged" if ok else "hover_delta_below_threshold")
        prev_cmd, prev_obs = (cmd_x, cmd_y), (obs_x, obs_y)
        cmd_x, cmd_y = cmd_x + err_x / sx, cmd_y + err_y / sy
    return VisualServoResult(False, cmd_x, cmd_y, limit, float(hover_probe_fn(cmd_x, cmd_y)), round(sx, 4), round(sy, 4), "max_iters_exceeded")
