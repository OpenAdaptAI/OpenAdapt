"""Playwright-driven reference backend (sync API, chromium, headless-capable).

Implements the `openadapt_flow.backend.Backend` protocol against a Playwright
`Page`: full-viewport PNG screenshots, mouse clicks at pixel coordinates,
keyboard typing, and key/chord presses. Viewport is fixed at 1280x800 with
deviceScaleFactor=1 so CSS pixels equal screenshot pixels.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    StructuralHandle,
    StructuralLocator,
)

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

_TOKEN_ATTRIBUTE_PREFIX = "data-openadapt-actuation-"

# The descriptor stays inside the page-local guard store. It binds the exact
# actionable node, its ancestry, and the enclosing record row while excluding
# the target's own cell (the same identity boundary as ``structured_text_at``).
_DESCRIBE_TARGET_JS = r"""(node) => {
    const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const row = node.closest('tr, [role="row"], li, [role="listitem"]');
    const own = node.closest(
        'td, th, [role="cell"], [role="gridcell"]'
    ) || node;
    let rowIdentity = '';
    if (row) {
        const path = [];
        let cursor = own;
        while (cursor && cursor !== row) {
            const parent = cursor.parentElement;
            if (!parent) break;
            path.unshift(Array.prototype.indexOf.call(parent.children, cursor));
            cursor = parent;
        }
        const clone = row.cloneNode(true);
        if (cursor === row && own !== row) {
            let cloneOwn = clone;
            for (const index of path) {
                cloneOwn = cloneOwn && cloneOwn.children[index];
            }
            if (cloneOwn) cloneOwn.remove();
        }
        rowIdentity = clean(
            (row.getAttribute('aria-label') || '') + ' ' +
            (clone.textContent || '')
        );
    }
    const ancestry = [];
    let cursor = node;
    for (let depth = 0; cursor && depth < 8; depth += 1) {
        const parent = cursor.parentElement;
        ancestry.push([
            cursor.tagName.toLowerCase(),
            cursor.id || '',
            cursor.getAttribute('role') || '',
            parent
                ? Array.prototype.indexOf.call(parent.children, cursor)
                : -1,
        ]);
        if (cursor === row) break;
        cursor = parent;
    }
    return {
        descriptor: JSON.stringify([
            1,
            [
                node.tagName.toLowerCase(),
                node.id || '',
                node.getAttribute('role') || '',
                node.getAttribute('aria-label') || '',
                node.getAttribute('name') || '',
                node.getAttribute('type') || '',
                clean(node.textContent).slice(0, 256),
            ],
            ancestry,
            rowIdentity,
        ]),
        rowIdentity: rowIdentity,
        row: row,
    };
}"""

_INSTALL_GUARD_BODY_JS = r"""
    const observed = describe(el);
    if (args.requireRowIdentity && !observed.rowIdentity) return null;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (!el.isConnected || rect.width <= 0 || rect.height <= 0 ||
            style.visibility === 'hidden' || style.display === 'none' ||
            Number(style.opacity || '1') === 0) {
        return null;
    }
    const cx = Math.round(rect.x + rect.width / 2);
    const cy = Math.round(rect.y + rect.height / 2);
    const ax = Number.isFinite(args.x) ? Math.round(args.x) : cx;
    const ay = Number.isFinite(args.y) ? Math.round(args.y) : cy;
    if (ax < 0 || ay < 0 || ax >= window.innerWidth ||
            ay >= window.innerHeight) {
        return null;
    }
    const top = document.elementFromPoint(ax, ay);
    if (!top || !(top === el || el.contains(top))) return null;
    let tokenMap = window[args.storeKey];
    if (!(tokenMap instanceof Map)) {
        tokenMap = new Map();
        Object.defineProperty(window, args.storeKey, {
            value: tokenMap,
            configurable: true,
        });
    }
    el.setAttribute(args.tokenAttribute, args.token);
    const entry = {
        el: el,
        descriptor: observed.descriptor,
        observer: null,
    };
    const invalidate = () => {
        if (entry.observer) entry.observer.disconnect();
        for (const candidate of document.querySelectorAll(
                '[' + args.tokenAttribute + ']')) {
            if (candidate.getAttribute(args.tokenAttribute) === args.token) {
                candidate.removeAttribute(args.tokenAttribute);
            }
        }
        tokenMap.delete(args.token);
    };
    entry.observer = new MutationObserver(() => {
        if (!el.isConnected || describe(el).descriptor !== entry.descriptor) {
            invalidate();
        }
    });
    entry.observer.observe(observed.row || el, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
    });
    tokenMap.set(args.token, entry);
    return {
        point: [ax, ay],
        offset: [ax - rect.x, ay - rect.y],
        region: [
            Math.round(rect.x),
            Math.round(rect.y),
            Math.round(rect.width),
            Math.round(rect.height),
        ],
        identity: observed.rowIdentity,
    };
