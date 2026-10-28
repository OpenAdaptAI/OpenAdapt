"""Bind an unlabeled submit control to the form it submits.

The problem
-----------
A consequential click must be tied to a record before Flow will deliver it.
For a control inside a table row, the row is the record: its identity text is
verified before the click and leased until delivery. An icon-only Save that
floats next to a form (openIMIS's claim Save is a floppy-disk button with no
text, accessible name, or tooltip, outside any row) has no such identity, so
Flow refuses the click. That refusal is correct: without more evidence, an
unlabeled icon could be any control on the page.

The binding
-----------
The record such a Save commits is the one the program itself just entered.
This module identifies the control by its structural relation to the fields
the program filled, records that relation at compile time, and verifies the
same relation on the live page immediately before the click:

1. **Form ownership.** Which HTML form owns the filled fields and whether the
   control shares it, sits outside every form, or belongs to another form.
2. **Submit role.** Whether the control is that form's submit button, another
   button inside it, or a detached button, plus its tag, type, role, and a
   digest of its icon geometry.
3. **Position relative to those fields.** The control must follow every
   filled field in document order, sit in the same quadrant of their bounding
   box, and keep the same tag path from the scope down to it.
4. **Uniqueness.** It must be the only rendered, unlabeled control that
   follows the filled fields inside that scope.

A binding is recorded only when every condition holds in the demonstration,
and a run clicks only when every recorded value matches the live page and the
control is still unique. Anything else is a refusal before input. The binding
never relaxes another check: the resolution ladder, the risk gate, the
identity band, and the delivery lease all still apply.

Everything observed is structural: tag names, ``type`` and ``role`` tokens,
booleans, counts, relation words, and a SHA-256 of the icon's vector geometry
(SVG path data, image intrinsic size, glyph code points of an unlabeled
control). No text, value, or URL leaves the page.

The page-side observer is :data:`OBSERVE_SUBMIT_BINDING_JS`, shared by the
Playwright backend (record and run) and the interactive recorder (record).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openadapt_flow.ir import (
    SUBMIT_BINDING_PATH_PATTERN,
    SUBMIT_BINDING_TOKEN_PATTERN,
    FormIdentity,
    SubmitBinding,
)

__all__ = [
    "OBSERVE_SUBMIT_BINDING_JS",
    "SUBMIT_BINDING_ACTIONABLE_SELECTOR",
    "SUBMIT_BINDING_FIELD_SELECTOR",
    "FormValues",
    "SubmitBindingObservation",
    "decide_form_identity",
    "decide_submit_binding",
    "describe_submit_binding",
    "form_value_digest",
    "form_values_from_page",
    "form_values_from_record",
    "form_values_record",
    "observation_from_page",
    "verify_form_identity",
    "verify_submit_binding",
]

#: Innermost interactive controls a person could press. The bound control is
#: the nearest of these that contains the clicked point.
SUBMIT_BINDING_ACTIONABLE_SELECTOR = (
    'button, a[href], input[type="button"], input[type="submit"],'
    ' input[type="reset"], input[type="image"], [role="button"], [role="link"]'
)

#: Elements that take typed input. Typing into one registers it as a filled
#: field of the entry a later unlabeled control may submit.
SUBMIT_BINDING_FIELD_SELECTOR = (
    'input:not([type="hidden"]):not([type="button"]):not([type="submit"])'
    ':not([type="reset"]):not([type="image"]):not([type="checkbox"])'
    ':not([type="radio"]):not([type="range"]):not([type="file"]),'
    ' select, textarea, [contenteditable=""], [contenteditable="true"],'
    ' [role="textbox"], [role="combobox"], [role="searchbox"],'
    ' [role="spinbutton"]'
)

# The raw icon geometry string is bounded in the page; this bounds what Python
# accepts before hashing it.
_MAX_ICON_GEOMETRY_CHARS = 20_000

#: Page-side observer: ``(control, fields) => {observation, canon}``.
#:
#: ``control`` is the actionable element at the clicked point; ``fields`` is
#: the list of elements the program (or the demonstrator) typed into. Returns
#: the structural observation and ``canon``, its canonical JSON (with the raw
#: icon geometry), which the delivery guard recomputes and compares.
_OBSERVE_SUBMIT_BINDING_TEMPLATE = r"""(control, fields, baseline) => {
    const ACTIONABLE = __ACTIONABLE__;
    const FIELD = __FIELD__;
    const ROW = 'tr, [role="row"], li, [role="listitem"]';
    const doc = control.ownerDocument;
    const win = doc.defaultView;
    const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const token = (value) => {
        const text = String(value || '').toLowerCase();
        if (!text) return null;
        return /^[a-z][a-z0-9-]{0,31}$/.test(text) ? text : 'other';
    };
    const meaningful = (value) => {
        let count = 0;
        for (const ch of clean(value)) {
            if (/[\p{L}\p{N}]/u.test(ch)) {
                count += 1;
                if (count >= 2) return true;
            }
        }
        return false;
    };
    const named = (el) => {
        const parts = [el.getAttribute('aria-label'), el.getAttribute('title')];
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            for (const id of labelledBy.split(/\s+/)) {
                const ref = id ? doc.getElementById(id) : null;
                if (ref) parts.push(ref.textContent);
            }
        }
        if (el.matches('input')) {
            const kind = String(el.type || '').toLowerCase();
            if ((kind === 'submit' || kind === 'reset') &&
                    !el.hasAttribute('value')) {
                return true;  // the browser draws "Submit" / "Reset"
            }
            parts.push(el.value, el.getAttribute('alt'));
        }
        for (const inner of el.querySelectorAll('[aria-label], [title], img[alt]')) {
            parts.push(
                inner.getAttribute('aria-label'),
                inner.getAttribute('title'),
                inner.getAttribute('alt'),
            );
        }
        if (el.id) {
            try {
                for (const label of doc.querySelectorAll(
                        'label[for="' + CSS.escape(el.id) + '"]')) {
                    parts.push(label.textContent);
                }
            } catch (_) {}
        }
        const wrapping = el.closest('label');
        if (wrapping) parts.push(wrapping.textContent);
        parts.push(el.textContent);
        return parts.some(meaningful);
    };
    const rendered = (el) => {
        const rect = el.getBoundingClientRect();
        if (!(rect.width > 0 && rect.height > 0)) return false;
        const style = win.getComputedStyle(el);
        return style.visibility !== 'hidden' && style.visibility !== 'collapse' &&
            style.display !== 'none' && Number(style.opacity || '1') !== 0;
    };
    const formOf = (el) => {
        const owner = el.form;
        if (owner && owner.localName === 'form') return owner;
        return el.closest('form');
    };
    const follows = (earlier, later) => Boolean(
        earlier.compareDocumentPosition(later) &
            Node.DOCUMENT_POSITION_FOLLOWING
    ) && !earlier.contains(later);
    const formFingerprint = (form, submitter, typed, baseline) => {
        // Where and how the form submits (origin, path and query), the
        // name and type of every control the submission would carry, and
        // the values of the controls the run did not type into.
        let action = '';
        const query = [];
        try {
            // A control named "action" or "method" (Trac's action radios)
            // shadows form.action; read the form's own properties.
            const formProp = (name) => Object.getOwnPropertyDescriptor(
                HTMLFormElement.prototype, name).get.call(form);
            const target = submitter.hasAttribute('formaction')
                ? submitter.formAction : formProp('action');
            const url = new URL(target, doc.baseURI);
            action = url.origin + url.pathname;
            for (const [key, value] of url.searchParams) query.push([key, value]);
        } catch (_) {}
        const method = String((submitter.hasAttribute('formmethod')
            ? submitter.formMethod : Object.getOwnPropertyDescriptor(
                HTMLFormElement.prototype, 'method').get.call(form))
            || 'get').toLowerCase();
        const controls = [];
        const values = [];
        // Hidden values that changed since the first field was filled in.
        const rotated = [];
        for (const el of Array.from(form.elements)) {
            const name = el.name;
            if (!name) continue;
            let disabled = false;
            try { disabled = el.matches(':disabled'); } catch (_) {}
            if (disabled) continue;
            const type = String(el.type || el.localName || '').toLowerCase();
            if (el === submitter) {
                controls.push(name + '\t' + type);
                continue;
            }
            if (['submit', 'button', 'reset', 'image'].indexOf(type) >= 0 ||
                    ['fieldset', 'object', 'output'].indexOf(el.localName) >= 0) {
                continue;
            }
            if (type === 'file') {
                // How many files it would send: an unexpected upload changes it.
                controls.push(name + '\t' + type);
                values.push([name, type, String(el.files ? el.files.length : 0)]);
                continue;
            }
            if ((type === 'checkbox' || type === 'radio') && !el.checked) continue;
            controls.push(name + '\t' + type);
            // A password's value never leaves the page.
            if (typed.indexOf(el) >= 0 || type === 'password') continue;
            if (el.localName === 'select') {
                for (const option of Array.from(el.selectedOptions)) {
                    values.push([name, type, String(option.value)]);
                }
            } else {
                values.push([name, type, String(el.value)]);
            }
            if (type === 'hidden' && baseline instanceof WeakMap &&
                    baseline.has(el) && baseline.get(el) !== String(el.value)) {
                rotated.push(name);
            }
        }
        controls.sort();
        return {action, query, method, controls, values, rotated};
    };
    const fieldLocal = (el, stop) => {
        // The first ancestor that holds any field decides: exactly one field
        // means the control is that field's own adornment (a clear button,
        // a picker toggle), never a commit for the whole entry.
        for (let node = el.parentElement; node; node = node.parentElement) {
            const count = node.querySelectorAll(FIELD).length;
            if (count > 0) return count === 1;
            if (node === stop) break;
        }
        return false;
    };
    const iconGeometry = (el) => {
        const parts = [];
        const shapes = el.querySelectorAll(
            'svg, path, polygon, polyline, circle, ellipse, rect, line, use, img'
        );
        const attrs = (node, names) => names.map(
            (name) => node.getAttribute(name) || ''
        ).join(',');
        for (const node of shapes) {
            const kind = node.localName;
            if (kind === 'svg') parts.push('s' + attrs(node, ['viewBox']));
            else if (kind === 'path') parts.push('p' + attrs(node, ['d']));
            else if (kind === 'polygon' || kind === 'polyline') {
                parts.push(kind.slice(0, 5) + attrs(node, ['points']));
            } else if (kind === 'circle') {
                parts.push('c' + attrs(node, ['cx', 'cy', 'r']));
            } else if (kind === 'ellipse') {
                parts.push('e' + attrs(node, ['cx', 'cy', 'rx', 'ry']));
            } else if (kind === 'rect') {
                parts.push('r' + attrs(node, ['x', 'y', 'width', 'height', 'rx', 'ry']));
            } else if (kind === 'line') {
                parts.push('l' + attrs(node, ['x1', 'y1', 'x2', 'y2']));
            } else if (kind === 'use') {
                parts.push('u' + (node.getAttribute('href') ||
                    node.getAttribute('xlink:href') || ''));
            } else if (kind === 'img') {
                parts.push('i' + node.naturalWidth + 'x' + node.naturalHeight);
            }
        }
        const glyphs = clean(el.textContent);
        if (glyphs) {
            parts.push('g' + Array.from(glyphs).map(
                (ch) => ch.codePointAt(0).toString(16)
            ).join('.'));
        }
        const joined = parts.join('|');
        return joined ? joined.slice(0, 20000) : null;
    };

    const result = {
        v: 1,
        tag: token(control.localName),
        type: (typeof control.type === 'string') ? token(control.type) : null,
        role: token(control.getAttribute('role')),
        labelled: named(control),
        in_row: Boolean(control.closest(ROW)),
        fixed: (() => {
            for (let node = control; node && node !== doc.documentElement;
                    node = node.parentElement) {
                const position = win.getComputedStyle(node).position;
                if (position === 'fixed' || position === 'sticky') return true;
            }
            return false;
        })(),
        fields: 0,
        form_owner: null,
        submit_role: null,
        scope_kind: null,
        scope_path: null,
        fields_in_scope: null,
        field_local: null,
        order: null,
        vertical: null,
        horizontal: null,
        candidates: null,
        counted: null,
        icon: null,
    };
    // A labelled control is identified by its name, not bound, so its icon,
    // position and uniqueness are not read. Its form relation still is: a
    // labelled save on a create-new form is tied to the record by the form
    // it submits (openadapt_flow.ir.FormIdentity). Its text never leaves the
    // page.
    if (!result.labelled) result.icon = iconGeometry(control);

    const seen = new Set();
    const live = [];
    for (const field of (fields || [])) {
        if (field && field.isConnected && field.ownerDocument === doc &&
                !seen.has(field)) {
            seen.add(field);
            live.push(field);
        }
    }
    live.sort((a, b) => (a === b ? 0 : (follows(a, b) ? -1 : 1)));
    result.fields = live.length;
    if (!live.length) {
        return {observation: result, canon: JSON.stringify(result)};
    }

    const forms = live.map(formOf);
    const firstForm = forms[0];
    const oneForm = forms.every((form) => form === firstForm);
    const anyForm = forms.some((form) => Boolean(form));
    const controlForm = formOf(control);
    let formOwner;
    if (oneForm && firstForm) {
        formOwner = controlForm === firstForm ? 'shared' :
            (controlForm ? 'other_form' : 'fields_only');
    } else if (!anyForm) {
        formOwner = controlForm ? 'other_form' : 'none';
    } else {
        formOwner = 'mixed';
    }
    result.form_owner = formOwner;
    let scope = null;
    if (formOwner === 'shared') {
        scope = firstForm;
    } else {
        const members = [control, ...(oneForm && firstForm ? [firstForm] : live)];
        scope = control.parentElement;
        while (scope && !members.every((node) => scope.contains(node))) {
            scope = scope.parentElement;
        }
    }
    if (!scope) {
        return {observation: result, canon: JSON.stringify(result)};
    }
    result.scope_kind = formOwner === 'shared' ? 'form' : 'container';
    result.submit_role = formOwner === 'shared'
        ? ((result.type === 'submit' || result.type === 'image')
            ? 'form_submit' : 'form_control')
        : 'detached';
    const path = [];
    for (let node = control; node && node !== scope; node = node.parentElement) {
        path.unshift(token(node.localName) || 'other');
    }
    result.scope_path = path.slice(-64).join('>');
    result.fields_in_scope = live.every((field) => scope.contains(field));
    result.field_local = fieldLocal(control, scope);
    const afterAll = live.every((field) => follows(field, control));
    const beforeAll = live.every((field) => follows(control, field));
    result.order = afterAll ? 'after' : (beforeAll ? 'before' : 'between');
    if (result.labelled) {
        // A create-new form's save: what the whole form would send, so a run
        // can tell this form from an edit form or a form scoped elsewhere
        // that looks the same (openadapt_flow.ir.FormIdentity).
        if (formOwner === 'shared' && result.submit_role === 'form_submit') {
            result.form = formFingerprint(scope, control, live, baseline);
        }
        return {observation: result, canon: JSON.stringify(result)};
    }

    let top = Infinity;
    let left = Infinity;
    let bottom = -Infinity;
    let right = -Infinity;
    for (const field of live) {
        const rect = field.getBoundingClientRect();
        top = Math.min(top, rect.top);
        left = Math.min(left, rect.left);
        bottom = Math.max(bottom, rect.bottom);
        right = Math.max(right, rect.right);
    }
    const box = control.getBoundingClientRect();
    const cx = box.left + box.width / 2;
    const cy = box.top + box.height / 2;
    result.vertical = cy > bottom ? 'below' : (cy < top ? 'above' : 'level');
    result.horizontal = cx > right ? 'right' : (cx < left ? 'left' : 'within');

    const last = live[live.length - 1];
    let candidates = 0;
    let counted = false;
    for (const el of scope.querySelectorAll(ACTIONABLE)) {
        if (el.querySelector(ACTIONABLE)) continue;
        if (!follows(last, el)) continue;
        if (live.some((field) => field.contains(el))) continue;
        if (el.closest(ROW)) continue;
        if (!rendered(el)) continue;
        if (named(el)) continue;
        if (fieldLocal(el, scope)) continue;
        candidates += 1;
        if (el === control) counted = true;
    }
    result.candidates = candidates;
    result.counted = counted;
    return {observation: result, canon: JSON.stringify(result)};
}"""
OBSERVE_SUBMIT_BINDING_JS = _OBSERVE_SUBMIT_BINDING_TEMPLATE.replace(
    "__ACTIONABLE__", json.dumps(SUBMIT_BINDING_ACTIONABLE_SELECTOR)
).replace("__FIELD__", json.dumps(SUBMIT_BINDING_FIELD_SELECTOR))


class SubmitBindingObservation(BaseModel):
    """One structural observation of a control and the filled fields.

    The recorder stores this (with the icon geometry replaced by its SHA-256)
    on the click event; the compiler turns it into a
    :class:`~openadapt_flow.ir.SubmitBinding` or an unbound reason; the
    replayer compares a live one against the recorded binding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1] = 1
    tag: Optional[str] = Field(default=None, pattern=SUBMIT_BINDING_TOKEN_PATTERN)
    type: Optional[str] = Field(default=None, pattern=SUBMIT_BINDING_TOKEN_PATTERN)
    role: Optional[str] = Field(default=None, pattern=SUBMIT_BINDING_TOKEN_PATTERN)
    labelled: bool
    in_row: bool
    fixed: bool
    fields: int = Field(ge=0, le=100_000)
    form_owner: Optional[
        Literal["shared", "fields_only", "none", "other_form", "mixed"]
    ] = None
    submit_role: Optional[Literal["form_submit", "form_control", "detached"]] = None
    scope_kind: Optional[Literal["form", "container"]] = None
    scope_path: Optional[str] = Field(default=None, pattern=SUBMIT_BINDING_PATH_PATTERN)
    fields_in_scope: Optional[bool] = None
    field_local: Optional[bool] = None
    order: Optional[Literal["after", "before", "between"]] = None
    vertical: Optional[Literal["above", "level", "below"]] = None
    horizontal: Optional[Literal["left", "within", "right"]] = None
    candidates: Optional[int] = Field(default=None, ge=0, le=100_000)
    counted: Optional[bool] = None
    icon_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: A labelled form-submit button of the filled form: SHA-256 of the
    #: submission's origin and path, its method, and SHA-256 of the sorted
    #: name/type list of the controls it would send. Structure only.
    action_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    form_method: Optional[Literal["get", "post", "dialog"]] = None
    controls_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")


