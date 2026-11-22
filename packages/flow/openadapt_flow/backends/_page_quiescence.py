"""Page-quiescence probe for the browser backend (DOM + network settle signal).

The replayer's default settle waits for two identical screenshots taken a poll
interval apart. In a browser the page can report the same thing directly and
more strictly: no DOM mutation, input/focus/scroll/load event, running
animation or transition, unloaded image or font, unfinished document, or
in-flight network request in ANY frame for a full quiet window, and the top
document has rendered at least two frames since the last change (so pending
style/layout work has reached the screen).

:meth:`PageQuiescence.probe` answers three ways:

* ``True``  -- every signal is quiet for the whole window;
* ``False`` -- something is still changing (keep waiting);
* ``None``  -- the page cannot be judged this way (a visible ``canvas``,
  ``video``, ``embed`` or ``object`` paints pixels without touching the DOM, a
  frame cannot be evaluated, the page is closing). The caller must use the
  screenshot settle.

Nothing here turns "unknown" into "quiet": a document whose observer was only
just installed reports busy, and an evaluation error reports ``None``.
"""

from __future__ import annotations

import time
from typing import Any, Optional

#: How long the top document may go without rendering a frame, in ms, before
#: the probe treats paint as unobservable (a hidden or covered window) and
#: the caller falls back to the screenshot settle.
PAINT_STALL_MS = 1000

# Installed in every document (init script for new documents, lazily for
# documents that already existed). Records the last time anything that can
# change pixels happened, as an absolute epoch-ms time comparable across
# frames. OpenAdapt's own guard attributes and Playwright's screenshot caret
# toggle are not page activity and are ignored. The top document also keeps
# the times of its last two rendered frames.
QUIESCENCE_INSTALL_JS = r"""
(() => {
    const KEY = '__oaflowQuiet';
    if (window[KEY]) return;
    const abs = () => performance.timeOrigin + performance.now();
    const state = { last: abs(), installed: abs(), why: 'installed' };
    Object.defineProperty(window, KEY, { value: state, enumerable: false });
    const bump = (why) => { state.last = abs(); state.why = String(why || ''); };
    const stripCaret = (v) => String(v)
        .replace(/caret-color:\s*transparent\s*!important;?/g, '').trim();
    try {
        const observer = new MutationObserver((records) => {
            for (const r of records) {
                if (r.type === 'attributes' && r.attributeName &&
                        r.attributeName.startsWith('data-openadapt-')) {
                    continue;
                }
                if (r.type === 'attributes' && r.attributeName === 'style') {
                    const now = (r.target.getAttribute &&
                        r.target.getAttribute('style')) || '';
                    if (stripCaret(now) === stripCaret(r.oldValue || '')) {
                        continue;
                    }
                }
                bump('mutation:' + r.type + ':' + (r.attributeName || ''));
                return;
            }
        });
        observer.observe(document, {
            subtree: true, childList: true, attributes: true,
            characterData: true, attributeOldValue: true,
        });
    } catch (e) {
        state.broken = true;
    }

    const COSMETIC = new Set([
        'outline', 'outline-color', 'outline-width', 'outline-offset',
        'outline-style', 'box-shadow', 'color', 'background-color',
        'border-color', 'border-top-color', 'border-right-color',
        'border-bottom-color', 'border-left-color', 'text-decoration-color',
        'caret-color', 'fill', 'stroke',
    ]);
    const cosmeticEvent = (ev) => ev && typeof ev.propertyName === 'string' &&
        COSMETIC.has(ev.propertyName);
    for (const type of [
        'input', 'change', 'scroll', 'focusin', 'focusout', 'selectionchange',
        'pointerdown', 'pointerup', 'pointermove', 'pointerover', 'pointerout',
        'mousedown', 'mouseup', 'mousemove', 'click', 'keydown', 'keyup',
        'wheel', 'load', 'error', 'resize', 'transitionstart', 'transitionrun',
        'transitionend', 'transitioncancel', 'animationstart',
        'animationiteration', 'animationend', 'animationcancel',
    ]) {
        const h = (ev) => { if (!cosmeticEvent(ev)) bump(type); };
        try { window.addEventListener(type, h, true); } catch (e) {}
        try { document.addEventListener(type, h, true); } catch (e) {}
    }
    if (window === window.top) {
        state.ticks = [0, 0];
        const tick = () => {
            state.ticks = [state.ticks[1], abs()];
            requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
    }
})();
"""