"""

_BIND_STRUCTURAL_TARGET_JS = (
    "(el, args) => { const describe = "
    + _DESCRIBE_TARGET_JS
    + ";"
    + _INSTALL_GUARD_BODY_JS
    + "}"
)

_BIND_COORDINATE_TARGET_JS = (
    r"""(args) => {
    const hit = document.elementFromPoint(args.x, args.y);
    if (!hit) return null;
    const el = hit.closest(
        'button, a[href], input[type="button"], input[type="submit"],' +
        ' input[type="reset"], input[type="checkbox"], input[type="radio"],' +
        ' select,' +
        ' [role="button"], [role="link"], [role="menuitem"],' +
        ' [role="tab"], [role="option"], [role="checkbox"],' +
        ' [role="radio"], [role="switch"]'
    );
    // Canvas/maps, sliders/ranges, text-editing caret positions, and generic
    // onclick regions are coordinate-semantic. They cannot be upgraded into
    // an element-level identity-bound click and must remain refused.
    if (!el || el.matches('canvas, input[type="range"], [role="slider"]')) {
        return null;
    }
    const describe = """
    + _DESCRIBE_TARGET_JS
    + ";"
    + _INSTALL_GUARD_BODY_JS
    + "}"
)

_GUARD_CURRENT_JS = (
    "(el, args) => { const describe = "
    + _DESCRIBE_TARGET_JS
    + r""";
    const tokenMap = window[args.storeKey];
    const entry = tokenMap instanceof Map ? tokenMap.get(args.token) : null;
    return Boolean(
        entry && entry.el === el &&
        el.getAttribute(args.tokenAttribute) === args.token &&
        entry.descriptor === describe(el).descriptor
    );
}"""
)

_CLEAN_GUARD_JS = r"""(args) => {
    const tokenMap = window[args.storeKey];
    const entry = tokenMap instanceof Map ? tokenMap.get(args.token) : null;
    if (entry && entry.observer) entry.observer.disconnect();
    for (const candidate of document.querySelectorAll(
            '[' + args.tokenAttribute + ']')) {
        if (candidate.getAttribute(args.tokenAttribute) === args.token) {
            candidate.removeAttribute(args.tokenAttribute);
        }
    }
    if (tokenMap instanceof Map) tokenMap.delete(args.token);
}"""


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
        # Opaque per-backend key keeps the WeakMap private from ordinary page
        # code. Python retains only token material keyed by the public
        # SHA-256 fingerprint; target/row text stays page-local and ephemeral.
        self._structural_store_key = f"__oaflow_structural_{uuid.uuid4().hex}"
        self._structural_tokens: dict[str, str] = {}

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
        ``name``. Requires a UNIQUE, on-screen, UNOCCLUDED match. A missing,
        off-viewport, or covered element returns None; ambiguity is an explicit
        structural refusal and cannot fall through to a weaker pixel match.
        The returned handle binds an opaque one-shot token to the exact Element
        and its enclosing-row identity for same-operation delivery.
        """
        try:
            loc = self._locator(locator)
            if loc is None:
                return None
            candidate_count = loc.count()
            if candidate_count == 0:
                return None
            if candidate_count != 1:
                raise StructuralResolutionRefused(
                    f"DOM locator is ambiguous: candidate_count={candidate_count}"
                )
            token = uuid.uuid4().hex
            observed = loc.evaluate(
                _BIND_STRUCTURAL_TARGET_JS,
                {
                    "storeKey": self._structural_store_key,
                    "tokenAttribute": self._token_attribute(token),
                    "token": token,
                    "requireRowIdentity": False,
                },
            )
            if not isinstance(observed, dict):
                return None
            point = observed.get("point")
            region = observed.get("region")
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(isinstance(value, int) for value in point)
                or not isinstance(region, list)
                or len(region) != 4
                or not all(isinstance(value, int) for value in region)
            ):
                return None
            fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest()
            # Bound Python-side token retention. Page-side entries are weak and
            # disappear with their DOM nodes; an evicted token is unusable.
            if len(self._structural_tokens) >= 128:
                evicted = self._structural_tokens.pop(
                    next(iter(self._structural_tokens))
                )
                self._cleanup_guard(evicted)
            self._structural_tokens[fingerprint] = token
            return StructuralHandle(
                point=(point[0], point[1]),
                region=(region[0], region[1], region[2], region[3]),
                target_fingerprint=fingerprint,
                supported_operations=["dom_click", "dom_double_click"],
            )
        except StructuralResolutionRefused:
            raise
        except Exception:
            return None

    def _locator(self, locator: StructuralLocator) -> Any:
        if locator.selector:
            return self.page.locator(locator.selector)
        if locator.role and locator.name:
            return self.page.get_by_role(locator.role, name=locator.name, exact=True)
        return None

    @staticmethod
    def _token_attribute(token: str) -> str:
        return f"{_TOKEN_ATTRIBUTE_PREFIX}{token}"

    def _cleanup_guard(self, token: str) -> None:
        try:
            self.page.evaluate(
                _CLEAN_GUARD_JS,
                {
                    "storeKey": self._structural_store_key,
                    "tokenAttribute": self._token_attribute(token),
                    "token": token,
                },
            )
        except Exception:
            # Navigation destroys the page-local store and is cleanup itself.
            pass

    def _token_locator(self, token: str) -> Any:
        return self.page.locator(f"[{self._token_attribute(token)}]")

    def _guard_is_current(self, locator: Any, token: str) -> bool:
        try:
            return bool(
                locator.evaluate(
                    _GUARD_CURRENT_JS,
                    {
                        "storeKey": self._structural_store_key,
                        "tokenAttribute": self._token_attribute(token),
                        "token": token,
                    },
                )
            )
        except Exception:
            return False

    def act_structural(
        self,
        locator: StructuralLocator,
        handle: StructuralHandle,
        *,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        """Atomically verify and click the exact DOM target resolved earlier.

        A strict locator must still resolve to the token-bound Element and its
        unchanged target/row descriptor. A short-lived MutationObserver removes
        the token on intervening identity mutation. The final action is a
        Playwright click/dblclick on that unique random-token locator, preserving
        Playwright's native pointer sequence and actionability checks rather
        than synthesizing DOM events. A replacement element or changed record
        row is a refusal, never a coordinate fallback.
        """

        fingerprint = handle.target_fingerprint
        if not fingerprint:
            raise StructuralResolutionRefused(
                "guarded DOM actuation requires a target fingerprint"
            )
        token = self._structural_tokens.pop(fingerprint, None)
        if token is None:
            raise StructuralResolutionRefused(
                "guarded DOM actuation token is missing, stale, or already consumed"
            )
        loc = self._locator(locator)
        if loc is None:
            self._cleanup_guard(token)
            raise StructuralResolutionRefused(
                "guarded DOM actuation requires an exact structural locator"
            )
        try:
            if loc.count() != 1 or not self._guard_is_current(loc, token):
                raise StructuralResolutionRefused(
                    "guarded DOM target or record identity changed before delivery"
                )
            token_locator = self._token_locator(token)
            if token_locator.count() != 1:
                raise StructuralResolutionRefused(
                    "guarded DOM token is missing or ambiguous at delivery"
                )
            if double:
                token_locator.dblclick(timeout=1000)
            else:
                token_locator.click(timeout=1000)
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "guarded DOM target changed or became unactionable before delivery"
            ) from exc
        finally:
            self._cleanup_guard(token)
        return ActionDeliveryReceipt(
            receipt_id=f"playwright-{uuid.uuid4().hex}",
            operation="dom_double_click" if double else "dom_click",
            native=False,
            target_fingerprint=fingerprint,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        """Click a visually resolved DOM target without a coordinate race.

        This method binds the exact actionable element and row descriptor at
        the verified point, then confirms the current frame still matches the
        identity-verified frame. A MutationObserver guards the short interval
        between that frame check and Playwright's real pointer delivery.
        Canvas or otherwise opaque targets cannot provide this binding and are
        refused.
        """

        point = (int(x), int(y))
        token = uuid.uuid4().hex
        try:
            observed = self.page.evaluate(
                _BIND_COORDINATE_TARGET_JS,
                {
                    "storeKey": self._structural_store_key,
                    "tokenAttribute": self._token_attribute(token),
                    "token": token,
                    "x": point[0],
                    "y": point[1],
                    "requireRowIdentity": True,
                },
            )
            if not isinstance(observed, dict):
                raise StructuralResolutionRefused(
                    "visual point is not an identity-bearing actionable DOM element"
                )
            offset = observed.get("offset")
            if (
                not isinstance(offset, list)
                or len(offset) != 2
                or not all(isinstance(value, (int, float)) for value in offset)
            ):
                raise StructuralResolutionRefused(
                    "visual DOM actuation could not bind the resolved point"
                )
            if hashlib.sha256(self.screenshot()).hexdigest() != expected_frame_sha256:
                raise StructuralResolutionRefused(
                    "visual frame changed after identity verification"
                )
            token_locator = self._token_locator(token)
            if token_locator.count() != 1 or not self._guard_is_current(
                token_locator, token
            ):
                raise StructuralResolutionRefused(
                    "visual target or record identity changed before delivery"
                )
            position = {"x": offset[0], "y": offset[1]}
            if double:
                token_locator.dblclick(position=position, timeout=1000)
            else:
                token_locator.click(position=position, timeout=1000)
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "identity-bound visual target became unactionable before delivery"
            ) from exc
        finally:
            self._cleanup_guard(token)
        fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest()
        return ActionDeliveryReceipt(
            receipt_id=f"playwright-coordinate-{uuid.uuid4().hex}",
            operation=(
                "guarded_coordinate_double_click"
                if double
                else "guarded_coordinate_click"
            ),
            native=False,
            target_fingerprint=fingerprint,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

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
