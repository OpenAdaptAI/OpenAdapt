"""Read a browser page as a :class:`~openadapt_flow.gui.model.Screen`, and act on it.

:data:`OBSERVE_JS` walks the page once and returns every visible control a
person can act on, links included (``a[href]`` and ``role="link"``), with its
role, accessible name, value, checked state, the containers it sits in and
the dialog that holds it. Roles come from the element's ARIA role, else from
its tag and type, never from its text.

:class:`PageActuator` drives a Playwright ``Page`` (sync API) and counts
writes: every request whose method is not GET, HEAD or OPTIONS, plus GET
requests whose path matches a pattern the pack declares (an app whose commit
is a plain link). Nothing else counts, so a page that saves by some other
route shows no write, and no write means no commit.

Handles are CSS paths (``#id`` when the id is unique, else ``nth-of-type``
steps), computed without touching the page.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence

from openadapt_flow.gui.model import ROLES, Container, Control, Dialog, Screen

__all__ = ["OBSERVE_JS", "PageActuator", "screen_from_dom"]

#: Cap on the page text kept; a longer page is a partial read.
TEXT_CAP = 20000

OBSERVE_JS = r"""(textCap) => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el.isConnected) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    return el.getClientRects().length > 0;
  };
  const path = (el) => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1)
      return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node.id && document.querySelectorAll('#' + CSS.escape(node.id)).length === 1) {
        parts.unshift('#' + CSS.escape(node.id));
        return parts.join(' > ');
      }
      const tag = node.tagName.toLowerCase();
      let i = 1;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling)
        if (s.tagName === node.tagName) i++;
      parts.unshift(tag + ':nth-of-type(' + i + ')');
      node = node.parentElement;
    }
    return 'html > ' + parts.join(' > ');
  };
  const byIds = (ids) => clean(String(ids || '').split(/\s+/).map((id) => {
    const n = id && document.getElementById(id);
    return n ? n.textContent : '';
  }).join(' '));
  const ownText = (el) => {
    const c = el.cloneNode(true);
    for (const x of c.querySelectorAll('input, select, textarea')) x.remove();
    return clean(c.textContent);
  };
  const roleOf = (el) => {
    const aria = (el.getAttribute('role') || '').toLowerCase();
    const map = {textbox: 'textbox', searchbox: 'textbox', checkbox: 'checkbox',
      switch: 'switch', radio: 'radio', combobox: 'combobox', listbox: 'listbox',
      button: 'button', link: 'link', tab: 'tab', menuitem: 'menuitem',
      gridcell: 'cell', cell: 'cell', spinbutton: 'textbox'};
    if (map[aria]) return map[aria];
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'button') return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'hidden' || t === 'file') return null;
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (['submit', 'button', 'reset', 'image'].includes(t)) return 'button';
      return 'textbox';
    }
    if (el.isContentEditable && el.getAttribute('contenteditable') !== null) return 'textbox';
    return null;
  };
  const nameOf = (el, role) => {
    const aria = clean(el.getAttribute('aria-label')) || byIds(el.getAttribute('aria-labelledby'));
    if (aria) return aria;
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return ownText(lab);
    }
    const wrap = el.closest('label');
    if (wrap) return ownText(wrap);
    if (['button', 'link', 'tab', 'menuitem'].includes(role)) {
      if (el.tagName === 'INPUT') return clean(el.value);
      return clean(el.textContent) || clean(el.getAttribute('title'));
    }
    return clean(el.getAttribute('title')) || clean(el.getAttribute('placeholder'))
      || clean(el.getAttribute('name'));
  };
  const KINDS = [['fieldset', 'group'], ['[role="group"]', 'group'],
    ['[role="dialog"], [role="alertdialog"], dialog', 'dialog'],
    ['[role="region"], section', 'region'], ['form', 'form'],
    ['tr, [role="row"]', 'row'], ['li, [role="listitem"], article', 'item']];
  const containerName = (node, kind) => {
    const aria = clean(node.getAttribute('aria-label')) || byIds(node.getAttribute('aria-labelledby'));
    if (aria) return aria;
    if (kind === 'group' && node.tagName === 'FIELDSET') {
      const lg = node.querySelector(':scope > legend');
      if (lg) return clean(lg.textContent);
    }
    if (kind === 'row') {
      const th = node.querySelector(':scope > th, :scope > [role="rowheader"]');
      return th ? clean(th.textContent) : '';
    }
    const h = node.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > h6, :scope > header h1, :scope > header h2, :scope > header h3, :scope > header h4');
    return h ? clean(h.textContent) : '';
  };
  const chain = (el) => {
    const out = [];
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      for (const [sel, kind] of KINDS) {
        if (n.matches(sel)) {
          out.unshift({handle: path(n), kind, name: containerName(n, kind),
                       text: clean(n.innerText).slice(0, 300)});
          break;
        }
      }
    }
    return out;
  };
  const dialogs = [];
  for (const d of document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open], [aria-modal="true"]')) {
    if (!visible(d)) continue;
    dialogs.push({handle: path(d), name: containerName(d, 'dialog'),
                  text: clean(d.innerText).slice(0, 2000),
                  modal: d.getAttribute('aria-modal') === 'true' || d.tagName === 'DIALOG' || d.getAttribute('role') === 'alertdialog'});
  }
  const dialogOf = (el) => {
    const d = el.closest('[role="dialog"], [role="alertdialog"], dialog[open], [aria-modal="true"]');
    return d && visible(d) ? path(d) : '';
  };
  const controls = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('input, select, textarea, button, a, [role], [contenteditable]')) {
    const role = roleOf(el);
    if (!role || seen.has(el) || !visible(el)) continue;
    seen.add(el);
    let value = '';
    let checked = null;
    if (role === 'checkbox' || role === 'switch' || role === 'radio') {
      checked = el.tagName === 'INPUT' ? !!el.checked : el.getAttribute('aria-checked') === 'true';
      value = el.tagName === 'INPUT' ? String(el.value || '') : '';
    } else if (el.tagName === 'SELECT') {
      const o = el.selectedOptions && el.selectedOptions[0];
      value = o ? clean(o.textContent) : '';
    } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      if (role !== 'button') value = String(el.value || '');
    } else if (role === 'textbox' || role === 'cell') {
      value = clean(el.textContent);
    }
    const errId = el.getAttribute('aria-errormessage') || '';
    controls.push({
      handle: path(el), role, name: nameOf(el, role), value, checked,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      readonly: !!el.readOnly || el.getAttribute('aria-readonly') === 'true',
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      containers: chain(el), dialog: dialogOf(el),
      target: role === 'link' ? (el.getAttribute('href') || '') : '',
      error: el.getAttribute('aria-invalid') === 'true' ? byIds(errId) : '',
    });
  }
  const errors = [];
  for (const a of document.querySelectorAll('[role="alert"]')) {
    if (visible(a) && clean(a.textContent)) errors.push(clean(a.textContent));
  }
  const text = String(document.body ? document.body.innerText : '');
  return {controls, dialogs, errors, title: document.title,
          location: location.pathname + location.search,
          text: text.slice(0, textCap), complete: text.length <= textCap};
}"""


def screen_from_dom(raw: dict[str, Any]) -> Screen:
    """Build a :class:`Screen` from what :data:`OBSERVE_JS` returned."""

    controls = []
    for c in raw.get("controls") or ():
        role = c.get("role") if c.get("role") in ROLES else "other"
        controls.append(
            Control(
                handle=str(c["handle"]),
                role=role,
                name=str(c.get("name") or ""),
                value=str(c.get("value") or ""),
                checked=c.get("checked"),
                disabled=bool(c.get("disabled")),
                readonly=bool(c.get("readonly")),
                required=bool(c.get("required")),
                containers=tuple(
                    Container(
                        str(b["handle"]),
                        str(b.get("kind") or "group"),
                        str(b.get("name") or ""),
                        str(b.get("text") or ""),
                    )
                    for b in c.get("containers") or ()
                ),
                dialog=str(c.get("dialog") or ""),
                target=str(c.get("target") or ""),
                error=str(c.get("error") or ""),
            )
        )
    return Screen(
        controls=tuple(controls),
        dialogs=tuple(
            Dialog(
                str(d["handle"]),
                str(d.get("name") or ""),
                str(d.get("text") or ""),
                bool(d.get("modal")),
            )
            for d in raw.get("dialogs") or ()
        ),
        location=str(raw.get("location") or ""),
        title=str(raw.get("title") or ""),
        text=str(raw.get("text") or ""),
        complete=bool(raw.get("complete")),
        errors=tuple(str(e) for e in raw.get("errors") or ()),
    )


_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _origin_of(url: str) -> str:
    """Return normalized ``scheme://netloc`` for an HTTP(S) URL, or empty string."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