# Judge one window's own document; '' when quiet, else a reason.
_JUDGE_DOC_JS = (
    r"""
// True when the top document renders no frames: it is hidden, or its last
// animation frame (or, before the first one, the probe's install) is older
// than PAINT_STALL_MS. A visible page renders a frame every 16 ms or so.
const paintStalled = (state, nowAbs) => {
    if (document.visibilityState === 'hidden') return true;
    const ticks = state.ticks || [0, 0];
    const since = ticks[1] > 0 ? ticks[1] : state.installed;
    return nowAbs - since > """
    + str(PAINT_STALL_MS)
    + r""";
};

const COSMETIC = new Set([
    'outline', 'outline-color', 'outline-width', 'outline-offset',
    'outline-style', 'box-shadow', 'color', 'background-color',
    'border-color', 'border-top-color', 'border-right-color',
    'border-bottom-color', 'border-left-color', 'text-decoration-color',
    'caret-color', 'fill', 'stroke',
]);
// A running animation/transition is cosmetic only when EVERY property it
// animates is a paint-only colour/ring property (focus and hover rings). Any
// transform, size, position, opacity or layout property keeps it blocking:
// those move or reveal content.
const cosmeticAnimation = (a) => {
    try {
        if (typeof a.transitionProperty === 'string') {
            return COSMETIC.has(a.transitionProperty);
        }
        const frames = a.effect && a.effect.getKeyframes ? a.effect.getKeyframes() : [];
        if (!frames.length) return false;
        for (const f of frames) {
            for (const k of Object.keys(f)) {
                if (k === 'offset' || k === 'computedOffset' || k === 'easing' ||
                        k === 'composite') continue;
                const css = k.replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
                if (!COSMETIC.has(css)) return false;
            }
        }
        return true;
    } catch (e) { return false; }
};
const judgeDoc = (win, windowMs, nowAbs) => {
    const doc = win.document;
    const state = win.__oaflowQuiet;
    if (!state) return 'missing';
    if (state.broken) return 'unavailable';
    for (const el of doc.querySelectorAll('canvas, video, embed, object')) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const s = win.getComputedStyle(el);
        if (s.visibility !== 'hidden' && s.display !== 'none') return 'unavailable';
    }
    if (doc.readyState !== 'complete') return 'busy:document';
    if (doc.fonts && doc.fonts.status === 'loading') return 'busy:fonts';
    for (const img of doc.images) {
        if (!img.complete) return 'busy:image';
    }
    try {
        for (const a of doc.getAnimations()) {
            if (!(a.playState === 'running' || a.pending)) continue;
            if (cosmeticAnimation(a)) continue;
            return 'busy:animation';
        }
    } catch (e) { return 'unavailable'; }
    if (nowAbs - state.installed < windowMs) return 'busy:installed';
    if (nowAbs - state.last < windowMs) return 'busy:changed:' + (state.why || '');
    return '';
};
"""
)

# Whole-page probe from the top document: walks same-origin child frames
# directly. Returns {verdict, windows, last}; verdict 'perframe' asks the
# caller to evaluate each frame itself (cross-origin child, missing state).
TREE_PROBE_JS = (
    r"""(windowMs) => {
    if (!window.__oaflowQuiet) {"""
    + QUIESCENCE_INSTALL_JS
    + r"""
        return { verdict: 'busy:installed', windows: 1, last: 0 };
    }
"""
    + _JUDGE_DOC_JS
    + r"""
    const nowAbs = performance.timeOrigin + performance.now();
    let windows = 0;
    let last = 0;
    let busy = '';
    // No document can be quiet before its last change (or its observer's
    // installation) is a full window old: the earliest a later probe can
    // answer quiet, so the caller need not look sooner.
    let ready = 0;
    const walk = (win) => {
        let doc;
        try { doc = win.document; void doc.readyState; } catch (e) { return 'perframe'; }
        windows += 1;
        const v = judgeDoc(win, windowMs, nowAbs);
        if (v === 'missing') return 'perframe';
        if (v === 'unavailable') return 'unavailable';
        if (v && !busy) busy = v;
        last = Math.max(last, win.__oaflowQuiet.last);
        ready = Math.max(ready, win.__oaflowQuiet.last, win.__oaflowQuiet.installed);
        for (const el of doc.querySelectorAll('iframe, frame')) {
            if (!el.contentWindow) continue;
            const r = walk(el.contentWindow);
            if (r) return r;
        }
        return '';
    };
    const r = walk(window);
    if (r) return { verdict: r, windows, last };
    if (busy) return { verdict: busy, windows, last, wait: ready + windowMs - nowAbs };
    const ticks = window.__oaflowQuiet.ticks || [0, 0];
    // Two rendered top-level frames after the last change in any frame.
    if (window === window.top && !(ticks[0] > last)) {
        // A page that renders no frames at all can never pass this check:
        // a hidden document, or a headed window the OS reports as covered
        // (macOS stops frames for a window behind another app's window).
        // Everything else is quiet, so let the screenshot settle decide.
        if (paintStalled(window.__oaflowQuiet, nowAbs)) {
            return { verdict: 'unavailable', windows, last };
        }
        return { verdict: 'busy:paint', windows, last };
    }
    return { verdict: 'quiet', windows, last };
}"""
)