#: Hidden controls named like a per-session or per-page token (CSRF and the
#: like). Their values differ on every page load, so they are never compared,
#: and their values are never kept.
_TOKEN_NAME = re.compile(
    r"csrf|xsrf|token|nonce|authenticity|verification|viewstate|eventvalidation",
    re.IGNORECASE,
)


#: A record id rather than a random token: hex groups joined by - or _ (a
#: UUID, ``enc-3f2a9c1e-0001``).
_RECORD_LIKE = re.compile(
    r"^[A-Za-z]{0,12}[-_]?[0-9A-Fa-f]{4,}(?:[-_][0-9A-Fa-f]{2,})+$"
)


def _is_token(name: str, value: str, rotated: bool = False) -> bool:
    """A hidden value (or query value) that is a per-session or per-page
    token, not compared: named like one AND shaped like one (at least 16
    characters, not all digits), and not shaped like a record id unless it
    changed while the demonstration was being filled in. A name alone
    (``encounter_token=7``), a record-shaped value (``enc-3f2a9c1e-0001``)
    and a value a script set while the form was filled in (a parent id) are
    compared."""

    bare = name.lstrip("?")
    if not _TOKEN_NAME.search(bare) or len(value) < 16 or value.isdigit():
        return False
    return rotated or not _RECORD_LIKE.match(value)


