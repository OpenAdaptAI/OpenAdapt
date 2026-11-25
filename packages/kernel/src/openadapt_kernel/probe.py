"""Reversible pre-contact occlusion and hover/focus differential probe.

Checks whether a target control is actually the topmost hit-tested element at
its center coordinates (catching transparent overlays and modal click-jacking)
and verifies a reversible hover/focus state response before committing a click.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
        return ProbeResult(
            safe_to_click=False,
            found=found,
            occluded=occluded,
            disabled=disabled,
            focus_responded=False,
            reason=reason,
            top_blocker=blocker,
        )

    focus_ok = False
    try:
        loc = page.locator(selector).first
        loc.hover(timeout=800)
        page.wait_for_timeout(80)
        focus_ok = bool(
            page.evaluate(
                """(sel) => {
                  const el = document.querySelector(sel);
                  if (!el) return false;
                  el.focus({ preventScroll: true });
                  return document.activeElement === el || el.matches(':hover');
                }""",
                selector,
            )
        )
    except Exception:
        focus_ok = False

    return ProbeResult(
        safe_to_click=True,
        found=True,
        occluded=False,
        disabled=False,
        focus_responded=focus_ok,
        reason="clear",
    )


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