# Per-frame fallback probe (used when a child frame is cross-origin or not
# yet instrumented): installs if needed, then judges only this document.
FRAME_PROBE_JS = (
    r"""(windowMs) => {
    if (!window.__oaflowQuiet) {"""
    + QUIESCENCE_INSTALL_JS
    + r"""
        return { verdict: 'busy:installed', last: 0 };
    }
"""
    + _JUDGE_DOC_JS
    + r"""
    const nowAbs = performance.timeOrigin + performance.now();
    const v = judgeDoc(window, windowMs, nowAbs);
    return { verdict: v || 'quiet', last: window.__oaflowQuiet.last };
}"""
)

TOP_TICKS_JS = (
    r"""() => {
    const state = window.__oaflowQuiet;
    if (!state || !state.ticks) return null;
    const nowAbs = performance.timeOrigin + performance.now();
    const ticks = state.ticks;
    const since = ticks[1] > 0 ? ticks[1] : state.installed;
    const stalled = document.visibilityState === 'hidden' ||
        nowAbs - since > """
    + str(PAINT_STALL_MS)
    + r""";
    return { ticks, stalled };
}"""
)


class PageQuiescence:
    """Network + DOM quiescence tracker bound to one Playwright page."""

    def __init__(self, page: Any, *, root_frame: Any = None) -> None:
        self._page = page
        self._root_frame = root_frame
        self._requests: dict[int, Any] = {}
        self._inflight: set[int] = set()
        self._last_network = time.monotonic()
        self._armed = False
        self.last_reason = ""
        self._frame_last = 0.0
        # Identity of the last change seen by a quiet probe: the latest change
        # time over all frames, the last network activity and the frame count.
        # Equal markers from two quiet probes mean nothing changed in between.
        self.marker: Optional[str] = None
        # After a busy answer: seconds before which no probe can answer
        # quiet (the latest change, in any frame or on the network, is not
        # yet a full window old). 0.0 when unknown.
        self.retry_after_s = 0.0
        try:
            page.add_init_script(QUIESCENCE_INSTALL_JS)
            page.on("request", self._on_request)
            page.on("requestfinished", self._on_done)
            page.on("requestfailed", self._on_done)
            self._armed = True
        except Exception:
            self._armed = False

    def set_root_frame(self, frame: Any) -> None:
        """Change the observed subtree without losing pending Page requests."""
        if frame == self._root_frame:
            return
        self._root_frame = frame
        self._inflight = {
            key
            for key, request in self._requests.items()
            if self._request_in_scope(request)
        }
        self._last_network = time.monotonic()
        self.marker = None

    def detach(self) -> None:
        """Release the Page event subscriptions owned by this tracker."""
        for event, callback in (
            ("request", self._on_request),
            ("requestfinished", self._on_done),
            ("requestfailed", self._on_done),
        ):
            self._page.remove_listener(event, callback)
        self._armed = False

    def _frame_in_scope(self, frame: Any) -> bool:
        if self._root_frame is None:
            return True
        while frame is not None:
            if frame == self._root_frame:
                return True
            frame = frame.parent_frame
        return False

    def _request_in_scope(self, request: Any) -> bool:
        if self._root_frame is None:
            return True
        try:
            return self._frame_in_scope(request.frame)
        except Exception:
            return False

    def _on_request(self, request: Any) -> None:
        self._requests[id(request)] = request
        if self._request_in_scope(request):
            self._inflight.add(id(request))
            self._last_network = time.monotonic()

    def _on_done(self, request: Any) -> None:
        self._requests.pop(id(request), None)
        if id(request) in self._inflight or self._request_in_scope(request):
            self._inflight.discard(id(request))
            self._last_network = time.monotonic()

    def _live_frames(self) -> Optional[list[Any]]:
        try:
            if self._root_frame is not None and self._root_frame.is_detached():
                return None
            return [
                f
                for f in self._page.frames
                if not f.is_detached() and self._frame_in_scope(f)
            ]
        except Exception:
            return None

    def _per_frame(self, frames: list[Any], window_ms: float) -> Optional[str]:
        busy = ""
        last = 0.0
        for frame in frames:
            try:
                out = frame.evaluate(FRAME_PROBE_JS, window_ms)
            except Exception:
                return None
            verdict = out.get("verdict") if isinstance(out, dict) else None
            if verdict in (None, "unavailable", "missing"):
                return None
            if verdict != "quiet" and not busy:
                busy = str(verdict)
            last = max(last, float(out.get("last") or 0.0))
        if busy:
            return busy
        try:
            top = self._page.evaluate(TOP_TICKS_JS)
        except Exception:
            return None
        ticks = top.get("ticks") if isinstance(top, dict) else None
        if not ticks or not float(ticks[0]) > last:
            # No frames rendered at all (hidden or covered window): paint
            # can't be observed, so the screenshot settle decides.
            if isinstance(top, dict) and top.get("stalled"):
                return None
            return "busy:paint"
        self._frame_last = last
        return "quiet"

    def probe(self, window_s: float) -> Optional[bool]:
        """One quiescence observation (see module docstring)."""
        if not self._armed:
            return None
        window_ms = max(1.0, float(window_s) * 1000.0)
        frames = self._live_frames()
        if not frames:
            return None
        self.retry_after_s = 0.0
        try:
            target = self._root_frame if self._root_frame is not None else self._page
            out = target.evaluate(TREE_PROBE_JS, window_ms)
        except Exception:
            return None
        verdict = out.get("verdict") if isinstance(out, dict) else None
        try:
            page_wait_s = max(0.0, float(out.get("wait") or 0.0) / 1000.0)
        except (AttributeError, TypeError, ValueError):
            page_wait_s = 0.0
        if verdict == "unavailable" or verdict is None:
            self.last_reason = "unavailable"
            return None
        if verdict == "quiet" and int(out.get("windows") or 0) != len(frames):
            # A frame the tree walk did not reach (e.g. a frame Playwright
            # knows about that the DOM walk cannot see): judge frame by frame.
            verdict = "perframe"
        self._frame_last = float(out.get("last") or 0.0)
        if verdict == "perframe":
            verdict = self._per_frame(frames, window_ms)
            if verdict is None:
                self.last_reason = "unavailable"
                return None
        if verdict == "quiet" and self._root_frame is not None:
            try:
                top = self._page.evaluate(TOP_TICKS_JS)
            except Exception:
                return None
            ticks = top.get("ticks") if isinstance(top, dict) else None
            if not ticks or not float(ticks[0]) > self._frame_last:
                # As in _per_frame: a top window that renders no frames
                # (hidden or covered) cannot show paint, so the screenshot
                # settle decides.
                if isinstance(top, dict) and top.get("stalled"):
                    self.last_reason = "unavailable"
                    return None
                verdict = "busy:paint"
        self.last_reason = str(verdict)
        if verdict != "quiet":
            network_wait_s = window_s - (time.monotonic() - self._last_network)
            # Never more than one window: a busy answer means the latest
            # change is younger than that.
            self.retry_after_s = min(
                max(page_wait_s, network_wait_s, 0.0), float(window_s)
            )
            return False
        # Network events are delivered while the evaluate calls above run.
        if self._inflight:
            self.last_reason = "busy:network"
            return False
        if time.monotonic() - self._last_network < window_s:
            self.last_reason = "busy:network-recent"
            self.retry_after_s = max(
                0.0, window_s - (time.monotonic() - self._last_network)
            )
            return False
        self.marker = (
            f"page:{self._frame_last:.3f}:{self._last_network:.6f}:{len(frames)}"
        )
        return True
