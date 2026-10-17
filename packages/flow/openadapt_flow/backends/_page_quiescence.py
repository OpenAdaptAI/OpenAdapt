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
_JUDGE_DOC_JS = r"""

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
    const walk = (win) => {
        let doc;
        try { doc = win.document; void doc.readyState; } catch (e) { return 'perframe'; }
        windows += 1;
        const v = judgeDoc(win, windowMs, nowAbs);
        if (v === 'missing') return 'perframe';
        if (v === 'unavailable') return 'unavailable';
        if (v && !busy) busy = v;
        last = Math.max(last, win.__oaflowQuiet.last);
        for (const el of doc.querySelectorAll('iframe, frame')) {
            if (!el.contentWindow) continue;
            const r = walk(el.contentWindow);
            if (r) return r;
        }
        return '';
    };
    const r = walk(window);
    if (r) return { verdict: r, windows, last };
    if (busy) return { verdict: busy, windows, last };
    const ticks = window.__oaflowQuiet.ticks || [0, 0];
    // Two rendered top-level frames after the last change in any frame.
    if (!(ticks[0] > last)) return { verdict: 'busy:paint', windows, last };
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

TOP_TICKS_JS = r"""() => (window.__oaflowQuiet && window.__oaflowQuiet.ticks) || null"""


class PageQuiescence:
    """Network + DOM quiescence tracker bound to one Playwright page."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._inflight: set[int] = set()
        self._last_network = time.monotonic()
        self._armed = False
        self.last_reason = ""
        self._frame_last = 0.0
        # Identity of the last change seen by a quiet probe: the latest change
        # time over all frames, the last network activity and the frame count.
        # Equal markers from two quiet probes mean nothing changed in between.
        self.marker: Optional[str] = None
        try:
            page.add_init_script(QUIESCENCE_INSTALL_JS)
            page.on("request", self._on_request)
            page.on("requestfinished", self._on_done)
            page.on("requestfailed", self._on_done)
            self._armed = True
        except Exception:
            self._armed = False

    def _on_request(self, request: Any) -> None:
        self._inflight.add(id(request))
        self._last_network = time.monotonic()

    def _on_done(self, request: Any) -> None:
        self._inflight.discard(id(request))
        self._last_network = time.monotonic()

    def _live_frames(self) -> Optional[list[Any]]:
        try:
            return [f for f in self._page.frames if not f.is_detached()]
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
            ticks = self._page.evaluate(TOP_TICKS_JS)
        except Exception:
            return None
        if not ticks or not float(ticks[0]) > last:
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
        try:
            out = self._page.evaluate(TREE_PROBE_JS, window_ms)
        except Exception:
            return None
        verdict = out.get("verdict") if isinstance(out, dict) else None
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
        self.last_reason = str(verdict)
        if verdict != "quiet":
            return False
        # Network events are delivered while the evaluate calls above run.
        if self._inflight:
            self.last_reason = "busy:network"
            return False
        if time.monotonic() - self._last_network < window_s:
            self.last_reason = "busy:network-recent"
            return False
        self.marker = (
            f"page:{self._frame_last:.3f}:{self._last_network:.6f}:{len(frames)}"
        )
        return True
