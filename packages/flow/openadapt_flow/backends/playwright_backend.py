"""Playwright-driven reference backend (sync API, chromium, headless-capable).

Implements the `openadapt_flow.backend.Backend` protocol against a Playwright
`Page`: full-viewport PNG screenshots, mouse clicks at pixel coordinates,
keyboard typing, and key/chord presses. Viewport is fixed at 1280x800 with
deviceScaleFactor=1 so CSS pixels equal screenshot pixels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

from openadapt_flow.ir import StructuralHandle, StructuralLocator

VIEWPORT: tuple[int, int] = (1280, 800)

_MODIFIER_ALIASES = {
    "meta": "Meta",
    "cmd": "Meta",
    "command": "Meta",
    "ctrl": "Control",
    "control": "Control",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
}

_NAMED_KEYS = {
    "enter": "Enter",
    "return": "Enter",
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
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
}


def _normalize_chord(key: str) -> str:
    """Normalize a key or chord like ``'Meta+a'`` to Playwright's format.

    Modifier aliases (``ctrl``, ``cmd``, ...) are canonicalized; common named
    keys are case-corrected; single characters pass through unchanged.

    Args:
        key: Key name or ``+``-joined chord (e.g. ``'Enter'``, ``'Meta+a'``).

    Returns:
        The Playwright-compatible key/chord string.
    """
    parts = [p for p in key.split("+") if p]
    normalized: list[str] = []
    for part in parts:
        lower = part.lower()
        if lower in _MODIFIER_ALIASES:
            normalized.append(_MODIFIER_ALIASES[lower])
        elif lower in _NAMED_KEYS:
            normalized.append(_NAMED_KEYS[lower])
        else:
            normalized.append(part)
    return "+".join(normalized)


class PlaywrightBackend:
    """`Backend` implementation over a Playwright sync-API `Page`.

    Attributes:
        page: The underlying Playwright page (public so record-time helpers
            such as the demo driver may use locators; replay never does).
    """

    def __init__(self, page: "Page") -> None:
        """Wrap an existing Playwright page.

        Args:
            page: A page created with viewport 1280x800, deviceScaleFactor=1.
        """
        self.page = page

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the page viewport in pixels."""
        size = self.page.viewport_size
        if size is None:  # pragma: no cover - viewport always set by launch()
            return VIEWPORT
        return (size["width"], size["height"])

    # -- structural observations (openadapt_flow.backend.StructuralBackend) --

    @property
    def url(self) -> Optional[str]:
        """Current page URL, or None if momentarily unobservable."""
        try:
            return self.page.url
        except Exception:
            return None

    @property
    def page_title(self) -> Optional[str]:
        """Current page title, or None if momentarily unobservable."""
        try:
            return self.page.title()
        except Exception:
            return None

    @property
    def page_count(self) -> Optional[int]:
        """Open pages in the browser context (new tabs are visible here even
        though the single-page screenshot never shows them)."""
        try:
            return len(self.page.context.pages)
        except Exception:
            return None

    # -- structured-text identity (openadapt_flow.backend.IdentityBackend) --

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        """Return the DOM text of the element/row under viewport pixel (x, y).

        Identity in this stack is verified against STRUCTURED text where the
        backend can provide it (see :class:`IdentityBackend`): the browser
        hands back the REAL characters of the row under the point -- a genuine
        digit ``0`` vs a letter ``O`` -- so the same-name/same-DOB
        glyph-collapse that defeats OCR (``MG4408`` vs ``MG44O8`` reading
        identically) simply cannot occur here; the two rows are different
        strings in the DOM.

        The point is in the same coordinate space as :meth:`click` (viewport
        CSS pixels at deviceScaleFactor=1). ``document.elementFromPoint`` finds
        the node under the point; we require an enclosing ROW-LIKE container
        (``tr`` / ``[role=row]`` / ``li`` / ``[role=listitem]``) so identity is
        judged on the whole record row (MRN + name + DOB + ...), not a single
        cell, and return its ``aria-label`` (when present) joined with the row's text
        EXCLUDING the clicked target's own cell/subtree -- that cell's label is
        the mutable evidence the ladder heals through (an Open->View relabel of
        the clicked control must not change identity), mirroring the OCR band
        excluding the target's own crop; identity rests on the row's OTHER
        cells (MRN, name, DOB, ...). A point with NO row-like ancestor -- a
        standalone control whose own text is a mutable, healable label --
        returns None (identity for such controls stays on the OCR / heal path).
        Whitespace is collapsed. Returns None when nothing is under the point
        or on any evaluation failure (never raises) -- the identity ladder then
        falls back to the OCR tier.
        """
        try:
            result = self.page.evaluate(
                """([px, py]) => {
                    const el = document.elementFromPoint(px, py);
                    if (!el) return null;
                    // Identity is a REPEATED-STRUCTURE (record-list) concept:
                    // only a genuine row-like container carries it. A
                    // standalone control (a Save button) has no row ancestor;
                    // its own text is a MUTABLE label the resolution ladder
                    // heals through, so we return null and leave it to the OCR
                    // / heal path -- mirroring the OCR band excluding the
                    // target's own label.
                    const row = el.closest(
                        'tr, [role="row"], li, [role="listitem"]'
                    );
                    if (!row) return null;
                    // Exclude the CLICKED target's own cell/subtree: its label
                    // is the mutable evidence the ladder heals through (an
                    // Open->View relabel of the clicked control must NOT change
                    // identity), mirroring the OCR band excluding the target's
                    // own crop. Identity rests on the row's OTHER cells.
                    const own = el.closest(
                        'td, th, [role="cell"], [role="gridcell"]'
                    ) || el;
                    own.setAttribute('data-oaflow-own', '1');
                    let body = '';
                    try {
                        const clone = row.cloneNode(true);
                        const marked = clone.querySelector(
                            '[data-oaflow-own="1"]'
                        );
                        if (marked) marked.remove();
                        body = clone.textContent || '';
                    } finally {
                        own.removeAttribute('data-oaflow-own');
                    }
                    const parts = [];
                    const aria = row.getAttribute
                        ? row.getAttribute('aria-label') : null;
                    if (aria) parts.push(aria);
                    if (body) parts.push(body);
                    const joined = parts.join(' ')
                        .replace(/\\s+/g, ' ').trim();
                    return joined || null;
                }""",
                [int(x), int(y)],
            )
        except Exception:
            return None
        return result or None

    def text_value_at(self, x: int, y: int) -> Optional[str]:
        """Return the exact value of the editable control under ``(x, y)``.

        This is an optional structural observation used only to verify that a
        TYPE action landed. It never appears in a report or event log. A point
        over a non-editable control, an inaccessible custom widget, or an
        evaluation error returns ``None`` so the runtime falls back to its
        visual verifier.
        """
        try:
            result = self.page.evaluate(
                """([px, py]) => {
                    const hit = document.elementFromPoint(px, py);
                    if (!hit) return null;
                    const el = hit.closest(
                        'input, textarea, [contenteditable="true"],' +
                        ' [role="textbox"]'
                    );
                    if (!el) return null;
                    if ('value' in el && typeof el.value === 'string') {
                        return el.value;
                    }
                    if (el.isContentEditable ||
                            el.getAttribute('role') === 'textbox') {
                        return el.textContent || '';
                    }
                    return null;
                }""",
                [int(x), int(y)],
            )
        except Exception:
            return None
        return result if isinstance(result, str) else None

    def focused_text_value(self) -> Optional[str]:
        """Return the exact value of the currently focused editable control."""
        try:
            result = self.page.evaluate(
                """() => {
                    const el = document.activeElement;
                    if (!el) return null;
                    const editable = el.matches(
                        'input, textarea, [contenteditable="true"],' +
                        ' [role="textbox"]'
                    ) ? el : null;
                    if (!editable) return null;
                    if ('value' in editable &&
                            typeof editable.value === 'string') {
                        return editable.value;
                    }
                    return editable.textContent || '';
                }"""
            )
        except Exception:
            return None
        return result if isinstance(result, str) else None

    # -- structural action (openadapt_flow.backend.StructuralActionBackend) --

    def structural_locator_at(self, x: int, y: int) -> Optional[StructuralLocator]:
        """Return a stable DOM locator for the element under (x, y).

        Walks from ``document.elementFromPoint`` to the nearest ACTIONABLE
        element (the control a user clicks) and derives a stable identity for
        it: a unique ``#id`` selector when available, else the element's ARIA
        ``role`` + accessible ``name``. Returns None when neither a unique id
        nor a role+name can be formed (the step then relies on the visual
        anchor). Coordinate space matches :meth:`click`.
        """
        try:
            result = self.page.evaluate(
                """([px, py]) => {
                    const el = document.elementFromPoint(px, py);
                    if (!el) return null;
                    const actionable = el.closest(
                        'button, a[href], input, select, textarea,' +
                        ' [role="button"], [role="link"], [role="menuitem"],' +
                        ' [role="tab"], [role="option"], [onclick], [data-id]'
                    ) || el;
                    const tag = actionable.tagName.toLowerCase();
                    let selector = null;
                    const id = actionable.id;
                    if (id && document.querySelectorAll(
                            '#' + CSS.escape(id)).length === 1) {
                        selector = '#' + CSS.escape(id);
                    }
                    let role = actionable.getAttribute('role');
                    if (!role) {
                        const map = {button: 'button', a: 'link',
                            input: 'textbox', select: 'combobox',
                            textarea: 'textbox'};
                        role = map[tag] || null;
                        if (tag === 'a' &&
                                !actionable.getAttribute('href')) role = null;
                    }
                    let name = actionable.getAttribute('aria-label');
                    if (!name) {
                        const t = (actionable.textContent || '')
                            .replace(/\\s+/g, ' ').trim();
                        name = t ? t.slice(0, 120) : null;
                    }
                    if (!selector && !(role && name)) return null;
                    return {selector: selector, role: role, name: name};
                }""",
                [int(x), int(y)],
            )
        except Exception:
            return None
        if not result:
            return None
        return StructuralLocator(
            selector=result.get("selector"),
            role=result.get("role"),
            name=result.get("name"),
        )

    def locate_structural(
        self, locator: StructuralLocator
    ) -> Optional[StructuralHandle]:
        """Locate ``locator``'s element in the live DOM; return its center.

        Resolves by the recorded ``selector`` first, else by ``role`` +
        ``name``. Requires a UNIQUE, on-screen, UNOCCLUDED match: a missing,
        ambiguous, off-viewport, or COVERED element (a modal / click-shield over
        it -- the hit test at its center returns another node) returns None so
        the resolver falls through to the visual ladder (and, for off-screen,
        its scroll-and-retry; for an opaque cover, a safe halt). The returned
        point is the element's center in :meth:`click` coordinate space, so the
        pre-click identity gate re-reads the SAME element there.
        """
        try:
            loc = None
            if locator.selector:
                loc = self.page.locator(locator.selector)
            elif locator.role and locator.name:
                loc = self.page.get_by_role(locator.role, name=locator.name, exact=True)
            if loc is None:
                return None
            if loc.count() != 1:
                return None
            box = loc.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                return None
            cx = int(round(box["x"] + box["width"] / 2))
            cy = int(round(box["y"] + box["height"] / 2))
            vw, vh = self.viewport
            if not (0 <= cx < vw and 0 <= cy < vh):
                return None
            # OCCLUSION / actionability gate: a stable DOM identity is NOT a
            # licence to click something the user could not. If the element is
            # covered (a modal, an opaque or transparent click-shield), the hit
            # test at its center returns some OTHER node, and we return None so
            # resolution falls through to the visual ladder (which also fails on
            # an opaque cover -> safe halt, no click). Only when the element --
            # or a descendant of it -- is the topmost node at the action point
            # do we resolve. This preserves the occlusion-safe-halt invariant
            # (tests/e2e/test_chaos.py) that structural coordinates would
            # otherwise bypass.
            topmost = loc.evaluate(
                "(el, pt) => {"
                " const n = document.elementFromPoint(pt[0], pt[1]);"
                " return !!n && (n === el || el.contains(n));"
                "}",
                [cx, cy],
            )
            if not topmost:
                return None
            return StructuralHandle(point=(cx, cy))
        except Exception:
            return None

    def screenshot(self) -> bytes:
        """Return the current full-viewport frame as PNG bytes."""
        return self.page.screenshot(type="png", full_page=False)

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at pixel coordinates via the mouse."""
        if double:
            self.page.mouse.dblclick(x, y)
        else:
            self.page.mouse.click(x, y)

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""
        self.page.keyboard.type(text)

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'Meta+a'``."""
        self.page.keyboard.press(_normalize_chord(key))

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture at the current mouse position.

        The wheel event targets whatever element is under the pointer, so
        scrolling works inside iframes and nested scroll containers exactly
        as it does for a human — position the pointer first (a preceding
        click does this naturally during both record and replay).
        """
        self.page.mouse.wheel(dx, dy)

    @classmethod
    def launch(
        cls,
        url: str,
        headless: bool = True,
        *,
        record_video_dir: Optional[str] = None,
    ) -> tuple["PlaywrightBackend", Callable[[], None]]:
        """Start Playwright + chromium, open ``url``, and return a backend.

        Args:
            url: URL to navigate the new page to.
            headless: Whether to launch chromium headless.
            record_video_dir: OPT-IN. When set, the page is created inside a
                browser context that records a WebM video of the session into
                this directory (one file per page, Playwright-named). ``None``
                (default) records nothing and has zero effect on normal runs —
                the page is created directly on the browser as before. The
                finished video is only flushed to disk after ``close()`` (which
                closes the context); read its path from ``backend.page.video``.

        Returns:
            ``(backend, close)`` where ``close()`` shuts down the browser and
            the Playwright driver (flushing the video first, when recording).
        """
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import ensure_chromium_installed

        ensure_chromium_installed()
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=headless)
        except Exception:
            pw.stop()
            raise
        viewport = {"width": VIEWPORT[0], "height": VIEWPORT[1]}
        context = None
        if record_video_dir is not None:
            # Opt-in session video: the page must live in a context so
            # Playwright can attach the recorder; the video finalizes on
            # context.close().
            context = browser.new_context(
                viewport=viewport,
                device_scale_factor=1,
                record_video_dir=record_video_dir,
                record_video_size=viewport,
            )
            page = context.new_page()
        else:
            page = browser.new_page(viewport=viewport, device_scale_factor=1)
        page.goto(url)
        backend = cls(page)

        def close() -> None:
            try:
                if context is not None:
                    context.close()  # flush the recorded video to disk
                browser.close()
            finally:
                pw.stop()

        return backend, close
