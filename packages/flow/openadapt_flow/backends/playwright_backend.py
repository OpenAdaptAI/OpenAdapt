"""Playwright-driven reference backend (sync API, chromium, headless-capable).

Implements the `openadapt_flow.backend.Backend` protocol against a Playwright
`Page`: full-viewport PNG screenshots, mouse clicks at pixel coordinates,
keyboard typing, and key/chord presses. Viewport is fixed at 1280x800 with
deviceScaleFactor=1 so CSS pixels equal screenshot pixels.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

from openadapt_flow.backend import ActionDeliveryUncertain, StructuralResolutionRefused
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

# Explicit application-owned DOM contracts for live context identity. These
# markers belong in ``<head>`` so the backend never derives a workflow or
# session identity from arbitrary body text that may contain customer data:
#
#   <meta name="openadapt-session-identity" content="<64 lowercase hex>">
#   <meta name="openadapt-workflow-state" content="eligibility.review">
#
# The session marker is an opaque digest. The workflow marker is a bounded,
# lowercase machine token that the application author promises is PHI-free
# (state names such as ``eligibility.review``, never record values).
_SESSION_IDENTITY_META = "openadapt-session-identity"
_WORKFLOW_STATE_IDENTITY_META = "openadapt-workflow-state"
_SESSION_IDENTITY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WORKFLOW_STATE_IDENTITY_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9._:-]{0,126}[a-z0-9])?$"
)


@dataclass
class _StructuralGuard:
    """Private one-shot binding retaining only token, scope, and frame selectors."""

    token: str
    scope: Any
    frame_path: tuple[str, ...]
    context: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _FramePoint:
    """A top-level point projected into one concrete document viewport."""

    scope: Any
    x: float
    y: float
    frame_path: tuple[str, ...]


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
        // A row/list item with an explicit accessible name has already
        // declared its record identity. Prefer that stable application-owned
        // signal over concatenating every sibling control label, which would
        // make harmless button renames look like a record-identity change.
        const declaredIdentity = clean(
            row.getAttribute('data-openadapt-identity') ||
            row.getAttribute('aria-label') ||
            ''
        );
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
        rowIdentity = declaredIdentity || clean(clone.textContent || '');
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
        contextObserver: null,
        context: {},
        invalidate: null,
        focusListener: null,
    };
    const invalidate = () => {
        if (entry.observer) entry.observer.disconnect();
        if (entry.contextObserver) entry.contextObserver.disconnect();
        if (entry.focusListener) {
            el.removeEventListener('blur', entry.focusListener, true);
        }
        for (const candidate of document.querySelectorAll(
                '[' + args.tokenAttribute + ']')) {
            if (candidate.getAttribute(args.tokenAttribute) === args.token) {
                candidate.removeAttribute(args.tokenAttribute);
            }
        }
        tokenMap.delete(args.token);
    };
    entry.invalidate = invalidate;
    // Any mutation in the target's record boundary after arming invalidates
    // the lease. Descriptor equality is deliberately irrelevant: hidden
    // attributes and pixel-identical node replacement can still change the
    // action that a click performs.
    entry.observer = new MutationObserver(invalidate);
    entry.observer.observe(observed.row || el, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
    });
    if (args.requireFocused) {
        if (document.activeElement !== el) {
            invalidate();
            return null;
        }
        entry.focusListener = invalidate;
        el.addEventListener('blur', entry.focusListener, true);
    }
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
    const actionable =
        'button, a[href], input[type="button"], input[type="submit"],' +
        ' input[type="reset"], input[type="checkbox"], input[type="radio"],' +
        ' select,' +
        ' [role="button"], [role="link"], [role="menuitem"],' +
        ' [role="tab"], [role="option"], [role="checkbox"],' +
        ' [role="radio"], [role="switch"]';
    const editable =
        ', input:not([type]), input[type="text"], input[type="search"],' +
        ' input[type="email"], input[type="url"], input[type="tel"],' +
        ' input[type="password"], input[type="number"], input[type="date"],' +
        ' input[type="time"], input[type="datetime-local"],' +
        ' input[type="month"], input[type="week"], textarea,' +
        ' [contenteditable=""], [contenteditable="true"], [role="textbox"]';
    const el = hit.closest(
        actionable + (args.allowEditable ? editable : '')
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

_BIND_FOCUSED_TARGET_JS = (
    r"""(args) => {
    const el = document.activeElement;
    if (!el || el === document.body || el === document.documentElement) {
        return null;
    }
    const describe = """
    + _DESCRIBE_TARGET_JS
    + ";"
    + _INSTALL_GUARD_BODY_JS
    + "}"
)

_STRUCTURED_TEXT_AT_JS = (
    r"""([px, py]) => {
    const el = document.elementFromPoint(px, py);
    if (!el) return null;
    const describe = """
    + _DESCRIBE_TARGET_JS
    + r""";
    return describe(el).rowIdentity || null;
}"""
)

_CONTEXT_CURRENT_JS = r"""(entry) => {
    const metaName = {
        session: 'openadapt-session-identity',
        workflow_state: 'openadapt-workflow-state',
    };
    for (const [kind, expected] of Object.entries(entry.context || {})) {
        if (kind === 'application') {
            let url;
            try {
                url = new URL(window.location.href);
            } catch (_) {
                return false;
            }
            let hostname = url.hostname.toLowerCase().replace(/\.+$/, '');
            if (!hostname) return false;
            const rendered = hostname.includes(':')
                ? '[' + hostname + ']'
                : hostname;
            const defaultPort =
                (url.protocol === 'http:' && url.port === '80') ||
                (url.protocol === 'https:' && url.port === '443');
            const observed = url.protocol.slice(0, -1) + '://' + rendered +
                (url.port && !defaultPort ? ':' + url.port : '');
            if (observed !== expected) return false;
            continue;
        }
        const name = metaName[kind];
        if (!name) return false;
        const markers = document.querySelectorAll(
            'head > meta[name="' + name + '"]'
        );
        if (markers.length !== 1 ||
                markers[0].getAttribute('content') !== expected) {
            return false;
        }
    }
    return true;
}"""

_GUARD_CURRENT_JS = (
    "(el, args) => { const describe = "
    + _DESCRIBE_TARGET_JS
    + "; const contextCurrent = "
    + _CONTEXT_CURRENT_JS
    + r""";
    const tokenMap = window[args.storeKey];
    const entry = tokenMap instanceof Map ? tokenMap.get(args.token) : null;
    return Boolean(
        entry && entry.el === el &&
        el.getAttribute(args.tokenAttribute) === args.token &&
        (!args.requireFocused || document.activeElement === el) &&
        contextCurrent(entry) &&
        entry.descriptor === describe(el).descriptor
    );
}"""
)

_BIND_CONTEXT_IDENTITY_JS = (
    r"""(args) => {
    const tokenMap = window[args.storeKey];
    if (!(tokenMap instanceof Map)) return true;
    const current = """
    + _CONTEXT_CURRENT_JS
    + r""";
    let valid = true;
    for (const entry of tokenMap.values()) {
        entry.context = entry.context || {};
        entry.context[args.kind] = args.value;
        if (!current(entry)) {
            valid = false;
            if (entry.invalidate) entry.invalidate();
            continue;
        }
        if (!entry.contextObserver && document.head) {
            entry.contextObserver = new MutationObserver(() => {
                if (!current(entry) && entry.invalidate) entry.invalidate();
            });
            entry.contextObserver.observe(document.head, {
                attributes: true,
                childList: true,
                subtree: true,
                attributeFilter: ['content', 'name'],
            });
        }
    }
    return valid;
}"""
)

_CLEAN_GUARD_JS = r"""(args) => {
    const tokenMap = window[args.storeKey];
    const entry = tokenMap instanceof Map ? tokenMap.get(args.token) : null;
    if (entry && entry.observer) entry.observer.disconnect();
    if (entry && entry.contextObserver) entry.contextObserver.disconnect();
    if (entry && entry.focusListener && entry.el) {
        entry.el.removeEventListener('blur', entry.focusListener, true);
    }
    for (const candidate of document.querySelectorAll(
            '[' + args.tokenAttribute + ']')) {
        if (candidate.getAttribute(args.tokenAttribute) === args.token) {
            candidate.removeAttribute(args.tokenAttribute);
        }
    }
    if (tokenMap instanceof Map) tokenMap.delete(args.token);
}"""

_STRUCTURAL_LOCATOR_AT_JS = r"""([px, py]) => {
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
        const map = {button: 'button', a: 'link', input: 'textbox',
            select: 'combobox', textarea: 'textbox'};
        role = map[tag] || null;
        if (tag === 'a' && !actionable.getAttribute('href')) role = null;
    }
    let name = actionable.getAttribute('aria-label');
    if (!name) {
        const text = (actionable.textContent || '').replace(/\s+/g, ' ').trim();
        name = text ? text.slice(0, 120) : null;
    }
    if (!selector && !(role && name)) return null;
    return {selector: selector, role: role, name: name};
}"""

_FRAME_SELECTOR_JS = r"""(el) => {
    const doc = el.ownerDocument;
    const unique = (selector) => {
        try { return doc.querySelectorAll(selector).length === 1; }
        catch (_) { return false; }
    };
    if (el.id) {
        const byId = '#' + CSS.escape(el.id);
        if (unique(byId)) return byId;
    }
    const segments = [];
    let node = el;
    while (node && node !== doc.documentElement) {
        const tag = node.tagName.toLowerCase();
        let index = 1;
        for (let sibling = node.previousElementSibling; sibling;
                sibling = sibling.previousElementSibling) {
            if (sibling.tagName === node.tagName) index += 1;
        }
        segments.unshift(tag + ':nth-of-type(' + index + ')');
        const candidate = segments.join(' > ');
        if (unique(candidate)) return candidate;
        node = node.parentElement;
    }
    return null;
}"""

_PROJECT_FRAME_POINT_JS = r"""(el, point) => {
    const [px, py] = point;
    if (document.elementFromPoint(px, py) !== el) return null;

    // getBoundingClientRect can safely invert only positive axis-aligned
    // scale/translation. Reject rotation, skew, mirroring, perspective, and
    // 3-D transforms on the frame or any ancestor rather than guessing.
    const epsilon = 1e-7;
    for (let node = el; node; node = node.parentElement) {
        const style = window.getComputedStyle(node);
        if (style.perspective && style.perspective !== 'none') return null;
        const rotate = style.rotate;
        if (rotate && rotate !== 'none' && rotate !== '0deg') return null;
        const scale = style.scale;
        if (scale && scale !== 'none') {
            const components = scale.trim().split(/\s+/).map(Number);
            if (components.length < 1 || components.length > 2 ||
                    components.some(value => !Number.isFinite(value) || value <= 0)) {
                return null;
            }
        }
        const transform = style.transform;
        if (transform && transform !== 'none') {
            let matrix;
            try { matrix = new DOMMatrixReadOnly(transform); }
            catch (_) { return null; }
            if (!matrix.is2D || Math.abs(matrix.b) > epsilon ||
                    Math.abs(matrix.c) > epsilon || matrix.a <= 0 || matrix.d <= 0) {
                return null;
            }
        }
    }

    const rect = el.getBoundingClientRect();
    const offsetWidth = Number(el.offsetWidth);
    const offsetHeight = Number(el.offsetHeight);
    if (!(rect.width > 0 && rect.height > 0 &&
            offsetWidth > 0 && offsetHeight > 0)) return null;
    const scaleX = rect.width / offsetWidth;
    const scaleY = rect.height / offsetHeight;
    if (!(Number.isFinite(scaleX) && Number.isFinite(scaleY) &&
            scaleX > 0 && scaleY > 0)) return null;
    const contentX = rect.left + Number(el.clientLeft) * scaleX;
    const contentY = rect.top + Number(el.clientTop) * scaleY;
    const localX = (px - contentX) / scaleX;
    const localY = (py - contentY) / scaleY;
    const inside = localX >= 0 && localY >= 0 &&
        localX < Number(el.clientWidth) && localY < Number(el.clientHeight);
    return inside ? {inside: true, x: localX, y: localY} : {inside: false};
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
        self._structural_tokens: dict[str, _StructuralGuard] = {}
        self._guarded_coordinate: Optional[
            tuple[tuple[int, int], str, str, tuple[float, float], bool]
        ] = None
        self._guarded_keyboard: Optional[tuple[str, str]] = None

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

    # -- live execution-context identity -------------------------------

    def _bind_context_identity(self, kind: str, value: str) -> bool:
        """Bind one observed context value to every pending one-shot guard."""

        pending = bool(
            self._structural_tokens
            or self._guarded_coordinate is not None
            or self._guarded_keyboard is not None
        )
        if not pending:
            return True
        try:
            root_valid = bool(
                self.page.evaluate(
                    _BIND_CONTEXT_IDENTITY_JS,
                    {
                        "storeKey": self._structural_store_key,
                        "kind": kind,
                        "value": value,
                    },
                )
            )
        except Exception:
            return False
        if not root_valid:
            return False
        # A child frame has its own Window and guard store, while application,
        # session, and workflow-state identity are deliberately top-level
        # contracts. Retain only those bounded values in Python and compare
        # them again immediately before delivery. The target descriptor and
        # row identity remain exclusively inside the child frame.
        for guard in self._structural_tokens.values():
            guard.context[kind] = value
        return True

    def _application_identity_value(self) -> Optional[str]:
        """Observe the top-level origin without mutating any pending guard."""

        try:
            current_url = self.page.url
            parts = urlsplit(current_url)
            scheme = parts.scheme.lower()
            hostname = parts.hostname
            port = parts.port
        except (AttributeError, TypeError, ValueError):
            return None
        except Exception:
            return None

        if scheme not in {"http", "https"} or not hostname:
            return None
        hostname = hostname.lower().rstrip(".")
        if not hostname or len(hostname) > 253:
            return None
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        origin = f"{scheme}://{rendered_host}"
        if port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            origin += f":{port}"
        if len(origin) > 320:
            return None
        return origin

    def application_identity(self) -> Optional[str]:
        """Return and bind the live top-level browser origin.

        User information, path, query, and fragment are never included, so
        record identifiers and other sensitive navigation state cannot enter
        identity evidence. Default ports are omitted.
        """

        origin = self._application_identity_value()
        if origin is None:
            return None
        return origin if self._bind_context_identity("application", origin) else None

    def _context_meta_content(self, name: str) -> Optional[str]:
        """Read one unambiguous live ``<head>`` context marker."""

        try:
            locator = self.page.locator(f'head > meta[name="{name}"]')
            if locator.count() != 1:
                return None
            content = locator.get_attribute("content")
        except Exception:
            return None
        return content if isinstance(content, str) else None

    def session_identity(self) -> Optional[str]:
        """Return the live opaque session digest declared by the page.

        The contract is exactly one direct ``<head>`` meta named
        ``openadapt-session-identity`` with a 64-character lowercase
        hexadecimal ``content`` value. Missing, duplicate, malformed, or
        unreadable markers return ``None``.
        """

        value = self._context_meta_content(_SESSION_IDENTITY_META)
        observed = (
            value
            if value is not None and _SESSION_IDENTITY_PATTERN.fullmatch(value)
            else None
        )
        if observed is None:
            return None
        return observed if self._bind_context_identity("session", observed) else None

    def workflow_state_identity(self) -> Optional[str]:
        """Return the live, application-declared PHI-free workflow-state token.

        The contract is exactly one direct ``<head>`` meta named
        ``openadapt-workflow-state``. Its ``content`` must be a 1-128 character
        lowercase machine token containing only letters, digits, ``.``, ``_``,
        ``:``, and ``-``. Missing, duplicate, malformed, or unreadable markers
        return ``None``.
        """

        value = self._context_meta_content(_WORKFLOW_STATE_IDENTITY_META)
        observed = (
            value
            if value is not None and _WORKFLOW_STATE_IDENTITY_PATTERN.fullmatch(value)
            else None
        )
        if observed is None:
            return None
        return (
            observed
            if self._bind_context_identity("workflow_state", observed)
            else None
        )

    def _context_guard_is_current(self, guard: _StructuralGuard) -> bool:
        """Re-read every bound top-level identity immediately before input."""

        for kind, expected in guard.context.items():
            if kind == "application":
                observed = self._application_identity_value()
            elif kind == "session":
                value = self._context_meta_content(_SESSION_IDENTITY_META)
                observed = (
                    value
                    if value is not None and _SESSION_IDENTITY_PATTERN.fullmatch(value)
                    else None
                )
            elif kind == "workflow_state":
                value = self._context_meta_content(_WORKFLOW_STATE_IDENTITY_META)
                observed = (
                    value
                    if value is not None
                    and _WORKFLOW_STATE_IDENTITY_PATTERN.fullmatch(value)
                    else None
                )
            else:  # defensive: a new unreviewed identity kind cannot pass
                return False
            if observed != expected:
                return False
        return True

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
        cell. An explicit ``data-openadapt-identity`` or accessible row name is
        authoritative; otherwise the row text EXCLUDING the clicked target's
        own cell/subtree is used -- that cell's label is
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
                _STRUCTURED_TEXT_AT_JS,
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

    def _frame_point(self, x: int, y: int) -> Optional[_FramePoint]:
        """Follow the actual hit-tested frame chain and project into its document.

        At each document boundary ``elementFromPoint`` must return the exact
        iframe/frame element we descend through. Hidden, occluded, and
        ``pointer-events:none`` frames therefore cannot contribute structural
        evidence. Entering frame content fails closed if the selector,
        positive axis-aligned transform, or child-frame mapping cannot be
        proven; it never falls back to the parent document.
        """

        scope: Any = self.page
        local_x = float(x)
        local_y = float(y)
        path: list[str] = []
        for _depth in range(9):
            hit = None
            candidate = None
            try:
                hit = scope.evaluate_handle(
                    "([px, py]) => document.elementFromPoint(px, py)",
                    [local_x, local_y],
                ).as_element()
                if hit is None:
                    return None
                tag = hit.evaluate("el => el.localName")
                if tag not in {"iframe", "frame"}:
                    return _FramePoint(scope, local_x, local_y, tuple(path))

                projection = hit.evaluate(_PROJECT_FRAME_POINT_JS, [local_x, local_y])
                if not isinstance(projection, dict):
                    return None
                # A hit on the frame's border belongs to the parent document.
                if projection.get("inside") is not True:
                    return _FramePoint(scope, local_x, local_y, tuple(path))
                if len(path) >= 8:
                    return None
                selector = hit.evaluate(_FRAME_SELECTOR_JS)
                if not isinstance(selector, str) or not selector or len(selector) > 512:
                    return None
                matches = scope.locator(selector)
                if matches.count() != 1:
                    return None
                candidate = matches.element_handle()
                if candidate is None or not hit.evaluate(
                    "(el, candidate) => el === candidate", candidate
                ):
                    return None
                child = hit.content_frame()
                if child is None:
                    return None
                child_width, child_height = child.evaluate(
                    "() => [window.innerWidth, window.innerHeight]"
                )
                next_x = float(projection["x"])
                next_y = float(projection["y"])
                if not (
                    0 <= next_x < float(child_width)
                    and 0 <= next_y < float(child_height)
                ):
                    return None
                path.append(selector)
                scope = child
                local_x = next_x
                local_y = next_y
            except Exception:
                return None
            finally:
                if candidate is not None:
                    candidate.dispose()
                if hit is not None:
                    hit.dispose()
        return None

    def _resolve_scope(self, frame_path: tuple[str, ...]) -> Optional[Any]:
        scope: Any = self.page
        for frame_selector in frame_path:
            frame_elements = scope.locator(frame_selector)
            count = frame_elements.count()
            if count == 0:
                return None
            if count != 1:
                raise StructuralResolutionRefused(
                    "DOM frame path is ambiguous: "
                    f"selector={frame_selector!r}, candidate_count={count}"
                )
            handle = frame_elements.element_handle()
            if handle is None:
                return None
            child = handle.content_frame()
            if child is None:
                return None
            scope = child
        return scope

    def _frame_chain_matches(
        self,
        scope: Any,
        frame_path: tuple[str, ...],
        box: dict[str, Any],
    ) -> bool:
        """Re-prove the exact hit-tested frame chain at a target box center."""

        try:
            x = int(round(float(box["x"]) + float(box["width"]) / 2))
            y = int(round(float(box["y"]) + float(box["height"]) / 2))
        except (KeyError, TypeError, ValueError):
            return False
        point = self._frame_point(x, y)
        return bool(
            point is not None
            and point.scope == scope
            and point.frame_path == frame_path
        )

    def _locator_with_scope(self, locator: StructuralLocator) -> tuple[Any, Any] | None:
        frame_path = tuple(locator.frame_path or ())
        scope = self._resolve_scope(frame_path)
        if scope is None:
            return None
        if locator.selector:
            return scope, scope.locator(locator.selector)
        if locator.role and locator.name:
            return scope, scope.get_by_role(locator.role, name=locator.name, exact=True)
        return None

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
            point = self._frame_point(int(x), int(y))
            if point is None:
                return None
            result = point.scope.evaluate(
                _STRUCTURAL_LOCATOR_AT_JS,
                [point.x, point.y],
            )
        except Exception:
            return None
        if not result:
            return None
        return StructuralLocator(
            selector=result.get("selector"),
            frame_path=list(point.frame_path) or None,
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
            resolved = self._locator_with_scope(locator)
            if resolved is None:
                return None
            scope, loc = resolved
            candidate_count = loc.count()
            if candidate_count == 0:
                return None
            if candidate_count != 1:
                raise StructuralResolutionRefused(
                    f"DOM locator is ambiguous: candidate_count={candidate_count}"
                )
            box = loc.bounding_box()
            if (
                not isinstance(box, dict)
                or float(box["width"]) <= 0
                or float(box["height"]) <= 0
                or not self._frame_chain_matches(
                    scope, tuple(locator.frame_path or ()), box
                )
            ):
                return None
            token = uuid.uuid4().hex
            try:
                observed = loc.evaluate(
                    _BIND_STRUCTURAL_TARGET_JS,
                    {
                        "storeKey": self._structural_store_key,
                        "tokenAttribute": self._token_attribute(token),
                        "token": token,
                        "requireRowIdentity": False,
                    },
                )
                box = loc.bounding_box()
                if (
                    not isinstance(observed, dict)
                    or not isinstance(box, dict)
                    or not self._frame_chain_matches(
                        scope, tuple(locator.frame_path or ()), box
                    )
                ):
                    self._cleanup_guard(token, scope)
                    return None
                if float(box["width"]) <= 0 or float(box["height"]) <= 0:
                    self._cleanup_guard(token, scope)
                    return None
                point = (
                    int(round(float(box["x"]) + float(box["width"]) / 2)),
                    int(round(float(box["y"]) + float(box["height"]) / 2)),
                )
                vw, vh = self.viewport
                if not (0 <= point[0] < vw and 0 <= point[1] < vh):
                    self._cleanup_guard(token, scope)
                    return None
                region = (
                    int(round(float(box["x"]))),
                    int(round(float(box["y"]))),
                    int(round(float(box["width"]))),
                    int(round(float(box["height"]))),
                )
            except Exception:
                self._cleanup_guard(token, scope)
                raise
            fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest()
            # Bound Python-side token retention. Page-side entries are weak and
            # disappear with their DOM nodes; an evicted token is unusable.
            if len(self._structural_tokens) >= 128:
                evicted = self._structural_tokens.pop(
                    next(iter(self._structural_tokens))
                )
                self._cleanup_guard(evicted.token, evicted.scope)
            self._structural_tokens[fingerprint] = _StructuralGuard(
                token=token,
                scope=scope,
                frame_path=tuple(locator.frame_path or ()),
            )
            return StructuralHandle(
                point=point,
                region=region,
                target_fingerprint=fingerprint,
                supported_operations=["dom_click", "dom_double_click"],
            )
        except StructuralResolutionRefused:
            raise
        except Exception:
            return None

    def _locator(self, locator: StructuralLocator) -> Any:
        resolved = self._locator_with_scope(locator)
        return None if resolved is None else resolved[1]

    @staticmethod
    def _token_attribute(token: str) -> str:
        return f"{_TOKEN_ATTRIBUTE_PREFIX}{token}"

    def _cleanup_guard(self, token: str, scope: Any = None) -> None:
        scope = self.page if scope is None else scope
        try:
            scope.evaluate(
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

    def _cancel_structural_guards(self) -> None:
        """Clean element-resolution guards no keyboard action will consume."""

        guards = list(self._structural_tokens.values())
        self._structural_tokens.clear()
        for guard in guards:
            self._cleanup_guard(guard.token, guard.scope)

    def cancel_pending_structural_guards(self) -> None:
        """Discard handles that an immediate fresh re-resolution replaces."""

        self._cancel_structural_guards()

    def _token_locator(self, token: str, scope: Any = None) -> Any:
        scope = self.page if scope is None else scope
        return scope.locator(f"[{self._token_attribute(token)}]")

    def _guard_is_current(
        self,
        locator: Any,
        token: str,
        *,
        require_focused: bool = False,
    ) -> bool:
        try:
            return bool(
                locator.evaluate(
                    _GUARD_CURRENT_JS,
                    {
                        "storeKey": self._structural_store_key,
                        "tokenAttribute": self._token_attribute(token),
                        "token": token,
                        "requireFocused": require_focused,
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
        guard = self._structural_tokens.pop(fingerprint, None)
        if guard is None:
            raise StructuralResolutionRefused(
                "guarded DOM actuation token is missing, stale, or already consumed"
            )
        try:
            resolved = self._locator_with_scope(locator)
            if resolved is None:
                raise StructuralResolutionRefused(
                    "guarded DOM actuation requires an exact structural locator"
                )
            scope, loc = resolved
            if (
                scope != guard.scope
                or tuple(locator.frame_path or ()) != guard.frame_path
            ):
                raise StructuralResolutionRefused(
                    "guarded DOM frame context changed before delivery"
                )
            if not self._context_guard_is_current(guard):
                raise StructuralResolutionRefused(
                    "guarded DOM execution context changed before delivery"
                )
            box = loc.bounding_box()
            if not isinstance(box, dict) or not self._frame_chain_matches(
                scope, guard.frame_path, box
            ):
                raise StructuralResolutionRefused(
                    "guarded DOM frame chain changed before delivery"
                )
            if loc.count() != 1 or not self._guard_is_current(loc, guard.token):
                raise StructuralResolutionRefused(
                    "guarded DOM target or record identity changed before delivery"
                )
            token_locator = self._token_locator(guard.token, guard.scope)
            if token_locator.count() != 1:
                raise StructuralResolutionRefused(
                    "guarded DOM token is missing or ambiguous at delivery"
                )
            try:
                # Separate Playwright's actionability trial from the real input
                # attempt.  Trial failures are proven pre-dispatch refusals;
                # only an exception from the subsequent real click is delivery
                # uncertainty.
                if double:
                    token_locator.dblclick(timeout=1000, trial=True)
                else:
                    token_locator.click(timeout=1000, trial=True)
            except Exception as exc:
                raise StructuralResolutionRefused(
                    "guarded DOM target was unactionable during its pre-dispatch trial"
                ) from exc
            if (
                not self._context_guard_is_current(guard)
                or loc.count() != 1
                or not self._guard_is_current(loc, guard.token)
            ):
                raise StructuralResolutionRefused(
                    "guarded DOM target or context changed after the "
                    "pre-dispatch actionability trial"
                )
            try:
                if double:
                    token_locator.dblclick(timeout=1000)
                else:
                    token_locator.click(timeout=1000)
            except Exception as exc:
                # Every structural/context/identity check above happens before
                # this call.  Once Playwright begins its action, an exception
                # (notably frame detach/navigation) cannot prove that the
                # browser emitted no input.  Surface a typed uncertainty so the
                # runtime verifies the effect exactly once and never retries.
                raise ActionDeliveryUncertain(
                    operation="dom_double_click" if double else "dom_click",
                    native=False,
                    target_fingerprint=fingerprint,
                    cause_type=type(exc).__name__,
                ) from exc
        except ActionDeliveryUncertain:
            raise
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "guarded DOM target changed or became unactionable before delivery"
            ) from exc
        finally:
            self._cleanup_guard(guard.token, guard.scope)
        return ActionDeliveryReceipt(
            receipt_id=f"playwright-{uuid.uuid4().hex}",
            operation="dom_double_click" if double else "dom_click",
            native=False,
            target_fingerprint=fingerprint,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def _arm_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        allow_editable: bool,
    ) -> None:
        self.cancel_guarded_coordinate()
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
                    "allowEditable": allow_editable,
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
            fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest()
            self._guarded_coordinate = (
                point,
                fingerprint,
                token,
                (float(offset[0]), float(offset[1])),
                allow_editable,
            )
        except Exception:
            self._cleanup_guard(token)
            raise

    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        """Bind an actionable visual point before identity readback."""

        self._arm_guarded_coordinate(x, y, allow_editable=False)

    def arm_guarded_editable_coordinate(self, x: int, y: int) -> None:
        """Bind an editable field for an identity-gated focusing click."""

        self._arm_guarded_coordinate(x, y, allow_editable=True)

    def cancel_guarded_coordinate(self) -> None:
        """Cancel and clean the current one-shot visual DOM binding."""

        pending = self._guarded_coordinate
        self._guarded_coordinate = None
        if pending is not None:
            self._cleanup_guard(pending[2])

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        """Consume the target binding armed before the fresh identity read.

        The already-armed MutationObserver covers hidden mutations,
        pixel-identical node replacement, and every target/row subtree change
        from before the identity observation through Playwright's real pointer
        delivery. Unlike a remote pixel-only lease, this DOM lease deliberately
        does not require byte-identical full-viewport screenshots: caret
        painting and unrelated page animation are not target changes.
        """

        point = (int(x), int(y))
        pending = self._guarded_coordinate
        self._guarded_coordinate = None
        if pending is None:
            raise StructuralResolutionRefused(
                "visual DOM actuation has no pre-identity target binding"
            )
        armed_point, fingerprint, token, offset, _editable = pending
        try:
            if armed_point != point:
                raise StructuralResolutionRefused(
                    "visual DOM actuation point changed after target binding"
                )
            # Retain the protocol argument for cross-backend compatibility.
            # Browser delivery is bound by the stronger target/record/context
            # lease below; remote pixel backends continue to enforce their
            # exact fresh-frame lease.
            del expected_frame_sha256
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

    def arm_guarded_keyboard(self, x: int, y: int) -> None:
        """Lease the exact focused element/record before identity readback."""

        # KEY delivery and the post-focus TYPE phase do not consume a structural
        # click handle. Remove those now-unused observers before adding the
        # keyboard token so the guards cannot invalidate one another merely by
        # cleaning their private DOM attributes.
        self.cancel_pending_structural_guards()
        self.cancel_guarded_keyboard()
        token = uuid.uuid4().hex
        try:
            observed = self.page.evaluate(
                _BIND_FOCUSED_TARGET_JS,
                {
                    "storeKey": self._structural_store_key,
                    "tokenAttribute": self._token_attribute(token),
                    "token": token,
                    "x": int(x),
                    "y": int(y),
                    "requireRowIdentity": True,
                    "requireFocused": True,
                },
            )
            if not isinstance(observed, dict):
                raise StructuralResolutionRefused(
                    "consequential keyboard action has no focused "
                    "identity-bearing DOM target at the resolved point"
                )
            fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest()
            self._guarded_keyboard = (fingerprint, token)
        except Exception:
            self._cleanup_guard(token)
            raise

    def cancel_guarded_keyboard(self) -> None:
        """Cancel and clean the current one-shot focused-element lease."""

        pending = self._guarded_keyboard
        self._guarded_keyboard = None
        if pending is not None:
            self._cleanup_guard(pending[1])

    def guarded_keyboard_frame(self) -> bytes:
        """Capture a caret-stable frame without mutating the focused field.

        ``caret="initial"`` prevents Playwright from toggling inline styles on
        the editable element inside a guarded record row. A temporary
        screenshot stylesheet hides the blinking caret from the pixels, so
        consecutive captures provide a stable resolver/identity observation;
        the stylesheet is injected outside the guarded row.
        """

        def capture() -> bytes:
            return self.page.screenshot(
                type="png",
                full_page=False,
                caret="initial",
                style="* { caret-color: transparent !important; }",
            )

        previous = capture()
        for _attempt in range(3):
            current = capture()
            if current == previous:
                return current
            previous = current
        # A continuously changing surface remains different from the earlier
        # preflight hash and is refused by the caller. Never coerce instability
        # into a matching frame.
        return previous

    def _act_guarded_keyboard(
        self,
        *,
        expected_frame_sha256: str,
        operation: str,
        deliver: Callable[[Any], None],
    ) -> ActionDeliveryReceipt:
        pending = self._guarded_keyboard
        self._guarded_keyboard = None
        if pending is None:
            raise StructuralResolutionRefused(
                "keyboard actuation has no pre-identity focused-element lease"
            )
        fingerprint, token = pending
        try:
            # Retain the protocol argument for cross-backend compatibility.
            # The browser's one-shot focused-element/record/context lease is
            # the authoritative race guard. Full-frame byte equality is both
            # weaker semantically and unstable around caret painting.
            del expected_frame_sha256
            token_locator = self._token_locator(token)
            if token_locator.count() != 1 or not self._guard_is_current(
                token_locator,
                token,
                require_focused=True,
            ):
                raise StructuralResolutionRefused(
                    "focused keyboard target or record changed before delivery"
                )
            deliver(token_locator)
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "identity-bound keyboard target became unactionable before delivery"
            ) from exc
        finally:
            self._cleanup_guard(token)
        return ActionDeliveryReceipt(
            receipt_id=f"playwright-keyboard-{uuid.uuid4().hex}",
            operation=operation,
            native=False,
            target_fingerprint=fingerprint,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def press_guarded(
        self,
        key: str,
        *,
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        """Press a key/chord through the pre-identity focused-element lease."""

        chord = _normalize_chord(key)
        return self._act_guarded_keyboard(
            expected_frame_sha256=expected_frame_sha256,
            operation="guarded_dom_key",
            deliver=lambda locator: locator.press(chord, timeout=1000),
        )

    def type_text_guarded(
        self,
        text: str,
        *,
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        """Type through the pre-identity focused-element lease."""

        return self._act_guarded_keyboard(
            expected_frame_sha256=expected_frame_sha256,
            operation="guarded_dom_type",
            deliver=lambda locator: locator.press_sequentially(text, timeout=1000),
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
