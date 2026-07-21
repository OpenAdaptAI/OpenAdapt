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
      const parts = [];
      const aria = row.getAttribute ? row.getAttribute('aria-label') : null;
      if (aria) parts.push(aria);
      if (body) parts.push(body);
      const joined = parts.join(' ').replace(/\s+/g, ' ').trim();
      return joined || null;
    } catch (e) { return null; }
  }

  function isSecretEl(el) {
    if (!el) return false;
    if ((el.type || '').toLowerCase() === 'password') return true;
    const n = el.name || '', i = el.id || '';
    return SECRET_NAMES.indexOf(n) >= 0 || SECRET_NAMES.indexOf(i) >= 0;
  }

  function emit(o) { try { window.__oaflow_emit(o); } catch (e) {} }

  document.addEventListener('click', (e) => {
    emit({
      kind: 'click',
      x: Math.round(e.clientX), y: Math.round(e.clientY),
      sid: structuredIdentity(e.clientX, e.clientY),
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
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import ensure_chromium_installed

        ensure_chromium_installed()
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
        elif kind == "click":
            self._flush_type()
            self._flush_scroll()
            self._record_click(ev)
        elif kind == "key":
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
                "secret": bool(ev.get("secret")),
                "value": "",
                "rect": ev.get("rect"),
            }
        pt = self._pending_type
        pt["secret"] = pt["secret"] or bool(ev.get("secret"))
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
        if pt["secret"]:
            rect = pt.get("rect") or None
            redact = tuple(rect) if rect and rect[2] and rect[3] else None
            self.recorder.record_observed(
                {"kind": "type"},
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
                {"kind": "type", "text": pt["value"]},
                before_png=self._last_frame,
                structural_before=structural_before,
                param=field,
                after_png=after_png,
                structural_after=structural_after,
            )
        else:
            # Non-secret, unparameterized: recorded as a literal (replayed
            # verbatim), matching the demo driver's username/note handling.
            self.recorder.record_observed(
                {"kind": "type", "text": pt["value"]},
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

    def _record_click(self, ev: dict[str, Any]) -> None:
        assert self.recorder is not None
        event: dict[str, Any] = {"kind": "click", "x": int(ev["x"]), "y": int(ev["y"])}
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
        self.recorder.record_observed(
            {"kind": "key", "key": ev["key"]},
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
