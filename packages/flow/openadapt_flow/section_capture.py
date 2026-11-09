"""Record the part of the form a target sits in, for named sections.

:data:`SECTION_CONTEXT_JS` is one JavaScript function, ``(el) => context``,
shared by the scripted Playwright backend and the interactive browser
recorder. For the element a person clicks it returns the nearest:

* section header the click itself opens (a tab, an accordion toggle, a
  ``<summary>``): ``{"kind": "tab" | "accordion", "text": ..., "header": true}``;
* enclosing ``<fieldset>`` legend, ``<details>`` summary, tab panel, dialog,
  collapsed panel (by the header that controls it), or labelled landmark;
* else the nearest heading before it in document order.

The text is the interface's own words, at most 120 characters, or nothing.
:func:`openadapt_flow.sections.context_from_events` reads it back from
``events.jsonl``; the compiler never puts it on a step, so it cannot change
what a program does.
"""

from __future__ import annotations

SECTION_CONTEXT_JS = r"""(el) => {
  try {
    if (!el || !el.ownerDocument) return null;
    const doc = el.ownerDocument;
    const clean = (value) => {
      const text = String(value || '').replace(/\s+/g, ' ').trim();
      return text && text.length <= 120 ? text : null;
    };
    const textOf = (node) => node ? clean(node.innerText || node.textContent) : null;
    const byIds = (ids) => clean(String(ids || '').split(/\s+/).map((id) => {
      const node = id ? doc.getElementById(id) : null;
      return node ? (node.textContent || '') : '';
    }).join(' '));
    const labelOf = (node) => node
      ? (clean(node.getAttribute('aria-label')) || byIds(node.getAttribute('aria-labelledby')))
      : null;
    const TAB = '[role="tab"], [data-toggle="tab"], [data-bs-toggle="tab"]';
    const HEADER = 'summary, ' + TAB + ', [aria-expanded], [data-toggle="collapse"],'
      + ' [data-bs-toggle="collapse"]';
    const FIELD = 'select, input, textarea, [role="combobox"], [role="listbox"],'
      + ' [aria-haspopup="listbox"]';
    const header = el.closest(HEADER);
    if (header && !header.matches(FIELD)) {
      const text = textOf(header) || labelOf(header);
      if (text) {
        return {kind: header.matches(TAB) ? 'tab' : 'accordion', text: text, header: true};
      }
    }
    const controller = (node) => {
      if (!node.id) return null;
      const id = CSS.escape(node.id);
      try {
        return doc.querySelector(
          '[aria-controls~="' + id + '"], [data-target="#' + id + '"],'
          + ' [data-bs-target="#' + id + '"], a[href="#' + id + '"]'
        );
      } catch (e) { return null; }
    };
    const LANDMARK_TAGS = ['section', 'form', 'nav', 'aside', 'main'];
    const LANDMARK_ROLES = ['region', 'group', 'form', 'navigation', 'complementary'];
    for (let node = el.parentElement; node && node !== doc.body; node = node.parentElement) {
      const tag = node.tagName.toLowerCase();
      const role = (node.getAttribute('role') || '').toLowerCase();
      if (tag === 'fieldset') {
        const text = textOf(node.querySelector(':scope > legend')) || labelOf(node);
        if (text) return {kind: 'legend', text: text};
      }
      if (tag === 'details') {
        const text = textOf(node.querySelector(':scope > summary'));
        if (text) return {kind: 'accordion', text: text};
      }
      if (role === 'tabpanel') {
        const text = labelOf(node) || textOf(controller(node));
        if (text) return {kind: 'tab', text: text};
      }
      if (tag === 'dialog' || role === 'dialog' || role === 'alertdialog') {
        const text = labelOf(node)
          || textOf(node.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"]'));
        if (text) return {kind: 'dialog', text: text};
      }
      const control = controller(node);
      if (control) {
        const text = textOf(control) || labelOf(control);
        if (text) return {kind: control.matches(TAB) ? 'tab' : 'accordion', text: text};
      }
      if (LANDMARK_TAGS.indexOf(tag) >= 0 || LANDMARK_ROLES.indexOf(role) >= 0) {
        const text = labelOf(node);
        if (text) return {kind: 'landmark', text: text};
      }
    }
    let heading = null;
    for (const h of doc.querySelectorAll('h1, h2, h3, h4, h5, h6, [role="heading"]')) {
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) heading = h;
      else break;
    }
    const text = textOf(heading);
    return text ? {kind: 'heading', text: text} : null;
  } catch (e) { return null; }
}"""
