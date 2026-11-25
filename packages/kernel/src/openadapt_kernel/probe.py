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
