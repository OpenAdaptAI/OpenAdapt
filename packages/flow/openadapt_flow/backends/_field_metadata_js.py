"""Passive browser field value, credential and label probes."""

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
#: Whether the focused element takes a sign-in credential. See
#: :meth:`PlaywrightBackend.focused_field_credential` for the rules.
_FIELD_CREDENTIAL_JS = r"""(el) => {
    while (el && el.shadowRoot && el.shadowRoot.activeElement) {
        el = el.shadowRoot.activeElement;
    }
    if (!el) return 'unknown';
    if (el.tagName !== 'INPUT') {
        // Closed shadow roots expose only their focused host. An opaque
        // focused host is not evidence that the control is non-sensitive.
        if (!el.matches('textarea, select') &&
                (el === el.ownerDocument.activeElement || el.matches(':focus-within'))) {
            return 'unknown';
        }
        return 'none';
    }
    const kind = (i) => (i.getAttribute('type') || 'text').toLowerCase();
    const tokens = (i) =>
        (i.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
    const secret = (i) => kind(i) === 'password' ||
        tokens(i).some((t) => t === 'current-password' || t === 'new-password');
    if (secret(el)) {
        return tokens(el).includes('new-password') ? 'new-password' : 'password';
    }
    if (tokens(el).includes('username')) return 'username';
    const texty = (i) => ['text', 'email', 'tel', 'search'].includes(kind(i));
    if (!texty(el)) return 'none';
    const parent = (n) => n.parentElement || n.getRootNode().host || null;
    const form = (n) => {
        if (n.form) return n.form;
        for (let p = parent(n); p; p = parent(p)) {
            if (p.tagName === 'FORM') return p;
        }
        return null;
    };
    const scope = form(el);
    const inputs = [];
    const visit = (root) => {
        for (const child of root.children || []) {
            if (child.tagName === 'INPUT' && form(child) === scope) inputs.push(child);
            visit(child);
            if (child.shadowRoot) visit(child.shadowRoot);
        }
    };
    visit(scope || el.ownerDocument);
    const passwords = inputs.filter(secret);
    if (!passwords.length) return 'none';
    // Consider every password candidate, including hidden autofill traps.
    // CSS visibility cannot prove which field the site really authenticates.
    // A uniquely paired text control is withheld; ambiguous controls refuse
    // declared inputs instead of falling through to plaintext recording.
    const candidates = new Set();
    for (const password of passwords) {
        const preceding = inputs.slice(0, inputs.indexOf(password)).reverse();
        const candidate = preceding.find(texty);
        if (candidate) candidates.add(candidate);
    }
    if (candidates.size === 1 && candidates.has(el)) return 'username';
    return 'unknown';
}"""

_CREDENTIAL_AT_JS = (
    "([x, y]) => { const el = document.elementFromPoint(x, y);"
    " if (!el) return null;"
    " if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') return 'frame';"
    " return (" + _FIELD_CREDENTIAL_JS + ")(el); }"
)

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
