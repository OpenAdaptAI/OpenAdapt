"""Playwright/CDP implementation of ``FormSurfaceBackend`` for ``form_tx``.

Provides quiescence-gated DOM snapshotting, deterministic dynamic-ID-resilient
selector synthesis, body-portaled combobox actuation, bounded child-window
popup lifecycle management, secret value scrubbing, and zero-mutation keepalive
verification. Zero application or vendor names.
"""

from __future__ import annotations

import contextlib
import fnmatch
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
    UIProfileSpec,
    WizardScreenSpec,
)

_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class SurfaceSettleTimeoutError(TimeoutError):
    """Raised when a form surface remains busy past ``ui_profile.max_settle_ms``."""


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


_CHECK_BUSY_AND_DOM_JS = r"""
(busySelectors) => {
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
            cur = cur.parentElement;
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    let busyMatch = '';
    for (const sel of (busySelectors || [])) {
        let nodes = [];
        try {
            nodes = document.querySelectorAll(sel);
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

    const bodyHtmlLen = document.body ? document.body.innerHTML.length : 0;
    const elemCount = document.querySelectorAll('*').length;
    let valAcc = 0;
    for (const inp of document.querySelectorAll('input, select, textarea')) {
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
    ]);
    const isPlaceholderText = (s) => PLACEHOLDERS.has(cleanText(s).toLowerCase());

    const DYNAMIC_ID_RE = /(?:[_:-]\d+|\d{2,}|_[0-9a-f]{6,}|^:)/i;

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
            cur = cur.parentElement;
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
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

    const safeQueryAll = (root, sel) => {
        if (!sel) return [];
        try {
            return Array.from(root.querySelectorAll(sel));
        } catch (e) {
            return [];
        }
    };

    const anyVisibleMatch = (selectors) => {
        for (const sel of (selectors || [])) {
            for (const el of safeQueryAll(document, sel)) {
                if (isVisible(el)) return true;
            }
        }
        return false;
    };

    const isUniqueMatch = (sel, target) => {
        if (!sel) return false;
        try {
            const nodes = document.querySelectorAll(sel);
            return nodes.length === 1 && nodes[0] === target;
        } catch (e) {
            return false;
        }
    };

    const quoteAttrVal = (val) => {
        return '"' + String(val).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
    };

    const structuralSelector = (target) => {
        const parts = [];
        let cur = target;
        while (cur && cur !== document.documentElement) {
            if (cur !== target && cur.id && !DYNAMIC_ID_RE.test(cur.id)) {
                const anchorSel = '#' + CSS.escape(cur.id);
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
                parts.unshift(tag);
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

    const computeDomSelector = (el) => {
        if (el.id && !DYNAMIC_ID_RE.test(el.id)) {
            const idSel = '#' + CSS.escape(el.id);
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
            const linked = safeQueryAll(document, `label[for=${quoteAttrVal(el.id)}]`);
            const parts = linked.map((lbl) => extractLabelCloneText(lbl)).filter(Boolean);
            if (parts.length) return parts.join(' ');
        }
        const labelledBy = cleanText(el.getAttribute('aria-labelledby'));
        if (labelledBy) {
            const parts = labelledBy
                .split(/\s+/)
                .map((id) => {
                    const ref = document.getElementById(id);
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
        return '';
    };

    const resolveSectionText = (el) => {
        let cur = el.parentElement;
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
                            const ref = document.getElementById(id);
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
            cur = cur.parentElement;
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
                const ref = document.getElementById(id);
                if (ref && isVisible(ref) && !isInteractiveElement(ref)) {
                    const t = cleanText(ref.textContent);
                    if (t) return t;
                }
            }
        }
        const descBy = cleanText(el.getAttribute('aria-describedby'));
        if (descBy) {
            for (const id of descBy.split(/\s+/)) {
                const ref = document.getElementById(id);
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
            checked = Boolean(el.checked);
            currentValue = String(el.value || '');
        } else if (role === 'checkbox') {
            checked = el.getAttribute('aria-checked') === 'true';
            currentValue = checked ? 'true' : 'false';
        } else if (role === 'radio') {
            checked = el.getAttribute('aria-checked') === 'true';
            currentValue = cleanText(el.getAttribute('data-value') || el.textContent || '');
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
    const cleanText = (s) => String(s || '').replace(/\s+/g, ' ').trim();

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
            cur = cur.parentElement;
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const safeQueryAll = (root, sel) => {
        if (!sel) return [];
        try {
            return Array.from(root.querySelectorAll(sel));
        } catch (e) {
            return [];
        }
    };

    const isUniqueMatch = (sel, target) => {
        try {
            const nodes = document.querySelectorAll(sel);
            return nodes.length === 1 && nodes[0] === target;
        } catch (e) {
            return false;
        }
    };

    const structuralSelector = (target) => {
        const parts = [];
        let cur = target;
        while (cur && cur !== document.documentElement) {
            if (cur === document.body) {
                parts.unshift('body');
                break;
            }
            const tag = cur.tagName.toLowerCase();
            const parent = cur.parentElement;
            if (!parent) {
                parts.unshift(tag);
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

    let triggerEl = null;
    if (cfg.triggerSelector) {
        try {
            triggerEl = document.querySelector(cfg.triggerSelector);
        } catch (e) {
            triggerEl = null;
        }
    }

    let candidateListboxes = [];
    const seenLb = new Set();
    if (triggerEl) {
        for (const attr of ['aria-controls', 'aria-owns']) {
            const refId = cleanText(triggerEl.getAttribute(attr));
            if (refId) {
                const lb = document.getElementById(refId);
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
            const selector = (optEl.id && isUniqueMatch('#' + CSS.escape(optEl.id), optEl))
                ? '#' + CSS.escape(optEl.id)
                : structuralSelector(optEl);
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


class PlaywrightFormSurface:
    """Playwright-backed ``FormSurfaceBackend`` with DOM/network quiescence barriers."""

    def __init__(
        self,
        page: Any,
        ui_profile: UIProfileSpec,
        *,
        base_origin: Optional[str] = None,
        keepalive_mutation_guard: bool = True,
        ignored_mutation_paths: tuple[str, ...] = (),
        _scoped_to_page_only: bool = False,
    ) -> None:
        self._page = page
        self._ui_profile = ui_profile
        self._base_origin = base_origin.rstrip("/") if base_origin else None
        self._keepalive_mutation_guard = keepalive_mutation_guard
        self._ignored_mutation_paths = tuple(ignored_mutation_paths)
        self._scoped_to_page_only = _scoped_to_page_only

        self._mutation_count = 0
        self._confirmed_mutation_count = 0
        self._pending_mutation_seqs: dict[int, tuple[int, bool, str]] = {}
        self._confirmed_page_mutation_seqs: dict[int, str] = {}
        self._failed_page_mutation_seqs: set[int] = set()
        # Requests whose status and headers arrived. The answer is the status:
        # a body cut off afterwards (the page navigated away) does not undo it.
        self._answered_mutation_ids: set[int] = set()
        self._body_cut_off_mutation_seqs: set[int] = set()
        self._total_mutation_count = 0
        self._all_time_raw_mutations = 0
        self._inflight_mutation_ids: set[int] = set()
        self._mutation_history: list[tuple[str, str]] = []
        self._detached = False

        self._quiescence = PageQuiescence(page)
        try:
            page.evaluate(QUIESCENCE_INSTALL_JS)
        except Exception:
            pass

        self._event_target: Any = getattr(page, "context", None) or page
        try:
            self._event_target.on("request", self._on_request)
            self._event_target.on("requestfinished", self._on_request_done)
            self._event_target.on("requestfailed", self._on_request_failed)
            self._event_target.on("response", self._on_response)
        except Exception:
            pass

    @property
    def ui_profile(self) -> UIProfileSpec:
        return self._ui_profile

    @property
    def mutation_count(self) -> int:
        """Count of non-ignored state-changing HTTP requests since construction/reset."""
        return self._mutation_count

    @property
    def confirmed_mutation_count(self) -> int:
        """Non-ignored state-changing requests the server answered with 2xx/3xx."""
        return self._confirmed_mutation_count

    def has_confirmed_mutation_since(
        self, since_mutation_count: int, *, url_patterns: Sequence[str] = ()
    ) -> bool:
        """Return True iff this page started a non-ignored mutation after
        ``since_mutation_count`` that succeeded with 2xx/3xx (matching
        ``url_patterns`` when given) and no own-page mutation started after it
        failed."""
        if any(seq > since_mutation_count for seq in self._failed_page_mutation_seqs):
            return False
        confirmed_paths = [
            path
            for seq, path in self._confirmed_page_mutation_seqs.items()
            if seq > since_mutation_count
        ]
        if not confirmed_paths:
            return False
        if url_patterns:
            return any(
                path == pat or fnmatch.fnmatchcase(path, pat)
                for path in confirmed_paths
                for pat in url_patterns
            )
        return True

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
        self._confirmed_mutation_count = 0
        self._pending_mutation_seqs.clear()
        self._confirmed_page_mutation_seqs.clear()
        self._failed_page_mutation_seqs.clear()
        self._total_mutation_count = 0

    def _detach(self) -> None:
        if self._detached:
            return
        self._detached = True
        for ev_name, cb in (
            ("request", self._on_request),
            ("requestfinished", self._on_request_done),
            ("requestfailed", self._on_request_failed),
            ("response", self._on_response),
        ):
            try:
                self._event_target.remove_listener(ev_name, cb)
            except Exception:
                pass

    def _request_belongs_to_own_page(self, request: Any) -> bool:
        try:
            frame = request.frame
            return frame is not None and frame.page == self._page
        except Exception:
            return False

    def _request_belongs_to_surface(self, request: Any) -> bool:
        if not self._scoped_to_page_only:
            return True
        return self._request_belongs_to_own_page(request)

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
            self._pending_mutation_seqs[id(request)] = (
                self._mutation_count,
                self._request_belongs_to_own_page(request),
                req_path,
            )

    def _on_request_done(self, request: Any) -> None:
        self._inflight_mutation_ids.discard(id(request))
        self._answered_mutation_ids.discard(id(request))
        self._pending_mutation_seqs.pop(id(request), None)

    def _on_request_failed(self, request: Any) -> None:
        self._inflight_mutation_ids.discard(id(request))
        answered = id(request) in self._answered_mutation_ids
        self._answered_mutation_ids.discard(id(request))
        entry = self._pending_mutation_seqs.pop(id(request), None)
        if entry is None:
            return
        seq, is_own_page, _path = entry
        if answered:
            # The server already answered with a status, which _on_response
            # judged. Only the body was not delivered (typically the page
            # navigated away before reading it); no save decision reads the
            # body, so this neither confirms nor fails anything further.
            self._body_cut_off_mutation_seqs.add(seq)
            return
        if is_own_page:
            # No status ever arrived: the request failed.
            self._failed_page_mutation_seqs.add(seq)

    def _on_response(self, response: Any) -> None:
        """Count state-changing requests started on this surface that succeeded."""
        try:
            request = response.request
            if self._detached or not self._request_belongs_to_surface(request):
                return
            status = int(response.status)
        except Exception:
            return
        entry = self._pending_mutation_seqs.get(id(request))
        if entry is None:
            return
        seq, is_own_page, req_path = entry
        self._answered_mutation_ids.add(id(request))
        if 200 <= status < 400:
            self._confirmed_mutation_count += 1
            if is_own_page:
                self._confirmed_page_mutation_seqs[seq] = req_path
        elif is_own_page:
            self._failed_page_mutation_seqs.add(seq)

    def _is_page_closed(self) -> bool:
        try:
            return bool(self._page.is_closed())
        except Exception:
            return True

    def _sleep_ms(self, duration_ms: float) -> None:
        ms = max(0.0, float(duration_ms))
        if ms <= 0.0 or self._is_page_closed():
            return
        try:
            self._page.wait_for_timeout(ms)
        except Exception as exc:
            if _is_target_closed_error(exc) or self._is_page_closed():
                return
            time.sleep(ms / 1000.0)

    def wait_for_quiescence(self) -> None:
        """Block until debounce grace elapses, busy selectors clear, and DOM+network settle."""
        if self._is_page_closed():
            return

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
            if self._is_page_closed():
                return

            try:
                dom_state = self._page.evaluate(_CHECK_BUSY_AND_DOM_JS, busy_selectors)
            except Exception as exc:
                if _is_target_closed_error(exc) or self._is_page_closed():
                    return
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
                    recheck = self._page.evaluate(
                        _CHECK_BUSY_AND_DOM_JS, busy_selectors
                    )
                except Exception as exc:
                    if _is_target_closed_error(exc) or self._is_page_closed():
                        return
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

        if self._is_page_closed():
            return
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

        raw = self._page.evaluate(_SNAPSHOT_WALKER_JS, walker_cfg)
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
        )

    def _resolve_live_locator(self, dom_selector: str) -> Any:
        if not dom_selector or not dom_selector.strip():
            raise MissingAnchorError("Empty dom_selector")
        loc = self._page.locator(dom_selector)
        total = loc.count()
        if total == 0:
            raise MissingAnchorError(
                f"Selector {dom_selector!r} matched 0 elements in live DOM"
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

    def set_checkbox_or_radio(self, dom_selector: str, checked: bool = True) -> None:
        """Set a checkbox or radio control and wait for settle."""
        loc = self._resolve_live_locator(dom_selector)
        meta = loc.evaluate(
            "(el) => ({ tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(), role: (el.getAttribute('role') || '').toLowerCase(), ariaChecked: el.getAttribute('aria-checked'), propChecked: Boolean(el.checked) })"
        )
        tag = meta.get("tag")
        inp_type = meta.get("type")
        role = meta.get("role")

        if tag == "input" and inp_type == "radio":
            if not checked:
                if meta.get("propChecked"):
                    raise ValueError(
                        f"Cannot uncheck native radio {dom_selector!r} directly"
                    )
            else:
                loc.check(timeout=self._ui_profile.max_settle_ms)
        elif tag == "input" and inp_type == "checkbox":
            if checked:
                loc.check(timeout=self._ui_profile.max_settle_ms)
            else:
                loc.uncheck(timeout=self._ui_profile.max_settle_ms)
        elif role in ("checkbox", "radio"):
            currently_checked = meta.get("ariaChecked") == "true"
            if currently_checked != checked:
                loc.click(timeout=self._ui_profile.max_settle_ms)
        else:
            raise ValueError(
                f"Element {dom_selector!r} is not a checkbox or radio control"
            )

        try:
            loc.blur(timeout=500)
        except Exception:
            pass
        self.wait_for_quiescence()

    def click(self, dom_selector: str) -> None:
        """Click ``dom_selector`` and wait for settle."""
        loc = self._resolve_live_locator(dom_selector)
        try:
            loc.click(timeout=self._ui_profile.max_settle_ms)
        except Exception as exc:
            if _is_target_closed_error(exc) and self._is_page_closed():
                return
            raise
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
            with self._page.context.expect_page(
                timeout=self._ui_profile.max_settle_ms
            ) as popup_info:
                loc.click(timeout=self._ui_profile.max_settle_ms)
            popup_page = popup_info.value
        except Exception as exc:
            raise PopupContractError(
                f"Failed to open child popup window from {trigger_selector!r}: {exc}"
            ) from exc

        try:
            child_surface = PlaywrightFormSurface(
                popup_page,
                self._ui_profile,
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
    "SurfaceSettleTimeoutError",
]
