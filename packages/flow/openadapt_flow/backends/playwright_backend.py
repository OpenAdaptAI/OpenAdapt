"""Playwright-driven reference backend (sync API, chromium, headless-capable).

Implements the `openadapt_flow.backend.Backend` protocol against a Playwright
`Page`: full-viewport PNG screenshots, mouse clicks at pixel coordinates,
keyboard typing, and key/chord presses. Viewport is fixed at 1280x800 with
deviceScaleFactor=1 so CSS pixels equal screenshot pixels.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional
from urllib.parse import urlsplit

from openadapt_flow.backends._page_quiescence import PageQuiescence
from openadapt_flow.backends._server_responses import ServerResponseLog

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

from openadapt_flow.backend import ActionDeliveryUncertain, StructuralResolutionRefused
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    StructuralHandle,
    StructuralLocator,
    SubmitBinding,
)
from openadapt_flow.runtime.resolver import (
    structural_resolution_fingerprint,
    visual_resolution_point_fingerprint,
)
from openadapt_flow.submit_binding import (
    OBSERVE_SUBMIT_BINDING_JS,
    SUBMIT_BINDING_FIELD_SELECTOR,
    observation_from_page,
    verify_submit_binding,
)

VIEWPORT: tuple[int, int] = (1280, 800)
# Secret-masked screenshots must not keep bytes from a changing frame tree.
# Login pages often attach and detach iframes for a few seconds after a click.
# Retry until the tree is quiet. Still refuse if it never settles.
_MASKED_SCREENSHOT_TIMEOUT_S = 8.0
_MASKED_SCREENSHOT_RETRY_SLEEP_S = 0.05
# A capture that overlaps a navigation commit (a form POST answered with a
# redirect, for example) fails in Chromium with "Unable to capture
# screenshot" or stalls until Playwright's timeout. Such a failure says
# nothing about the page; wait for the new document and capture again.
_NAVIGATION_SCREENSHOT_TIMEOUT_S = 30.0
_NAVIGATION_SCREENSHOT_ATTEMPT_TIMEOUT_MS = 10_000
_TRANSIENT_SCREENSHOT_ERRORS = (
    "Unable to capture screenshot",
    "Timeout",
    "Execution context was destroyed",
)

# How long a click that reported its page closed waits for the page to read
# as closed before the close counts as confirmed.
_PAGE_CLOSE_CONFIRM_S = 1.0


def _is_target_closed_error(exc: BaseException) -> bool:
    """Whether Playwright reported the page, context or browser closed."""
    return type(exc).__name__ == "TargetClosedError"


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
#   <meta name="openadapt-application-version" content="8.0.0.3">
#
# The session marker is an opaque digest. The workflow marker is a bounded,
# lowercase machine token that the application author promises is PHI-free
# (state names such as ``eligibility.review``, never record values).
_SESSION_IDENTITY_META = "openadapt-session-identity"
_ENVIRONMENT_IDENTITY_META = "openadapt-environment-identity"
_WORKFLOW_STATE_IDENTITY_META = "openadapt-workflow-state"
_APPLICATION_VERSION_META = "openadapt-application-version"
_SESSION_IDENTITY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_APPLICATION_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
_WORKFLOW_STATE_IDENTITY_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9._:-]{0,126}[a-z0-9])?$"
)


class ScreenshotMaskStabilityError(RuntimeError):
    """The browser frame tree changed across every masked screenshot attempt."""


@dataclass
class _StructuralGuard:
    """Private one-shot binding retaining only token, scope, and frame selectors."""

    token: str
    scope: Any
    frame_path: tuple[str, ...]
    context: dict[str, str] = field(default_factory=dict)


@dataclass
class _CoordinateGuard:
    """One-shot visual target lease in one exact document/frame context."""

    point: tuple[int, int]
    local_point: tuple[float, float]
    fingerprint: str
    token: str
    offset: tuple[float, float]
    allow_editable: bool
    scope: Any
    frame_path: tuple[str, ...]
    context: dict[str, str] = field(default_factory=dict)


@dataclass
class _KeyboardGuard:
    """One-shot focused-element lease in one exact document/frame context."""

    point: tuple[int, int]
    local_point: tuple[float, float]
    fingerprint: str
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
    // Retain the complete live DOM state for the bounded record boundary,
    // excluding only OpenAdapt's own random lease attribute.  This remains in
    // the page-local guard store; it is never returned in a report.  Comparing
    // the final state, rather than treating every MutationObserver callback as
    // irreversible, admits framework-owned hover/actionability churn only
    // when the exact same Element and DOM state have been restored.
    const boundary = (row || node).cloneNode(true);
    for (const candidate of [boundary, ...boundary.querySelectorAll('*')]) {
        for (const attr of Array.from(candidate.attributes || [])) {
            if (attr.name.startsWith('data-openadapt-actuation-')) {
                candidate.removeAttribute(attr.name);
            }
        }
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
            boundary.outerHTML,
        ]),
        rowIdentity: rowIdentity,
        row: row,
    };
}"""