class PageActuator:
    """An :class:`~openadapt_flow.gui.model.Actuator` over a Playwright page.

    ``write_get_paths`` are regular expressions, from the pack, for GET
    requests that change state on this app (a commit drawn as a plain link).
    ``allowed_origins`` are extra origins (from the pack) whose write responses
    count in addition to the page's own origin; third-party beacons to other
    origins are ignored.
    """

    def __init__(
        self,
        page: Any,
        *,
        write_get_paths: Sequence[str] = (),
        allowed_origins: Sequence[str] = (),
    ) -> None:
        self.page = page
        self._writes = 0
        self._write_errors: list[str] = []
        self._get_writes = tuple(re.compile(p) for p in write_get_paths)
        self._allowed_origins = frozenset(
            o for o in (_origin_of(raw) for raw in allowed_origins) if o
        )
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)

    def _is_counted_write_request(self, request: Any) -> bool:
        req_url = str(request.url)
        req_origin = _origin_of(req_url)
        page_origin = _origin_of(str(getattr(self.page, "url", "") or ""))
        if not req_origin:
            return False
        if req_origin != page_origin and req_origin not in self._allowed_origins:
            return False
        method = str(request.method).upper()
        if method not in _READ_ONLY_METHODS:
            return True
        if self._get_writes:
            from urllib.parse import urlsplit

            path = urlsplit(req_url).path
            if any(p.search(path) for p in self._get_writes):
                return True
        return False

    def _on_response(self, response: Any) -> None:
        request = response.request
        if not self._is_counted_write_request(request):
            return
        status = int(response.status)
        if 200 <= status < 400:
            self._writes += 1
        elif status >= 400:
            from urllib.parse import urlsplit

            method = str(request.method).upper()
            path = urlsplit(str(request.url)).path or "/"
            self._write_errors.append(f"{method} {path} returned HTTP {status}")

    def _on_request_failed(self, request: Any) -> None:
        if not self._is_counted_write_request(request):
            return
        from urllib.parse import urlsplit

        method = str(request.method).upper()
        path = urlsplit(str(request.url)).path or "/"
        failure = getattr(request, "failure", None) or "request failed"
        self._write_errors.append(f"{method} {path} failed: {failure}")

    def write_errors(self) -> tuple[str, ...]:
        """Refuting HTTP 4xx/5xx or failed write-request errors observed so far."""
        return tuple(self._write_errors)

    def _settle(self) -> None:
        self.page.wait_for_load_state("load")
        self.page.wait_for_load_state("networkidle")

    def observe(self) -> Screen:
        """Read the page once it settles. A read cut off by a navigation is
        retried on the new page; after three tries the error is raised."""

        from playwright.sync_api import Error as PlaywrightError

        for attempt in range(3):
            self._settle()
            try:
                return screen_from_dom(self.page.evaluate(OBSERVE_JS, TEXT_CAP))
            except PlaywrightError as exc:
                if attempt == 2 or "context was destroyed" not in str(exc):
                    raise
                self.page.wait_for_timeout(100)
        raise AssertionError("unreachable")

    def click(self, handle: str) -> None:
        self.page.locator(handle).click()
        # A click may start a navigation or a request a moment later; give
        # it time to start before waiting for the page to settle.
        self.page.wait_for_timeout(150)
        self._settle()

    def set_text(self, handle: str, value: str) -> None:
        self.page.locator(handle).fill(value)
        self.page.locator(handle).dispatch_event("change")
        self._settle()

    def set_checked(self, handle: str, checked: bool) -> None:
        self.page.locator(handle).set_checked(checked)
        self._settle()

    def write_count(self) -> Optional[int]:
        return self._writes
