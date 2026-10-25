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
from typing import Literal, Mapping, Optional, Sequence

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
    "SubmitBindingObservation",
    "decide_form_identity",
    "decide_submit_binding",
    "describe_submit_binding",
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
_OBSERVE_SUBMIT_BINDING_TEMPLATE = r"""(control, fields) => {
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
    if observation.submit_role not in ("form_submit", "form_control"):
        return "the control is not a button of that form"
    return None


def decide_form_identity(
    observation: SubmitBindingObservation,
) -> tuple[Optional[FormIdentity], Optional[str]]:
    """A labelled save's form identity from the demonstration, or a reason.

    Returns ``(identity, None)`` when the control is a button of the one form
    that owns every field typed into since the last save, else
    ``(None, reason)``.
    """

    reason = _no_form_identity_reason(observation)
    if reason is not None:
        return None, reason
    assert observation.submit_role in ("form_submit", "form_control")
    return (
        FormIdentity(
            filled_fields=observation.fields,
            submit_role=observation.submit_role,
        ),
        None,
    )


def verify_form_identity(
    identity: FormIdentity,
    observation: SubmitBindingObservation,
    *,
    changed_fields: int,
) -> Optional[str]:
    """Return None when the live form still identifies the record.

    ``observation`` is the live control's observation against the fields this
    run typed into; ``changed_fields`` counts those fields in its form that
    did not end up holding exactly what the run typed (a controlled input
    that reformatted it) or changed since. Otherwise return the first
    difference, in plain words.
    """

    reason = _no_form_identity_reason(observation)
    if reason is not None:
        return reason
    if observation.fields != identity.filled_fields:
        return (
            f"this run has typed into {observation.fields} field(s) still on the "
            f"page; the demonstration had typed into {identity.filled_fields}"
        )
    if observation.submit_role != identity.submit_role:
        return (
            f"the control's submit role is {observation.submit_role!r}, it was "
            f"{identity.submit_role!r}"
        )
    if changed_fields:
        return (
            f"{changed_fields} field(s) of the form don't hold exactly the value "
            "this run typed"
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