_INSTALL_GUARD_BODY_JS = (
    r"""
    const observeSubmitBinding = """
    + OBSERVE_SUBMIT_BINDING_JS
    + r""";
    const observed = describe(el);
    let recordTie = observed.rowIdentity;
    if (args.requireRowIdentity && !recordTie && args.submitBinding) {
        // No row identity: an unlabeled control may still be tied to the
        // record the program entered, by its recorded submit binding. Python
        // verified this exact observation against the compiled binding; the
        // guard proves the element still yields it, atomically with install.
        const fills = window[args.submitBinding.fillKey];
        const live = observeSubmitBinding(el, Array.isArray(fills) ? fills : []);
        if (live.canon === args.submitBinding.expected) {
            recordTie = 'submit-binding';
        }
    }
    if (args.requireRowIdentity && !recordTie && args.admitFormField) {
        // Opt-in (backend ``keyboard_admits_form_fields``): a focused,
        // uniquely identified data-entry control on a form that has no record
        // row yet (a new-record form). The lease binds this exact element;
        // its identity is still verified against the compiled structured
        // template by the readback that follows, so this only replaces the
        // missing row tie, never the identity check.
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (el.matches('input, select, textarea') && el.id &&
                !['hidden', 'password', 'submit', 'button', 'reset', 'image',
                  'file'].includes(type) &&
                document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
            recordTie = 'form-field:' + el.id;
        }
    }
    if (args.requireRowIdentity && !recordTie) return null;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (!el.isConnected || rect.width <= 0 || rect.height <= 0 ||
            style.visibility === 'hidden' || style.display === 'none' ||
            Number(style.opacity || '1') === 0) {
        return null;
    }
    const cx = Math.round(rect.x + rect.width / 2);
    const cy = Math.round(rect.y + rect.height / 2);
    const ax = Number.isFinite(args.x) ? Number(args.x) : cx;
    const ay = Number.isFinite(args.y) ? Number(args.y) : cy;
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
        submitBinding: args.submitBinding || null,
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
)

_BIND_STRUCTURAL_TARGET_JS = (
    "(el, args) => { const describe = "
    + _DESCRIBE_TARGET_JS
    + ";"
    + _INSTALL_GUARD_BODY_JS
    + "}"
)

# The element a guarded coordinate click binds: the nearest actionable
# control containing the resolved point. The submit-binding probe binds the
# same element, so the observation it verifies is the one the lease guards.
GUARDED_ACTIONABLE_SELECTOR = (
    'button, a[href], input[type="button"], input[type="submit"],'
    ' input[type="reset"], input[type="checkbox"], input[type="radio"],'
    " select,"
    ' [role="button"], [role="link"], [role="menuitem"],'
    ' [role="tab"], [role="option"], [role="checkbox"],'
    ' [role="radio"], [role="switch"]'
)

_BIND_COORDINATE_TARGET_JS = (
    r"""(args) => {
    const hit = document.elementFromPoint(args.x, args.y);
    if (!hit) return null;
    const actionable = """
    + json.dumps(GUARDED_ACTIONABLE_SELECTOR)
    + r""";
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

# Observe the submit binding of the control at a point: the same element a
# guarded coordinate click would bind, against the fields typed into so far.
_SUBMIT_BINDING_AT_JS = (
    r"""(args) => {
    const hit = document.elementFromPoint(args.x, args.y);
    if (!hit) return null;
    const control = hit.closest("""
    + json.dumps(GUARDED_ACTIONABLE_SELECTOR)
    + r""");
    if (!control || control.matches(
            'canvas, input[type="range"], [role="slider"]')) {
        return null;
    }
    const observe = """
    + OBSERVE_SUBMIT_BINDING_JS
    + r""";
    const fills = window[args.fillKey];
    return observe(control, Array.isArray(fills) ? fills : []);
}"""
)

# Remember the field that is about to receive typed text. The list lives in
# the field's own document under a per-backend random key and holds element
# references only: never a value.
_REGISTER_FILLED_FIELD_JS = (
    r"""(el, key) => {
    if (!el.matches("""
    + json.dumps(SUBMIT_BINDING_FIELD_SELECTOR)
    + r""")) return false;
    let fills = window[key];
    if (!Array.isArray(fills)) {
        fills = [];
        Object.defineProperty(window, key, {value: fills, configurable: true});
    }
    if (fills.indexOf(el) < 0) fills.push(el);
    return true;
}"""
)

# Page-structure identity of a FORM FIELD that sits in no record row.
#
# A data-entry form (OpenEMR's new-patient page, most CRUD screens) lays its
# fields out in label/control groups (``.form-group.row`` and the like), not
# table rows, so the row rule above abstains and identity used to fall to the
# OCR band read across the field's visual row. This mirrors that band in the
# DOM: the nearest small ancestor group that carries label text, rendered as
# its label text plus the CURRENT VALUE of every OTHER control in the group
# (the field values typed earlier in the run, which the OCR band also reads),
# with the clicked control itself excluded (its own value is about to be
# overwritten; that is mutable evidence, exactly like the OCR band's excluded
# target crop). Select controls contribute only their selected option, never
# the option list. Password and hidden inputs contribute nothing. Returns null
# when the hit is not a form control or no bounded labeled group exists, so
# identity stays on the OCR tier.
_FIELD_GROUP_IDENTITY_JS = r"""(hit) => {
    const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const CONTROLS = 'input, select, textarea';
    const target = hit && hit.closest ? hit.closest(CONTROLS) : null;
    if (!target) return null;
    const tType = (target.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'password', 'submit', 'button', 'reset', 'image']
            .includes(tType)) {
        return null;
    }
    const render = (group) => {
        const originals = Array.from(group.querySelectorAll(CONTROLS));
        const clone = group.cloneNode(true);
        const copies = Array.from(clone.querySelectorAll(CONTROLS));
        if (copies.length !== originals.length) return null;
        for (let i = 0; i < originals.length; i += 1) {
            const el = originals[i];
            const type = (el.getAttribute('type') || '').toLowerCase();
            let text = '';
            if (el === target || type === 'hidden' || type === 'password') {
                text = '';
            } else if (el.localName === 'select') {
                const opt = el.selectedOptions && el.selectedOptions[0];
                text = opt ? clean(opt.textContent) : '';
            } else if (type === 'checkbox' || type === 'radio') {
                text = el.checked ? '[x]' : '[ ]';
            } else if (typeof el.value === 'string') {
                text = el.value;
            }
            copies[i].replaceWith(document.createTextNode(' ' + text + ' '));
        }
        for (const junk of clone.querySelectorAll('script, style, template')) {
            junk.remove();
        }
        return clean(clone.textContent);
    };
    const labelText = (group) => {
        const clone = group.cloneNode(true);
        for (const c of clone.querySelectorAll(
                CONTROLS + ', script, style, template, option')) {
            c.remove();
        }
        return clean(clone.textContent);
    };
    let cursor = target.parentElement;
    for (let depth = 0; cursor && depth < 4; depth += 1) {
        const tag = cursor.localName;
        if (tag === 'body' || tag === 'html' || tag === 'form' ||
                tag === 'fieldset') {
            return null;
        }
        if (cursor.querySelectorAll(CONTROLS).length > 8) return null;
        if (labelText(cursor)) {
            const identity = render(cursor);
            return identity && identity.length <= 512 ? identity : null;
        }
        cursor = cursor.parentElement;
    }
    return null;
}"""

# Visible-text candidates in ONE document: the deepest elements whose RENDERED
# text (innerText: display/visibility-aware) contains the normalized target,
# that are not transparent and intersect this document's viewport. Returns
# the frame-local center of each candidate's visible box. The caller still
# hit-tests every candidate through the real frame chain (occlusion, clipping
# by the parent frame) before believing it.
_VISIBLE_TEXT_CANDIDATES_JS = r"""(target) => {
    const norm = (v) => (v || '').replace(/\s+/g, '').toLowerCase();
    const want = norm(target);
    if (!want || !document.body) return [];
    const out = [];
    const vw = window.innerWidth, vh = window.innerHeight;
    const opaque = (el) => {
        for (let c = el; c && c.nodeType === 1; c = c.parentElement) {
            const s = getComputedStyle(c);
            if (s.visibility === 'hidden' || s.display === 'none') return false;
            if (parseFloat(s.opacity) < 0.1) return false;
        }
        const s = getComputedStyle(el);
        const m = /rgba?\(([^)]+)\)/.exec(s.color || '');
        if (m) {
            const parts = m[1].split(',').map((x) => parseFloat(x));
            if (parts.length === 4 && parts[3] < 0.1) return false;
        }
        return parseFloat(s.fontSize) >= 6;
    };
    const visit = (el) => {
        if (!norm(el.textContent).includes(want)) return;
        let deeper = false;
        for (const child of el.children) {
            if (norm(child.textContent).includes(want)) { deeper = true; visit(child); }
        }
        if (deeper || out.length >= 8) return;
        if (!norm(el.innerText).includes(want)) return;
        if (!opaque(el)) return;
        for (const r of el.getClientRects()) {
            const x0 = Math.max(0, r.left), y0 = Math.max(0, r.top);
            const x1 = Math.min(vw, r.right), y1 = Math.min(vh, r.bottom);
            if (x1 - x0 < 2 || y1 - y0 < 2) continue;
            out.push([(x0 + x1) / 2, (y0 + y1) / 2]);
            break;
        }
    };
    visit(document.body);
    return out;
}"""

# At a hit-tested point: is the topmost element (or an ancestor) the one whose
# rendered text contains the target? Proves the text is not covered.
_TEXT_AT_POINT_JS = r"""([px, py, target]) => {
    const norm = (v) => (v || '').replace(/\s+/g, '').toLowerCase();
    const want = norm(target);
    // The topmost element at the point, or one of its ancestors, must be a
    // DEEPEST holder of the text (no child holds it too). Climbing to a big
    // container that merely contains the text elsewhere does not count.
    let el = document.elementFromPoint(px, py);
    for (let i = 0; el && i < 6; i += 1, el = el.parentElement) {
        if (!norm(el.innerText).includes(want)) continue;
        for (const child of el.children) {
            if (norm(child.textContent).includes(want)) return false;
        }
        return true;
    }
    return false;
}"""

_FOCUSED_SELECT_AT_JS = r"""([px, py]) => {
    const hit = document.elementFromPoint(px, py);
    const sel = hit && hit.closest ? hit.closest('select') : null;
    if (!sel || sel !== document.activeElement) return null;
    const opt = sel.selectedOptions && sel.selectedOptions[0];
    return opt ? (opt.label || opt.textContent || '') : null;
}"""

_STRUCTURED_TEXT_AT_JS = (
    r"""([px, py]) => {
    const el = document.elementFromPoint(px, py);
    if (!el) return null;
    const describe = """
    + _DESCRIBE_TARGET_JS
    + r""";
    const rowIdentity = describe(el).rowIdentity;
    if (rowIdentity) return rowIdentity;
    const fieldGroupIdentity = """
    + _FIELD_GROUP_IDENTITY_JS
    + r""";
    return fieldGroupIdentity(el);
}"""
)

_TEXT_VALUE_AT_JS = r"""([px, py]) => {
    const hit = document.elementFromPoint(px, py);
    if (!hit) return null;
    const el = hit.closest(
        'input, textarea, [contenteditable=""], [contenteditable="true"],' +
        ' [role="textbox"]'
    );
    if (!el) return null;
    if ('value' in el && typeof el.value === 'string') return el.value;
    if (el.isContentEditable || el.getAttribute('role') === 'textbox') {
        return el.textContent || '';
    }
    return null;
}"""

# The text the element under a point shows: an input's value, else its
# rendered text (innerText honours CSS visibility). Null when nothing is hit.
_DISPLAYED_TEXT_AT_JS = r"""([px, py]) => {
    const el = document.elementFromPoint(px, py);
    if (!el || el === document.body || el === document.documentElement) {
        return null;
    }
    if ('value' in el && typeof el.value === 'string' &&
        el.matches('input, textarea, select')) return el.value;
    const text = (el.innerText || el.textContent || '').trim();
    return text || null;
}"""

_EDITABLE_VALUE_JS = r"""(el) => {
    if (!el.matches(
            'input, textarea, [contenteditable=""], [contenteditable="true"],' +
            ' [role="textbox"]')) return null;
    if ('value' in el && typeof el.value === 'string') return el.value;
    return el.textContent || '';
}"""

# Best available human label for a (focused) editable field, best-first:
# associated <label for=...>, wrapping <label>, aria-label, aria-labelledby,
# placeholder, name attribute, title. Whitespace-collapsed; null when the
# element is not an editable field or carries no label evidence. PASSIVE
# record-time metadata only (see openadapt_flow.backend.FieldLabelBackend).
_FIELD_LABEL_JS = r"""(el) => {
    if (!el.matches(
            'input, textarea, select, [contenteditable=""],' +
            ' [contenteditable="true"], [role="textbox"]')) return null;
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    if (el.id) {
        try {
            const forLabel = document.querySelector(
                'label[for="' + CSS.escape(el.id) + '"]'
            );
            const t = clean(forLabel && forLabel.textContent);
            if (t) return t;
        } catch (e) {}
    }
    const wrapping = el.closest('label');
    if (wrapping) {
        // Label TEXT only: a control nested inside the label (e.g. a
        // <textarea>) contributes its typed VALUE to textContent -- strip
        // embedded controls so the captured label is never the value.
        const cloned = wrapping.cloneNode(true);
        for (const child of cloned.querySelectorAll(
                'input, textarea, select, [contenteditable=""],' +
                ' [contenteditable="true"], [role="textbox"]')) {
            child.remove();
        }
        const t = clean(cloned.textContent);
        if (t) return t;
    }
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
        const parts = [];
        for (const id of labelledby.split(/\s+/)) {
            const ref = document.getElementById(id);
            const t = clean(ref && ref.textContent);
            if (t) parts.push(t);
        }
        if (parts.length) return parts.join(' ');
    }
    const placeholder = clean(el.getAttribute('placeholder'));
    if (placeholder) return placeholder;
    const name = clean(el.getAttribute('name'));
    if (name) return name;
    const title = clean(el.getAttribute('title'));
    if (title) return title;
    return null;
}"""

_GUARD_CONTAINS_POINT_JS = r"""(el, point) => {
    const [px, py] = point;
    const hit = document.elementFromPoint(px, py);
    return Boolean(hit && (hit === el || el.contains(hit)));
}"""

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
    + "; const observeSubmitBinding = "
    + OBSERVE_SUBMIT_BINDING_JS
    + r""";
    const tokenMap = window[args.storeKey];
    const entry = tokenMap instanceof Map ? tokenMap.get(args.token) : null;
    // A lease armed by a submit binding stays valid only while the element
    // still yields the exact binding observation verified at arm time.
    const bindingCurrent = () => {
        if (!entry.submitBinding) return true;
        const fills = window[entry.submitBinding.fillKey];
        return observeSubmitBinding(
            el, Array.isArray(fills) ? fills : []
        ).canon === entry.submitBinding.expected;
    };
    return Boolean(
        entry && entry.el === el &&
        el.getAttribute(args.tokenAttribute) === args.token &&
        (!args.requireFocused || document.activeElement === el) &&
        contextCurrent(entry) &&
        entry.descriptor === describe(el).descriptor &&
        bindingCurrent()
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

_STRUCTURAL_LOCATOR_AT_JS = (
    r"""([px, py]) => {
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
    const fieldLabel = """
    + _FIELD_LABEL_JS
    + r""";
    let name = actionable.getAttribute('aria-label');
    if (!name) name = fieldLabel(actionable);
    if (!name) {
        const text = (actionable.textContent || '').replace(/\s+/g, ' ').trim();
        name = text ? text.slice(0, 120) : null;
    }
    if (!selector && !(role && name)) return null;
    return {selector: selector, role: role, name: name};
}"""
)

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

    # Guarded keyboard actuation normally requires the focused element to sit
    # in a record row. A backend for a new-record form (no row exists yet) may
    # opt in to admitting a focused, uniquely identified form control instead;
    # identity readback against the compiled template still decides.
    keyboard_admits_form_fields: bool = False

    def __init__(
        self,
        page: "Page",
        *,
        screenshot_scale: Literal["css", "device"] = "device",
        screenshot_mask_selectors: tuple[str, ...] = (),
        structural_state_reader: Optional[Callable[[], dict[str, Any]]] = None,
        screenshot_guard: Optional[Callable[[], None]] = None,
        closed_page_frame: bool = False,
        bring_to_front_before_capture: bool = False,
    ) -> None:
        """Wrap an existing Playwright page.

        Args:
            page: A Playwright page.
            screenshot_scale: Pixel scale for retained screenshots. The
                ordinary launched-browser path uses Playwright's ``device``
                default. A browser attached through CDP uses ``css`` so DOM
                event coordinates and retained frame pixels stay in the same
                coordinate system even on a high-density display.
            screenshot_mask_selectors: CSS selectors whose matching elements
                are blacked out by Chromium before screenshot bytes reach
                Python. The interactive recorder uses this for password and
                declared-secret fields on every retained frame. Locators are
                rebuilt from every current document frame for each screenshot
                so a child-frame field or a frame added after startup cannot
                bypass the mask.
            structural_state_reader: Optional source-time sanitized URL/title
                reader. Recording paths use it to keep raw reflected secrets
                out of Python-side structural evidence.
            screenshot_guard: Optional fail-closed check that runs before
                Chromium creates screenshot bytes. Recording paths use it to
                bind or refuse closed-shadow secret boundaries.
            closed_page_frame: Multi-surface PAGE surfaces only (a popup or
                tab, docs/MULTI_SURFACE.md). Once the page has closed itself,
                ``screenshot`` returns an all-black frame of the last size
                instead of raising: a closed popup shows nothing. It never
                shows another window, every screen postcondition fails on it,
                and acting elsewhere still needs an explicit surface switch.
                The primary page keeps the default (a closed page raises).
            bring_to_front_before_capture: Make ``page`` the browser's
                foreground tab before each screenshot. Chromium draws no new
                frames for a background tab, so a capture of one can wait
                forever (Playwright's own launch disables that throttling with
                command-line flags; a person's browser reached over CDP has no
                such flags). Recording in an attached browser sets this: a
                popup or tab the recorded page opens takes the foreground, and
                the opener's after-frame must still be a live, current frame.
        """
        self.page = page
        self._closed_page_frame = closed_page_frame
        self._bring_to_front_before_capture = bring_to_front_before_capture
        self._last_frame_size: Optional[tuple[int, int]] = None
        # JavaScript dialogs (docs/MULTI_SURFACE.md, "Dialogs"): no listener
        # is registered until a step arms one, so a bundle without dialog
        # steps keeps Playwright's default (dismiss) exactly as before.
        self._dialog_listening = False
        self._dialog_armed: Optional[
            tuple[str, Optional[str], Optional[re.Pattern[str]]]
        ] = None
        self._dialogs_seen: list[dict[str, Any]] = []
        self._screenshot_scale = screenshot_scale
        self._screenshot_mask_selectors = screenshot_mask_selectors
        self._structural_state_reader = structural_state_reader
        self._screenshot_guard = screenshot_guard
        self._screenshot_frame_generation = 0
        self._screenshot_frame_listener = self._handle_screenshot_frame_lifecycle
        self._screenshot_frame_tracking = False
        if self._screenshot_mask_selectors:
            for event in ("frameattached", "framedetached", "framenavigated"):
                self.page.on(event, self._screenshot_frame_listener)
            self._screenshot_frame_tracking = True
        # Opaque per-backend key keeps the WeakMap private from ordinary page
        # code. Python retains only token material keyed by the public
        # SHA-256 fingerprint; target/row text stays page-local and ephemeral.
        self._structural_store_key = f"__oaflow_structural_{uuid.uuid4().hex}"
        # Fields this backend typed into, kept page-side per document (element
        # references only) for submit bindings. A new backend starts empty.
        self._fill_store_key = f"__oaflow_filled_{uuid.uuid4().hex}"
        self._structural_tokens: dict[str, _StructuralGuard] = {}
        self._guarded_coordinate: Optional[_CoordinateGuard] = None
        # One-shot expectation that the next click closes this page surface
        # (Step.closes_surface; see expect_page_close).
        self._page_close_expected = False
        self._guarded_keyboard: Optional[_KeyboardGuard] = None
        self._qualification_environment: Optional[tuple[str, str, str, str]] = None
        self._qualification_input_guard: Optional[Callable[[], None]] = None
        self._presentation_viewport_loaded = False
        self._presentation_viewport: Optional[tuple[int, int, float]] = None
        # DOM + network quiescence signal for the browser settle (see
        # openadapt_flow.backends._page_quiescence). Armed at construction:
        # network requests issued before this point are not tracked.
        self._quiescence = PageQuiescence(page)
        # The server's answers to write requests, kept as save evidence (see
        # openadapt_flow.backends._server_responses). Never a check.
        self._server_responses = ServerResponseLog(page)

    def server_response_mark(self) -> int:
        """Where the log of server answers to write requests stands now."""
        return self._server_responses.mark()

    def server_responses_since(self, mark: int) -> list[Any]:
        """Server answers to the page's own write requests sent after
        ``mark``; never blocks on a response body."""
        return self._server_responses.since(mark)

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the page viewport in pixels."""
        size = self.page.viewport_size
        if size is None:
            # A Chromium page reached through ``connect_over_cdp`` normally
            # has no Playwright viewport emulation. Reading the live CSS
            # viewport avoids both the old fixed 1280x800 fallback and any
            # mutation of the operator's browser window.
            try:
                live = self.page.evaluate(
                    "() => ({width: window.innerWidth, height: window.innerHeight})"
                )
                width = int(live["width"])
                height = int(live["height"])
                if width > 0 and height > 0:
                    return (width, height)
            except Exception:
                pass
            return VIEWPORT
        return (size["width"], size["height"])

    def browser_presentation_viewport(self) -> Optional[tuple[int, int, float]]:
        """Return exact top-level CSS geometry for an external overlay.

        This is presentation metadata only.  It neither injects into the page
        nor exposes a selector or target identity, and replay never consumes it
        for resolution, actuation, or verification.
        """

        size = self.page.viewport_size
        if size is None:
            return None
        dimensions = (int(size["width"]), int(size["height"]))
        if self._presentation_viewport_loaded:
            cached = self._presentation_viewport
            if cached is None or cached[:2] != dimensions:
                return None
            return cached
        self._presentation_viewport_loaded = True
        try:
            dpr = float(self.page.evaluate("() => window.devicePixelRatio"))
            if not math.isfinite(dpr) or dpr <= 0:
                return None
            self._presentation_viewport = (*dimensions, dpr)
        except Exception:
            self._presentation_viewport = None
        return self._presentation_viewport

    # -- structural observations (openadapt_flow.backend.StructuralBackend) --

    @property
    def url(self) -> Optional[str]:
        """Current page URL, or None if momentarily unobservable."""
        if self._structural_state_reader is not None:
            try:
                value = self._structural_state_reader().get("url")
                return value if isinstance(value, str) else None
            except Exception:
                return None
        try:
            return self.page.url
        except Exception:
            return None

    @property
    def page_title(self) -> Optional[str]:
        """Current page title, or None if momentarily unobservable."""
        if self._structural_state_reader is not None:
            try:
                value = self._structural_state_reader().get("title")
                return value if isinstance(value, str) else None
            except Exception:
                return None
        try:
            return self.page.title()
        except Exception:
            return None

    @property
    def page_count(self) -> Optional[int]:
        """Open pages in the browser context (new tabs are visible here even
        though the single-page screenshot never shows them)."""
        try:
            return sum(1 for page in self.page.context.pages if not page.is_closed())
        except Exception:
            return None

    def wait_for_page_count_above(self, count: int, timeout_s: float) -> bool:
        """Wait on the context's ``page`` event until more than ``count``
        pages are open, or ``timeout_s`` passes. True when they are.

        Event-driven on purpose: with the sync API a sleep dispatches
        nothing, so a popup a click opened can stay out of
        ``context.pages`` until the next Playwright call.
        """

        import time as _time

        deadline = _time.monotonic() + max(0.0, float(timeout_s))
        while True:
            current = self.page_count
            if current is not None and current > count:
                return True
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return False
            try:
                self.page.context.wait_for_event(
                    "page", timeout=max(1.0, remaining * 1000.0)
                )
            except Exception:
                # Timed out (or the context closed): one last look above.
                current = self.page_count
                return current is not None and current > count

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
        guards: list[_StructuralGuard | _CoordinateGuard | _KeyboardGuard] = list(
            self._structural_tokens.values()
        )
        if self._guarded_coordinate is not None:
            guards.append(self._guarded_coordinate)
        if self._guarded_keyboard is not None:
            guards.append(self._guarded_keyboard)
        for guard in guards:
            guard.context[kind] = value
        return True

    @staticmethod
    def _origin_from_url(current_url: object) -> Optional[str]:
        """Normalize one observed browser URL to its exact origin."""

        try:
            if not isinstance(current_url, str):
                return None
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

    def _application_identity_value(self) -> Optional[str]:
        """Observe the top-level origin without mutating any pending guard."""

        try:
            current_url = self.page.url
        except Exception:
            return None
        return self._origin_from_url(current_url)

    def _qualification_environment_values(
        self,
    ) -> Optional[tuple[str, str, str, str]]:
        """Read origin, version, and session in one top-level JS observation."""

        try:
            payload = self.page.evaluate(
                """() => ({
                    href: window.location.href,
                    versions: Array.from(document.head.querySelectorAll(
                        'meta[name="openadapt-application-version"]'
                    ), node => node.getAttribute('content')),
                    sessions: Array.from(document.head.querySelectorAll(
                        'meta[name="openadapt-session-identity"]'
                    ), node => node.getAttribute('content')),
                    environments: Array.from(document.head.querySelectorAll(
                        'meta[name="openadapt-environment-identity"]'
                    ), node => node.getAttribute('content')),
                })"""
            )
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        origin = self._origin_from_url(payload.get("href"))
        versions = payload.get("versions")
        sessions = payload.get("sessions")
        environments = payload.get("environments")
        if (
            origin is None
            or not isinstance(versions, list)
            or len(versions) != 1
            or not isinstance(versions[0], str)
            or _APPLICATION_VERSION_PATTERN.fullmatch(versions[0]) is None
            or not isinstance(sessions, list)
            or len(sessions) != 1
            or not isinstance(sessions[0], str)
            or _SESSION_IDENTITY_PATTERN.fullmatch(sessions[0]) is None
            or not isinstance(environments, list)
            or len(environments) != 1
            or not isinstance(environments[0], str)
            or _SESSION_IDENTITY_PATTERN.fullmatch(environments[0]) is None
        ):
            return None
        return origin, versions[0], sessions[0], environments[0]

    def qualification_environment_identity(
        self,
    ) -> Optional[tuple[str, str, str, str]]:
        """Bind one atomic environment observation for qualification replay."""

        observed = self._qualification_environment_values()
        if observed is None:
            return None
        self._qualification_environment = observed
        return observed

    def _assert_qualification_environment_current(self) -> None:
        """Refuse every input edge after a bound qualification context changes."""

        if self._qualification_input_guard is not None:
            self._qualification_input_guard()
        expected = self._qualification_environment
        if (
            expected is not None
            and self._qualification_environment_values() != expected
        ):
            raise StructuralResolutionRefused(
                "qualification browser environment changed before input"
            )

    def set_qualification_input_guard(
        self, guard: Optional[Callable[[], None]]
    ) -> None:
        """Install or clear the run-scoped qualification input guard."""

        self._qualification_input_guard = guard

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

    def application_version_identity(self) -> Optional[str]:
        """Return an exact, application-owned version marker from ``<head>``."""

        value = self._context_meta_content(_APPLICATION_VERSION_META)
        observed = (
            value
            if value is not None and _APPLICATION_VERSION_PATTERN.fullmatch(value)
            else None
        )
        if observed is None:
            return None
        return (
            observed
            if self._bind_context_identity("application_version", observed)
            else None
        )

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

    def _context_guard_is_current(
        self,
        guard: _StructuralGuard | _CoordinateGuard | _KeyboardGuard,
    ) -> bool:
        """Re-read every bound top-level identity immediately before input."""

        if self._qualification_environment is not None and (
            self._qualification_environment_values() != self._qualification_environment
        ):
            return False
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
            elif kind == "application_version":
                value = self._context_meta_content(_APPLICATION_VERSION_META)
                observed = (
                    value
                    if value is not None
                    and _APPLICATION_VERSION_PATTERN.fullmatch(value)
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
        The one exception is a data-entry FORM FIELD (input / select /
        textarea, never a button) inside a small labeled group: its identity
        is the group's label text plus the current values of the group's
        OTHER controls, excluding the field itself (see
        ``_FIELD_GROUP_IDENTITY_JS``) -- the DOM counterpart of the OCR band
        read across the field's row.
        Whitespace is collapsed. Returns None when nothing is under the point
        or on any evaluation failure (never raises) -- the identity ladder then
        falls back to the OCR tier.
        """
        try:
            point = self._frame_point(int(x), int(y))
            if point is None:
                return None
            result = point.scope.evaluate(
                _STRUCTURED_TEXT_AT_JS,
                [point.x, point.y],
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
            point = self._frame_point(int(x), int(y))
            if point is None:
                return None
            result = point.scope.evaluate(_TEXT_VALUE_AT_JS, [point.x, point.y])
        except Exception:
            return None
        return result if isinstance(result, str) else None

    def displayed_text_at(self, x: int, y: int) -> Optional[str]:
        """Return the text the element under ``(x, y)`` shows, from the DOM.

        A read-only structural observation for a demonstrated read-back. Never
        logged. Returns None when the point hits nothing or evaluation fails.
        """
        try:
            point = self._frame_point(int(x), int(y))
            if point is None:
                return None
            result = point.scope.evaluate(_DISPLAYED_TEXT_AT_JS, [point.x, point.y])
        except Exception:
            return None
        return result if isinstance(result, str) else None

    def focused_text_value(self) -> Optional[str]:
        """Return the exact value of the currently focused editable control."""

        return self._focused_element_eval(_EDITABLE_VALUE_JS)

    def focused_field_label(self) -> Optional[str]:
        """Return the focused field's best available human label, or None.

        RECORD-TIME seam (:class:`openadapt_flow.backend.FieldLabelBackend`):
        when the demonstrator types, the recorder captures the receiving
        field's label -- associated DOM ``<label>``, ``aria-label`` /
        ``aria-labelledby``, ``placeholder``, ``name`` attribute, or ``title``,
        best-first -- as passive evidence for the compile-time
        parameter-proposal pass. A cheap read-only DOM query on the focused
        element (same frame-descent discipline as :meth:`focused_text_value`);
        never called at replay, never raises.
        """

        return self._focused_element_eval(_FIELD_LABEL_JS)

    def _focused_element_eval(self, js: str) -> Optional[str]:
        """Evaluate ``js`` on the focused element, descending nested frames.

        The focused element is bound in its EXACT document/frame context: at
        each iframe/frame boundary the descent re-proves the frame chain
        (selector uniqueness, identity, geometry) before entering, mirroring
        :meth:`_frame_point`'s fail-closed posture. Returns the string result
        or None on any mismatch/failure (never raises).
        """

        result = self._focused_element_call(js)
        return result if isinstance(result, str) else None

    def _register_focused_field(self) -> None:
        """Remember the focused field as one this backend is typing into.

        Submit bindings relate an unlabeled control to the fields the program
        filled (:mod:`openadapt_flow.submit_binding`). A failure here is not
        an error: a field that was not registered makes a later binding check
        refuse, which is the safe direction.
        """

        self._focused_element_call(
            _REGISTER_FILLED_FIELD_JS, arg=self._fill_store_key, with_arg=True
        )

    def _focused_element_call(
        self, js: str, *, arg: Any = None, with_arg: bool = False
    ) -> Any:
        """Evaluate ``js`` on the exactly bound focused element (see above).

        Returns the raw result, or None on any frame mismatch or failure.
        """

        scope: Any = self.page
        frame_path: list[str] = []
        try:
            for _depth in range(9):
                focused = scope.evaluate_handle(
                    "() => document.activeElement"
                ).as_element()
                if focused is None:
                    return None
                candidate = None
                try:
                    tag = focused.evaluate("el => el.localName")
                    if tag not in {"iframe", "frame"}:
                        box = focused.bounding_box()
                        if not isinstance(box, dict) or not self._frame_chain_matches(
                            scope, tuple(frame_path), box
                        ):
                            return None
                        if with_arg:
                            return focused.evaluate(js, arg)
                        return focused.evaluate(js)
                    if len(frame_path) >= 8:
                        return None
                    selector = focused.evaluate(_FRAME_SELECTOR_JS)
                    if (
                        not isinstance(selector, str)
                        or not selector
                        or len(selector) > 512
                    ):
                        return None
                    matches = scope.locator(selector)
                    if matches.count() != 1:
                        return None
                    candidate = matches.element_handle()
                    if candidate is None or not focused.evaluate(
                        "(el, expected) => el === expected", candidate
                    ):
                        return None
                    child = focused.content_frame()
                    if child is None:
                        return None
                    frame_path.append(selector)
                    scope = child
                finally:
                    if candidate is not None:
                        candidate.dispose()
                    focused.dispose()
            return None
        except Exception:
            return None

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
                # The handle's region is the target's VISIBLE box: its
                # bounding box clipped to the viewport. A control that runs a
                # few pixels past the viewport edge (a full-width section
                # header at the bottom of the screen) is still evidenced only
                # by the part the retained frame shows; the center point was
                # already required to be inside the viewport above.
                bx = int(round(float(box["x"])))
                by = int(round(float(box["y"])))
                bw = int(round(float(box["width"])))
                bh = int(round(float(box["height"])))
                x0, y0 = max(0, bx), max(0, by)
                x1, y1 = min(vw, bx + bw), min(vh, by + bh)
                if x1 <= x0 or y1 <= y0:
                    self._cleanup_guard(token, scope)
                    return None
                region = (x0, y0, x1 - x0, y1 - y0)
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
                supported_operations=["dom_click", "dom_double_click", "dom_drag"],
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

    def _point_guard_is_current(
        self,
        guard: _CoordinateGuard | _KeyboardGuard,
        locator: Any,
        *,
        require_focused: bool = False,
    ) -> bool:
        """Re-prove a top-level point, child scope, token, and live context."""

        if not self._context_guard_is_current(guard):
            return False
        current = self._frame_point(*guard.point)
        if (
            current is None
            or current.scope != guard.scope
            or current.frame_path != guard.frame_path
            or not math.isclose(
                current.x, guard.local_point[0], rel_tol=0.0, abs_tol=1e-6
            )
            or not math.isclose(
                current.y, guard.local_point[1], rel_tol=0.0, abs_tol=1e-6
            )
        ):
            return False
        try:
            return bool(
                locator.count() == 1
                and self._guard_is_current(
                    locator,
                    guard.token,
                    require_focused=require_focused,
                )
                and locator.evaluate(
                    _GUARD_CONTAINS_POINT_JS,
                    [current.x, current.y],
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
        unchanged complete bounded DOM descriptor. The same checks are repeated
        after Playwright's actionability trial and immediately before the real
        input, so transient framework presentation churn is admitted only when
        the exact same Element and DOM state have been restored. The final
        action is a Playwright click/dblclick on that unique random-token
        locator, preserving Playwright's native pointer sequence rather than
        synthesizing DOM events. A replacement element, lasting hidden
        attribute change, or changed record row is a refusal, never a coordinate
        fallback.
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
            # This is the last no-input boundary.  The externally supplied
            # qualification observer can cover context that the target page
            # cannot declare (for example a browser inside a managed remote
            # session), so the DOM guard alone is not sufficient.
            self._assert_qualification_environment_current()
            close_expected = self._take_page_close_expectation()
            open_before = not self._page_is_closed()
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
                # runtime verifies the effect exactly once and never retries,
                # unless this page was expected to close itself on this click.
                if not (
                    _is_target_closed_error(exc)
                    and self._closed_itself_as_expected(close_expected, open_before)
                ):
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

    def drag_structural_guarded(
        self,
        source_locator: StructuralLocator,
        source_handle: StructuralHandle,
        destination_locator: StructuralLocator,
        destination_handle: StructuralHandle,
    ) -> ActionDeliveryReceipt:
        """Drag between two exact DOM elements retained from one resolution pass."""

        source_fingerprint = source_handle.target_fingerprint
        destination_fingerprint = destination_handle.target_fingerprint
        if (
            source_fingerprint is None
            or destination_fingerprint is None
            or source_fingerprint == destination_fingerprint
        ):
            raise StructuralResolutionRefused(
                "guarded DOM drag requires two distinct target fingerprints"
            )
        source_guard = self._structural_tokens.pop(source_fingerprint, None)
        destination_guard = self._structural_tokens.pop(destination_fingerprint, None)
        if source_guard is None or destination_guard is None:
            for guard in (source_guard, destination_guard):
                if guard is not None:
                    self._cleanup_guard(guard.token, guard.scope)
            raise StructuralResolutionRefused(
                "guarded DOM drag endpoint is missing, stale, or already consumed"
            )

        def current_token_locator(
            locator: StructuralLocator,
            guard: _StructuralGuard,
        ) -> Any:
            resolved = self._locator_with_scope(locator)
            if resolved is None:
                raise StructuralResolutionRefused(
                    "guarded DOM drag endpoint no longer resolves uniquely"
                )
            scope, candidate = resolved
            if (
                scope != guard.scope
                or tuple(locator.frame_path or ()) != guard.frame_path
            ):
                raise StructuralResolutionRefused(
                    "guarded DOM drag frame context changed before delivery"
                )
            if (
                not self._context_guard_is_current(guard)
                or candidate.count() != 1
                or not self._guard_is_current(candidate, guard.token)
            ):
                raise StructuralResolutionRefused(
                    "guarded DOM drag endpoint, record, or context changed before delivery"
                )
            token_locator = self._token_locator(guard.token, guard.scope)
            if token_locator.count() != 1:
                raise StructuralResolutionRefused(
                    "guarded DOM drag token is missing or ambiguous at delivery"
                )
            return token_locator

        try:
            source = current_token_locator(source_locator, source_guard)
            destination = current_token_locator(destination_locator, destination_guard)
            try:
                source.drag_to(destination, timeout=1000, trial=True)
            except Exception as exc:
                raise StructuralResolutionRefused(
                    "guarded DOM drag endpoints were unactionable during pre-dispatch trial"
                ) from exc
            source = current_token_locator(source_locator, source_guard)
            destination = current_token_locator(destination_locator, destination_guard)
            self._assert_qualification_environment_current()
            try:
                source.drag_to(destination, timeout=1000)
            except Exception as exc:
                raise ActionDeliveryUncertain(
                    operation="guarded_dom_drag",
                    native=False,
                    target_fingerprint=source_fingerprint,
                    cause_type=type(exc).__name__,
                ) from exc
        except ActionDeliveryUncertain:
            raise
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "guarded DOM drag endpoints changed before delivery"
            ) from exc
        finally:
            self._cleanup_guard(source_guard.token, source_guard.scope)
            self._cleanup_guard(destination_guard.token, destination_guard.scope)

        return ActionDeliveryReceipt(
            receipt_id=f"playwright-{uuid.uuid4().hex}",
            operation="guarded_dom_drag",
            native=False,
            target_fingerprint=structural_resolution_fingerprint(
                source_locator,
                source_handle,
            ),
            destination_fingerprint=structural_resolution_fingerprint(
                destination_locator,
                destination_handle,
            ),
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def _arm_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        allow_editable: bool,
        submit_binding: Optional[dict[str, str]] = None,
    ) -> None:
        self.cancel_guarded_coordinate()
        point = (int(x), int(y))
        token = uuid.uuid4().hex
        scope: Any = self.page
        try:
            frame_point = self._frame_point(*point)
            if frame_point is None:
                raise StructuralResolutionRefused(
                    "visual point has no exact hit-tested DOM frame context"
                )
            scope = frame_point.scope
            observed = scope.evaluate(
                _BIND_COORDINATE_TARGET_JS,
                {
                    "storeKey": self._structural_store_key,
                    "tokenAttribute": self._token_attribute(token),
                    "token": token,
                    "x": frame_point.x,
                    "y": frame_point.y,
                    "requireRowIdentity": True,
                    "allowEditable": allow_editable,
                    "submitBinding": submit_binding,
                },
            )
            if not isinstance(observed, dict):
                raise StructuralResolutionRefused(
                    "the unlabeled control changed while its submit binding "
                    "was being leased"
                    if submit_binding is not None
                    else "visual point is not an identity-bearing actionable "
                    "DOM element"
                )
            offset = observed.get("offset")
            bound_point = observed.get("point")
            if (
                not isinstance(offset, list)
                or len(offset) != 2
                or not all(isinstance(value, (int, float)) for value in offset)
                or not isinstance(bound_point, list)
                or len(bound_point) != 2
                or not all(isinstance(value, (int, float)) for value in bound_point)
            ):
                raise StructuralResolutionRefused(
                    "visual DOM actuation could not bind the resolved point"
                )
            fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest()
            self._guarded_coordinate = _CoordinateGuard(
                point=point,
                local_point=(float(bound_point[0]), float(bound_point[1])),
                fingerprint=fingerprint,
                token=token,
                offset=(float(offset[0]), float(offset[1])),
                allow_editable=allow_editable,
                scope=scope,
                frame_path=frame_point.frame_path,
            )
        except Exception:
            self._cleanup_guard(token, scope)
            raise

    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        """Bind an actionable visual point before identity readback."""

        self._arm_guarded_coordinate(x, y, allow_editable=False)

    def arm_guarded_editable_coordinate(self, x: int, y: int) -> None:
        """Bind an editable field for an identity-gated focusing click."""

        self._arm_guarded_coordinate(x, y, allow_editable=True)

    # -- submit binding (openadapt_flow.backend.SubmitBindingBackend) --------

    def _observe_submit_binding(self, x: int, y: int) -> Optional[dict[str, Any]]:
        """Raw page observation (``{observation, canon}``) at a point."""

        point = self._frame_point(int(x), int(y))
        if point is None:
            return None
        raw = point.scope.evaluate(
            _SUBMIT_BINDING_AT_JS,
            {"x": point.x, "y": point.y, "fillKey": self._fill_store_key},
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("canon"), str):
            return None
        return raw

    def submit_binding_at(self, x: int, y: int) -> Optional[dict[str, Any]]:
        """Observe how the control at ``(x, y)`` relates to the filled fields.

        RECORD-TIME seam. Returns the PHI-free structural observation of
        :mod:`openadapt_flow.submit_binding` (the icon geometry already
        replaced by its SHA-256), or None when no actionable control is at the
        point or the page cannot be read. Never raises.
        """

        try:
            raw = self._observe_submit_binding(x, y)
            if raw is None:
                return None
            observation = observation_from_page(raw.get("observation"))
        except Exception:
            return None
        return observation.model_dump(mode="json", exclude_none=True)

    def arm_guarded_submit_coordinate(
        self, x: int, y: int, *, binding: SubmitBinding
    ) -> None:
        """Arm a guarded click whose record tie is a verified submit binding.

        The control at the resolved point must yield an observation that
        matches ``binding`` (form ownership, submit role, position relative to
        the fields this backend typed into, icon, and uniqueness). The lease
        then holds that exact observation: delivery refuses if the element,
        the fields, or any other unlabeled control after them changes.

        Raises:
            StructuralResolutionRefused: when there is no control, the page
                cannot be read, or the live binding differs from ``binding``.
        """

        self.cancel_guarded_coordinate()
        try:
            raw = self._observe_submit_binding(x, y)
        except Exception as exc:
            raise StructuralResolutionRefused(
                "could not read the page structure to verify the submit binding"
            ) from exc
        if raw is None:
            raise StructuralResolutionRefused(
                "no actionable control at the resolved point to verify the "
                "submit binding against"
            )
        try:
            observation = observation_from_page(raw.get("observation"))
        except ValueError as exc:
            raise StructuralResolutionRefused(
                "the page returned a malformed submit-binding observation"
            ) from exc
        reason = verify_submit_binding(binding, observation)
        if reason is not None:
            raise StructuralResolutionRefused(
                "the unlabeled control at the resolved point is not bound to "
                f"the form this run filled: {reason}"
            )
        self._arm_guarded_coordinate(
            x,
            y,
            allow_editable=False,
            submit_binding={
                "fillKey": self._fill_store_key,
                "expected": str(raw["canon"]),
            },
        )

    def cancel_guarded_coordinate(self) -> None:
        """Cancel and clean the current one-shot visual DOM binding."""

        pending = self._guarded_coordinate
        self._guarded_coordinate = None
        if pending is not None:
            self._cleanup_guard(pending.token, pending.scope)

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
        button: str = "left",
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
        if button not in {"left", "right"}:
            self._cleanup_guard(pending.token, pending.scope)
            raise StructuralResolutionRefused(
                f"unsupported guarded pointer button {button!r}"
            )
        if double and button != "left":
            self._cleanup_guard(pending.token, pending.scope)
            raise StructuralResolutionRefused(
                "guarded right-button double click is not supported"
            )
        operation = (
            "guarded_coordinate_double_click"
            if double
            else (
                "guarded_coordinate_right_click"
                if button == "right"
                else "guarded_coordinate_click"
            )
        )
        try:
            if pending.point != point:
                raise StructuralResolutionRefused(
                    "visual DOM actuation point changed after target binding"
                )
            # Retain the protocol argument for cross-backend compatibility.
            # Browser delivery is bound by the stronger target/record/context
            # lease below; remote pixel backends continue to enforce their
            # exact fresh-frame lease.
            del expected_frame_sha256
            token_locator = self._token_locator(pending.token, pending.scope)
            if not self._point_guard_is_current(
                pending,
                token_locator,
            ):
                raise StructuralResolutionRefused(
                    "visual target, frame, record, or context changed before delivery"
                )
            position = {"x": pending.offset[0], "y": pending.offset[1]}
            try:
                if double:
                    token_locator.dblclick(
                        position=position,
                        timeout=1000,
                        trial=True,
                    )
                else:
                    token_locator.click(
                        position=position,
                        timeout=1000,
                        trial=True,
                        button=button,
                    )
            except Exception as exc:
                raise StructuralResolutionRefused(
                    "identity-bound visual target was unactionable during its "
                    "pre-dispatch trial"
                ) from exc
            if not self._point_guard_is_current(
                pending,
                token_locator,
            ):
                raise StructuralResolutionRefused(
                    "visual target, frame, record, or context changed after the "
                    "pre-dispatch actionability trial"
                )
            self._assert_qualification_environment_current()
            close_expected = self._take_page_close_expectation()
            open_before = not self._page_is_closed()
            try:
                if double:
                    token_locator.dblclick(position=position, timeout=1000)
                else:
                    token_locator.click(
                        position=position,
                        timeout=1000,
                        button=button,
                    )
            except Exception as exc:
                if not (
                    _is_target_closed_error(exc)
                    and self._closed_itself_as_expected(close_expected, open_before)
                ):
                    raise ActionDeliveryUncertain(
                        operation=operation,
                        native=False,
                        target_fingerprint=pending.fingerprint,
                        cause_type=type(exc).__name__,
                    ) from exc
        except ActionDeliveryUncertain:
            raise
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "identity-bound visual target became unactionable before delivery"
            ) from exc
        finally:
            self._cleanup_guard(pending.token, pending.scope)
        return ActionDeliveryReceipt(
            receipt_id=f"playwright-coordinate-{uuid.uuid4().hex}",
            operation=operation,
            native=False,
            target_fingerprint=pending.fingerprint,
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
        scope: Any = self.page
        try:
            point = (int(x), int(y))
            frame_point = self._frame_point(*point)
            if frame_point is None:
                raise StructuralResolutionRefused(
                    "consequential keyboard action has no exact hit-tested "
                    "DOM frame context"
                )
            scope = frame_point.scope
            observed = scope.evaluate(
                _BIND_FOCUSED_TARGET_JS,
                {
                    "storeKey": self._structural_store_key,
                    "tokenAttribute": self._token_attribute(token),
                    "token": token,
                    "x": frame_point.x,
                    "y": frame_point.y,
                    "requireRowIdentity": True,
                    "requireFocused": True,
                    "admitFormField": bool(self.keyboard_admits_form_fields),
                },
            )
            if not isinstance(observed, dict):
                raise StructuralResolutionRefused(
                    "consequential keyboard action has no focused "
                    "identity-bearing DOM target at the resolved point"
                )
            bound_point = observed.get("point")
            if (
                not isinstance(bound_point, list)
                or len(bound_point) != 2
                or not all(isinstance(value, (int, float)) for value in bound_point)
            ):
                raise StructuralResolutionRefused(
                    "focused keyboard target could not bind the resolved point"
                )
            fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest()
            self._guarded_keyboard = _KeyboardGuard(
                point=point,
                local_point=(float(bound_point[0]), float(bound_point[1])),
                fingerprint=fingerprint,
                token=token,
                scope=scope,
                frame_path=frame_point.frame_path,
            )
        except Exception:
            self._cleanup_guard(token, scope)
            raise

    def cancel_guarded_keyboard(self) -> None:
        """Cancel and clean the current one-shot focused-element lease."""

        pending = self._guarded_keyboard
        self._guarded_keyboard = None
        if pending is not None:
            self._cleanup_guard(pending.token, pending.scope)

    def guarded_keyboard_frame(self) -> bytes:
        """Capture a caret-stable frame without mutating the focused field.

        ``caret="initial"`` prevents Playwright from toggling inline styles on
        the editable element inside a guarded record row. A temporary
        screenshot stylesheet hides the caret from the pixels and stops it
        blinking (``caret-animation: manual``, ignored by browsers that don't
        know it), so consecutive captures provide a stable resolver/identity
        observation; the stylesheet is injected outside the guarded row.
        Without the second rule a transparent caret still blinks, and each
        blink repaints: in Chromium consecutive frames then differ in a few
        anti-aliased pixels, so a frame digest taken by one capture didn't
        match the next one (a readiness drill's signed frame, for example).
        """

        def capture() -> bytes:
            options: dict[str, Any] = {}
            if self._screenshot_scale == "css":
                options["scale"] = "css"
            return self.page.screenshot(
                type="png",
                full_page=False,
                caret="initial",
                style=(
                    "* { caret-color: transparent !important; "
                    "caret-animation: manual !important; }"
                ),
                **options,
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
        try:
            # Retain the protocol argument for cross-backend compatibility.
            # The browser's one-shot focused-element/record/context lease is
            # the authoritative race guard. Full-frame byte equality is both
            # weaker semantically and unstable around caret painting.
            del expected_frame_sha256
            token_locator = self._token_locator(pending.token, pending.scope)
            if not self._point_guard_is_current(
                pending,
                token_locator,
                require_focused=True,
            ):
                raise StructuralResolutionRefused(
                    "focused keyboard target, frame, record, or context changed "
                    "before delivery"
                )
            self._assert_qualification_environment_current()
            try:
                deliver(token_locator)
            except Exception as exc:
                raise ActionDeliveryUncertain(
                    operation=operation,
                    native=False,
                    target_fingerprint=pending.fingerprint,
                    cause_type=type(exc).__name__,
                ) from exc
        except ActionDeliveryUncertain:
            raise
        except StructuralResolutionRefused:
            raise
        except Exception as exc:
            raise StructuralResolutionRefused(
                "identity-bound keyboard target became unactionable before delivery"
            ) from exc
        finally:
            self._cleanup_guard(pending.token, pending.scope)
        return ActionDeliveryReceipt(
            receipt_id=f"playwright-keyboard-{uuid.uuid4().hex}",
            operation=operation,
            native=False,
            target_fingerprint=pending.fingerprint,
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

        self._register_focused_field()
        return self._act_guarded_keyboard(
            expected_frame_sha256=expected_frame_sha256,
            operation="guarded_dom_type",
            deliver=lambda locator: locator.press_sequentially(text, timeout=1000),
        )

    def _handle_screenshot_frame_lifecycle(self, _frame: Any = None) -> None:
        """Advance the irreversible frame-tree generation."""

        self._screenshot_frame_generation += 1

    def stop_screenshot_mask_tracking(self) -> None:
        """Remove recording-only frame listeners from an external page."""

        if not self._screenshot_frame_tracking:
            return
        for event in ("frameattached", "framedetached", "framenavigated"):
            try:
                self.page.remove_listener(event, self._screenshot_frame_listener)
            except Exception:
                pass
        self._screenshot_frame_tracking = False

    @staticmethod
    def _same_frames(left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
        return len(left) == len(right) and all(
            before is after for before, after in zip(left, right)
        )

    def focused_select_label_at(self, x: int, y: int) -> Optional[str]:
        """Selected option label of the FOCUSED ``<select>`` under ``(x, y)``.

        ``None`` unless the hit-tested element at the point (through the real
        frame chain) is a select that also holds focus. Used to verify typed
        input into a dropdown without OCR; never raises.
        """
        try:
            point = self._frame_point(int(x), int(y))
            if point is None:
                return None
            out = point.scope.evaluate(_FOCUSED_SELECT_AT_JS, [point.x, point.y])
        except Exception:
            return None
        return out if isinstance(out, str) else None

    def page_change_marker(self) -> Optional[str]:
        """Marker of the last change seen by the latest quiet probe, or None.

        Two quiet probes with the same marker prove nothing in the page (DOM,
        events, network, frames) changed between them.
        """
        tracker = getattr(self, "_quiescence", None)
        return getattr(tracker, "marker", None) if tracker is not None else None

    def visible_text_present(self, text: str) -> Optional[bool]:
        """Is ``text`` rendered visibly in the viewport, per the DOM?

        Returns ``True`` only when some element's rendered text (``innerText``)
        contains ``text`` as an exact case-insensitive substring once all
        whitespace is removed (the OCR tier's squash; recorded OCR texts often
        lose spaces), and
        that element is not hidden or transparent, intersects its document's
        viewport, AND a hit test through the real frame chain at that point
        lands on it (so text under a dialog, clipped by its frame, or scrolled
        out of view does not count). Otherwise ``None``: the DOM could not
        prove presence, and the caller must fall back to its visual check. It
        never answers ``False``; a DOM miss is not evidence of absence (text
        can be painted by canvas or images). Never raises.
        """
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            frames = [f for f in self.page.frames if not f.is_detached()]
        except Exception:
            return None
        main = self.page.main_frame
        for frame in frames:
            try:
                candidates = frame.evaluate(_VISIBLE_TEXT_CANDIDATES_JS, text)
            except Exception:
                continue
            if not candidates:
                continue
            offset = (0.0, 0.0)
            if frame is not main:
                try:
                    handle = frame.frame_element()
                    box = handle.bounding_box()
                    inner = handle.evaluate(
                        "el => [el.clientLeft + parseFloat(getComputedStyle(el)"
                        ".paddingLeft || 0), el.clientTop + parseFloat("
                        "getComputedStyle(el).paddingTop || 0)]"
                    )
                except Exception:
                    continue
                if not box:
                    continue
                offset = (box["x"] + float(inner[0]), box["y"] + float(inner[1]))
            for cx, cy in candidates:
                x = int(round(cx + offset[0]))
                y = int(round(cy + offset[1]))
                try:
                    point = self._frame_point(x, y)
                except Exception:
                    point = None
                if point is None:
                    continue
                scope_frame = main if point.scope is self.page else point.scope
                if scope_frame != frame:
                    continue
                try:
                    if point.scope.evaluate(
                        _TEXT_AT_POINT_JS, [point.x, point.y, text]
                    ):
                        return True
                except Exception:
                    continue
        return None

    def page_quiescent(self, window_s: float) -> Optional[bool]:
        """Has the page been quiet (DOM, events, animations, network) for
        ``window_s``, with the result rendered?

        ``True`` only when every frame and the network were quiet for the whole
        window with at least two frames rendered since the last change; ``False``
        while anything is still changing; ``None`` when the page cannot be
        judged from page signals (visible canvas/video, evaluation failure),
        in which case the caller must use the screenshot settle. Never raises.
        """
        tracker = getattr(self, "_quiescence", None)
        if tracker is None:
            return None
        return tracker.probe(window_s)

    def _capture_across_navigation(self, options: dict[str, Any]) -> bytes:
        """Capture the viewport, retrying only failures caused by a navigation.

        The screenshot guard runs before every attempt, so a retry can never
        capture a page the guard would refuse. Any other error, or a
        transient one that persists past the deadline, propagates.
        """
        deadline = time.monotonic() + _NAVIGATION_SCREENSHOT_TIMEOUT_S
        while True:
            if self._screenshot_guard is not None:
                self._screenshot_guard()
            try:
                return self.page.screenshot(
                    type="png",
                    full_page=False,
                    timeout=_NAVIGATION_SCREENSHOT_ATTEMPT_TIMEOUT_MS,
                    **options,
                )
            except Exception as error:
                message = str(error)
                if (
                    not any(m in message for m in _TRANSIENT_SCREENSHOT_ERRORS)
                    or time.monotonic() >= deadline
                ):
                    raise
            try:
                self.page.wait_for_load_state(
                    "load", timeout=_NAVIGATION_SCREENSHOT_ATTEMPT_TIMEOUT_MS
                )
            except Exception:
                pass
            time.sleep(_MASKED_SCREENSHOT_RETRY_SLEEP_S)

    def screenshot(self) -> bytes:
        """Return a stable current full-viewport frame as PNG bytes."""
        if self._closed_page_frame and self._page_is_closed():
            return self._closed_frame()
        try:
            if self._bring_to_front_before_capture:
                # A no-op for the tab already in front.
                self.page.bring_to_front()
            png = self._live_screenshot()
        except Exception:
            # The page closed itself while the frame was being captured.
            if self._closed_page_frame and self._page_is_closed():
                return self._closed_frame()
            raise
        if self._closed_page_frame:
            try:
                from openadapt_flow.runtime.resolver import png_size

                self._last_frame_size = png_size(png)
            except Exception:
                pass
        return png

    # -- JavaScript dialogs -------------------------------------------------

    def arm_dialog(
        self,
        response: str,
        message: Optional[str],
        message_pattern: Optional[str] = None,
    ) -> None:
        """Answer the next dialog with ``response`` if it shows ``message``.

        ``message_pattern`` (a full-match regex) replaces ``message`` when
        given. With neither (recording) any dialog is answered and its text
        reported. A dialog whose text does not match is DISMISSED, never
        accepted, and reported as a mismatch. Every dialog this page shows
        while the listener exists is answered and reported, so an unexpected
        one can never be accepted by default.
        """

        if response not in ("accept", "dismiss"):
            raise ValueError("a dialog response is 'accept' or 'dismiss'")
        if not self._dialog_listening:
            self.page.on("dialog", self._on_dialog)
            self._dialog_listening = True
        pattern = (
            re.compile(message_pattern, re.DOTALL)
            if message_pattern is not None
            else None
        )
        self._dialog_armed = (response, message, pattern)
        self._dialogs_seen = []

    def take_dialogs(self, timeout_s: float = 0.0) -> list[dict[str, Any]]:
        """The dialogs answered since the last arm, then disarm.

        Waits up to ``timeout_s`` for the first one (a dialog can open a
        moment after the input edge). Playwright delivers the dialog event
        only while its loop runs, so the wait runs on the page.
        """

        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while not self._dialogs_seen and time.monotonic() < deadline:
            if self._page_is_closed():
                break
            try:
                self.page.wait_for_timeout(50)
            except Exception:
                break
        seen, self._dialogs_seen = self._dialogs_seen, []
        self._dialog_armed = None
        return seen

    def _on_dialog(self, dialog: Any) -> None:
        try:
            message = str(dialog.message)
        except Exception:
            message = ""
        try:
            kind = str(dialog.type)
        except Exception:
            kind = ""
        armed, self._dialog_armed = self._dialog_armed, None
        matched = False
        if armed is not None:
            _response, expected, pattern = armed
            if pattern is not None:
                matched = pattern.fullmatch(message) is not None
            else:
                matched = expected is None or expected == message
        response = armed[0] if armed is not None and matched else "dismiss"
        self._dialogs_seen.append(
            {
                "message": message,
                "type": kind,
                "response": response,
                "matched": matched,
            }
        )
        try:
            if response == "accept":
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:
            pass

    def _page_is_closed(self) -> bool:
        try:
            return bool(self.page.is_closed())
        except Exception:
            return True

    def _closed_frame(self) -> bytes:
        """An all-black frame: what a closed popup or tab shows."""

        from io import BytesIO

        from PIL import Image

        size = self._last_frame_size
        if size is None:
            try:
                viewport = self.page.viewport_size or {}
                size = (int(viewport["width"]), int(viewport["height"]))
            except Exception:
                size = (1280, 800)
        buffer = BytesIO()
        Image.new("RGB", size, (0, 0, 0)).save(buffer, format="PNG")
        return buffer.getvalue()

    def _live_screenshot(self) -> bytes:
        base_options: dict[str, Any] = {}
        if self._screenshot_scale == "css":
            base_options["scale"] = "css"
        if not self._screenshot_mask_selectors:
            return self._capture_across_navigation(base_options)

        deadline = time.monotonic() + _MASKED_SCREENSHOT_TIMEOUT_S
        while True:
            # A retry can observe a different secret boundary even after the
            # frame tree settles. Rebind or refuse before every capture.
            if self._screenshot_guard is not None:
                self._screenshot_guard()
            generation = self._screenshot_frame_generation
            frames = tuple(self.page.frames)
            if generation != self._screenshot_frame_generation:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_MASKED_SCREENSHOT_RETRY_SLEEP_S)
                continue
            options = dict(base_options)
            options["mask"] = [
                frame.locator(selector)
                for frame in frames
                for selector in self._screenshot_mask_selectors
            ]
            options["mask_color"] = "#000000"
            try:
                png = self.page.screenshot(type="png", full_page=False, **options)
                # Flush lifecycle events that Chromium sent with or before the
                # screenshot response before accepting the in-memory bytes.
                self.page.evaluate("() => null")
            except Exception:
                if generation != self._screenshot_frame_generation:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_MASKED_SCREENSHOT_RETRY_SLEEP_S)
                    continue
                raise
            # A closed root can appear during capture without changing the
            # frame tree. Its privacy refusal must escape the retry handler.
            if self._screenshot_guard is not None:
                self._screenshot_guard()
            current_frames = tuple(self.page.frames)
            if generation == self._screenshot_frame_generation and self._same_frames(
                frames, current_frames
            ):
                return png
            # ``png`` is intentionally discarded here. It never reaches the
            # recorder, disk, or a compiled bundle.
            if time.monotonic() >= deadline:
                break
            time.sleep(_MASKED_SCREENSHOT_RETRY_SLEEP_S)
        raise ScreenshotMaskStabilityError(
            "the browser frame tree changed during every secret-masked "
            "screenshot attempt; recording was refused"
        )

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at pixel coordinates via the mouse.

        A click that closes its own page surface completes normally when that
        close was expected (:meth:`expect_page_close`). An unexpected report
        that the page closed during the click raises
        :class:`~openadapt_flow.backend.ActionDeliveryUncertain`.
        """
        self._assert_qualification_environment_current()
        expected = self._take_page_close_expectation()
        open_before = not self._page_is_closed()
        try:
            if double:
                self.page.mouse.dblclick(x, y)
            else:
                self.page.mouse.click(x, y)
        except Exception as exc:
            if not _is_target_closed_error(exc):
                raise
            if self._closed_itself_as_expected(expected, open_before):
                return
            raise ActionDeliveryUncertain(
                operation="coordinate_double_click" if double else "coordinate_click",
                native=False,
                cause_type=type(exc).__name__,
            ) from exc

    # -- a click that closes its own popup ---------------------------------

    def expect_page_close(self) -> None:
        """Expect the next click on this page to close it (one-shot).

        Only a page surface (``closed_page_frame=True``: a popup or tab a step
        opened) honours it. On the primary page a close during a click stays
        an uncertain delivery.
        """
        self._page_close_expected = True

    def cancel_page_close(self) -> None:
        """Drop an expectation no click consumed."""
        self._page_close_expected = False

    def page_closed(self) -> bool:
        """Whether this page has closed."""
        return self._page_is_closed()

    def _take_page_close_expectation(self) -> bool:
        expected, self._page_close_expected = self._page_close_expected, False
        return expected and self._closed_page_frame

    def _closed_itself_as_expected(self, expected: bool, open_before: bool) -> bool:
        """Whether a click that raised "page closed" was delivered.

        It was when the step expected its page to close, the page was open
        when the click started, and the page is confirmed closed now. The
        browser reports the close only after the page began handling input,
        and an open page cannot close itself without running its own script.
        """
        if not (expected and open_before):
            return False
        deadline = time.monotonic() + _PAGE_CLOSE_CONFIRM_S
        while True:
            if self._page_is_closed():
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                # The sync API applies the page's close event only while it
                # is inside a call; a short wait lets it arrive.
                self.page.wait_for_timeout(20)
            except Exception:
                time.sleep(0.02)

    def right_click(self, x: int, y: int) -> None:
        """Open the context menu at a resolved point."""

        self._assert_qualification_environment_current()
        try:
            self.page.mouse.click(x, y, button="right")
        except Exception as exc:
            raise ActionDeliveryUncertain(
                operation="coordinate_right_click",
                native=False,
                cause_type=type(exc).__name__,
            ) from exc

    def drag(self, x: int, y: int, end_x: int, end_y: int) -> None:
        """Drag between two independently resolved points."""

        self._assert_qualification_environment_current()
        self.page.mouse.move(x, y)
        down_attempted = False
        try:
            self._assert_qualification_environment_current()
            down_attempted = True
            self.page.mouse.down(button="left")
            self.page.mouse.move(end_x, end_y)
        except Exception as exc:
            raise ActionDeliveryUncertain(
                operation="coordinate_drag",
                native=False,
                cause_type=type(exc).__name__,
            ) from exc
        finally:
            if down_attempted:
                try:
                    self.page.mouse.up(button="left")
                except Exception as exc:
                    raise ActionDeliveryUncertain(
                        operation="coordinate_drag",
                        native=False,
                        cause_type=type(exc).__name__,
                    ) from exc

    def drag_guarded(
        self,
        x: int,
        y: int,
        end_x: int,
        end_y: int,
        *,
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        """Bind a fresh source lease and exact-frame destination to one drag."""

        point = (int(x), int(y))
        pending = self._guarded_coordinate
        self._guarded_coordinate = None
        if pending is None:
            raise StructuralResolutionRefused(
                "visual DOM drag has no pre-identity source binding"
            )
        try:
            if pending.point != point:
                raise StructuralResolutionRefused(
                    "visual DOM drag source changed after target binding"
                )
            token_locator = self._token_locator(pending.token, pending.scope)
            if not self._point_guard_is_current(pending, token_locator):
                raise StructuralResolutionRefused(
                    "visual drag source, record, or context changed before delivery"
                )
            current_sha256 = hashlib.sha256(self.screenshot()).hexdigest()
            if not hmac.compare_digest(current_sha256, expected_frame_sha256):
                raise StructuralResolutionRefused(
                    "visual drag frame changed after both endpoints were resolved"
                )
            down_attempted = False
            try:
                self.page.mouse.move(*point)
                self._assert_qualification_environment_current()
                down_attempted = True
                self.page.mouse.down(button="left")
                self.page.mouse.move(int(end_x), int(end_y))
            except Exception as exc:
                if down_attempted:
                    raise ActionDeliveryUncertain(
                        operation="guarded_coordinate_drag",
                        native=False,
                        target_fingerprint=pending.fingerprint,
                        cause_type=type(exc).__name__,
                    ) from exc
                raise StructuralResolutionRefused(
                    "identity-bound drag became unactionable before delivery"
                ) from exc
            finally:
                if down_attempted:
                    try:
                        self.page.mouse.up(button="left")
                    except Exception as exc:
                        raise ActionDeliveryUncertain(
                            operation="guarded_coordinate_drag",
                            native=False,
                            target_fingerprint=pending.fingerprint,
                            cause_type=type(exc).__name__,
                        ) from exc
        finally:
            self._cleanup_guard(pending.token, pending.scope)
        return ActionDeliveryReceipt(
            receipt_id=f"playwright-coordinate-{uuid.uuid4().hex}",
            operation="guarded_coordinate_drag",
            native=False,
            target_fingerprint=visual_resolution_point_fingerprint(
                expected_frame_sha256,
                point,
            ),
            destination_fingerprint=visual_resolution_point_fingerprint(
                expected_frame_sha256,
                (int(end_x), int(end_y)),
            ),
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""
        self._assert_qualification_environment_current()
        self._register_focused_field()
        self.page.keyboard.type(text)

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'Meta+a'``."""
        self._assert_qualification_environment_current()
        self.page.keyboard.press(_normalize_chord(key))

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture at the current mouse position.

        The wheel event targets whatever element is under the pointer, so
        scrolling works inside iframes and nested scroll containers exactly
        as it does for a human — position the pointer first (a preceding
        click does this naturally during both record and replay).
        """
        self._assert_qualification_environment_current()
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
        shared = _SHARED_BROWSER.get(headless)
        if shared is not None:
            pw, browser = shared
        else:
            pw, browser = _start_browser(headless)
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
            if shared is not None:
                # A shared browser outlives this page; closing the page's own
                # context discards its cookies, storage, and cache.
                if context is not None:
                    context.close()
                else:
                    page.context.close()
                return
            try:
                if context is not None:
                    context.close()  # flush the recorded video to disk
                browser.close()
            finally:
                pw.stop()

        return backend, close


from openadapt_flow.backends._browser_process import (  # noqa: E402
    _SHARED_BROWSER,
    _start_browser,
    shared_browser,  # noqa: F401 - re-exported
)