@dataclass(frozen=True)
class FormValues:
    """Values of the controls a run did not type into, by control name.

    Plaintext, so it never goes into an observation or a bundle: the
    compiler keeps salted digests (:func:`decide_form_identity`) and a run
    compares digests. ``exempt`` names hidden or query tokens that are not
    compared: named like a token and shaped like one (see :func:`_is_token`).
    Query parameter values are keyed ``?name``.
    """

    values: Mapping[str, tuple[str, ...]]
    exempt: frozenset[str]


def form_values_from_page(raw: object) -> Optional[FormValues]:
    """The untouched control values in a page observation, or None when it
    has no form fingerprint. Token values are dropped here."""

    if not isinstance(raw, Mapping):
        return None
    form = raw.get("form")
    if not isinstance(form, Mapping):
        return None
    grouped: dict[str, list[str]] = {}
    exempt: set[str] = set()
    rotated = {str(name) for name in form.get("rotated") or ()}
    items = [tuple(str(part) for part in item) for item in form.get("values") or ()]
    for item in form.get("query") or ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("form query values are malformed")
        items.append(("?" + str(item[0]), "query", str(item[1])))
    for item in items:
        if len(item) != 3:
            raise ValueError("form values are malformed")
        name, kind, value = item
        if kind in ("hidden", "query") and _is_token(name, value, name in rotated):
            exempt.add(name)
            continue
        grouped.setdefault(name, []).append(value)
    return FormValues(
        values={name: tuple(sorted(found)) for name, found in grouped.items()},
        exempt=frozenset(exempt),
    )


