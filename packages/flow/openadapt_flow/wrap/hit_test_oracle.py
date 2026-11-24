"""Physical hit-test pre-flight and closed-loop post-click delta oracle.

Before clicking ``(x, y)``, :func:`evaluate_hit_test` inspects the top DOM
element returned by ``document.elementsFromPoint(x, y)`` to catch:

- empty hit tests (no element at coordinates),
- disabled targets (``disabled`` or ``aria-disabled="true"``),
- overlay/banner/modal occlusion covering an interactive control underneath,
- grid/table row-header misalignment (click landed on the wrong row), and
- label mismatches when an expected label is supplied.

After clicking, :func:`evaluate_post_click_delta` compares before/after DOM
digests and HTTP response statuses to detect HTTP 4xx/5xx failures and
``SILENT_NOOP_CLICK`` events.

Safety invariant: when ``is_irreversible=True``, a ``SILENT_NOOP_CLICK`` is
NEVER marked safe to auto-retry; it routes to system-of-record read-back
(``next_action="verify_readback"``) so duplicate submissions cannot happen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DECISION_ALLOW = "allow"
DECISION_BLOCK = "block"

DELTA_STATE_CHANGED = "state_changed"
DELTA_SILENT_NOOP = "silent_noop_click"
DELTA_HTTP_ERROR = "http_error"

NEXT_CONTINUE = "continue"
NEXT_RETRY_OR_REGROUND = "retry_or_reground"
NEXT_VERIFY_READBACK = "verify_readback"
NEXT_ESCALATE = "escalate"

#: Pure JS pre-flight snippet evaluated in the page before clicking (x, y).
HIT_TEST_PREFLIGHT_JS = r"""
({ x, y }) => {
  const stack = document.elementsFromPoint(x, y) || [];
  if (stack.length === 0) {
    return {
      top_element_missing: true,
      top_tag: "",
      top_role: "",
      top_text: "",
      disabled: false,
      aria_disabled: false,
      occluded_by_overlay: false,
      aligned_row_key: null,
      interactive_underneath: false,
    };
  }
  const top = stack[0];
  const isInteractive = (el) => {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (["a", "button", "input", "select", "textarea", "option"].includes(tag)) {
      return true;
    }
    const role = (el.getAttribute("role") || "").toLowerCase();
    return ["button", "link", "menuitem", "tab", "checkbox", "radio", "switch"].includes(role);
  };
  const isOverlayLike = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const style = window.getComputedStyle(el);
    const pos = style ? style.position : "";
    const z = style ? parseInt(style.zIndex || "0", 10) : 0;
    const role = (el.getAttribute("role") || "").toLowerCase();
    const cls = (typeof el.className === "string" ? el.className : "").toLowerCase();
    if (["dialog", "alertdialog", "banner", "status"].includes(role)) return true;
    if (cls.includes("overlay") || cls.includes("backdrop") || cls.includes("toast") || cls.includes("cookie")) {
      return true;
    }
    return (pos === "fixed" || pos === "sticky") && z >= 10 && !isInteractive(el);
  };
  const interactiveUnderneath = stack.slice(1).some((el) => isInteractive(el));
  const occludedByOverlay = !isInteractive(top) && interactiveUnderneath && isOverlayLike(top);
  const rowEl = top.closest ? top.closest("tr, [role='row']") : null;
  let alignedRowKey = null;
  if (rowEl) {
    const rowHeader = rowEl.querySelector("th, [role='rowheader'], [data-row-key]");
    if (rowHeader) {
      alignedRowKey = (
        rowHeader.getAttribute("data-row-key") ||
        rowHeader.textContent ||
        ""
      ).trim();
    } else if (rowEl.getAttribute("data-row-key")) {
      alignedRowKey = rowEl.getAttribute("data-row-key").trim();
    }
  }
  const topText = (
    top.getAttribute("aria-label") ||
    top.innerText ||
    top.textContent ||
    top.value ||
    ""
  ).trim();
  return {
    top_element_missing: false,
    top_tag: (top.tagName || "").toLowerCase(),
    top_role: (top.getAttribute("role") || "").toLowerCase(),
    top_text: topText.slice(0, 240),
    disabled: Boolean(top.disabled) || top.hasAttribute("disabled"),
    aria_disabled: (top.getAttribute("aria-disabled") || "").toLowerCase() === "true",
    occluded_by_overlay: occludedByOverlay,
    aligned_row_key: alignedRowKey,
    interactive_underneath: interactiveUnderneath,
  };
}
"""

#: Pure JS structural DOM digest snippet evaluated before and after a click.
DOM_DIGEST_JS = r"""
() => {
  const text = (document.body && document.body.innerText) ? document.body.innerText : "";
  const inputs = Array.from(document.querySelectorAll("input, textarea, select"))
    .map((el) => `${el.name || el.id || el.tagName}=${el.type === "checkbox" || el.type === "radio" ? el.checked : el.value}`)
    .join("|");
  const dialogs = document.querySelectorAll("dialog[open], [role='dialog'], [role='alertdialog']").length;
  const payload = `${location.href}\n${document.title}\n${dialogs}\n${inputs}\n${text}`;
  let h = 2166136261;
  for (let i = 0; i < payload.length; i++) {
    h ^= payload.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return {
    url: location.href,
    title: document.title,
    dialog_count: dialogs,
    text_length: text.length,
    digest: (h >>> 0).toString(16).padStart(8, "0"),
  };
}
"""


@dataclass(frozen=True)
class HitTestProbe:
    """Normalized physical hit-test observation at coordinates ``(x, y)``."""

    top_element_missing: bool = False
    top_tag: str = ""
    top_role: str = ""
    top_text: str = ""
    disabled: bool = False
    aria_disabled: bool = False
    occluded_by_overlay: bool = False
    aligned_row_key: str | None = None
    interactive_underneath: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HitTestProbe:
        row_key = raw.get("aligned_row_key")
        return cls(
            top_element_missing=bool(raw.get("top_element_missing", False)),
            top_tag=str(raw.get("top_tag", "")),
            top_role=str(raw.get("top_role", "")),
            top_text=str(raw.get("top_text", "")),
            disabled=bool(raw.get("disabled", False)),
            aria_disabled=bool(raw.get("aria_disabled", False)),
            occluded_by_overlay=bool(raw.get("occluded_by_overlay", False)),
            aligned_row_key=str(row_key) if row_key is not None else None,
            interactive_underneath=bool(raw.get("interactive_underneath", False)),
        )


@dataclass(frozen=True)
class HitTestDecision:
    """Pre-flight click decision."""

    decision: str  # DECISION_ALLOW | DECISION_BLOCK
    code: str
    detail: str


@dataclass(frozen=True)
class DomDigest:
    """Compact structural DOM snapshot used for closed-loop delta comparison."""

    url: str
    title: str
    dialog_count: int
    text_length: int
    digest: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> DomDigest:
        return cls(
            url=str(raw.get("url", "")),
            title=str(raw.get("title", "")),
            dialog_count=int(raw.get("dialog_count", 0)),
            text_length=int(raw.get("text_length", 0)),
            digest=str(raw.get("digest", "")),
        )


@dataclass(frozen=True)
class PostClickDeltaDecision:
    """Closed-loop post-click delta classification."""

    status: str  # DELTA_STATE_CHANGED | DELTA_SILENT_NOOP | DELTA_HTTP_ERROR
    retry_safe: bool
    next_action: str  # NEXT_CONTINUE | NEXT_RETRY_OR_REGROUND | NEXT_VERIFY_READBACK | NEXT_ESCALATE
    code: str
    detail: str
    error_statuses: tuple[int, ...] = ()


@dataclass(frozen=True)
class SafeClickOutcome:
    """Combined pre-flight and post-click outcome from :func:`safe_click_preflight`."""

    clicked: bool
    preflight: HitTestDecision
    delta: PostClickDeltaDecision | None = None


def evaluate_hit_test(
    probe: HitTestProbe | Mapping[str, Any],
    *,
    expected_row_key: str | None = None,
    expected_label: str | None = None,
) -> HitTestDecision:
    """Evaluate a physical hit-test probe before issuing a click."""
    obs = probe if isinstance(probe, HitTestProbe) else HitTestProbe.from_mapping(probe)

    if obs.top_element_missing:
        return HitTestDecision(
            decision=DECISION_BLOCK,
            code="empty_hit_test",
            detail="No DOM element found at target coordinates.",
        )

    if obs.disabled or obs.aria_disabled:
        return HitTestDecision(
            decision=DECISION_BLOCK,
            code="target_disabled",
            detail=f"Target element <{obs.top_tag}> is disabled or aria-disabled.",
        )

    if obs.occluded_by_overlay:
        return HitTestDecision(
            decision=DECISION_BLOCK,
            code="overlay_occlusion",
            detail=(
                f"Target is occluded by overlay/banner <{obs.top_tag}> "
                f"(role={obs.top_role!r}) over an interactive control."
            ),
        )

    if expected_row_key is not None:
        actual = (obs.aligned_row_key or "").strip().lower()
        expected = expected_row_key.strip().lower()
        if not actual or actual != expected:
            return HitTestDecision(
                decision=DECISION_BLOCK,
                code="row_header_misalignment",
                detail=(
                    f"Grid row header mismatch: expected {expected_row_key!r}, "
                    f"hit row {obs.aligned_row_key!r}."
                ),
            )

    if expected_label is not None:
        if expected_label.strip().lower() not in obs.top_text.strip().lower():
            return HitTestDecision(
                decision=DECISION_BLOCK,
                code="target_label_mismatch",
                detail=(
                    f"Target text {obs.top_text!r} does not contain expected "
                    f"label {expected_label!r}."
                ),
            )

    return HitTestDecision(
        decision=DECISION_ALLOW,
        code="hit_test_clear",
        detail=f"Target <{obs.top_tag}> is visible, enabled, and aligned.",
    )


def evaluate_post_click_delta(
    before_digest: DomDigest | Mapping[str, Any],
    after_digest: DomDigest | Mapping[str, Any],
    *,
    http_statuses: Sequence[int] = (),
    is_irreversible: bool = False,
) -> PostClickDeltaDecision:
    """Evaluate closed-loop post-click DOM digest and HTTP response statuses.

    Never marks a ``SILENT_NOOP_CLICK`` as ``retry_safe=True`` when
    ``is_irreversible=True``; routes irreversible no-ops to read-back instead
    so duplicate submissions cannot occur.
    """
    before = (
        before_digest
        if isinstance(before_digest, DomDigest)
        else DomDigest.from_mapping(before_digest)
    )
    after = (
        after_digest
        if isinstance(after_digest, DomDigest)
        else DomDigest.from_mapping(after_digest)
    )

    err_codes = tuple(int(s) for s in http_statuses if int(s) >= 400)
    if err_codes:
        return PostClickDeltaDecision(
            status=DELTA_HTTP_ERROR,
            retry_safe=False,
            next_action=NEXT_ESCALATE,
            code="http_error_after_click",
            detail=f"Observed HTTP error status(es) after click: {list(err_codes)}.",
            error_statuses=err_codes,
        )

    dom_identical = (
        before.digest == after.digest
        and before.url == after.url
        and before.dialog_count == after.dialog_count
    )
    if dom_identical:
        if is_irreversible:
            return PostClickDeltaDecision(
                status=DELTA_SILENT_NOOP,
                retry_safe=False,
                next_action=NEXT_VERIFY_READBACK,
                code="irreversible_silent_noop",
                detail=(
                    "DOM digest did not move after an irreversible click; "
                    "auto-retry is disabled. Route to read-back before retrying."
                ),
            )
        return PostClickDeltaDecision(
            status=DELTA_SILENT_NOOP,
            retry_safe=True,
            next_action=NEXT_RETRY_OR_REGROUND,
            code="reversible_silent_noop",
            detail="DOM digest did not move after a reversible click; safe to reground.",
        )

    return PostClickDeltaDecision(
        status=DELTA_STATE_CHANGED,
        retry_safe=False,
        next_action=NEXT_CONTINUE,
        code="dom_state_delta_observed",
        detail=f"Observed DOM digest transition {before.digest} -> {after.digest}.",
    )


async def safe_click_preflight(
    page: Any,
    x: float,
    y: float,
    *,
    expected_row_key: str | None = None,
    expected_label: str | None = None,
    is_irreversible: bool = False,
    settle_ms: int = 150,
    http_statuses_provider: Callable[[], Sequence[int] | Awaitable[Sequence[int]]]
    | None = None,
) -> SafeClickOutcome:
    """Async Playwright helper: pre-flight hit-test, click, and post-click delta oracle."""
    raw_probe = await page.evaluate(HIT_TEST_PREFLIGHT_JS, {"x": x, "y": y})
    preflight = evaluate_hit_test(
        raw_probe,
        expected_row_key=expected_row_key,
        expected_label=expected_label,
    )
    if preflight.decision != DECISION_ALLOW:
        return SafeClickOutcome(clicked=False, preflight=preflight, delta=None)

    before_raw = await page.evaluate(DOM_DIGEST_JS)
    await page.mouse.click(x, y)
    if settle_ms > 0:
        await asyncio.sleep(settle_ms / 1000.0)
    after_raw = await page.evaluate(DOM_DIGEST_JS)

    statuses: Sequence[int] = ()
    if http_statuses_provider is not None:
        provided = http_statuses_provider()
        if isinstance(provided, Awaitable):
            statuses = await provided
        else:
            statuses = provided

    delta = evaluate_post_click_delta(
        before_raw,
        after_raw,
        http_statuses=statuses,
        is_irreversible=is_irreversible,
    )
    return SafeClickOutcome(clicked=True, preflight=preflight, delta=delta)


__all__ = [
    "DECISION_ALLOW",
    "DECISION_BLOCK",
    "DELTA_HTTP_ERROR",
    "DELTA_SILENT_NOOP",
    "DELTA_STATE_CHANGED",
    "DOM_DIGEST_JS",
    "DomDigest",
    "HIT_TEST_PREFLIGHT_JS",
    "HitTestDecision",
    "HitTestProbe",
    "NEXT_CONTINUE",
    "NEXT_ESCALATE",
    "NEXT_RETRY_OR_REGROUND",
    "NEXT_VERIFY_READBACK",
    "PostClickDeltaDecision",
    "SafeClickOutcome",
    "evaluate_hit_test",
    "evaluate_post_click_delta",
    "safe_click_preflight",
]
