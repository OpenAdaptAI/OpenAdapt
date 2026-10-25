"""Run a form's own script on the run's values, with every write blocked.

The ``web_form`` API step sends one request in place of the clicks that fill
and submit a form. Most forms run script on the way: key and change
handlers on the fields, an ``onsubmit`` validator, a click handler on the
save button, a page listener for ``submit``, custom validate.js rules such
as OpenEMR's ``pastDate``. The API step can't read what that script does,
so it runs it, before each save, the way the person's steps would:

1. open the form page in a scratch copy of the live session: a separate
   browser context with the session's cookies, no service workers, every
   request of every page in it routed, every WebSocket closed, and
   EventSource and RTCPeerConnection stubbed out;
2. while the page loads, let only same-origin GET requests through (no
   other origin at all). Once it has loaded, block every request except
   same-origin scripts and stylesheets and the reads the person's click
   made in the recording (``submitter_requests``, such as OpenEMR's
   duplicate search), so nothing the page does can write;
3. type the run's values into the fields the person typed or picked, with
   real key events (``keydown``, ``keyup``, ``input``), so key handlers run
   as they do for the person. A field that isn't on screen when the form
   opens (on a tab the person opened first) can't be typed that way, so the
   run is unevaluable;
4. click the control the person clicked (``submitter``) with a real
   click; when the recording names none, submit the form, and refuse a form
   whose buttons have click handlers. When the page then shows the recorded
   reads (for example a confirm dialog), click the control the person
   clicked to confirm (``confirm``), in whichever frame has it;
5. the page must then submit the form: a last ``submit`` listener sees it
   go through, the form's own ``submit()`` is called, or the browser's POST
   reaches the route (blocked). The body compared is the one the page
   submits, taken when it submits. Declared validate.js ``constraints``
   run too.

The result is ``valid`` only when the page submitted exactly once, raised no
error, showed no dialog, opened no window or socket, and tried nothing else.
Anything else is a reason for the GUI steps to run instead. Comparing the
body with the one the API step built is the caller's job
(:meth:`WebFormActuator.prepare`); any difference keeps the save on the GUI.
Model-free.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse as up
from dataclasses import dataclass, field
from typing import Any, Optional

#: Before any page script: what a form's own ``submit()`` would send is
#: recorded instead of navigating; window.open, WebSocket, EventSource and
#: RTCPeerConnection do nothing but say they were used; every click
#: listener's target is noted.
_INSTRUMENT_JS = """
(() => {
  const pairs = (f, s) => Array.from(s ? new FormData(f, s) : new FormData(f)).map(
    ([k, v]) => [k, typeof v === 'string' ? v : '<file>']);
  const state = { programmatic: [], channels: [], clickTargets: [], pairs,
                  submits: [], reached: 0, prevented: 0 };
  Object.defineProperty(window, '__openadaptFormEval', { value: state });
  HTMLFormElement.prototype.submit = function () {
    try { state.programmatic.push(pairs(this)); } catch (e) { state.programmatic.push(null); }
  };
  window.open = function (url) { state.channels.push('window.open ' + String(url || '')); return null; };
  for (const name of ['WebSocket', 'EventSource', 'RTCPeerConnection', 'webkitRTCPeerConnection']) {
    if (name in window) {
      window[name] = function () { state.channels.push(name); throw new Error(name + ' is blocked'); };
    }
  }
  if (navigator.sendBeacon) {
    navigator.sendBeacon = function (url) { state.channels.push('sendBeacon ' + String(url)); return false; };
  }
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    if (['click', 'mousedown', 'mouseup', 'pointerdown', 'pointerup'].includes(type)) {
      state.clickTargets.push(this);
    }
    return add.call(this, type, fn, opts);
  };
})();
"""

#: Finds the form, marks it for the locators, and adds the last submit
#: listener (it records each submit that gets through, then stops it).
_SETUP_JS = """
({ actionPath, leaf }) => {
  const state = window.__openadaptFormEval;
  if (!state) return { unevaluable: 'the scratch page could not be instrumented' };
  const form = Array.from(document.forms).find((f) => {
    const p = new URL(f.getAttribute('action') || location.href, location.href).pathname;
    return p === actionPath || (leaf && p.split('/').pop() === leaf);
  });
  if (!form) return { unevaluable: 'the scratch page has no form that posts to the save' };
  form.setAttribute('data-openadapt-eval', '1');
  window.addEventListener('submit', (e) => {
    if (e.target !== form) return;
    state.reached += 1;
    if (e.defaultPrevented) { state.prevented += 1; return; }
    try { state.submits.push(state.pairs(form, e.submitter || null)); } catch (x) { state.submits.push(state.pairs(form)); }
    e.preventDefault();
  });
  return {};
}
"""

_PRESUBMIT_JS = """
() => {
  const state = window.__openadaptFormEval;
  const form = document.querySelector('form[data-openadapt-eval]');
  const invalid = form.noValidate ? [] : Array.from(form.elements)
    .filter((e) => e.willValidate && !e.checkValidity())
    .map((e) => `${e.name || e.id}: ${e.validationMessage}`);
  const listened = new Set(state.clickTargets);
  const buttons = Array.from(form.querySelectorAll(
    'button, input[type=submit], input[type=button], input[type=image]'));
  return { invalid, buttonScript: buttons.some((b) => b.hasAttribute('onclick') || listened.has(b)) };
}
"""

_COLLECT_JS = """
() => {
  const state = window.__openadaptFormEval;
  const form = document.querySelector('form[data-openadapt-eval]');
  let rules = null, threw = null;
  if (form && typeof window.validate === 'function' && window.constraints && typeof window.constraints === 'object') {
    try {
      const values = typeof window.validate.collectFormValues === 'function'
        ? window.validate.collectFormValues(form) : Object.fromEntries(new FormData(form));
      rules = window.validate(values, window.constraints) || null;
    } catch (e) { threw = `validate.js: ${String(e && e.message || e)}`; }
  }
  return { submits: state.submits, programmatic: state.programmatic, reached: state.reached,
           prevented: state.prevented, channels: state.channels, rules, threw };
}
"""

#: Files a page loads as pictures or fonts; any other image request after
#: load (``new Image().src = '/track.php?...'``) counts as the page sending.
_STATIC = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|bmp|woff2?|ttf|otf|eot)$", re.I)

#: How long a real click or keystroke may wait for its element.
_ACT_TIMEOUT_MS = 5000


@dataclass
class FormEvaluation:
    """What the form's own script made of the run's values."""

    #: ``valid``, ``invalid`` (the page would refuse these values) or
    #: ``unevaluable`` (the script couldn't be run faithfully).
    status: str
    reason: str = ""
    #: The body the page submits, ``(name, value)`` in order, taken when it
    #: submits.
    serialized: Optional[list[tuple[str, str]]] = None
    #: How the page submitted: ``listener``, ``form.submit()`` or ``POST``.
    via: str = ""
    #: Requests the page tried to send and were blocked, ``METHOD /path``.
    blocked: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def valid(self) -> bool:
        return self.status == "valid"


def _summary(method: str, url: str) -> str:
    return f"{method} {up.urlsplit(url).path}"


def _control(scratch: Any, name: str) -> Any:
    return scratch.locator(f"form[data-openadapt-eval] [name={json.dumps(name)}]").first


def evaluate_form(
    page: Any,
    base_url: str,
    form_path: str,
    action_path: str,
    fields: list[tuple[str, str]],
    *,
    submitter: Optional[str] = None,
    submitter_requests: tuple[str, ...] = (),
    confirm: Optional[str] = None,
    timeout_s: float = 20.0,
    settle_ms: int = 300,
) -> FormEvaluation:
    """Run the form's script in a scratch copy of ``page``'s session.

    ``fields`` are the values the person would type or pick (the API step's
    ``param`` and ``select_label`` fields). Never raises: a failure to run
    the script is an ``unevaluable`` result.
    """

    started = time.monotonic()
    late: list[dict[str, Any]] = []
    errors: list[str] = []
    dialogs: list[str] = []
    stray: list[str] = []
    phase = {"loaded": False}
    origin = up.urlsplit(base_url).netloc
    reads = {r.split(" ", 1)[1] for r in submitter_requests if r.startswith("GET ")}
    context = None

    def route(r: Any) -> None:
        req = r.request
        parts = up.urlsplit(req.url)
        same_origin = parts.netloc == origin
        if req.resource_type in ("image", "media", "font") and (
            not phase["loaded"] or _STATIC.search(parts.path)
        ):
            r.abort()  # never needed to run the form's script
            return
        if not phase["loaded"]:
            if req.method in ("GET", "HEAD") and same_origin:
                r.continue_()
            else:
                r.abort()
            return
        if (
            same_origin
            and req.method == "GET"
            and (req.resource_type in ("script", "stylesheet") or parts.path in reads)
        ):
            # Static code, or a read the person's click made in the
            # recording (the API step makes it too, as a pre-check).
            r.continue_()
            return
        try:
            body = req.post_data
        except Exception:  # noqa: BLE001 - a binary body has no text
            body = None
        late.append(
            {
                "what": _summary(req.method, req.url),
                "method": req.method,
                "path": parts.path,
                "body": body,
                "content_type": (req.headers or {}).get("content-type", ""),
            }
        )
        r.abort()

    def done(
        status: str, reason: str = "", serialized: Any = None, via: str = ""
    ) -> FormEvaluation:
        return FormEvaluation(
            status=status,
            reason=reason,
            serialized=serialized,
            via=via,
            blocked=[b["what"] for b in late],
            seconds=round(time.monotonic() - started, 3),
        )

    try:
        live = page.context
        browser = getattr(live, "browser", None)
        if browser is None:
            return done(
                "unevaluable", "this session can't open a separate scratch context"
            )
        context = browser.new_context(
            storage_state=live.storage_state(),
            viewport=page.viewport_size or {"width": 1280, "height": 800},
            service_workers="block",
        )
        # Every page of the scratch context goes through the route, and no
        # socket leaves it.
        context.route("**/*", route)
        context.route_web_socket(
            "**", lambda ws: (stray.append(f"WebSocket {ws.url}"), ws.close())
        )
        context.add_init_script(_INSTRUMENT_JS)
        scratch = context.new_page()

        def on_page(other: Any) -> None:
            # A popup or new tab: the scratch run can't follow it.
            stray.append(f"a new page ({other.url or 'about:blank'})")
            try:
                other.close()
            except Exception:  # noqa: BLE001 - already closed
                pass

        context.on("page", on_page)
        scratch.on("pageerror", lambda exc: errors.append(str(exc)[:200]))

        def on_dialog(d: Any) -> None:
            dialogs.append(f"{d.type}: {d.message[:160]}")
            try:
                d.dismiss()
            except Exception:  # noqa: BLE001 - already handled
                pass

        scratch.on("dialog", on_dialog)
        resp = scratch.goto(
            base_url + form_path, wait_until="load", timeout=timeout_s * 1000.0
        )
        if resp is None or resp.status != 200:
            return done(
                "unevaluable",
                f"the form page returned {getattr(resp, 'status', 'nothing')} in the scratch page",
            )
        if errors:
            return done(
                "unevaluable",
                f"the form page raised an error as it loaded ({errors[0]})",
            )
        phase["loaded"] = True
        setup = scratch.evaluate(
            _SETUP_JS,
            {"actionPath": action_path, "leaf": action_path.rsplit("/", 1)[-1]},
        )
        if setup.get("unevaluable"):
            return done("unevaluable", setup["unevaluable"])
        problem = _type_values(scratch, fields)
        if problem:
            return done("unevaluable", problem)
        scratch.wait_for_timeout(settle_ms)
        pre = scratch.evaluate(_PRESUBMIT_JS)
        if submitter:
            target = scratch.locator(submitter).first
            if target.count() == 0:
                return done(
                    "unevaluable", f"the scratch page has no {submitter} to click"
                )
            target.click(timeout=_ACT_TIMEOUT_MS)
        elif pre.get("buttonScript"):
            return done(
                "unevaluable",
                "a button of the form runs script on click and the recording names "
                "no control to click",
            )
        else:
            last = fields[-1][0] if fields else None
            if last:
                _control(scratch, last).evaluate("e => e.blur()")
            if pre.get("invalid"):
                return done(
                    "invalid",
                    "the browser's own checks refuse these values ("
                    + "; ".join(pre["invalid"][:3])
                    + ")",
                )
            scratch.evaluate(
                "() => document.querySelector('form[data-openadapt-eval]').requestSubmit()"
            )
        scratch.wait_for_timeout(settle_ms)
        state = scratch.evaluate(_COLLECT_JS)
        if confirm and not _submitted(state, late, action_path):
            # The recorded reads ran; the person confirmed in what they
            # showed. Confirm the same way, wherever the control is.
            try:
                scratch.wait_for_load_state("load", timeout=_ACT_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - no navigation is fine
                pass
            frame = _frame_with(scratch, confirm)
            if frame is not None:
                frame.locator(confirm).first.click(timeout=_ACT_TIMEOUT_MS)
                scratch.wait_for_timeout(settle_ms)
                state = scratch.evaluate(_COLLECT_JS)
        return _verdict(state, late, errors, dialogs, stray, action_path, done)
    except Exception as exc:  # noqa: BLE001 - a failure to run is unevaluable
        return done("unevaluable", f"the scratch page failed ({type(exc).__name__})")
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup only
            pass


def _type_values(scratch: Any, fields: list[tuple[str, str]]) -> Optional[str]:
    """Type each value the way a person does; why it couldn't be, or None."""

    for name, value in fields:
        control = _control(scratch, name)
        if control.count() == 0:
            return f"{name} is not a control in the form"
        tag, kind = control.evaluate("e => [e.tagName, (e.type || '').toLowerCase()]")
        if kind in ("checkbox", "radio", "file", "hidden"):
            return f"{name} is a {kind} the API step doesn't fill"
        if not control.is_visible():
            # Only real, trusted input counts; a field the person reached
            # through another step (a tab, a section) can't be typed here.
            return (
                f"{name} isn't on screen when the form opens, so it can't be typed "
                "with real key events"
            )
        if tag == "SELECT":
            label = control.evaluate(
                "(e, v) => { const o = Array.from(e.options).find((x) => x.value === v);"
                " return o ? o.text.trim() : null; }",
                value,
            )
            if label is None:
                return f"{name} has no option with value {value!r}"
            control.focus(timeout=_ACT_TIMEOUT_MS)
            # A focused select picks the option whose label is typed.
            control.press_sequentially(label, timeout=_ACT_TIMEOUT_MS)
            if control.evaluate("e => e.value") != value:
                return f"typing {label!r} into {name} didn't pick its option"
            continue
        control.click(timeout=_ACT_TIMEOUT_MS)
        control.press("ControlOrMeta+a", timeout=_ACT_TIMEOUT_MS)
        control.press("Delete", timeout=_ACT_TIMEOUT_MS)
        if value:
            control.press_sequentially(value, timeout=_ACT_TIMEOUT_MS)
    return None