def form_values_record(values: FormValues) -> dict[str, Any]:
    """``values`` as JSON for a recording event."""

    return {
        "values": {k: list(v) for k, v in values.values.items()},
        "exempt": sorted(values.exempt),
    }


def form_values_from_record(raw: object) -> Optional[FormValues]:
    """Read :func:`form_values_record` back, or None."""

    if not isinstance(raw, Mapping):
        return None
    values = raw.get("values")
    if not isinstance(values, Mapping):
        return None
    return FormValues(
        values={str(k): tuple(sorted(str(x) for x in v)) for k, v in values.items()},
        exempt=frozenset(str(x) for x in raw.get("exempt") or ()),
    )


def form_value_digest(salt_hex: str, values: Sequence[str]) -> str:
    """Salted digest of one control's value(s): the identity-template scheme."""

    from openadapt_flow.runtime.identity_template import salted_digest

    return salted_digest(salt_hex, "\x1f".join(sorted(values)))


def observation_from_page(raw: object) -> SubmitBindingObservation:
    """Validate a page-side observation and replace its icon by a digest.

    ``raw`` is the ``observation`` object returned by
    :data:`OBSERVE_SUBMIT_BINDING_JS` (raw icon geometry under ``icon``), or a
    stored observation that already carries ``icon_sha256``.

    Raises:
        ValueError: when ``raw`` is not a well-formed observation.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("submit-binding observation is not an object")
    data = dict(raw)
    form = data.pop("form", None)
    if form is not None:
        if not isinstance(form, Mapping):
            raise ValueError("submit-binding form fingerprint is malformed")
        action = str(form.get("action") or "")
        query = form.get("query") or []
        if not isinstance(query, list) or not all(
            isinstance(q, (list, tuple)) and len(q) == 2 for q in query
        ):
            raise ValueError("submit-binding form query is malformed")
        # The address includes its query parameter NAMES; their values are
        # compared like the form's other untouched values.
        action += "?" + "&".join(sorted(str(q[0]) for q in query))
        controls = form.get("controls") or []
        if not isinstance(controls, list) or not all(
            isinstance(c, str) for c in controls
        ):
            raise ValueError("submit-binding form controls are malformed")
        data["action_sha256"] = hashlib.sha256(action.encode("utf-8")).hexdigest()
        data["form_method"] = str(form.get("method") or "get").lower()
        data["controls_sha256"] = hashlib.sha256(
            "\n".join(sorted(controls)).encode("utf-8")
        ).hexdigest()
    icon = data.pop("icon", None)
    if icon is not None:
        if not isinstance(icon, str) or len(icon) > _MAX_ICON_GEOMETRY_CHARS:
            raise ValueError("submit-binding icon geometry is malformed")
        if "icon_sha256" in data and data["icon_sha256"] is not None:
            raise ValueError("submit-binding observation has two icon values")
        data["icon_sha256"] = hashlib.sha256(icon.encode("utf-8")).hexdigest()
    try:
        return SubmitBindingObservation.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"submit-binding observation is malformed: {exc}") from exc


def _unbindable_reason(observation: SubmitBindingObservation) -> Optional[str]:
    """Why ``observation`` cannot carry a binding, in plain words."""

    if observation.labelled:
        return "the control has an accessible name, so its name identifies it"
    if observation.tag is None:
        return "the control's element type could not be read"
    if observation.in_row:
        return (
            "the control is inside a table row or list item, whose own identity "
            "ties it to a record"
        )
    if observation.fields <= 0:
        return "no field the program typed into is on the page"
    if observation.form_owner == "other_form":
        return "the control belongs to a different form than the filled fields"
    if observation.form_owner == "mixed":
        return "the filled fields belong to more than one form"
    if (
        observation.form_owner is None
        or observation.scope_kind is None
        or observation.scope_path is None
        or observation.submit_role is None
    ):
        return "no element contains both the control and the filled fields"
    if observation.fields_in_scope is not True:
        return "a filled field lies outside the form or container of the control"
    if observation.field_local is not False:
        return "the control belongs to a single field's own widget"
    if observation.order != "after":
        where = observation.order or "unknown"
        return (
            f"the control does not follow the filled fields (it is {where} them "
            "in page order)"
        )
    if observation.vertical is None or observation.horizontal is None:
        return "the control's position relative to the filled fields is unknown"
    if observation.counted is not True:
        return "the control is not a rendered, unlabeled control after the fields"
    if observation.candidates != 1:
        return (
            f"{observation.candidates} unlabeled controls follow the filled "
            "fields, so which one submits them is ambiguous"
        )
    return None


def _no_form_identity_reason(observation: SubmitBindingObservation) -> Optional[str]:
    """Why a labelled control's form can't identify the record, or None."""

    if not observation.labelled:
        return "the control has no name, so a submit binding identifies it"
    if observation.in_row:
        return (
            "the control is inside a table row or list item, whose own identity "
            "ties it to a record"
        )
    if observation.fields <= 0:
        return "no field the program typed into is on the page"
    if observation.form_owner != "shared":
        return "the control is not in the form that owns the filled fields"
    if observation.fields_in_scope is not True:
        return "a filled field lies outside the control's form"
    if observation.field_local is not False:
        return "the control belongs to a single field's own widget"
    if observation.submit_role != "form_submit":
        # Another button's effect is whatever its script does, so only the
        # form's own submit button identifies what is submitted.
        return "the control is not that form's submit button"
    return None


def decide_form_identity(
    observation: SubmitBindingObservation,
    values: Optional[FormValues] = None,
    *,
    salt_hex: Optional[str] = None,
) -> tuple[Optional[FormIdentity], Optional[str]]:
    """A labelled save's form identity from the demonstration, or a reason.

    Returns ``(identity, None)`` when the control is the submit button of the
    one form that owns every field typed into since the last save and the
    recording has the form's fingerprint and untouched values, else
    ``(None, reason)``.
    """

    from openadapt_flow.runtime.identity_template import new_salt_hex

    reason = _no_form_identity_reason(observation)
    if reason is not None:
        return None, reason
    if (
        observation.action_sha256 is None
        or observation.form_method is None
        or observation.controls_sha256 is None
        or values is None
    ):
        return None, "the recording has no fingerprint of the form"
    salt = salt_hex if salt_hex is not None else new_salt_hex()
    return (
        FormIdentity(
            filled_fields=observation.fields,
            submit_role="form_submit",
            action_sha256=observation.action_sha256,
            method=observation.form_method,
            controls_sha256=observation.controls_sha256,
            value_salt=salt,
            value_digests={
                name: form_value_digest(salt, found)
                for name, found in sorted(values.values.items())
            },
            exempt=tuple(sorted(values.exempt)),
        ),
        None,
    )


def verify_form_identity(
    identity: FormIdentity,
    observation: SubmitBindingObservation,
    *,
    changed_fields: int,
    values: Optional[FormValues],
) -> Optional[str]:
    """Return None when the live form still identifies the record.

    ``observation`` is the live control's observation against the fields this
    run typed into; ``changed_fields`` counts those fields in its form that
    did not end up holding exactly what the run typed (a controlled input
    that reformatted it) or changed since; ``values`` are the live values of
    the controls the run did not type into. Otherwise return the first
    difference, in plain words.
    """

    reason = _no_form_identity_reason(observation)
    if reason is not None:
        return reason
    if observation.submit_role != identity.submit_role:
        return (
            f"the control's submit role is {observation.submit_role!r}, it was "
            f"{identity.submit_role!r}"
        )
    if observation.fields != identity.filled_fields:
        return (
            f"this run has typed into {observation.fields} field(s) still on the "
            f"page; the demonstration had typed into {identity.filled_fields}"
        )
    if observation.action_sha256 != identity.action_sha256:
        return "the form submits to a different address than in the demonstration"
    if observation.form_method != identity.method:
        return (
            f"the form submits with {observation.form_method!r}, it submitted with "
            f"{identity.method!r} in the demonstration"
        )
    if observation.controls_sha256 != identity.controls_sha256:
        return "the form would send a different set of fields than in the demonstration"
    if changed_fields:
        return (
            f"{changed_fields} field(s) of the form don't hold exactly the value "
            "this run typed"
        )
    if values is None:
        return "could not read the values of the form's other fields"
    live = {
        name: found
        for name, found in values.values.items()
        if name not in identity.exempt
    }
    for name in sorted(set(live) | set(identity.value_digests)):
        recorded = identity.value_digests.get(name)
        if recorded is None or name not in live:
            return f"the form's {name!r} field is not the one demonstrated"
        if form_value_digest(identity.value_salt, live[name]) != recorded:
            return (
                f"the form's {name!r} field, which this run did not type into, "
                "holds a different value than in the demonstration"
            )
    return None


def decide_submit_binding(
    observation: SubmitBindingObservation,
    *,
    field_step_ids: Sequence[str] = (),
) -> tuple[Optional[SubmitBinding], Optional[str]]:
    """Turn a demonstration's observation into a binding or a reason.

    Returns ``(binding, None)`` when every condition in
    :class:`~openadapt_flow.ir.SubmitBinding` holds, else ``(None, reason)``.
    """

    reason = _unbindable_reason(observation)
    if reason is not None:
        return None, reason
    form_owner = observation.form_owner
    if (
        observation.tag is None
        or observation.scope_path is None
        or observation.scope_kind is None
        or observation.submit_role is None
        or observation.vertical is None
        or observation.horizontal is None
        or form_owner not in ("shared", "fields_only", "none")
    ):  # pragma: no cover - _unbindable_reason already refused these
        return None, "the structural evidence for this control is incomplete"
    binding = SubmitBinding(
        control_tag=observation.tag,
        control_type=observation.type,
        control_role=observation.role,
        icon_sha256=observation.icon_sha256,
        fixed_position=observation.fixed,
        form_owner=form_owner,
        submit_role=observation.submit_role,
        scope_kind=observation.scope_kind,
        scope_path=observation.scope_path,
        filled_fields=observation.fields,
        vertical=observation.vertical,
        horizontal=observation.horizontal,
        field_step_ids=tuple(field_step_ids),
    )
    return binding, None


def verify_submit_binding(
    binding: SubmitBinding,
    observation: SubmitBindingObservation,
) -> Optional[str]:
    """Return None when the live ``observation`` matches ``binding``.

    Otherwise return the first difference, in plain words. The live control
    must satisfy every binding condition on its own (in particular, it must
    still be the only unlabeled control after the filled fields) and match
    every recorded value.
    """

    reason = _unbindable_reason(observation)
    if reason is not None:
        return reason
    if observation.tag != binding.control_tag:
        return (
            f"the control is a <{observation.tag}>, the recorded control was a "
            f"<{binding.control_tag}>"
        )
    if observation.type != binding.control_type:
        return (
            f"the control's type is {observation.type!r}, it was "
            f"{binding.control_type!r}"
        )
    if observation.role != binding.control_role:
        return (
            f"the control's role is {observation.role!r}, it was "
            f"{binding.control_role!r}"
        )
    if observation.icon_sha256 != binding.icon_sha256:
        return "the control draws a different icon than the recorded control"
    if observation.form_owner != binding.form_owner:
        return (
            f"form ownership is {observation.form_owner!r}, it was "
            f"{binding.form_owner!r}"
        )
    if observation.submit_role != binding.submit_role:
        return (
            f"the control's submit role is {observation.submit_role!r}, it was "
            f"{binding.submit_role!r}"
        )
    if observation.scope_kind != binding.scope_kind:
        return (
            f"the binding scope is a {observation.scope_kind}, it was a "
            f"{binding.scope_kind}"
        )
    if observation.scope_path != binding.scope_path:
        return (
            "the control sits at a different place in the page structure "
            f"({observation.scope_path!r}, recorded {binding.scope_path!r})"
        )
    if observation.fields != binding.filled_fields:
        return (
            f"this run has typed into {observation.fields} field(s) still on the "
            f"page; the demonstration had typed into {binding.filled_fields}"
        )
    if (observation.vertical, observation.horizontal) != (
        binding.vertical,
        binding.horizontal,
    ):
        return (
            "the control moved relative to the filled fields (now "
            f"{observation.vertical}/{observation.horizontal}, recorded "
            f"{binding.vertical}/{binding.horizontal})"
        )
    if observation.fixed != binding.fixed_position:
        return "the control's fixed positioning changed"
    return None


def describe_submit_binding(binding: SubmitBinding) -> str:
    """One plain-language line of the binding's evidence, for lint and review."""

    owner = {
        "shared": "inside the form that owns the filled fields",
        "fields_only": "outside the form that owns the filled fields",
        "none": "in the same container as the filled fields (no form element)",
    }[binding.form_owner]
    role = {
        "form_submit": "that form's submit button",
        "form_control": "a button of that form",
        "detached": "a button outside any form",
    }[binding.submit_role]
    steps = (
        f" (typed by {', '.join(binding.field_step_ids)})"
        if binding.field_step_ids
        else ""
    )
    placement = "fixed, " if binding.fixed_position else ""
    return (
        f"<{binding.control_tag}> {role}, {owner}; after all "
        f"{binding.filled_fields} filled field(s){steps}, {placement}"
        f"{binding.vertical}/{binding.horizontal} of them; the only unlabeled "
        f"control after them; path {binding.scope_path}"
    )
