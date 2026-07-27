"""Interactive recorder: capture a demonstration the USER drives live.

``openadapt-flow record --url <app>`` opens a real (headed) Playwright browser
pointed at the user's OWN app and simply watches: it listens to the user's
real clicks, typing, key presses and scrolls via in-page capture-phase DOM
listeners (the same technique ``playwright codegen`` uses) and writes the
EXACT recording format the compiler already consumes (``meta.json`` +
``events.jsonl`` + ``frames/{i:04d}_before.png`` / ``_after.png``).

    record --url … → compile → replay

closes the self-serve loop for the user's own app, not just the bundled demo.

Design (why it looks the way it does):

* **Append-only binding, work in the loop.** Calling any Playwright page
  method from inside an ``expose_binding`` callback deadlocks the sync driver,
  so the binding callback does the ONE cheap thing it safely can — append the
  raw event to a Python list — and the main loop drains that list and does all
  the screenshotting/settling. Listeners are installed with ``add_init_script``
  so they survive navigations, and a navigating click's event is delivered
  over the pipe before the new document loads.
* **Frames chain like a driven demo.** A demonstration's screen is static
  between actions, so each step's BEFORE frame is simply the previous step's
  settled frame (captured before the current action happened — no post-
  navigation race), and its AFTER frame is captured once the screen settles.
* **Structured identity is captured in-page** at click time (pre-navigation),
  mirroring ``PlaywrightBackend.structured_text_at`` exactly, so the compiler's
  DOM-identity tier arms on interactively-recorded bundles too.

Secrets never touch Python: a field is secret when it is ``input[type=
password]`` or its name/id is passed via ``--secret``. For a secret field the
in-page listener emits NO value at all (only that a secret was typed, plus the
field rectangle for redaction); the literal is never read, never sent over the
pipe, never written to meta/events/frames/bundle. See ``ir.Step.secret`` and
``docs`` for the full contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.recorder import Recorder

# Named keys worth recording as their own KEY step (navigation/submit intent).
# Editing keys (Backspace/Delete) are intentionally omitted: their effect is
# already reflected in the field's value, read via the ``input`` event.
_SPECIAL_KEYS = (
    "Enter",
    "Tab",
    "Escape",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "PageUp",
    "PageDown",
    "Home",
    "End",
)

# In-page recorder script. Installed via add_init_script so it re-arms on every
# document (navigations). Emits raw events to the Python side via the
# __oaflow_emit binding. __SECRET_NAMES__ / __SPECIAL_KEYS__ are substituted in.
_INIT_JS = r"""
(() => {
  if (window.__oaflowInstalled) return;
  window.__oaflowInstalled = true;
  const SECRET_NAMES = __SECRET_NAMES__;
  const IDENT_NAMES = __IDENT_NAMES__;
  const SPECIAL = __SPECIAL_KEYS__;

  function identifierRect() {
    // Bounding rect of the operator-marked record-identifying field
    // (--identifier FIELD): the patient-banner / MRN element whose PIXELS the
    // compiler crops (anchor.identifier_crop) to arm the pixel-compare
    // identity tier on remote-display replays. Captured at CLICK time (the
    // rect the clicked frame shows), first marked field present wins.
    try {
      for (const key of IDENT_NAMES) {
        const el = document.getElementsByName(key)[0]
          || document.getElementById(key);
        if (!el || !el.getBoundingClientRect) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          return [Math.round(r.left), Math.round(r.top),
                  Math.round(r.width), Math.round(r.height)];
        }
      }
    } catch (e) {}
    return null;
  }

  function structuredIdentity(px, py) {
    // Mirrors PlaywrightBackend.structured_text_at: the REAL characters of the
    // clicked row (MRN/name/DOB), excluding the clicked target's own cell.
    try {
      const el = document.elementFromPoint(px, py);
      if (!el) return null;
      const row = el.closest('tr, [role="row"], li, [role="listitem"]');
      if (!row) return null;
      const declared = (
        row.getAttribute('data-openadapt-identity')
        || row.getAttribute('aria-label')
        || ''
      ).replace(/\s+/g, ' ').trim();
      if (declared) return declared;
      const own = el.closest('td, th, [role="cell"], [role="gridcell"]') || el;
      own.setAttribute('data-oaflow-own', '1');
      let body = '';
      try {
        const clone = row.cloneNode(true);
        const marked = clone.querySelector('[data-oaflow-own="1"]');
        if (marked) marked.remove();
        body = clone.textContent || '';
      } finally {
        own.removeAttribute('data-oaflow-own');
      }
      const joined = body.replace(/\s+/g, ' ').trim();
      return joined || null;
    } catch (e) { return null; }
  }

  function targetRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['button', 'submit', 'reset'].indexOf(type) >= 0) return 'button';
      return 'textbox';
    }
    return null;
  }

  function targetName(el) {
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const labelledBy = (el.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {
      const value = labelledBy.split(/\s+/).map((id) => {
        const node = document.getElementById(id);
        return node ? (node.textContent || '').trim() : '';
      }).filter(Boolean).join(' ');
      if (value) return value;
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label && (label.textContent || '').trim()) return label.textContent.trim();
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping !== el && (wrapping.textContent || '').trim()) {
      return wrapping.textContent.trim();
    }
    for (const attr of ['alt', 'title', 'placeholder']) {
      const value = (el.getAttribute(attr) || '').trim();
      if (value) return value;
    }
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    return text ? text.slice(0, 200) : null;
  }

  function uniqueSelector(el) {
    if (el.id) {
      const selector = `#${CSS.escape(el.id)}`;
      if (document.querySelectorAll(selector).length === 1) return selector;
    }
    for (const attr of ['data-testid', 'data-test', 'name']) {
      const value = el.getAttribute(attr);
      if (!value) continue;
      const selector = `${el.tagName.toLowerCase()}[${attr}="${CSS.escape(value)}"]`;
      if (document.querySelectorAll(selector).length === 1) return selector;
    }
    return null;
  }

  function structuralTarget(px, py) {
    try {
      const el = document.elementFromPoint(px, py);
      if (!el) return null;
      const target = {
        selector: uniqueSelector(el),
        role: targetRole(el),
        name: targetName(el),
      };
      return (target.selector || target.role || target.name) ? target : null;
    } catch (e) { return null; }
  }

  function isSecretEl(el) {
    if (!el) return false;
    if ((el.type || '').toLowerCase() === 'password') return true;
    const n = el.name || '', i = el.id || '';
    return SECRET_NAMES.indexOf(n) >= 0 || SECRET_NAMES.indexOf(i) >= 0;
  }

  function fieldLabel(el) {
    // Best available human label for the receiving field, best-first:
    // associated <label for=...>, wrapping <label>, aria-label,
    // aria-labelledby, placeholder, name attribute, title. Mirrors
    // PlaywrightBackend.focused_field_label exactly. Passive metadata for
    // the compile-time parameter-proposal pass; NEVER the field's value.
    try {
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
    } catch (e) {}
    return null;
  }

  function emit(o) { try { window.__oaflow_emit(o); } catch (e) {} }

  let pointerDown = null;
  let suppressClick = false;
  document.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    pointerDown = {
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      sid: structuredIdentity(e.clientX, e.clientY),
      structural: structuralTarget(e.clientX, e.clientY),
      idr: identifierRect(),
    };
  }, true);

  document.addEventListener('pointerup', (e) => {
    if (e.button !== 0 || !pointerDown) return;
    const start = pointerDown;
    pointerDown = null;
    const endX = Math.round(e.clientX), endY = Math.round(e.clientY);
    if (Math.hypot(endX - start.x, endY - start.y) < 5) return;
    suppressClick = true;
    setTimeout(() => { suppressClick = false; }, 0);
    emit({
      kind: 'drag', x: start.x, y: start.y, end_x: endX, end_y: endY,
      sid: start.sid, structural: start.structural,
      end_structural: structuralTarget(endX, endY), idr: start.idr,
      url: location.href, title: document.title,
    });
  }, true);

  document.addEventListener('click', (e) => {
    if (e.button !== 0) return;
    if (suppressClick) { suppressClick = false; return; }
    emit({
      kind: 'click',
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      sid: structuredIdentity(e.clientX, e.clientY),
      structural: structuralTarget(e.clientX, e.clientY),
      idr: identifierRect(),
      url: location.href, title: document.title,
    });
  }, true);

  document.addEventListener('contextmenu', (e) => {
    emit({
      kind: 'right_click',
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      sid: structuredIdentity(e.clientX, e.clientY),
      structural: structuralTarget(e.clientX, e.clientY),
      idr: identifierRect(),
      url: location.href, title: document.title,
    });
  }, true);

  document.addEventListener('input', (e) => {
    const el = e.target;
    const secret = isSecretEl(el);
    const r = (el.getBoundingClientRect && el.getBoundingClientRect())
      || { left: 0, top: 0, width: 0, height: 0 };
    const o = {
      kind: 'input',
      field: el.name || el.id || null,
      label: fieldLabel(el),
      secret: secret,
      rect: [Math.round(r.left), Math.round(r.top),
             Math.round(r.width), Math.round(r.height)],
      url: location.href, title: document.title,
    };
    // The literal value of a SECRET field is never read or transmitted.
    if (!secret) o.value = (el.value != null ? String(el.value) : '');
    emit(o);
  }, true);

  document.addEventListener('keydown', (e) => {
    const modifiers = [];
    if (e.ctrlKey) modifiers.push('ctrl');
    if (e.altKey) modifiers.push('alt');
    if (e.shiftKey) modifiers.push('shift');
    if (e.metaKey) modifiers.push('meta');
    const pureModifier = ['Control', 'Alt', 'Shift', 'Meta'].indexOf(e.key) >= 0;
    const shiftedText = modifiers.length === 1
      && modifiers[0] === 'shift' && e.key.length === 1;
    if (modifiers.length && !pureModifier && !shiftedText && !e.repeat) {
      emit({
        kind: 'hotkey', key: e.key, modifiers,
        url: location.href, title: document.title,
      });
      return;
    }
    if (SPECIAL.indexOf(e.key) < 0) return;
    emit({ kind: 'key', key: e.key, url: location.href, title: document.title });
  }, true);

  document.addEventListener('wheel', (e) => {
    emit({
      kind: 'scroll',
      dx: Math.round(e.deltaX), dy: Math.round(e.deltaY),
      url: location.href, title: document.title,
    });
  }, true);
})();
"""


class InteractiveRecorder:
    """Drives a live headed browser and records what the user does.

    Use :func:`record_interactive` for the common case; this class is exposed
    for tests, which drive synthetic input via :attr:`page` and pump the loop
    deterministically.
    """

    def __init__(
        self,
        url: str,
        out_dir: Path | str,
        *,
        secret_fields: tuple[str, ...] = (),
        param_fields: tuple[str, ...] = (),
        identifier_fields: tuple[str, ...] = (),
        headless: bool = False,
        poll_ms: int = 60,
        settle_timeout_s: float = 5.0,
        settle_stable_frames: int = 2,
        settle_interval_s: float = 0.15,
        viewport: tuple[int, int] = (1280, 800),
    ) -> None:
        self._url = url
        self._out_dir = Path(out_dir)
        self._secret_fields = set(secret_fields)
        self._param_fields = set(param_fields)
        self._identifier_fields = set(identifier_fields)
        self._headless = headless
        self._poll_ms = poll_ms
        self._viewport = viewport
        self._settle = dict(
            settle_timeout_s=settle_timeout_s,
            settle_stable_frames=settle_stable_frames,
            settle_interval_s=settle_interval_s,
        )
        self._pyq: list[dict[str, Any]] = []
        self._pending_type: Optional[dict[str, Any]] = None
        self._pending_scroll: Optional[dict[str, Any]] = None
        self.done = False

        # Set on start().
        self._pw = None
        self._browser = None
        self.page = None
        self.backend: Optional[PlaywrightBackend] = None
        self.recorder: Optional[Recorder] = None
        self._last_frame: bytes = b""
        self._last_structural: dict[str, Any] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch the browser, install the in-page listeners, capture the
        initial settled frame."""
        from openadapt_flow._browser_setup import ensure_chromium_installed

        ensure_chromium_installed()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=self._headless)
        except Exception:
            self._pw.stop()
            raise
        self.page = self._browser.new_page(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            device_scale_factor=1,
        )
        self.page.on("close", lambda _=None: setattr(self, "done", True))
        self.page.expose_binding(
            "__oaflow_emit",
            lambda source, detail: self._pyq.append(detail),
        )
        init_js = (
            _INIT_JS.replace(
                "__SECRET_NAMES__", json.dumps(sorted(self._secret_fields))
            )
            .replace("__IDENT_NAMES__", json.dumps(sorted(self._identifier_fields)))
            .replace("__SPECIAL_KEYS__", json.dumps(list(_SPECIAL_KEYS)))
        )
        self.page.add_init_script(init_js)
        self.page.goto(self._url)
        try:
            self.page.wait_for_load_state("load")
        except Exception:
            pass
        self.backend = PlaywrightBackend(self.page)
        self.recorder = Recorder(
            self.backend, self._out_dir, app_url=self._url, **self._settle
        )
        self._last_frame = self.recorder._wait_settled()
        self._last_structural = self._structural_state()

    def run(self) -> Path:
        """Human loop: pump until the user stops (Ctrl-C / closes the window),
        then flush and finish."""
        print(
            f"Recording {self._url}\n"
            "  Perform your workflow in the browser window.\n"
            "  Press Ctrl-C here (or close the browser window) to finish."
        )
        try:
            while not self.done:
                if not self._pump():
                    break
        except KeyboardInterrupt:
            print("\n[record] stopping…")
        return self.finish()

    def run_script(self, script: Callable[[Any, Callable[[], None]], None]) -> Path:
        """Scripted loop (tests): run ``script(page, pump)`` — which performs
        synthetic input and calls ``pump()`` to let the recorder drain — then
        flush and finish."""
        script(self.page, self.pump)
        return self.finish()

    def finish(self) -> Path:
        """Flush trailing input, write meta.json, tear the browser down."""
        try:
            self._flush_type()
            self._flush_scroll()
        finally:
            assert self.recorder is not None
            out = self.recorder.finish()
            try:
                if self._browser is not None:
                    self._browser.close()
            finally:
                if self._pw is not None:
                    self._pw.stop()
        return out

    # -- event pump ----------------------------------------------------------

    def pump(self) -> bool:
        """One public pump tick (used by scripted tests). Returns False when
        the page/browser is gone."""
        return self._pump()

    def _pump(self) -> bool:
        try:
            self.page.wait_for_timeout(self._poll_ms)
        except Exception:
            self.done = True
            return False
        batch = self._pyq[:]
        del self._pyq[:]
        if not batch:
            # Distinct scroll gestures are separated by pauses; flush a
            # completed scroll on idle so each becomes its own step. A type run
            # is NOT idle-flushed (a mid-word pause must not split it) — it
            # flushes on the next boundary event or at finish().
            self._flush_scroll()
            return True
        for ev in batch:
            self._process(ev)
        return True

    def _process(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "input":
            self._flush_scroll()
            self._accumulate_input(ev)
        elif kind == "scroll":
            self._flush_type()
            self._accumulate_scroll(ev)
        elif kind in {"click", "right_click", "drag"}:
            self._flush_type()
            self._flush_scroll()
            self._record_pointer(ev)
        elif kind in {"key", "hotkey"}:
            self._flush_type()
            self._flush_scroll()
            self._record_key(ev)

    # -- accumulation / flush ------------------------------------------------

    def _accumulate_input(self, ev: dict[str, Any]) -> None:
        field = ev.get("field")
        if self._pending_type is not None and self._pending_type.get("field") != field:
            self._flush_type()  # focus moved to a different field
        if self._pending_type is None:
            self._pending_type = {
                "field": field,
                "label": ev.get("label"),
                "secret": bool(ev.get("secret")),
                "value": "",
                "rect": ev.get("rect"),
            }
        pt = self._pending_type
        pt["secret"] = pt["secret"] or bool(ev.get("secret"))
        if ev.get("label"):
            pt["label"] = ev["label"]
        if ev.get("rect"):
            pt["rect"] = ev["rect"]
        if not pt["secret"]:
            pt["value"] = ev.get("value", pt["value"])
        # The structural context for the whole run is its FIRST input's frame.
        pt.setdefault("structural_before", dict(self._last_structural))
        # Capture the field-with-text after-frame NOW, while the typed value is
        # on screen and BEFORE any following navigating action executes. In a
        # human recording the pump cadence reaches here between the last
        # keystroke and the next click, so this frame is the settled field —
        # not a screen the next click has already navigated to.
        assert self.backend is not None
        pt["after_frame"] = self.backend.screenshot()
        pt["structural_after"] = self._structural_state()

    def _flush_type(self) -> None:
        pt = self._pending_type
        self._pending_type = None
        if pt is None:
            return
        field = pt.get("field")
        structural_before = pt.get("structural_before", self._last_structural)
        assert self.recorder is not None
        after_png = pt.get("after_frame")
        structural_after = pt.get("structural_after")
        # The receiving field's label rides on every TYPE event as PASSIVE
        # evidence (never the value): the compiler's deterministic
        # parameter-proposal pass names a proposed parameter from it, gated
        # behind operator confirmation (compiler.annotate.FieldLabelAnnotator).
        label_evidence: dict[str, Any] = {}
        if pt.get("label"):
            label_evidence["field_label"] = pt["label"]
        if pt["secret"]:
            rect = pt.get("rect") or None
            redact = tuple(rect) if rect and rect[2] and rect[3] else None
            self.recorder.record_observed(
                {"kind": "type", **label_evidence},
                before_png=self._last_frame,
                structural_before=structural_before,
                param=field or "secret",
                secret=True,
                redact_region=redact,
                after_png=after_png,
                structural_after=structural_after,
            )
        elif field and field in self._param_fields:
            self.recorder.record_observed(
                {"kind": "type", "text": pt["value"], **label_evidence},
                before_png=self._last_frame,
                structural_before=structural_before,
                param=field,
                after_png=after_png,
                structural_after=structural_after,
            )
        else:
            # Non-secret, unparameterized: recorded as a literal (replayed
            # verbatim), matching the demo driver's username/note handling.
            # The field rect rides along so the compiler's nearby-OCR label
            # fallback has a place to look when no DOM label exists.
            rect = pt.get("rect") or None
            if rect and rect[2] and rect[3]:
                label_evidence["field_rect"] = [int(v) for v in rect]
            self.recorder.record_observed(
                {"kind": "type", "text": pt["value"], **label_evidence},
                before_png=self._last_frame,
                structural_before=structural_before,
                after_png=after_png,
                structural_after=structural_after,
            )
        self._set_last(after_png, structural_after)

    def _accumulate_scroll(self, ev: dict[str, Any]) -> None:
        if self._pending_scroll is None:
            self._pending_scroll = {
                "dx": 0,
                "dy": 0,
                "structural_before": dict(self._last_structural),
            }
        ps = self._pending_scroll
        ps["dx"] += int(ev.get("dx", 0))
        ps["dy"] += int(ev.get("dy", 0))
        # Post-scroll after-state, captured now (before any following action).
        assert self.backend is not None
        ps["after_frame"] = self.backend.screenshot()
        ps["structural_after"] = self._structural_state()

    def _flush_scroll(self) -> None:
        ps = self._pending_scroll
        self._pending_scroll = None
        if ps is None or (ps["dx"] == 0 and ps["dy"] == 0):
            return
        assert self.recorder is not None
        after_png = ps.get("after_frame")
        structural_after = ps.get("structural_after")
        self.recorder.record_observed(
            {"kind": "scroll", "dx": ps["dx"], "dy": ps["dy"]},
            before_png=self._last_frame,
            structural_before=ps.get("structural_before", self._last_structural),
            after_png=after_png,
            structural_after=structural_after,
        )
        self._set_last(after_png, structural_after)

    def _record_pointer(self, ev: dict[str, Any]) -> None:
        assert self.recorder is not None
        kind = str(ev["kind"])
        event: dict[str, Any] = {
            "kind": kind,
            "x": int(ev["x"]),
            "y": int(ev["y"]),
        }
        if kind == "drag":
            event.update(end_x=int(ev["end_x"]), end_y=int(ev["end_y"]))
            if ev.get("end_structural"):
                event["drag_end_structural"] = ev["end_structural"]
        if ev.get("structural"):
            event["structural"] = ev["structural"]
        # Marked identifier field rect (--identifier FIELD), captured in-page
        # at click time: the compiler crops these pixels
        # (anchor.identifier_crop) to arm the pixel identity tier.
        idr = ev.get("idr")
        if idr:
            event["identifier_region"] = [int(v) for v in idr]
        self.recorder.record_observed(
            event,
            before_png=self._last_frame,
            structural_before=self._last_structural,
            structured_identity=ev.get("sid"),
        )
        self._advance()

    def _record_key(self, ev: dict[str, Any]) -> None:
        assert self.recorder is not None
        event: dict[str, Any] = {"kind": ev["kind"], "key": ev["key"]}
        if ev["kind"] == "hotkey":
            event["modifiers"] = [str(value) for value in ev.get("modifiers", [])]
        self.recorder.record_observed(
            event,
            before_png=self._last_frame,
            structural_before=self._last_structural,
        )
        self._advance()

    # -- internals -----------------------------------------------------------

    def _advance(self) -> None:
        """After an IMMEDIATE step (click/key), the current settled frame
        becomes the next step's BEFORE frame."""
        assert self.backend is not None
        self._last_frame = self.backend.screenshot()
        self._last_structural = self._structural_state()

    def _set_last(
        self, after_png: Optional[bytes], structural_after: Optional[dict]
    ) -> None:
        """After a DEFERRED/coalesced step (type/scroll), the next step's
        BEFORE frame is the after-state captured when the step actually
        happened — NOT a live screenshot, which a later navigating action may
        already have moved on from."""
        if after_png is not None:
            self._last_frame = after_png
        else:
            self._advance()
            return
        if structural_after is not None:
            self._last_structural = structural_after

    def _structural_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for attr, key in (
            ("url", "url"),
            ("page_title", "title"),
            ("page_count", "pages"),
        ):
            try:
                value = getattr(self.backend, attr, None)
            except Exception:
                value = None
            if value is not None:
                state[key] = value
        return state


def record_interactive(
    url: str,
    out_dir: Path | str,
    *,
    secret_fields: tuple[str, ...] = (),
    param_fields: tuple[str, ...] = (),
    identifier_fields: tuple[str, ...] = (),
    headless: bool = False,
    script: Optional[Callable[[Any, Callable[[], None]], None]] = None,
    **kwargs: Any,
) -> Path:
    """Record a live demonstration the user drives against ``url``.

    Args:
        url: The app to record against (the user's own app).
        out_dir: Recording output directory (meta.json + events.jsonl +
            frames/), the exact format ``compile`` consumes.
        secret_fields: Field ``name``/``id`` values to treat as secrets, in
            addition to any ``input[type=password]`` (auto-detected). A
            secret's literal value is never persisted (see module docstring).
        param_fields: Field ``name``/``id`` values recorded as PARAMETERS
            (their demonstrated value becomes the default, overridable at
            replay with ``--param``); all other non-secret typed fields are
            recorded as literals.
        identifier_fields: Field ``name``/``id`` values marking the
            RECORD-IDENTIFYING region (patient banner / MRN field). At each
            click the first marked field present on the page contributes its
            bounding rect to the event (``identifier_region``); the compiler
            crops those pixels (``anchor.identifier_crop``) so the pixel
            identity tier arms — including on a bundle later replayed over a
            remote-display/pixel substrate (Citrix/RDP).
        headless: Run the browser headless (used by scripted/CI recording;
            a human recording is headed).
        script: Test hook — ``script(page, pump)`` drives synthetic input and
            pumps the loop; when given, the human wait loop is skipped.

    Returns:
        The recording directory.
    """
    session = InteractiveRecorder(
        url,
        out_dir,
        secret_fields=secret_fields,
        param_fields=param_fields,
        identifier_fields=identifier_fields,
        headless=headless,
        **kwargs,
    )
    session.start()
    if script is not None:
        return session.run_script(script)
    return session.run()