def _frame_with(scratch: Any, selector: str) -> Any:
    for frame in scratch.frames:
        try:
            if frame.locator(selector).count() > 0:
                return frame
        except Exception:  # noqa: BLE001 - a detached frame
            continue
    return None


def _submitted(
    state: dict[str, Any], late: list[dict[str, Any]], action_path: str
) -> bool:
    saves = [b for b in late if b["method"] == "POST" and b["path"] == action_path]
    return bool(state.get("submits") or state.get("programmatic") or saves)


def _pairs(items: Any) -> list[tuple[str, str]]:
    return [(str(k), str(v)) for k, v in items or []]


def _verdict(
    state: dict[str, Any],
    late: list[dict[str, Any]],
    errors: list[str],
    dialogs: list[str],
    stray: list[str],
    action_path: str,
    done: Any,
) -> FormEvaluation:
    """Decide from what the page did; any doubt is a reason for the GUI."""

    saves = [b for b in late if b["method"] == "POST" and b["path"] == action_path]
    others = [b for b in late if b not in saves]
    if others:
        sent = sorted({b["what"] for b in others})
        return done(
            "unevaluable",
            f"the form's script tried to send {', '.join(sent)} while it ran (blocked)",
        )
    channels = list(state.get("channels") or []) + stray
    if channels:
        return done(
            "unevaluable",
            f"the form's script opened {channels[0]} while it ran (blocked)",
        )
    if errors:
        return done("unevaluable", f"the form's script raised an error ({errors[0]})")
    if state.get("threw"):
        return done(
            "unevaluable", f"the form's script raised an error ({state['threw']})"
        )
    if dialogs:
        return done("invalid", f"the form's script showed a dialog ({dialogs[0]})")
    if state.get("rules"):
        names = ", ".join(sorted(state["rules"])[:5])
        return done(
            "invalid", f"the form's validation rules refuse these values ({names})"
        )
    # What the page submits, at the moment it submits; exactly once.
    bodies: list[tuple[str, list[tuple[str, str]]]] = []
    for save in saves:
        if save["body"] is None or "x-www-form-urlencoded" not in save["content_type"]:
            return done(
                "unevaluable", "the page submitted a body the API step can't compare"
            )
        bodies.append(("POST", up.parse_qsl(save["body"], keep_blank_values=True)))
    bodies += [("listener", _pairs(b)) for b in state.get("submits") or []]
    for b in state.get("programmatic") or []:
        if b is None:
            return done("unevaluable", "the page's own submit could not be read")
        bodies.append(("form.submit()", _pairs(b)))
    if len(bodies) > 1:
        return done(
            "unevaluable",
            f"the page submitted the form {len(bodies)} times while its script ran",
        )
    if bodies:
        via, body = bodies[0]
        return done("valid", "", body, via)
    if state.get("prevented"):
        return done("invalid", "the form's script refused the submit for these values")
    return done(
        "invalid",
        "the page didn't submit the form (its script may have refused these values)",
    )
