"""Playwright/CDP implementation of ``FormSurfaceBackend`` for ``form_tx``.

Provides quiescence-gated DOM snapshotting, deterministic dynamic-ID-resilient
selector synthesis, body-portaled combobox actuation, bounded child-window
popup lifecycle management, secret value scrubbing, and zero-mutation keepalive
verification. Zero application or vendor names.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import re
import time
from collections.abc import Iterator, Sequence
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from openadapt_flow.backends._page_quiescence import (
    QUIESCENCE_INSTALL_JS,
    PageQuiescence,
)
from openadapt_flow.form_tx.codecs import collapse_whitespace
from openadapt_flow.form_tx.surface import (
    AmbiguousAnchorError,
    MissingAnchorError,
)
from openadapt_flow.form_tx.types import (
    EntityTableSpec,
    FormSnapshot,
    ModalRuleSpec,
    ObservedControl,
    ObservedEntityRow,
    ObservedModal,
    ObservedOption,
    ObservedTextPair,
    UIProfileSpec,
    WizardScreenSpec,
)

_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class SurfaceSettleTimeoutError(TimeoutError):
    """Raised when a form surface remains busy past ``ui_profile.max_settle_ms``."""


class SurfaceContextError(RuntimeError):
    """The selected browser document is closed, detached, ambiguous or stale."""


class _NoMatchingFrameError(SurfaceContextError):
    """A routed surface has no candidate yet; a settle barrier may wait for one."""


class SurfaceStateError(RuntimeError):
    """A control did not take the state the surface set; the run must stop."""


def _css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class PopupContractError(RuntimeError):
    """Raised when a child popup window fails to open or violates URL/title contracts."""


class KeepaliveMutationError(RuntimeError):
    """Raised when ``ping_keepalive`` triggers any state-changing HTTP request."""


def _is_target_closed_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name == "TargetClosedError":
        return True
    msg = str(exc)
    return "Target page, context or browser has been closed" in msg or (
        "Target closed" in msg
    )


def _strip_url_to_origin_and_path(raw_url: str) -> str:
    """Strip query parameters, userinfo, and fragments, retaining only origin + path."""
    if not raw_url:
        return ""
    parts = urlsplit(raw_url)
    if not parts.scheme or parts.scheme in ("about", "data", "javascript"):
        if parts.scheme == "about":
            return f"about:{parts.path or 'blank'}"
        return f"{parts.scheme}:"
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    path = parts.path or "/"
    return f"{parts.scheme}://{netloc}{path}"


_DOCUMENT_CONTEXT_JS = r"""
() => {
    const key = '__openadaptFormDocumentId';
    if (!Object.prototype.hasOwnProperty.call(document, key)) {
        Object.defineProperty(document, key, {
            value: (typeof crypto.randomUUID === 'function')
                ? crypto.randomUUID()
                : `${performance.timeOrigin}:${Math.random()}:${Math.random()}`,
        });
    }
    return `${document[key]}:${location.href}`;
}
"""


_DOM_HELPERS_JS = r"""
    // A page may replace or remove the global CSS object; never rely on it.
    const cssEscape = (value) => {
        const str = String(value);
        let out = '';
        for (let i = 0; i < str.length; i += 1) {
            const ch = str.charAt(i);
            const code = str.charCodeAt(i);
            if (code === 0) {
                out += '\uFFFD';
            } else if ((code >= 0x30 && code <= 0x39) &&
                    (i === 0 || (i === 1 && str.charAt(0) === '-'))) {
                out += '\\' + code.toString(16) + ' ';
            } else if (code >= 0x80 || ch === '-' || ch === '_' ||
                    (code >= 0x30 && code <= 0x39) ||
                    (code >= 0x41 && code <= 0x5a) || (code >= 0x61 && code <= 0x7a)) {
                out += (i === 0 && ch === '-' && str.length === 1) ? '\\-' : ch;
            } else {
                out += '\\' + ch;
            }
        }
        return out;
    };
    // Composed-tree helpers shared by every in-page script. Open shadow roots
    // are walked like ordinary subtrees; closed shadow roots stay invisible.
    // A control inside a shadow root gets a selector of the form
    // ``<host selector> >> <selector unique inside that root>``, which
    // Playwright locators accept, and ``resolveSel`` resolves the same way.
    const rootOf = (el) => (el && el.getRootNode) ? el.getRootNode() : document;
    const parentAcross = (el) => {
        if (!el) return null;
        if (el.parentElement) return el.parentElement;
        const r = rootOf(el);
        return (r && r.host) ? r.host : null;
    };
    const byIdIn = (el, id) => {
        const r = rootOf(el);
        let ref = null;
        if (r && typeof r.getElementById === 'function') ref = r.getElementById(id);
        return ref || document.getElementById(id);
    };
    let __hasShadow = null;
    const docHasShadow = () => {
        if (__hasShadow === null) {
            __hasShadow = false;
            for (const e of document.querySelectorAll('*')) {
                if (e.shadowRoot) { __hasShadow = true; break; }
            }
        }
        return __hasShadow;
    };
    const shallowQueryAll = (root, sel) => {
        if (!sel || !root) return [];
        try {
            return Array.from(root.querySelectorAll(sel));
        } catch (e) {
            return [];
        }
    };
    // Every element matching ``sel`` under ``scope`` in composed-tree order,
    // descending into open shadow roots (a host's shadow content first).
    const safeQueryAll = (scope, sel) => {
        if (!sel || !scope) return [];
        if (!docHasShadow()) return shallowQueryAll(scope, sel);
        try {
            document.createDocumentFragment().querySelector(sel);
        } catch (e) {
            return [];
        }
        const out = [];
        const walk = (node) => {
            for (const child of Array.from(node.children || [])) {
                if (child.matches(sel)) out.push(child);
                if (child.shadowRoot) walk(child.shadowRoot);
                walk(child);
            }
        };
        if (scope.shadowRoot) walk(scope.shadowRoot);
        walk(scope);
        return out;
    };
    const resolveSel = (sel) => {
        if (!sel) return [];
        const parts = String(sel).split(/\s+>>\s+/);
        let scopes = [document];
        for (const part of parts) {
            const next = [];
            for (const sc of scopes) {
                for (const el of safeQueryAll(sc, part)) {
                    if (el !== sc && !next.includes(el)) next.push(el);
                }
            }
            scopes = next;
            if (!scopes.length) break;
        }
        return scopes;
    };
    const isVisible = (el) => {
        if (!el || !(el instanceof Element)) return false;
        let cur = el;
        while (cur && cur !== document.documentElement) {
            if (cur.hasAttribute('hidden')) return false;
            if (cur.tagName === 'DIALOG' && !cur.open &&
                    cur.getAttribute('data-modal-open') !== 'true') {
                return false;
            }
            const cs = window.getComputedStyle(cur);
            if (cs.display === 'none' || cs.visibility === 'hidden' ||
                    cs.visibility === 'collapse') {
                return false;
            }
            cur = parentAcross(cur);
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const DYNAMIC_ID_RE = /(?:[_:-]\d+|\d{2,}|_[0-9a-f]{6,}|^:)/i;
    // Unique inside the element's own root, counting what a Playwright
    // locator would also see through nested open shadow roots.
    // For an element in a shadow root the scope is its host's whole composed
    // subtree: a ``host >> inner`` locator also matches the host's light-DOM
    // descendants (slotted nodes included), so ``inner`` must be unique there.
    const uniquenessScope = (target) => {
        const r = rootOf(target);
        return (r && r !== document && r.host) ? r.host : r;
    };
    const isUniqueMatch = (sel, target) => {
        if (!sel) return false;
        const nodes = safeQueryAll(uniquenessScope(target), sel);
        return nodes.length === 1 && nodes[0] === target;
    };
    const quoteAttrVal = (val) => {
        return '"' + String(val).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
    };
    const structuralSelector = (target) => {
        const parts = [];
        let cur = target;
        while (cur && cur !== document.documentElement) {
            if (cur !== target && cur.id && !DYNAMIC_ID_RE.test(cur.id)) {
                const anchorSel = '#' + cssEscape(cur.id);
                if (isUniqueMatch(anchorSel, cur)) {
                    parts.unshift(anchorSel);
                    break;
                }
            }
            const tag = cur.tagName.toLowerCase();
            if (cur === document.body) {
                parts.unshift('body');
                break;
            }
            const parent = cur.parentElement;
            if (!parent) {
                // Top of a shadow root (or detached): siblings are the root's children.
                const r = rootOf(cur);
                const sibs = (r && r !== document && r.children) ? Array.from(r.children) : [cur];
                const sameTag = sibs.filter((sib) => sib.tagName === cur.tagName);
                parts.unshift(sameTag.length > 1 ? `${tag}:nth-of-type(${sameTag.indexOf(cur) + 1})` : tag);
                break;
            }
            const sameTag = Array.from(parent.children).filter(
                (sib) => sib.tagName === cur.tagName
            );
            if (sameTag.length === 1) {
                parts.unshift(tag);
            } else {
                const idx = sameTag.indexOf(cur) + 1;
                parts.unshift(`${tag}:nth-of-type(${idx})`);
            }
            cur = parent;
        }
        return parts.join(' > ');
    };
    const localSelector = (el) => {
        if (el.id && !DYNAMIC_ID_RE.test(el.id)) {
            const idSel = '#' + cssEscape(el.id);
            if (isUniqueMatch(idSel, el)) return idSel;
        }
        const testId = el.getAttribute('data-testid');
        if (testId && !DYNAMIC_ID_RE.test(testId)) {
            const tidSel = `[data-testid=${quoteAttrVal(testId)}]`;
            if (isUniqueMatch(tidSel, el)) return tidSel;
        }
        const tag = el.tagName.toLowerCase();
        const nameAttr = el.getAttribute('name') || '';
        if (nameAttr && !DYNAMIC_ID_RE.test(nameAttr)) {
            const typeAttr = (el.getAttribute('type') || '').toLowerCase();
            if ((typeAttr === 'radio' || typeAttr === 'checkbox') && el.hasAttribute('value')) {
                const nvSel = `${tag}[name=${quoteAttrVal(nameAttr)}][value=${quoteAttrVal(el.getAttribute('value') || '')}]`;
                if (isUniqueMatch(nvSel, el)) return nvSel;
            }
            const nameSel = `${tag}[name=${quoteAttrVal(nameAttr)}]`;
            if (isUniqueMatch(nameSel, el)) return nameSel;
        }
        return structuralSelector(el);
    };
    const computeDomSelector = (el) => {
        const r = rootOf(el);
        if (r && r !== document && r.host) {
            return computeDomSelector(r.host) + ' >> ' + localSelector(el);
        }
        return localSelector(el);
    };
"""


_CHECK_BUSY_AND_DOM_JS = r"""
(busySelectors) => {
/*@DOM_HELPERS@*/
    let busyMatch = '';
    for (const sel of (busySelectors || [])) {
        let nodes = [];
        try {
            nodes = safeQueryAll(document, sel);
        } catch (e) {
            continue;
        }
        for (const el of nodes) {
            if (isVisible(el)) {
                busyMatch = sel;
                break;
            }
        }
        if (busyMatch) break;
    }

    let bodyHtmlLen = document.body ? document.body.innerHTML.length : 0;
    const allEls = safeQueryAll(document, '*');
    for (const host of allEls) {
        if (host.shadowRoot) bodyHtmlLen += host.shadowRoot.innerHTML.length;
    }
    const elemCount = allEls.length;
    let valAcc = 0;
    for (const inp of safeQueryAll(document, 'input, select, textarea')) {
        valAcc = ((valAcc * 31) + String(inp.value || '').length + (inp.checked ? 7 : 3)) >>> 0;
    }
    return {
        readyState: document.readyState || '',
        busySelector: busyMatch,
        domSig: `${document.readyState}:${elemCount}:${bodyHtmlLen}:${valAcc}`,
    };
}
"""


_SNAPSHOT_WALKER_JS = r"""
(cfg) => {
/*@DOM_HELPERS@*/
    const cleanText = (s) => String(s || '').replace(/\s+/g, ' ').trim();

    const PLACEHOLDERS = new Set([
        '',
        '-- select --',
        'select...',
        'choose...',
        '-- choose --',
        'select an option',
        'please select',
        'none selected',
        'select one',
        '-- select one --',
        'select',
        'choose one',
        'please choose',
        'choose',
        '-- choose one --',
    ]);
    const isPlaceholderText = (s) => PLACEHOLDERS.has(cleanText(s).toLowerCase());


    const SECRET_AUTOCOMPLETE = new Set([
        'one-time-code',
        'current-password',
        'new-password',
        'cc-csc',
        'cc-number',
    ]);
    const SECRET_WORDS = new Set([
        'password',
        'passwd',
        'pwd',
        'passcode',
        'otp',
        'totp',
        'cvv',
        'cvc',
        'secret',
    ]);
    const SECRET_PHRASES_RE = /(?:one[_-]?time[_-]?code|onetimecode|mfa[_-]?code|auth[_-]?code|api[_-]?key|apikey|client[_-]?secret|clientsecret|access[_-]?token|accesstoken|private[_-]?key|privatekey)/i;

    const detectSecret = (el, inputType, nameAttr, idAttr) => {
        if (inputType === 'password') return true;
        if (el.hasAttribute('data-secret')) {
            const ds = cleanText(el.getAttribute('data-secret')).toLowerCase();
            if (ds !== 'false' && ds !== '0') return true;
        }
        const ac = cleanText(el.getAttribute('autocomplete')).toLowerCase();
        if (ac) {
            for (const tok of ac.split(/\s+/)) {
                if (SECRET_AUTOCOMPLETE.has(tok)) return true;
            }
        }
        const combined = `${nameAttr} ${idAttr}`;
        if (SECRET_PHRASES_RE.test(combined)) return true;
        const normalizedWords = combined
            .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
            .toLowerCase()
            .split(/[^a-z0-9]+/)
            .filter(Boolean);
        for (const w of normalizedWords) {
            if (SECRET_WORDS.has(w)) return true;
        }
        return false;
    };

    const isOpenModal = (el) => {
        if (!el || !(el instanceof Element)) return false;
        if (el.tagName === 'DIALOG') {
            if (!(el.open === true || el.getAttribute('data-modal-open') === 'true')) {
                return false;
            }
        }
        if (el.getAttribute('data-modal-open') === 'false') return false;
        if (el.getAttribute('aria-hidden') === 'true') return false;
        return isVisible(el);
    };

    const anyVisibleMatch = (selectors) => {
        for (const sel of (selectors || [])) {
            for (const el of safeQueryAll(document, sel)) {
                if (isVisible(el)) return true;
            }
        }
        return false;
    };

    const extractLabelCloneText = (labelEl) => {
        const clone = labelEl.cloneNode(true);
        const stripSelectors = [
            'input', 'select', 'textarea', 'button', 'output',
            '[role="combobox"]', '[role="listbox"]',
            ...(cfg.fieldErrorSelectors || []),
        ];
        for (const s of stripSelectors) {
            try {
                for (const child of Array.from(clone.querySelectorAll(s))) {
                    child.remove();
                }
            } catch (e) {}
        }
        return cleanText(clone.textContent);
    };

    const resolveLabelText = (el, tag, inputType, role) => {
        if (el.id) {
            const linked = shallowQueryAll(rootOf(el), `label[for=${quoteAttrVal(el.id)}]`);
            const parts = linked.map((lbl) => extractLabelCloneText(lbl)).filter(Boolean);
            if (parts.length) return parts.join(' ');
        }
        const labelledBy = cleanText(el.getAttribute('aria-labelledby'));
        if (labelledBy) {
            const parts = labelledBy
                .split(/\s+/)
                .map((id) => {
                    const ref = byIdIn(el, id);
                    return ref ? cleanText(ref.textContent) : '';
                })
                .filter(Boolean);
            if (parts.length) return parts.join(' ');
        }
        const ariaLabel = cleanText(el.getAttribute('aria-label'));
        if (ariaLabel) return ariaLabel;

        const wrappingLabel = el.closest('label');
        if (wrappingLabel) {
            const wt = extractLabelCloneText(wrappingLabel);
            if (wt) return wt;
        }

        if (tag === 'button' && role !== 'combobox') {
            return cleanText(el.textContent);
        }
        if (tag === 'input' && (inputType === 'button' || inputType === 'submit' || inputType === 'reset')) {
            return cleanText(el.value || el.getAttribute('value') || '');
        }

        // Unlinked sibling <label> in a single-control field wrapper
        const parent = el.parentElement;
        if (parent) {
            const sibControls = safeQueryAll(
                parent,
                'input:not([type="hidden"]), select, textarea, button, [role="combobox"], [role="checkbox"], [role="radio"]'
            );
            if (sibControls.length === 1) {
                for (const child of Array.from(parent.children)) {
                    if (child.tagName === 'LABEL' && !child.getAttribute('for')) {
                        const st = extractLabelCloneText(child);
                        if (st) return st;
                    }
                }
            }
        }
        // An ARIA radio, checkbox or switch named only by its own text.
        if (tag !== 'input' && (role === 'radio' || role === 'checkbox' || role === 'switch')) {
            const own = cleanText(el.textContent);
            if (own) return own;
        }
        // A native radio or checkbox followed by bare text ("<input> Yes").
        if (tag === 'input' && (inputType === 'radio' || inputType === 'checkbox')) {
            let node = el.nextSibling;
            let text = '';
            while (node) {
                if (node.nodeType === Node.TEXT_NODE) {
                    text += node.textContent;
                } else if (node.nodeType === Node.ELEMENT_NODE) {
                    if (isInteractiveElement(node) || node.tagName === 'BR' ||
                            node.tagName === 'LABEL' ||
                            safeQueryAll(node, 'input, select, textarea, button').length) {
                        break;
                    }
                    text += node.textContent;
                }
                if (cleanText(text) && node.nextSibling &&
                        node.nextSibling.nodeType === Node.ELEMENT_NODE &&
                        isInteractiveElement(node.nextSibling)) {
                    break;
                }
                node = node.nextSibling;
            }
            const t = cleanText(text);
            if (t) return t;
        }
        // A table layout: the label is in the cell before the control's cell.
        // Only when the control is alone in its cell and the label cell holds
        // no control.
        const cell = el.closest('td, th');
        if (cell && !(tag === 'input' && (inputType === 'radio' || inputType === 'checkbox'))) {
            const CONTROLS = 'input:not([type="hidden"]), select, textarea, button, [role="combobox"], [role="checkbox"], [role="radio"]';
            if (safeQueryAll(cell, CONTROLS).length === 1) {
                let prev = cell.previousElementSibling;
                while (prev && !cleanText(prev.textContent) &&
                        !safeQueryAll(prev, CONTROLS).length) {
                    prev = prev.previousElementSibling;
                }
                if (prev && (prev.tagName === 'TD' || prev.tagName === 'TH') &&
                        !safeQueryAll(prev, CONTROLS).length) {
                    const lt = extractLabelCloneText(prev);
                    if (lt) return lt;
                }
            }
        }
        return '';
    };

    // The visible name of a radio group: its radiogroup's aria-labelledby or
    // aria-label, else the legend of its fieldset.
    const groupLabelFor = (el) => {
        const rg = el.closest('[role="radiogroup"]');
        if (rg) {
            const lby = cleanText(rg.getAttribute('aria-labelledby'));
            if (lby) {
                const root = rg.getRootNode();
                const txt = lby.split(/\s+/).map((id) => {
                    const ref = (root.getElementById ? root.getElementById(id) : null) ||
                        document.getElementById(id);
                    return ref ? cleanText(ref.textContent) : '';
                }).filter(Boolean).join(' ');
                if (txt) return txt;
            }
            const al = cleanText(rg.getAttribute('aria-label'));
            if (al) return al;
        }
        const fs = el.closest('fieldset');
        if (fs) {
            const legend = Array.from(fs.children).find((c) => c.tagName === 'LEGEND');
            if (legend) return cleanText(legend.textContent);
        }
        return '';
    };

    const resolveSectionText = (el) => {
        let cur = parentAcross(el);
        while (cur && cur !== document.documentElement) {
            if (cur.tagName === 'FIELDSET') {
                const legend = Array.from(cur.children).find((c) => c.tagName === 'LEGEND');
                if (legend) {
                    const lt = cleanText(legend.textContent);
                    if (lt) return lt;
                }
            }
            const role = (cur.getAttribute('role') || '').toLowerCase();
            if (role === 'region') {
                const lby = cleanText(cur.getAttribute('aria-labelledby'));
                if (lby) {
                    const txt = lby
                        .split(/\s+/)
                        .map((id) => {
                            const ref = byIdIn(el, id);
                            return ref ? cleanText(ref.textContent) : '';
                        })
                        .filter(Boolean)
                        .join(' ');
                    if (txt) return txt;
                }
                const al = cleanText(cur.getAttribute('aria-label'));
                if (al) return al;
                const regHeading = cur.querySelector('h1, h2, h3');
                if (regHeading && cleanText(regHeading.textContent)) {
                    return cleanText(regHeading.textContent);
                }
            }
            const directHeadings = Array.from(cur.children).filter(
                (c) => (c.tagName === 'H1' || c.tagName === 'H2' || c.tagName === 'H3') &&
                    isVisible(c) &&
                    Boolean(c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)
            );
            if (directHeadings.length) {
                const ht = cleanText(directHeadings[directHeadings.length - 1].textContent);
                if (ht) return ht;
            }
            cur = parentAcross(cur);
        }
        const allHeadings = safeQueryAll(document, 'h1, h2, h3').filter(
            (h) => isVisible(h) &&
                Boolean(h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)
        );
        if (allHeadings.length) {
            return cleanText(allHeadings[allHeadings.length - 1].textContent);
        }
        return '';
    };

    const isInteractiveElement = (node) => {
        if (!node || !(node instanceof Element)) return false;
        const t = node.tagName.toLowerCase();
        if (t === 'input' || t === 'select' || t === 'textarea' || t === 'button') return true;
        const r = (node.getAttribute('role') || '').toLowerCase();
        return r === 'combobox' || r === 'checkbox' || r === 'radio' || r === 'listbox' || r === 'option';
    };

    const matchesAnySelector = (node, selectors) => {
        for (const s of (selectors || [])) {
            try {
                if (node.matches(s)) return true;
            } catch (e) {}
        }
        return false;
    };

    const resolveFieldErrorText = (el) => {
        const errIdAttr = cleanText(el.getAttribute('aria-errormessage'));
        if (errIdAttr) {
            for (const id of errIdAttr.split(/\s+/)) {
                const ref = byIdIn(el, id);
                if (ref && isVisible(ref) && !isInteractiveElement(ref)) {
                    const t = cleanText(ref.textContent);
                    if (t) return t;
                }
            }
        }
        const descBy = cleanText(el.getAttribute('aria-describedby'));
        if (descBy) {
            for (const id of descBy.split(/\s+/)) {
                const ref = byIdIn(el, id);
                if (ref && isVisible(ref) && !isInteractiveElement(ref)) {
                    if (el.getAttribute('aria-invalid') === 'true' ||
                            matchesAnySelector(ref, cfg.fieldErrorSelectors)) {
                        const t = cleanText(ref.textContent);
                        if (t) return t;
                    }
                }
            }
        }
        for (const key of [el.id, el.getAttribute('name')].filter(Boolean)) {
            for (const node of safeQueryAll(document, `[data-error-for=${quoteAttrVal(key)}]`)) {
                if (isVisible(node) && !isInteractiveElement(node)) {
                    const t = cleanText(node.textContent);
                    if (t) return t;
                }
            }
        }
        const container = el.closest('.field, .form-field, .form-group, .form-row, label') || el.parentElement;
        if (container && container !== document.body && container !== document.querySelector('form')) {
            const innerControls = safeQueryAll(
                container,
                'input:not([type="hidden"]), select, textarea, [role="combobox"], [role="checkbox"], [role="radio"]'
            );
            if (innerControls.length <= 1) {
                for (const sel of (cfg.fieldErrorSelectors || [])) {
                    for (const errNode of safeQueryAll(container, sel)) {
                        if (errNode === el || isInteractiveElement(errNode)) continue;
                        if (isVisible(errNode)) {
                            const t = cleanText(errNode.textContent);
                            if (t) return t;
                        }
                    }
                }
            }
        }
        if (el.getAttribute('aria-invalid') === 'true') {
            const msg = cleanText(el.getAttribute('data-error-message') || '');
            if (msg) return msg;
        }
        return '';
    };

    // 1. Controls
    const rawControlNodes = safeQueryAll(
        document,
        'input, select, textarea, button, output, [role="combobox"], [role="checkbox"], [role="radio"], [data-readonly-output]'
    );
    const controls = [];
    const usedControlIds = new Set();

    for (let i = 0; i < rawControlNodes.length; i += 1) {
        const el = rawControlNodes[i];
        const tag = el.tagName.toLowerCase();
        const inputType = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
        if (tag === 'input' && inputType === 'hidden') continue;
        const role = cleanText(el.getAttribute('role')).toLowerCase();
        if (role === 'option') continue;
        if (!isVisible(el)) continue;

        const domSelector = computeDomSelector(el);
        const nameAttr = el.getAttribute('name') || '';
        const idAttr = el.id || '';
        const labelText = resolveLabelText(el, tag, inputType, role);
        const sectionText = resolveSectionText(el);
        const isSecret = detectSecret(el, inputType, nameAttr, idAttr);

        let checked = null;
        let currentValue = '';
        let options = [];
        let radioValue = '';
        let radioGroup = '';
        let groupLabel = '';

        if (tag === 'select') {
            options = Array.from(el.options).map((opt) => ({
                value: String(opt.value ?? ''),
                label: cleanText(opt.textContent || opt.label || ''),
            }));
            const selOpt = el.selectedOptions && el.selectedOptions.length ? el.selectedOptions[0] : null;
            if (!selOpt) {
                currentValue = '';
            } else {
                const optLabel = cleanText(selOpt.textContent || selOpt.label || '');
                const optVal = String(selOpt.value ?? '');
                const isPlaceholder =
                    selOpt.hasAttribute('data-placeholder') ||
                    (selOpt.disabled && selOpt.index === 0) ||
                    (optVal === '' && isPlaceholderText(optLabel)) ||
                    (isPlaceholderText(optVal) && isPlaceholderText(optLabel));
                currentValue = isPlaceholder ? '' : (optVal !== '' ? optVal : optLabel);
            }
        } else if (role === 'combobox') {
            if (el.hasAttribute('data-selected-value')) {
                const dv = cleanText(el.getAttribute('data-selected-value'));
                currentValue = isPlaceholderText(dv) ? '' : dv;
            } else if (tag === 'input') {
                const iv = String(el.value || '');
                currentValue = isPlaceholderText(iv) ? '' : iv;
            } else {
                const placeholderAttr = cleanText(el.getAttribute('data-placeholder'));
                const tv = cleanText(el.textContent);
                currentValue = (isPlaceholderText(tv) || (placeholderAttr && tv === placeholderAttr)) ? '' : tv;
            }
        } else if (tag === 'input' && inputType === 'checkbox') {
            checked = Boolean(el.checked);
            currentValue = checked ? 'true' : 'false';
        } else if (tag === 'input' && inputType === 'radio') {
            // An unchecked radio holds no answer: it reads as empty. Its own
            // value is kept in radio_value, and options list its group.
            checked = Boolean(el.checked);
            radioValue = String(el.value || '');
            currentValue = checked ? radioValue : '';
            groupLabel = groupLabelFor(el);
            if (nameAttr) {
                radioGroup = 'name:' + nameAttr;
                const scope = el.form || el.getRootNode() || document;
                const peers = safeQueryAll(scope, 'input[type="radio"]').filter(
                    (r) => r.name === nameAttr && (r.form || null) === (el.form || null)
                );
                options = peers.map((r) => ({
                    value: String(r.value || ''),
                    label: resolveLabelText(r, 'input', 'radio', ''),
                }));
            }
        } else if (role === 'checkbox') {
            checked = el.getAttribute('aria-checked') === 'true';
            currentValue = checked ? 'true' : 'false';
        } else if (role === 'radio') {
            // Like a native radio: unchecked reads as empty; options list the
            // radios of its radiogroup (or, without one, its parent).
            checked = el.getAttribute('aria-checked') === 'true';
            radioValue = cleanText(el.getAttribute('data-value') || el.textContent || '');
            currentValue = checked ? radioValue : '';
            groupLabel = groupLabelFor(el);
            const container = el.closest('[role="radiogroup"]') || el.parentElement;
            if (container) {
                radioGroup = 'aria:' + computeDomSelector(container);
                options = safeQueryAll(container, '[role="radio"]').map((r) => ({
                    value: cleanText(r.getAttribute('data-value') || r.textContent || ''),
                    label: resolveLabelText(r, r.tagName.toLowerCase(), '', 'radio'),
                }));
            }
        } else if (tag === 'button') {
            currentValue = '';
        } else if (tag === 'output' || el.hasAttribute('data-readonly-output')) {
            currentValue = cleanText(el.value !== undefined ? el.value : el.textContent);
        } else {
            currentValue = String(el.value ?? '');
        }

        if (isSecret && currentValue) {
            currentValue = '<secret>';
        }

        const required =
            Boolean(el.required) ||
            el.getAttribute('aria-required') === 'true' ||
            /\*\s*$/.test(labelText);
        const disabled =
            Boolean(el.disabled) ||
            el.getAttribute('aria-disabled') === 'true' ||
            Boolean(el.closest('fieldset[disabled]'));
        const readonly =
            Boolean(el.readOnly) ||
            el.hasAttribute('readonly') ||
            el.getAttribute('aria-readonly') === 'true' ||
            tag === 'output' ||
            el.hasAttribute('data-readonly-output');
        const errorText = resolveFieldErrorText(el);

        let baseId = idAttr || nameAttr || `ctrl_${controls.length}`;
        let controlId = baseId;
        let suffix = 1;
        while (usedControlIds.has(controlId)) {
            suffix += 1;
            controlId = `${baseId}#${suffix}`;
        }
        usedControlIds.add(controlId);

        controls.push({
            control_id: controlId,
            dom_selector: domSelector,
            tag,
            input_type: inputType,
            role,
            name_attr: nameAttr,
            id_attr: idAttr,
            label_text: labelText,
            section_text: sectionText,
            current_value: currentValue,
            checked,
            required,
            disabled,
            readonly,
            visible: true,
            error_text: errorText,
            options,
            is_secret: isSecret,
            radio_value: radioValue,
            radio_group: radioGroup,
            group_label: groupLabel,
        });
    }

    // 2. Entity Tables
    const entityRows = [];
    for (const table of (cfg.entityTables || [])) {
        const rows = safeQueryAll(document, table.row_selector).filter(isVisible);
        for (let rIdx = 0; rIdx < rows.length; rIdx += 1) {
            const rowEl = rows[rIdx];
            const values = {};
            const colMap = table.column_selectors || {};
            for (const [colKey, colSel] of Object.entries(colMap)) {
                let cellEl = null;
                try {
                    cellEl = rowEl.querySelector(colSel);
                } catch (e) {
                    cellEl = null;
                }
                if (!cellEl) {
                    values[colKey] = '';
                } else if (cellEl.tagName === 'INPUT' || cellEl.tagName === 'SELECT' || cellEl.tagName === 'TEXTAREA') {
                    values[colKey] = cleanText(cellEl.value || '');
                } else {
                    values[colKey] = cleanText(cellEl.textContent || '');
                }
            }
            let hasDismiss = false;
            if (table.dismiss_button_selector) {
                for (const btn of safeQueryAll(rowEl, table.dismiss_button_selector)) {
                    if (isVisible(btn) && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {
                        hasDismiss = true;
                        break;
                    }
                }
            }
            entityRows.push({
                table_id: table.table_id,
                entity_kind: table.entity_kind,
                row_index: rIdx,
                row_selector: computeDomSelector(rowEl),
                values,
                has_dismiss_action: hasDismiss,
            });
        }
    }

    // 3. Active Modals
    const activeModals = [];
    const seenModalNodes = new Set();

    for (const rule of (cfg.modalRules || [])) {
        for (const modalEl of safeQueryAll(document, rule.container_selector)) {
            if (!isOpenModal(modalEl) || seenModalNodes.has(modalEl)) continue;
            seenModalNodes.add(modalEl);
            let msgText = '';
            if (rule.message_selector) {
                let msgEl = null;
                try {
                    msgEl = modalEl.querySelector(rule.message_selector);
                } catch (e) {
                    msgEl = null;
                }
                msgText = msgEl ? cleanText(msgEl.textContent) : '';
            } else {
                msgText = cleanText(modalEl.textContent);
            }
            const containerSel = isUniqueMatch(rule.container_selector, modalEl)
                ? rule.container_selector
                : computeDomSelector(modalEl);
            activeModals.push({
                modal_id: rule.modal_id,
                container_selector: containerSel,
                message_text: msgText,
            });
            break;
        }
    }

    for (const sel of (cfg.modalDialogSelectors || [])) {
        for (const modalEl of safeQueryAll(document, sel)) {
            if (!isOpenModal(modalEl) || seenModalNodes.has(modalEl)) continue;
            let ancestorCaptured = false;
            for (const existing of seenModalNodes) {
                if (existing.contains(modalEl) || modalEl.contains(existing)) {
                    ancestorCaptured = true;
                    break;
                }
            }
            if (ancestorCaptured) continue;
            seenModalNodes.add(modalEl);
            const modalId = modalEl.id || modalEl.getAttribute('data-modal-id') || `modal_${seenModalNodes.size}`;
            activeModals.push({
                modal_id: modalId,
                container_selector: computeDomSelector(modalEl),
                message_text: cleanText(modalEl.textContent),
            });
        }
    }

    // 4. Banner Errors
    const bannerErrors = [];
    const seenBannerEls = new Set();
    const seenBannerTexts = new Set();
    for (const sel of (cfg.errorBannerSelectors || [])) {
        for (const el of safeQueryAll(document, sel)) {
            if (seenBannerEls.has(el) || !isVisible(el)) continue;
            seenBannerEls.add(el);
            const txt = cleanText(el.textContent);
            if (txt && !seenBannerTexts.has(txt)) {
                seenBannerTexts.add(txt);
                bannerErrors.push(txt);
            }
        }
    }

    // 5. Screen landmarks & body dataset
    const screenLandmarks = {};
    for (const scr of (cfg.screens || [])) {
        if (scr.landmark_selector) {
            screenLandmarks[scr.screen_id] = anyVisibleMatch([scr.landmark_selector]);
        }
    }
    const bodyScreenId = (document.body && document.body.dataset && document.body.dataset.screenId)
        ? cleanText(document.body.dataset.screenId)
        : '';

    return {
        title: cleanText(document.title || ''),
        auth_wall_present: anyVisibleMatch(cfg.authWallSelectors),
        authenticated_landmark_present: anyVisibleMatch(cfg.authenticatedLandmarkSelectors),
        screen_landmarks: screenLandmarks,
        body_screen_id: bodyScreenId,
        controls,
        entity_rows: entityRows,
        active_modals: activeModals,
        banner_errors: bannerErrors,
    };
}
"""


_EXTRACT_PORTAL_OPTIONS_JS = r"""
(cfg) => {
/*@DOM_HELPERS@*/
    const cleanText = (s) => String(s || '').replace(/\s+/g, ' ').trim();

    let triggerEl = null;
    if (cfg.triggerSelector) {
        const found = resolveSel(cfg.triggerSelector);
        triggerEl = found.length === 1 ? found[0] : null;
    }

    let candidateListboxes = [];
    const seenLb = new Set();
    if (triggerEl) {
        for (const attr of ['aria-controls', 'aria-owns']) {
            const refId = cleanText(triggerEl.getAttribute(attr));
            if (refId) {
                const lb = byIdIn(triggerEl, refId);
                if (lb && isVisible(lb) && !seenLb.has(lb)) {
                    seenLb.add(lb);
                    candidateListboxes.push(lb);
                }
            }
        }
    }
    if (candidateListboxes.length === 0) {
        for (const sel of (cfg.listboxSelectors || [])) {
            for (const lb of safeQueryAll(document, sel)) {
                if (isVisible(lb) && !seenLb.has(lb)) {
                    seenLb.add(lb);
                    candidateListboxes.push(lb);
                }
            }
        }
    }

    if (candidateListboxes.length === 0) {
        return { status: 'missing_listbox', options: [] };
    }
    if (candidateListboxes.length > 1) {
        return { status: 'ambiguous_listbox', count: candidateListboxes.length, options: [] };
    }

    const listbox = candidateListboxes[0];
    const seenOpts = new Set();
    const options = [];
    for (const optSel of (cfg.optionSelectors || [])) {
        for (const optEl of safeQueryAll(listbox, optSel)) {
            if (seenOpts.has(optEl) || !isVisible(optEl)) continue;
            seenOpts.add(optEl);
            const label = cleanText(optEl.textContent || optEl.getAttribute('aria-label') || '');
            let value = optEl.getAttribute('data-option-value');
            if (value === null) value = optEl.getAttribute('data-value');
            if (value === null) value = optEl.getAttribute('value');
            if (value === null) value = label;
            const selector = computeDomSelector(optEl);
            options.push({
                value: String(value),
                label: String(label),
                selector,
            });
        }
    }
    return { status: 'ok', options };
}
"""


# Label and value pairs shown as page text (definition lists, tables and
# "Label: value" lines). Kept out of the controls so nothing fills them; only
# read-back and output extraction use them. Open shadow roots are traversed;
# closed ones cannot be read.
_TEXT_PAIRS_JS = r"""
(cfg) => {
    const MAX_PAIRS = cfg.maxPairs || 500;
    const MAX_LEN = cfg.maxValueLength || 500;
    const MAX_LABEL = 120;
    const cleanText = (s) => String(s || '').replace(/\s+/g, ' ').trim();

    const parentOf = (el) => {
        if (el.parentElement) return el.parentElement;
        const root = el.getRootNode ? el.getRootNode() : null;
        return (root && root.host) ? root.host : null;
    };

    const isVisible = (el) => {
        if (!el || !(el instanceof Element)) return false;
        let cur = el;
        while (cur && cur !== document.documentElement) {
            if (cur.hasAttribute('hidden')) return false;
            if (cur.tagName === 'DIALOG' && !cur.open &&
                    cur.getAttribute('data-modal-open') !== 'true') {
                return false;
            }
            const cs = window.getComputedStyle(cur);
            if (cs.display === 'none' || cs.visibility === 'hidden' ||
                    cs.visibility === 'collapse') {
                return false;
            }
            cur = parentOf(cur);
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    // Hidden by style or attribute; an empty element with no size still counts
    // as shown, so an empty value reads as empty rather than vanishing.
    const isShown = (el) => {
        let cur = el;
        while (cur && cur !== document.documentElement) {
            if (cur.hasAttribute('hidden')) return false;
            const cs = window.getComputedStyle(cur);
            if (cs.display === 'none' || cs.visibility === 'hidden' ||
                    cs.visibility === 'collapse') {
                return false;
            }
            cur = parentOf(cur);
        }
        return true;
    };

    // Every open shadow root, nested ones included.
    const roots = [document];
    for (let i = 0; i < roots.length && roots.length < 2000; i += 1) {
        for (const el of roots[i].querySelectorAll('*')) {
            if (el.shadowRoot) roots.push(el.shadowRoot);
        }
    }
    const deepAll = (sel) => {
        const out = [];
        for (const r of roots) {
            try {
                for (const el of r.querySelectorAll(sel)) out.push(el);
            } catch (e) {}
        }
        return out;
    };

    const CONTROL_SEL = 'input:not([type="hidden"]), select, textarea, button, ' +
        '[role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"], [contenteditable="true"]';
    const STRIP_SEL = CONTROL_SEL + ', option, script, style, noscript, template';
    const hasControl = (el) => {
        try {
            return Boolean(el.querySelector(CONTROL_SEL));
        } catch (e) {
            return false;
        }
    };
    const textOf = (el) => {
        const clone = el.cloneNode(true);
        try {
            for (const child of Array.from(clone.querySelectorAll(STRIP_SEL))) child.remove();
        } catch (e) {}
        return cleanText(clone.textContent);
    };
    const normLabel = (s) => cleanText(String(s || '').replace(/[:：]\s*$/, '').replace(/\*\s*$/, ''));

    const HEADING_SEL = 'h1, h2, h3, h4, legend, caption';
    const headings = deepAll(HEADING_SEL).filter(isVisible);
    const sectionOf = (el) => {
        let best = '';
        const root = el.getRootNode();
        for (const h of headings) {
            if (h.getRootNode() !== root) continue;
            if (h === el || h.contains(el)) {
                if (h.tagName === 'CAPTION' || h.tagName === 'LEGEND') best = cleanText(h.textContent);
                continue;
            }
            if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
                best = cleanText(h.textContent);
            }
        }
        // A table's caption names the rows inside it.
        const table = el.closest ? el.closest('table') : null;
        if (table && table.caption && cleanText(table.caption.textContent)) {
            return cleanText(table.caption.textContent);
        }
        return best;
    };

    const pairs = [];
    const consumed = new Set();
    const push = (label, value, el, source) => {
        if (pairs.length >= MAX_PAIRS) return;
        const l = normLabel(label);
        if (!l || l.length > MAX_LABEL || !/[A-Za-z0-9À-￿]/.test(l)) return;
        pairs.push({
            label: l,
            value: cleanText(value).slice(0, MAX_LEN),
            section_text: sectionOf(el),
            source,
        });
    };
    const isConsumed = (el) => {
        let cur = el;
        for (let d = 0; cur && d < 12; d += 1) {
            if (consumed.has(cur)) return true;
            cur = parentOf(cur);
        }
        return false;
    };

    // 1. Definition lists: dt then its dd(s).
    for (const dl of deepAll('dl')) {
        if (!isVisible(dl) || hasControl(dl)) continue;
        const items = [];
        for (const child of Array.from(dl.children)) {
            if (child.tagName === 'DT' || child.tagName === 'DD') {
                items.push(child);
            } else if (child.tagName === 'DIV') {
                for (const g of Array.from(child.children)) {
                    if (g.tagName === 'DT' || g.tagName === 'DD') items.push(g);
                }
            }
        }
        let labels = [];
        let values = [];
        let anchor = null;
        const flush = () => {
            if (labels.length && anchor) {
                for (const l of labels) push(l, values.join(', '), anchor, 'dl');
            }
            labels = [];
            values = [];
            anchor = null;
        };
        // A hidden dd still ends its group: it counts as an empty value, so
        // a later dd is never joined onto earlier labels. A hidden dt starts a
        // group nobody can see, and its dd values are dropped.
        let hiddenGroup = false;
        for (const it of items) {
            consumed.add(it);
            const shown = isShown(it);
            if (it.tagName === 'DT') {
                if (values.length || !shown) flush();
                hiddenGroup = !shown;
                if (!shown) continue;
                labels.push(textOf(it));
                if (!anchor) anchor = it;
            } else if (hiddenGroup) {
                continue;
            } else if (labels.length) {
                values.push(shown ? textOf(it) : '');
            }
        }
        flush();
        consumed.add(dl);
    }

    // 2. Tables without form controls.
    for (const table of deepAll('table')) {
        if (!isVisible(table)) continue;
        const rows = Array.from(table.rows || []).filter(
            (r) => r.closest('table') === table && isVisible(r)
        );
        if (!rows.length) continue;
        const cellsOf = (r) => Array.from(r.cells || []);
        const first = cellsOf(rows[0]);
        const headerRow = first.length >= 2 && first.every((c) => c.tagName === 'TH');
        const dataRows = rows.slice(1);
        if (headerRow && dataRows.length === 1 && !hasControl(table)) {
            const cells = cellsOf(dataRows[0]);
            if (cells.length === first.length) {
                for (let i = 0; i < cells.length; i += 1) {
                    push(textOf(first[i]), textOf(cells[i]), cells[i], 'table_columns');
                }
                consumed.add(table);
                continue;
            }
        }
        for (const row of rows) {
            const cells = cellsOf(row);
            if (cells.length !== 2) continue;
            if (hasControl(row)) continue;
            if (cells[0].tagName === 'TH' && cells[1].tagName === 'TH') continue;
            if (cells[0].colSpan > 1 || cells[1].colSpan > 1) continue;
            push(textOf(cells[0]), textOf(cells[1]), cells[0], 'table_row');
            consumed.add(row);
        }
    }

    // 3. Two leaf children: <div><span>Label:</span><span>value</span></div>.
    const INLINE = new Set([
        'B', 'STRONG', 'EM', 'I', 'SPAN', 'SMALL', 'LABEL', 'ABBR', 'CODE', 'BR',
        'A', 'TIME', 'MARK', 'SUP', 'SUB', 'U', 'DATA', 'OUTPUT', 'BDI',
    ]);
    const isLeaf = (el) => Array.from(el.children).every((c) => INLINE.has(c.tagName) && c.children.length === 0);
    const BLOCK_SEL = 'p, li, div, span, td, dd';
    const emitted = new Set();
    for (const el of deepAll(BLOCK_SEL)) {
        if (pairs.length >= MAX_PAIRS) break;
        if (isConsumed(el) || !isVisible(el) || hasControl(el)) continue;
        const kids = Array.from(el.children).filter((c) => c.tagName !== 'BR' && textOf(c));
        if (kids.length !== 2) continue;
        if (!kids.every((k) => k.children.length === 0 || isLeaf(k))) continue;
        const loose = Array.from(el.childNodes).filter(
            (n) => n.nodeType === Node.TEXT_NODE && cleanText(n.textContent)
        );
        if (loose.length) continue;
        const firstText = textOf(kids[0]);
        const labelish = /[:：]\s*$/.test(firstText) ||
            ['DT', 'TH', 'LABEL', 'STRONG', 'B'].includes(kids[0].tagName);
        if (!labelish) continue;
        push(firstText, textOf(kids[1]), el, 'inline');
        emitted.add(el);
        consumed.add(el);
    }

    // 4. "Label: value" text in a leaf block.
    const LINE_RE = /^([^:：]{1,120})[:：]\s*([\s\S]*)$/;
    const candidates = deepAll(BLOCK_SEL).filter(
        (el) => !isConsumed(el) && isLeaf(el) && !hasControl(el) && isVisible(el)
    );
    const candSet = new Set(candidates);
    for (const el of candidates) {
        if (pairs.length >= MAX_PAIRS) break;
        let up = parentOf(el);
        let nested = false;
        for (let d = 0; up && d < 6; d += 1) {
            if (candSet.has(up)) {
                nested = true;
                break;
            }
            up = parentOf(up);
        }
        if (nested) continue;
        const text = textOf(el);
        const m = LINE_RE.exec(text);
        if (!m) continue;
        if (/^\/\//.test(m[2])) continue; // a URL, not a label
        push(m[1], m[2], el, 'inline');
    }
    return pairs;
}
"""

# Every in-page script shares one set of composed-tree helpers.
_CHECK_BUSY_AND_DOM_JS = _CHECK_BUSY_AND_DOM_JS.replace(
    "/*@DOM_HELPERS@*/", _DOM_HELPERS_JS
)
_SNAPSHOT_WALKER_JS = _SNAPSHOT_WALKER_JS.replace("/*@DOM_HELPERS@*/", _DOM_HELPERS_JS)
_EXTRACT_PORTAL_OPTIONS_JS = _EXTRACT_PORTAL_OPTIONS_JS.replace(
    "/*@DOM_HELPERS@*/", _DOM_HELPERS_JS
)


class PlaywrightFormSurface:
    """Form backend for a Page, a native Frame, or a bounded frame route.

    ``frame_path_patterns`` are ordered regular expressions, full-matched against
    observed frame URL paths. The first matching pattern must identify exactly
    one attached frame on ``base_origin`` (or the Page's initial HTTP origin).
    Unknown, cross-origin and ambiguous contexts are refused. Paths are pack
    data (``UIProfileSpec.frame_path_patterns``, used when the argument is
    empty); no application routing rules live in this backend.

    Controls inside open shadow roots are observed and acted on like any
    others; their selectors read ``<host selector> >> <selector inside the
    root>``. Closed shadow roots cannot be read and stay invisible.
    """

    def __init__(
        self,
        page: Any,
        ui_profile: UIProfileSpec,
        *,
        base_origin: Optional[str] = None,
        keepalive_mutation_guard: bool = True,
        ignored_mutation_paths: tuple[str, ...] = (),
        _scoped_to_page_only: bool = False,
        frame_path_patterns: tuple[str, ...] = (),
    ) -> None:
        self._page = page
        self._owner_page = getattr(page, "page", None) or page
        self._is_frame = self._owner_page is not page
        # The UI profile carries frame routing as data; an explicit argument wins.
        frame_path_patterns = frame_path_patterns or ui_profile.frame_path_patterns
        self._frame_path_patterns = tuple(re.compile(p) for p in frame_path_patterns)
        origin_url = base_origin or str(self._owner_page.url or "")
        self._frame_origin = self._url_origin(origin_url)
        if self._frame_path_patterns and self._frame_origin is None:
            raise ValueError(
                "Frame routing requires an explicit or loaded HTTP(S) origin"
            )
        self._last_snapshot_context_id: Optional[str] = None
        self._ui_profile = ui_profile
        self._base_origin = base_origin.rstrip("/") if base_origin else None
        self._keepalive_mutation_guard = keepalive_mutation_guard
        self._ignored_mutation_paths = tuple(ignored_mutation_paths)
        self._scoped_to_page_only = _scoped_to_page_only

        self._mutation_count = 0
        self._total_mutation_count = 0
        self._all_time_raw_mutations = 0
        self._inflight_mutation_ids: set[int] = set()
        self._mutation_history: list[tuple[str, str]] = []
        self._detached = False

        self._quiescence = PageQuiescence(
            self._owner_page, root_frame=page if self._is_frame else None
        )
        try:
            self._owner_page.evaluate(QUIESCENCE_INSTALL_JS)
            if self._is_frame:
                page.evaluate(QUIESCENCE_INSTALL_JS)
        except Exception:
            pass

        self._event_target: Any = (
            self._owner_page
            if self._is_frame or self._frame_path_patterns
            else getattr(page, "context", None) or page
        )
        try:
            self._event_target.on("request", self._on_request)
            self._event_target.on("requestfinished", self._on_request_done)
            self._event_target.on("requestfailed", self._on_request_done)
        except Exception:
            pass

    @staticmethod
    def _url_origin(url: str) -> Optional[tuple[str, str, int]]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return (
            parts.scheme,
            parts.hostname,
            parts.port or (443 if parts.scheme == "https" else 80),
        )

    def _refresh_target(self) -> None:
        """Select one allowed same-origin document; never fall back to a shell."""
        self._assert_available(check_frame=False)
        if self._frame_path_patterns:
            for pattern in self._frame_path_patterns:
                matches = [
                    frame
                    for frame in self._owner_page.frames
                    if not frame.is_detached()
                    and pattern.fullmatch(urlsplit(frame.url).path or "/")
                ]
                if any(
                    self._url_origin(frame.url) != self._frame_origin
                    for frame in matches
                ):
                    raise SurfaceContextError(
                        "Allowed frame path is on an untrusted origin"
                    )
                if len(matches) > 1:
                    raise SurfaceContextError("Allowed frame path is ambiguous")
                if matches:
                    self._page = matches[0]
                    self._is_frame = True
                    self._quiescence.set_root_frame(self._page)
                    break
            else:
                raise _NoMatchingFrameError(
                    "No attached frame matches the allowed origin and paths"
                )
        self._assert_available()

    def _popup_closed_itself(self) -> bool:
        """A child popup window may close itself after its apply button.

        That is the popup's normal end, not a lost form: the parent surface
        settles and reads back the result. Frames and main pages never count.
        """
        return (
            self._scoped_to_page_only
            and not self._is_frame
            and not self._detached
            and self._is_page_closed()
        )

    def _assert_available(self, *, check_frame: bool = True) -> None:
        if self._detached or self._owner_page.is_closed():
            raise SurfaceContextError("Form surface Page is closed or released")
        if check_frame and self._is_frame and self._page.is_detached():
            raise SurfaceContextError(
                "Form surface Frame is detached; observe a fresh context"
            )

    def _document_context_id(self) -> str:
        self._assert_available()
        raw = self._page.evaluate(_DOCUMENT_CONTEXT_JS)
        # Full location is bound, including query/hash record identity, but never
        # copied into the structural snapshot or human-facing halt.
        return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()

    @property
    def ui_profile(self) -> UIProfileSpec:
        return self._ui_profile

    @property
    def mutation_count(self) -> int:
        """Count of non-ignored state-changing HTTP requests since construction/reset."""
        return self._mutation_count

    @property
    def total_mutation_count(self) -> int:
        """Count of all state-changing HTTP requests (including ignored UI PPR paths)."""
        return self._total_mutation_count

    @property
    def ignored_mutation_paths(self) -> tuple[str, ...]:
        return self._ignored_mutation_paths

    @property
    def inflight_mutations(self) -> int:
        return len(self._inflight_mutation_ids)

    def reset_mutation_counter(self) -> None:
        self._mutation_count = 0
        self._total_mutation_count = 0

    def _detach(self) -> None:
        if self._detached:
            return
        self._detached = True
        self._quiescence.detach()
        for ev_name, cb in (
            ("request", self._on_request),
            ("requestfinished", self._on_request_done),
            ("requestfailed", self._on_request_done),
        ):
            try:
                self._event_target.remove_listener(ev_name, cb)
            except Exception:
                pass

    def _request_belongs_to_surface(self, request: Any) -> bool:
        if not (
            self._scoped_to_page_only or self._is_frame or self._frame_path_patterns
        ):
            return True
        try:
            frame = request.frame
            return frame is not None and frame.page == self._owner_page
        except Exception:
            return False

    def _is_ignored_mutation_path(self, url_path: str) -> bool:
        for pat in self._ignored_mutation_paths:
            if url_path == pat or fnmatch.fnmatchcase(url_path, pat):
                return True
        return False

    def _on_request(self, request: Any) -> None:
        if self._detached or not self._request_belongs_to_surface(request):
            return
        try:
            method = str(request.method or "").upper()
            raw_url = str(request.url or "")
        except Exception:
            return
        if method not in _MUTATION_METHODS:
            return
        self._inflight_mutation_ids.add(id(request))
        self._all_time_raw_mutations += 1
        self._total_mutation_count += 1
        clean_url = _strip_url_to_origin_and_path(raw_url)
        self._mutation_history.append((method, clean_url))
        req_path = urlsplit(raw_url).path or "/"
        if not self._is_ignored_mutation_path(req_path):
            self._mutation_count += 1

    def _on_request_done(self, request: Any) -> None:
        self._inflight_mutation_ids.discard(id(request))

    def _is_page_closed(self) -> bool:
        try:
            return bool(self._owner_page.is_closed())
        except Exception:
            return True

    def _sleep_ms(self, duration_ms: float) -> None:
        ms = max(0.0, float(duration_ms))
        if ms <= 0.0 or self._is_page_closed():
            return
        try:
            self._owner_page.wait_for_timeout(ms)
        except Exception as exc:
            if _is_target_closed_error(exc) or self._is_page_closed():
                return
            time.sleep(ms / 1000.0)

    def _read_dom_state(self, busy_selectors: list[str]) -> dict[str, Any]:
        frames = self._quiescence._live_frames()
        if not frames:
            raise SurfaceContextError("Form document subtree is unavailable")
        states = [
            frame.evaluate(_CHECK_BUSY_AND_DOM_JS, busy_selectors) for frame in frames
        ]
        return {
            "readyState": "complete"
            if all(s.get("readyState") == "complete" for s in states)
            else "loading",
            "busySelector": next(
                (s.get("busySelector") for s in states if s.get("busySelector")), ""
            ),
            "domSig": repr([s.get("domSig") for s in states]),
        }

    def wait_for_quiescence(self) -> None:
        """Block until debounce grace elapses, busy selectors clear, and DOM+network settle."""
        if self._popup_closed_itself():
            return
        self._assert_available(check_frame=False)

        max_settle_s = self._ui_profile.max_settle_ms / 1000.0
        deadline = time.monotonic() + max_settle_s

        if self._ui_profile.debounce_grace_ms > 0:
            remaining_ms = max(0.0, (deadline - time.monotonic()) * 1000.0)
            self._sleep_ms(min(float(self._ui_profile.debounce_grace_ms), remaining_ms))

        window_s = self._ui_profile.settle_window_ms / 1000.0
        busy_selectors = list(self._ui_profile.busy_selectors)
        last_reason = "initializing"
        fallback_sig: Optional[str] = None
        fallback_since: float = 0.0

        while time.monotonic() < deadline:
            if self._popup_closed_itself():
                return
            try:
                self._refresh_target()
            except _NoMatchingFrameError:
                # A navigation may detach a dialog before attaching its allowed
                # successor. No document is read or acted on during that gap.
                last_reason = "busy:no_matching_frame"
                fallback_sig = None
                self._sleep_ms(
                    min(25.0, max(1.0, (deadline - time.monotonic()) * 1000.0))
                )
                continue

            try:
                dom_state = self._read_dom_state(busy_selectors)
            except Exception as exc:
                if _is_target_closed_error(exc) or self._is_page_closed():
                    raise SurfaceContextError(
                        "Form surface closed while settling"
                    ) from exc
                last_reason = f"busy:eval_error:{type(exc).__name__}"
                fallback_sig = None
                self._sleep_ms(
                    min(25.0, max(1.0, (deadline - time.monotonic()) * 1000.0))
                )
                continue

            ready_state = dom_state.get("readyState", "")
            busy_sel = dom_state.get("busySelector", "")
            dom_sig = str(dom_state.get("domSig", ""))

            if ready_state != "complete":
                last_reason = f"busy:ready_state:{ready_state}"
                fallback_sig = None
                self._sleep_ms(
                    min(25.0, max(1.0, (deadline - time.monotonic()) * 1000.0))
                )
                continue

            if busy_sel:
                last_reason = f"busy:selector:{busy_sel}"
                fallback_sig = None
                self._sleep_ms(
                    min(25.0, max(1.0, (deadline - time.monotonic()) * 1000.0))
                )
                continue

            if self._inflight_mutation_ids or self._quiescence._inflight:
                last_reason = "busy:inflight_network"
                fallback_sig = None
                self._sleep_ms(
                    min(25.0, max(1.0, (deadline - time.monotonic()) * 1000.0))
                )
                continue

            probe_verdict = self._quiescence.probe(window_s=window_s)
            if probe_verdict is True:
                try:
                    recheck = self._read_dom_state(busy_selectors)
                except Exception as exc:
                    if _is_target_closed_error(exc) or self._is_page_closed():
                        raise SurfaceContextError(
                            "Form surface closed while settling"
                        ) from exc
                    last_reason = "busy:post_probe_eval"
                    self._sleep_ms(20.0)
                    continue
                if (
                    recheck.get("readyState") == "complete"
                    and not recheck.get("busySelector")
                    and not self._inflight_mutation_ids
                    and not self._quiescence._inflight
                ):
                    return
                last_reason = "busy:post_probe_changed"
                self._sleep_ms(20.0)
                continue

            if probe_verdict is False:
                last_reason = self._quiescence.last_reason or "busy:probe"
                fallback_sig = None
                retry_ms = min(max(15.0, self._quiescence.retry_after_s * 1000.0), 50.0)
                self._sleep_ms(
                    min(retry_ms, max(1.0, (deadline - time.monotonic()) * 1000.0))
                )
                continue

            # probe_verdict is None (e.g. visible <canvas> / unavailable frame):
            # require full window_s of network quiet + stable DOM signature.
            now_mono = time.monotonic()
            if (now_mono - self._quiescence._last_network) < window_s:
                last_reason = "busy:fallback_network_recent"
                fallback_sig = None
                self._sleep_ms(min(25.0, max(1.0, (deadline - now_mono) * 1000.0)))
                continue

            if fallback_sig != dom_sig:
                fallback_sig = dom_sig
                fallback_since = now_mono
            elif (
                (now_mono - fallback_since) >= window_s
                and self._quiescence._last_network <= fallback_since
                and not self._quiescence._inflight
                and not self._inflight_mutation_ids
            ):
                return

            last_reason = f"busy:fallback_dom_settle:{self._quiescence.last_reason}"
            step_ms = min(max(20.0, self._ui_profile.settle_window_ms / 2.0), 50.0)
            self._sleep_ms(
                min(step_ms, max(1.0, (deadline - time.monotonic()) * 1000.0))
            )

        if last_reason == "busy:no_matching_frame":
            raise SurfaceContextError(
                "No allowed frame became available before the settle deadline"
            )
        self._assert_available()
        raise SurfaceSettleTimeoutError(
            f"Form surface failed to reach quiescence within {self._ui_profile.max_settle_ms}ms "
            f"(last_reason={last_reason!r})"
        )

    def _resolve_screen_id(
        self,
        screens: Sequence[WizardScreenSpec],
        page_title: str,
        screen_landmarks: dict[str, bool],
        body_screen_id: str,
    ) -> Optional[str]:
        for spec in screens:
            has_landmark = spec.landmark_selector is not None
            has_title = spec.title_pattern is not None
            if not has_landmark and not has_title:
                continue
            landmark_ok = (
                bool(screen_landmarks.get(spec.screen_id)) if has_landmark else True
            )
            title_ok = (
                bool(
                    re.search(spec.title_pattern or "", page_title, flags=re.IGNORECASE)
                )
                if has_title
                else True
            )
            if landmark_ok and title_ok:
                return spec.screen_id
        return body_screen_id or None

    def snapshot(
        self,
        *,
        entity_tables: Sequence[object] = (),
        modal_rules: Sequence[object] = (),
        screens: Sequence[object] = (),
    ) -> FormSnapshot:
        """Wait for surface quiescence and capture a fresh ``FormSnapshot``."""
        self.wait_for_quiescence()

        parsed_tables = [
            t if isinstance(t, EntityTableSpec) else EntityTableSpec.model_validate(t)
            for t in entity_tables
        ]
        parsed_modals = [
            m if isinstance(m, ModalRuleSpec) else ModalRuleSpec.model_validate(m)
            for m in modal_rules
        ]
        parsed_screens = [
            s if isinstance(s, WizardScreenSpec) else WizardScreenSpec.model_validate(s)
            for s in screens
        ]

        walker_cfg = {
            "errorBannerSelectors": list(self._ui_profile.error_banner_selectors),
            "fieldErrorSelectors": list(self._ui_profile.field_error_selectors),
            "modalDialogSelectors": list(self._ui_profile.modal_dialog_selectors),
            "authWallSelectors": list(self._ui_profile.auth_wall_selectors),
            "authenticatedLandmarkSelectors": list(
                self._ui_profile.authenticated_landmark_selectors
            ),
            "entityTables": [
                {
                    "table_id": t.table_id,
                    "entity_kind": t.entity_kind,
                    "row_selector": t.row_selector,
                    "column_selectors": dict(t.column_selectors),
                    "dismiss_button_selector": t.dismiss_button_selector,
                }
                for t in parsed_tables
            ],
            "modalRules": [
                {
                    "modal_id": m.modal_id,
                    "container_selector": m.container_selector,
                    "message_selector": m.message_selector,
                }
                for m in parsed_modals
            ],
            "screens": [
                {
                    "screen_id": s.screen_id,
                    "landmark_selector": s.landmark_selector,
                }
                for s in parsed_screens
            ],
        }

        context_id = self._document_context_id()
        raw = self._page.evaluate(_SNAPSHOT_WALKER_JS, walker_cfg)
        text_pairs = self._read_text_pairs()
        if self._document_context_id() != context_id:
            raise SurfaceContextError("Form document changed while taking a snapshot")
        self._last_snapshot_context_id = context_id
        url_clean = _strip_url_to_origin_and_path(str(self._page.url or ""))
        title = str(raw.get("title") or "")

        controls_list: list[ObservedControl] = []
        for item in raw.get("controls") or ():
            is_secret = bool(item.get("is_secret"))
            cur_val = str(item.get("current_value") or "")
            if is_secret and cur_val:
                cur_val = "<secret>"
            opts = tuple(
                ObservedOption(
                    value=str(o.get("value") or ""),
                    label=str(o.get("label") or ""),
                )
                for o in (item.get("options") or ())
            )
            controls_list.append(
                ObservedControl(
                    control_id=str(item["control_id"]),
                    dom_selector=str(item["dom_selector"]),
                    tag=str(item.get("tag") or ""),
                    input_type=str(item.get("input_type") or ""),
                    role=str(item.get("role") or ""),
                    name_attr=str(item.get("name_attr") or ""),
                    id_attr=str(item.get("id_attr") or ""),
                    label_text=str(item.get("label_text") or ""),
                    section_text=str(item.get("section_text") or ""),
                    current_value=cur_val,
                    checked=item.get("checked"),
                    required=bool(item.get("required")),
                    disabled=bool(item.get("disabled")),
                    readonly=bool(item.get("readonly")),
                    visible=bool(item.get("visible", True)),
                    error_text=str(item.get("error_text") or ""),
                    options=opts,
                    is_secret=is_secret,
                    radio_value=str(item.get("radio_value") or ""),
                    radio_group=str(item.get("radio_group") or ""),
                    group_label=str(item.get("group_label") or ""),
                )
            )

        entity_rows_list = tuple(
            ObservedEntityRow(
                table_id=str(r["table_id"]),
                entity_kind=str(r["entity_kind"]),
                row_index=int(r["row_index"]),
                row_selector=str(r["row_selector"]),
                values={str(k): str(v) for k, v in (r.get("values") or {}).items()},
                has_dismiss_action=bool(r.get("has_dismiss_action")),
            )
            for r in (raw.get("entity_rows") or ())
        )

        active_modals_list = tuple(
            ObservedModal(
                modal_id=str(m["modal_id"]),
                container_selector=str(m["container_selector"]),
                message_text=str(m.get("message_text") or ""),
            )
            for m in (raw.get("active_modals") or ())
        )

        screen_id = self._resolve_screen_id(
            parsed_screens,
            page_title=title,
            screen_landmarks=dict(raw.get("screen_landmarks") or {}),
            body_screen_id=str(raw.get("body_screen_id") or ""),
        )

        return FormSnapshot(
            url_origin_and_path=url_clean,
            context_id=context_id,
            title=title,
            screen_id=screen_id,
            auth_wall_present=bool(raw.get("auth_wall_present")),
            authenticated_landmark_present=bool(
                raw.get("authenticated_landmark_present")
            ),
            controls=tuple(controls_list),
            entity_rows=entity_rows_list,
            active_modals=active_modals_list,
            banner_errors=tuple(str(b) for b in (raw.get("banner_errors") or ())),
            inflight_mutations=self.inflight_mutations,
            text_pairs=text_pairs,
        )

    def _read_text_pairs(self) -> tuple[ObservedTextPair, ...]:
        """Read label and value pairs shown as page text.

        A failure to read them leaves the tuple empty, so any read-back that
        needs them ends ``not_checked``, never passed.
        """
        try:
            raw_pairs = self._page.evaluate(
                _TEXT_PAIRS_JS, {"maxPairs": 500, "maxValueLength": 500}
            )
        except Exception:
            return ()
        pairs: list[ObservedTextPair] = []
        for item in raw_pairs or ():
            source = str(item.get("source") or "inline")
            if source not in ("dl", "table_row", "table_columns", "inline"):
                source = "inline"
            pairs.append(
                ObservedTextPair(
                    label=str(item.get("label") or ""),
                    value=str(item.get("value") or ""),
                    section_text=str(item.get("section_text") or ""),
                    source=source,  # type: ignore[arg-type]
                )
            )
        return tuple(pairs)

    def _resolve_live_locator(self, dom_selector: str) -> Any:
        self._refresh_target()
        if (
            self._last_snapshot_context_id is not None
            and self._document_context_id() != self._last_snapshot_context_id
        ):
            raise SurfaceContextError(
                "Form document changed; take a fresh snapshot before acting"
            )
        if not dom_selector or not dom_selector.strip():
            raise MissingAnchorError("Empty dom_selector")
        loc = self._page.locator(dom_selector)
        total = loc.count()
        if total == 0:
            raise MissingAnchorError(
                f"Selector {dom_selector!r} matched 0 elements in live DOM"
            )
        if total > 1 and " >> " in dom_selector:
            # A shadow-root selector is emitted unique across its host's whole
            # subtree. More than one match means a light-DOM or slotted node
            # (visible or not) shares it, so the observed element is not
            # certain: refuse rather than act on the one that happens to show.
            raise AmbiguousAnchorError(
                f"Shadow-root selector {dom_selector!r} matched {total} elements "
                f"in live DOM"
            )
        if total == 1:
            if not loc.is_visible():
                raise MissingAnchorError(
                    f"Selector {dom_selector!r} matched 1 element, but it is not visible"
                )
            return loc
        visible_indices = [i for i in range(total) if loc.nth(i).is_visible()]
        if not visible_indices:
            raise MissingAnchorError(
                f"Selector {dom_selector!r} matched {total} elements, none visible"
            )
        if len(visible_indices) > 1:
            raise AmbiguousAnchorError(
                f"Selector {dom_selector!r} matched {len(visible_indices)} visible elements"
            )
        return loc.nth(visible_indices[0])

    def _resolve_navigation_url(self, url_or_path: str) -> str:
        parsed = urlsplit(url_or_path)
        if parsed.scheme in ("http", "https", "about", "file"):
            return url_or_path
        if self._base_origin:
            return urljoin(self._base_origin + "/", url_or_path)
        cur = urlsplit(str(self._page.url or ""))
        if cur.scheme in ("http", "https") and cur.netloc:
            return urljoin(f"{cur.scheme}://{cur.netloc}/", url_or_path)
        raise ValueError(
            f"Cannot resolve relative URL {url_or_path!r} without base_origin or loaded HTTP page"
        )

    def goto(self, url_or_path: str) -> None:
        """Navigate to ``url_or_path`` and wait for quiescence."""
        self._refresh_target()
        target = self._resolve_navigation_url(url_or_path)
        self._page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=self._ui_profile.max_settle_ms,
        )
        self.wait_for_quiescence()

    def fill_text(self, dom_selector: str, value: str) -> None:
        """Focus, clear, enter ``value`` into ``dom_selector``, blur, and wait for settle."""
        loc = self._resolve_live_locator(dom_selector)
        loc.click(timeout=self._ui_profile.max_settle_ms)
        loc.fill(str(value), timeout=self._ui_profile.max_settle_ms)
        try:
            loc.blur(timeout=500)
        except Exception:
            # An onchange handler may have already detached/re-rendered the input.
            pass
        self.wait_for_quiescence()

    def select_native_option(self, dom_selector: str, option_value: str) -> None:
        """Select ``option_value`` in a native ``<select>`` and wait for settle."""
        loc = self._resolve_live_locator(dom_selector)
        has_exact_value = bool(
            loc.evaluate(
                "(el, val) => el.tagName === 'SELECT' && Array.from(el.options).some((o) => o.value === val)",
                option_value,
            )
        )
        if has_exact_value:
            loc.select_option(
                value=option_value, timeout=self._ui_profile.max_settle_ms
            )
        else:
            has_label = bool(
                loc.evaluate(
                    "(el, val) => el.tagName === 'SELECT' && Array.from(el.options).some((o) => (o.textContent || '').replace(/\\s+/g, ' ').trim() === val)",
                    collapse_whitespace(option_value),
                )
            )
            if not has_label:
                raise MissingAnchorError(
                    f"Option {option_value!r} not found in <select> {dom_selector!r}"
                )
            loc.select_option(
                label=collapse_whitespace(option_value),
                timeout=self._ui_profile.max_settle_ms,
            )
        try:
            loc.blur(timeout=500)
        except Exception:
            pass
        self.wait_for_quiescence()

    _CHECK_STATE_JS = (
        "(el) => ({ tag: el.tagName.toLowerCase(), "
        "type: (el.getAttribute('type') || '').toLowerCase(), "
        "role: (el.getAttribute('role') || '').toLowerCase(), "
        "name: el.getAttribute('name') || '', value: el.getAttribute('value'), "
        "ariaChecked: el.getAttribute('aria-checked'), "
        "propChecked: Boolean(el.checked) })"
    )

    def _read_check_state(self, dom_selector: str) -> dict[str, Any]:
        loc = self._resolve_live_locator(dom_selector)
        return dict(loc.evaluate(self._CHECK_STATE_JS))

    def set_checkbox_or_radio(self, dom_selector: str, checked: bool = True) -> None:
        """Set a checkbox or radio by clicking it, then confirm the state took.

        A click, not ``Locator.check``: many pages replace the element on
        change, and ``check`` waits on the original element until it times
        out. After the click the control is found again by a stable selector
        (``name`` plus ``value`` for a native radio or checkbox, else the
        original selector) and its state is re-read. If the state did not
        take, a ``SurfaceStateError`` is raised, which stops the run.
        """
        meta = self._read_check_state(dom_selector)
        tag, inp_type, role = meta.get("tag"), meta.get("type"), meta.get("role")
        native = tag == "input" and inp_type in ("radio", "checkbox")
        if not native and role not in ("checkbox", "radio", "switch"):
            raise ValueError(
                f"Element {dom_selector!r} is not a checkbox or radio control"
            )
        if native:
            current = bool(meta.get("propChecked"))
        else:
            current = meta.get("ariaChecked") == "true"
        if native and inp_type == "radio" and not checked:
            if current:
                raise ValueError(
                    f"Cannot uncheck native radio {dom_selector!r} directly"
                )
            return

        # Where to find the control again if the page re-renders it.
        recheck_selector = dom_selector
        name, value = meta.get("name") or "", meta.get("value")
        if native and name:
            stable = f'input[type="{inp_type}"][name={_css_string(name)}]'
            if value is not None:
                stable += f"[value={_css_string(str(value))}]"
            if " >> " in dom_selector:
                stable = dom_selector.rsplit(" >> ", 1)[0] + " >> " + stable
            recheck_selector = stable

        # The control's value-free identity, to find it again if the page
        # redraws it with new ids and names.
        identity = self._check_identity(dom_selector)

        if current != checked:
            loc = self._resolve_live_locator(dom_selector)
            loc.click(timeout=self._ui_profile.max_settle_ms)
            try:
                loc.blur(timeout=500)
            except Exception:
                pass
        self.wait_for_quiescence()

        try:
            after = self._read_check_state(recheck_selector)
        except (MissingAnchorError, AmbiguousAnchorError) as exc:
            redrawn = self._find_by_check_identity(identity)
            if redrawn is None:
                raise SurfaceStateError(
                    f"Could not find {dom_selector!r} again after setting it: {exc}"
                ) from exc
            now = bool(redrawn.checked)
        else:
            if native:
                now = bool(after.get("propChecked"))
            else:
                now = after.get("ariaChecked") == "true"
        if now != checked:
            raise SurfaceStateError(
                f"{dom_selector!r} did not become "
                f"{'checked' if checked else 'unchecked'} after a click"
            )

    @staticmethod
    def _identity_of(ctrl: ObservedControl) -> tuple[str, ...]:
        return (
            " ".join(ctrl.section_text.split()).casefold(),
            " ".join(ctrl.group_label.split()).casefold(),
            " ".join(ctrl.label_text.split()).casefold(),
            ctrl.input_type.casefold(),
            ctrl.role.casefold(),
            ctrl.radio_value,
        )

    def _check_identity(self, dom_selector: str) -> Optional[tuple[str, ...]]:
        """Section, labels, kind and own value of a checkbox or radio, if labelled."""
        try:
            snap = self.snapshot()
        except Exception:
            return None
        ctrl = next((c for c in snap.controls if c.dom_selector == dom_selector), None)
        if ctrl is None or not (ctrl.label_text.strip() or ctrl.group_label.strip()):
            return None
        return self._identity_of(ctrl)

    def _find_by_check_identity(
        self, identity: Optional[tuple[str, ...]]
    ) -> Optional[ObservedControl]:
        """The one visible control with ``identity`` after a redraw, or None."""
        if identity is None:
            return None
        snap = self.snapshot()
        same = [
            c for c in snap.controls if c.visible and self._identity_of(c) == identity
        ]
        return same[0] if len(same) == 1 else None

    def click(self, dom_selector: str) -> None:
        """Click ``dom_selector`` and wait for settle."""
        loc = self._resolve_live_locator(dom_selector)
        try:
            loc.click(timeout=self._ui_profile.max_settle_ms)
        except Exception as exc:
            if (
                self._scoped_to_page_only
                and not self._is_frame
                and _is_target_closed_error(exc)
                and self._is_page_closed()
            ):
                return
            raise
        # A child popup may deliberately close itself after posting its result.
        # Its parent context manager still settles/read-backs the owning surface.
        if self._scoped_to_page_only and not self._is_frame and self._is_page_closed():
            return
        self.wait_for_quiescence()

    def _read_portal_listbox_state(self, dom_selector: str) -> dict[str, Any]:
        return dict(
            self._page.evaluate(
                _EXTRACT_PORTAL_OPTIONS_JS,
                {
                    "triggerSelector": dom_selector,
                    "listboxSelectors": list(self._ui_profile.portal_listbox_selectors),
                    "optionSelectors": list(self._ui_profile.portal_option_selectors),
                },
            )
        )

    def open_portal_combobox_options(
        self, dom_selector: str
    ) -> tuple[ObservedOption, ...]:
        """Open a detached/portal combobox and read its rendered options."""
        loc = self._resolve_live_locator(dom_selector)
        aria_expanded = (loc.get_attribute("aria-expanded") or "").strip().lower()
        existing = self._read_portal_listbox_state(dom_selector)
        if aria_expanded != "true" or existing.get("status") != "ok":
            loc.click(timeout=self._ui_profile.max_settle_ms)
            self.wait_for_quiescence()

        deadline = time.monotonic() + (self._ui_profile.max_settle_ms / 1000.0)
        state = self._read_portal_listbox_state(dom_selector)
        while state.get("status") == "missing_listbox" and time.monotonic() < deadline:
            self._sleep_ms(25.0)
            state = self._read_portal_listbox_state(dom_selector)

        status = state.get("status")
        if status == "ambiguous_listbox":
            raise AmbiguousAnchorError(
                f"Multiple visible portal listboxes ({state.get('count')}) open for {dom_selector!r}"
            )
        if status != "ok":
            raise MissingAnchorError(
                f"No visible portal listbox matched {self._ui_profile.portal_listbox_selectors} "
                f"after opening {dom_selector!r}"
            )

        return tuple(
            ObservedOption(
                value=str(o.get("value") or ""),
                label=str(o.get("label") or ""),
            )
            for o in (state.get("options") or ())
        )

    def type_portal_combobox_query(
        self, dom_selector: str, query: str
    ) -> tuple[ObservedOption, ...]:
        """Type ``query`` into an editable combobox and read the suggestions.

        Clears the input, sends one key event per character (so input and
        keyup handlers that fetch suggestions run as they would for a person),
        waits for the surface to settle, then reads the one visible listbox.
        Raises ``MissingAnchorError`` when no listbox appears and
        ``AmbiguousAnchorError`` when several do.
        """
        if not query:
            raise MissingAnchorError(
                f"Empty type-ahead query for {dom_selector!r}; nothing to type"
            )
        loc = self._resolve_live_locator(dom_selector)
        if not loc.is_editable():
            raise MissingAnchorError(
                f"Combobox {dom_selector!r} does not accept typed text"
            )
        timeout = self._ui_profile.max_settle_ms
        loc.click(timeout=timeout)
        loc.fill("", timeout=timeout)
        loc.press_sequentially(str(query), delay=25, timeout=timeout)
        self.wait_for_quiescence()

        deadline = time.monotonic() + (timeout / 1000.0)
        state = self._read_portal_listbox_state(dom_selector)
        while state.get("status") == "missing_listbox" and time.monotonic() < deadline:
            self._sleep_ms(25.0)
            state = self._read_portal_listbox_state(dom_selector)

        status = state.get("status")
        if status == "ambiguous_listbox":
            raise AmbiguousAnchorError(
                f"Multiple visible portal listboxes ({state.get('count')}) open for {dom_selector!r}"
            )
        if status != "ok":
            raise MissingAnchorError(
                f"No suggestions appeared after typing into {dom_selector!r}"
            )
        return tuple(
            ObservedOption(
                value=str(o.get("value") or ""),
                label=str(o.get("label") or ""),
            )
            for o in (state.get("options") or ())
        )

    def choose_portal_combobox_option(
        self, dom_selector: str, option: ObservedOption
    ) -> None:
        """Choose ``option`` in an open portal combobox and wait for settle."""
        state = self._read_portal_listbox_state(dom_selector)
        if state.get("status") != "ok":
            self.open_portal_combobox_options(dom_selector)
            state = self._read_portal_listbox_state(dom_selector)

        if state.get("status") == "ambiguous_listbox":
            raise AmbiguousAnchorError(
                f"Multiple visible portal listboxes ({state.get('count')}) open for {dom_selector!r}"
            )
        if state.get("status") != "ok":
            raise MissingAnchorError(
                f"Portal listbox is not open when choosing option {option!r} on {dom_selector!r}"
            )

        raw_options = list(state.get("options") or ())
        target_label = collapse_whitespace(option.label)
        exact_matches = [
            o
            for o in raw_options
            if str(o.get("value") or "") == option.value
            and collapse_whitespace(str(o.get("label") or "")) == target_label
        ]
        if not exact_matches:
            exact_matches = [
                o
                for o in raw_options
                if str(o.get("value") or "") == option.value
                or collapse_whitespace(str(o.get("label") or "")) == target_label
            ]

        if not exact_matches:
            raise MissingAnchorError(
                f"Option {option!r} not found in portal listbox; available: "
                f"{[(o.get('value'), o.get('label')) for o in raw_options]}"
            )
        if len(exact_matches) > 1:
            raise AmbiguousAnchorError(
                f"Option {option!r} matched {len(exact_matches)} portal options"
            )

        opt_selector = str(exact_matches[0]["selector"])
        opt_loc = self._resolve_live_locator(opt_selector)
        opt_loc.click(timeout=self._ui_profile.max_settle_ms)
        self.wait_for_quiescence()

    @contextlib.contextmanager
    def open_popup_surface(
        self,
        trigger_selector: str,
        *,
        url_pattern: Optional[str] = None,
        title_pattern: Optional[str] = None,
    ) -> Iterator[PlaywrightFormSurface]:
        """Click ``trigger_selector``, verify popup contract, yield child surface, settle parent on exit."""
        loc = self._resolve_live_locator(trigger_selector)
        popup_page: Any = None
        child_surface: Optional[PlaywrightFormSurface] = None

        try:
            with self._owner_page.context.expect_page(
                predicate=lambda child: child.opener() == self._owner_page,
                timeout=self._ui_profile.max_settle_ms,
            ) as popup_info:
                loc.click(timeout=self._ui_profile.max_settle_ms)
            popup_page = popup_info.value
        except Exception as exc:
            raise PopupContractError(
                f"Failed to open child popup window from {trigger_selector!r}: {exc}"
            ) from exc

        try:
            # A child window is its own page: the parent's frame routing (which
            # names the frame that holds the main form) does not apply to it.
            child_surface = PlaywrightFormSurface(
                popup_page,
                self._ui_profile.model_copy(update={"frame_path_patterns": ()}),
                base_origin=self._base_origin,
                keepalive_mutation_guard=self._keepalive_mutation_guard,
                ignored_mutation_paths=self._ignored_mutation_paths,
                _scoped_to_page_only=True,
            )
            popup_page.wait_for_load_state(
                "domcontentloaded", timeout=self._ui_profile.max_settle_ms
            )
            child_surface.wait_for_quiescence()

            popup_clean_url = _strip_url_to_origin_and_path(str(popup_page.url or ""))
            if url_pattern is not None and not re.search(
                url_pattern, popup_clean_url, flags=re.IGNORECASE
            ):
                raise PopupContractError(
                    f"Popup URL {popup_clean_url!r} failed url_pattern {url_pattern!r}"
                )

            popup_title = collapse_whitespace(str(popup_page.title() or ""))
            if title_pattern is not None and not re.search(
                title_pattern, popup_title, flags=re.IGNORECASE
            ):
                raise PopupContractError(
                    f"Popup title {popup_title!r} failed title_pattern {title_pattern!r}"
                )

            yield child_surface
        finally:
            if child_surface is not None:
                child_surface._detach()
            if popup_page is not None:
                try:
                    if not popup_page.is_closed():
                        popup_page.close()
                except Exception:
                    pass
            if not self._is_page_closed():
                self.wait_for_quiescence()

    def ping_keepalive(self, path: str) -> int:
        """Issue an in-page GET ``fetch(path)`` and enforce zero state-changing requests."""
        self._refresh_target()
        if not path or not path.strip():
            raise ValueError("Keepalive path must be non-empty")

        cur_url = str(self._page.url or "")
        if not path.startswith(("http://", "https://")) and not cur_url.startswith(
            ("http://", "https://")
        ):
            target = self._resolve_navigation_url(path)
        else:
            target = path

        raw_before = self._all_time_raw_mutations
        history_idx_before = len(self._mutation_history)

        status = int(
            self._page.evaluate(
                """async (keepaliveTarget) => {
                    const resp = await fetch(keepaliveTarget, {
                        method: 'GET',
                        credentials: 'same-origin',
                    });
                    try {
                        await resp.text();
                    } catch (e) {}
                    await new Promise((resolve) => setTimeout(resolve, 15));
                    return resp.status;
                }""",
                target,
            )
        )
        self._sleep_ms(15.0)

        mutations_triggered = self._all_time_raw_mutations - raw_before
        if self._keepalive_mutation_guard and mutations_triggered > 0:
            observed = self._mutation_history[history_idx_before:]
            raise KeepaliveMutationError(
                f"Keepalive GET {path!r} violated zero-mutation contract by triggering "
                f"{mutations_triggered} state-changing request(s): {observed}"
            )
        return status


__all__ = [
    "KeepaliveMutationError",
    "PlaywrightFormSurface",
    "PopupContractError",
    "SurfaceContextError",
    "SurfaceStateError",
    "SurfaceSettleTimeoutError",
]
