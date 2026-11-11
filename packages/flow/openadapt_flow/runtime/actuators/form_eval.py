"""Run a form's own script on the run's values, with every write blocked.

The ``web_form`` API step sends one request in place of the clicks that fill
and submit a form. Most forms run script on the way: key and change
handlers on the fields, an ``onsubmit`` validator, a click handler on the
save button, a page listener for ``submit``, custom validate.js rules such
as a ``pastDate`` rule. The API step can't read what that script does,
so it runs it, before each save, the way the person's steps would:

1. open the form page in a scratch copy of the live session: a separate
   browser context with the session's cookies, worker and window containment,
   every request routed, every WebSocket closed, and EventSource and
   RTCPeerConnection stubbed out;
2. while the page loads, let only same-origin GET requests through (no
   other origin at all). Once it has loaded, block every request except the
   exact scripts and stylesheets the page loaded, the reads the person's
   click made in the recording (``submitter_requests``, such as a
   duplicate-record search) and the static ``.js``/``.css`` files of a recorded
   read's own frame, so nothing the page does can write; a form submit or a
   blocked channel in any frame counts as the page sending;
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

from openadapt_flow.backends.worker_containment import WorkerContainment, contain_workers

URLENCODED = "application/x-www-form-urlencoded"

#: Before any page script: what a form's own ``submit()`` would send is
#: recorded instead of navigating; WebSocket, EventSource and
#: RTCPeerConnection do nothing but say they were used; every click
#: listener's target is noted.
_INSTRUMENT_JS = """
(() => {
  const pairs = (f, s) => Array.from(s ? new FormData(f, s) : new FormData(f)).map(
    ([k, v]) => [k, typeof v === 'string' ? v : (v && v.size === 0 && v.name === '' ? '' : '<file>')]);
  const native = (f, key) => Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, key).get.call(f);
  const submission = (f, s) => ({
    fields: pairs(f, s),
    destination: s && s.hasAttribute('formaction') ? s.formAction : native(f, 'action'),
    method: s && s.hasAttribute('formmethod') ? s.formMethod : native(f, 'method'),
    encoding: s && s.hasAttribute('formenctype') ? s.formEnctype : native(f, 'enctype'),
    target: (s && s.hasAttribute('formtarget') ? s.formTarget : native(f, 'target'))
      || (document.querySelector('base[target]') || {}).target || ''
  });
  const state = { programmatic: [], channels: [], clickTargets: [], pairs, submission,
                  submits: [], reached: 0, prevented: 0 };
  Object.defineProperty(window, '__openadaptFormEval', { value: state });
  HTMLFormElement.prototype.submit = function () {
    try { state.programmatic.push(submission(this, null)); } catch (e) { state.programmatic.push(null); }
  };
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
({ actionPath, formAction, names }) => {
  const state = window.__openadaptFormEval;
  if (!state) return { unevaluable: 'the scratch page could not be instrumented' };
  let forms = Array.from(document.forms).filter((f) => {
    const p = new URL(f.getAttribute('action') || location.href, document.baseURI).pathname;
    return p === (formAction || actionPath);
  });
  // Several forms may post there (a settings form with no action posts to
  // its own page): only the one holding every field typed can be the save.
  if (forms.length > 1) forms = forms.filter((f) => names.every((n) => f.elements.namedItem(n)));
  if (forms.length !== 1) return { unevaluable: 'the scratch page has no unique form that posts to the save' };
  const form = forms[0];
  form.setAttribute('data-openadapt-eval', '1');
  window.addEventListener('submit', (e) => {
    if (e.target !== form) return;
    state.reached += 1;
    if (e.defaultPrevented) { state.prevented += 1; return; }
    try { state.submits.push(state.submission(form, e.submitter || null)); } catch (x) { state.submits.push(null); }
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
    #: Declared step writes the page's script sent (and the route blocked):
    #: ``(METHOD /path, body pairs or None)``.
    writes: list[tuple[str, Optional[list[tuple[str, str]]]]] = field(
        default_factory=list
    )

    @property
    def valid(self) -> bool:
        return self.status == "valid"


def _summary(method: str, url: str) -> str:
    return f"{method} {up.urlsplit(url).path}"


def _destination(url: Optional[str]) -> Optional[tuple[str, str, int, str, str]]:
    """An explicit HTTP destination, never an assumed local origin."""
    if not isinstance(url, str):
        return None
    try:
        parts = up.urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            return None
        port = (
            parts.port
            if parts.port is not None
            else (443 if parts.scheme == "https" else 80)
        )
        return (
            parts.scheme,
            parts.hostname.lower(),
            port,
            parts.path or "/",
            parts.query,
        )
    except (TypeError, ValueError):
        return None


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
    step_writes: tuple[str, ...] = (),
    form_action: Optional[str] = None,
    encoding: str = URLENCODED,
    timeout_s: float = 20.0,
    settle_ms: int = 300,
) -> FormEvaluation:
    """Observe native submission and script in a scratch session copy.

    ``fields`` are the values the person would type or pick (the API step's
    ``param`` and ``select_label`` fields). ``form_action`` is the form's own
    action when the page's script sends the save to ``action_path`` instead
    of submitting the form: the form is found by it, and the script's
    request to ``action_path`` (blocked) is the save compared. The path may
    include its bound query. Native submitter overrides and script requests
    must match the complete destination, method and ``encoding``. Never raises:
    a failure to run the script is an ``unevaluable`` result.
    """

    started = time.monotonic()
    late: list[dict[str, Any]] = []
    errors: list[str] = []
    dialogs: list[str] = []
    stray: list[str] = []
    phase = {"loaded": False}
    origin = _destination(base_url)
    # Match the transport's actual URL construction; resolving a relative
    # path here could claim an origin the outgoing request does not use.
    expected_url = base_url + action_path
    expected = _destination(expected_url)
    reads = {r.split(" ", 1)[1] for r in submitter_requests if r.startswith("GET ")}
    #: Exact script and stylesheet URLs the form page loaded.
    loaded_code: set[str] = set()
    context: Any = None
    containment: Optional[WorkerContainment] = None

    def containment_problem() -> Optional[str]:
        if containment is None:
            return None
        if containment.problem is not None:
            return containment.problem
        return containment.violations[0] if containment.violations else None

    def _allowed_after_load(req: Any, parts: Any) -> bool:
        """Once loaded, only what the person's steps also load gets through.

        The exact scripts and stylesheets the form page loaded; the reads the
        person's click made in the recording (the API step makes them too, as
        pre-checks); and the static ``.js``/``.css`` files a recorded read's
        own frame loads (the dialog it shows). A script or stylesheet a
        handler adds at another URL is the page sending something.
        """

        if parts.path in reads and req.resource_type in ("document", "xhr", "fetch"):
            return True
        if req.resource_type not in ("script", "stylesheet"):
            return False
        if req.url in loaded_code:
            return True
        try:
            frame_path = up.urlsplit(req.frame.url).path
        except Exception:  # noqa: BLE001 - no frame
            return False
        return (
            frame_path in reads
            and not parts.query
            and parts.path.endswith(
                ".css" if req.resource_type == "stylesheet" else ".js"
            )
        )

    def route(r: Any) -> None:
        req = r.request
        parts = up.urlsplit(req.url)
        destination = _destination(req.url)
        same_origin = (
            destination is not None
            and origin is not None
            and destination[:3] == origin[:3]
        )
        if req.resource_type in ("image", "media", "font") and (
            not phase["loaded"] or _STATIC.search(parts.path)
        ):
            r.abort()  # never needed to run the form's script
            return
        if not phase["loaded"]:
            if req.method in ("GET", "HEAD") and same_origin:
                if req.resource_type in ("script", "stylesheet"):
                    loaded_code.add(req.url)
                r.continue_()
            else:
                r.abort()
            return
        if same_origin and req.method == "GET" and _allowed_after_load(req, parts):
            r.continue_()
            return
        try:
            body = req.post_data
        except Exception:  # noqa: BLE001 - a binary body has no text
            body = None
        content_type = (req.headers or {}).get("content-type", "")
        request_encoding = content_type.split(";", 1)[0].strip().lower()
        if body is not None and content_type.startswith("multipart/form-data"):
            # Compared as fields; a body that carries a file isn't comparable.
            from openadapt_flow.network_capture import parse_multipart

            parsed = parse_multipart(body.encode("utf-8"), content_type)
            body = (
                up.urlencode([(k, v) for k, v in parsed[0]])
                if parsed is not None
                else None
            )
            content_type = URLENCODED if parsed is not None else content_type
        what = _summary(req.method, req.url)
        late.append(
            {
                "what": what,
                "method": req.method,
                "path": parts.path,
                "body": body,
                "content_type": content_type,
                "encoding": request_encoding,
                "save": same_origin
                and destination == expected
                and req.method == "POST",
                "step": same_origin and not parts.query and what in step_writes,
            }
        )
        if late[-1]["step"] or late[-1]["save"]:
            # A declared step write, or the save a page script sends itself:
            # it never leaves, but the page sees it accepted, as it was in
            # the recording (a refused request would raise in the script).
            r.fulfill(status=200, body="")
        else:
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

    def run_scratch() -> FormEvaluation:
        nonlocal context, containment
        if origin is None or expected is None or origin[:3] != expected[:3]:
            return done(
                "unevaluable", "the save has no bound same-origin HTTP destination"
            )
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

        def on_web_socket(ws: Any) -> None:
            stray.append(f"WebSocket {ws.url}")
            ws.close()

        context.route_web_socket("**", on_web_socket)
        context.add_init_script(_INSTRUMENT_JS)
        scratch = context.new_page()
        # The shared route runs first and delegates this page to route().
        containment = contain_workers(scratch, report_window_open=True)
        scratch.on("pageerror", lambda exc: errors.append(str(exc)[:200]))

        def on_dialog(d: Any) -> None:
            dialogs.append(f"{d.type}: {d.message[:160]}")
            try:
                d.dismiss()
            except Exception:  # noqa: BLE001 - already handled
                pass

        scratch.on("dialog", on_dialog)
        if problem := containment_problem():
            return done("unevaluable", problem)
        resp = scratch.goto(
            base_url + form_path, wait_until="load", timeout=timeout_s * 1000.0
        )
        if problem := containment_problem():
            return done("unevaluable", problem)
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
            {
                "actionPath": expected[3],
                "formAction": form_action,
                "names": [name for name, _ in fields],
            },
        )
        if setup.get("unevaluable"):
            return done("unevaluable", setup["unevaluable"])
        problem = _type_values(scratch, fields)
        if problem:
            return done("unevaluable", problem)
        scratch.wait_for_timeout(settle_ms)
        pre = scratch.evaluate(_PRESUBMIT_JS)
        if problem := containment_problem():
            return done("unevaluable", problem)
        if submitter:
            target = scratch.locator(submitter)
            if target.count() != 1:
                return done(
                    "unevaluable",
                    f"the scratch page has no unique {submitter} to click",
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
        state = _collect(scratch)
        if problem := containment_problem():
            return done("unevaluable", problem)
        if confirm and not _submitted(state, late):
            # The recorded reads ran; the person confirmed in what they
            # showed. Confirm the same way, wherever the control is.
            try:
                scratch.wait_for_load_state("load", timeout=_ACT_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - no navigation is fine
                pass
            frame = _frame_with(scratch, confirm)
            if problem := containment_problem():
                return done("unevaluable", problem)
            if frame is not None:
                frame.locator(confirm).first.click(timeout=_ACT_TIMEOUT_MS)
                scratch.wait_for_timeout(settle_ms)
                state = _collect(scratch)
        # The declared step writes the page's own script sent (blocked), with
        # their bodies, for the caller to compare; they aren't stray sends.
        writes = [b for b in late if b["step"]]
        verdict = _verdict(
            state,
            [b for b in late if not b["step"]],
            errors,
            dialogs,
            stray,
            expected_url,
            encoding,
            done,
        )
        verdict.writes = [
            (
                b["what"],
                up.parse_qsl(b["body"], keep_blank_values=True)
                if b["body"] is not None
                and "x-www-form-urlencoded" in b["content_type"]
                else None,
            )
            for b in writes
        ]
        return verdict

    close_failed = False
    try:
        result = run_scratch()
    except Exception as exc:  # noqa: BLE001 - a failure to run is unevaluable
        result = done("unevaluable", f"the scratch page failed ({type(exc).__name__})")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - the scratch lifetime is unconfirmed
                close_failed = True
    # Closing the context can deliver a final violation notification.
    if problem := containment_problem():
        return done("unevaluable", problem)
    if close_failed:
        return done("unevaluable", "the scratch context could not be closed")
    return result


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


#: What a frame other than the form's did: its own form submits and the
#: channels it tried, all of which count as the page sending.
_FRAME_JS = """
() => {
  const state = window.__openadaptFormEval;
  if (!state) return { uninstrumented: true };
  return { programmatic: state.programmatic.length, submits: state.submits.length,
           channels: state.channels };
}
"""


def _collect(scratch: Any) -> dict[str, Any]:
    """The form frame's record, plus anything any other frame did."""

    state = scratch.evaluate(_COLLECT_JS)
    for frame in scratch.frames:
        if frame == scratch.main_frame:
            continue
        try:
            other = frame.evaluate(_FRAME_JS)
        except Exception:  # noqa: BLE001 - a detached or cross-origin frame
            other = {"uninstrumented": True}
        url = frame.url or "a frame"
        if other.get("uninstrumented") and url not in ("", "about:blank"):
            state.setdefault("channels", []).append(f"a frame it can't read ({url})")
        for channel in other.get("channels") or []:
            state.setdefault("channels", []).append(f"{channel} in {url}")
        if other.get("programmatic") or other.get("submits"):
            state.setdefault("channels", []).append(f"a form submit in {url}")
    return state


def _frame_with(scratch: Any, selector: str) -> Any:
    for frame in scratch.frames:
        try:
            if frame.locator(selector).count() > 0:
                return frame
        except Exception:  # noqa: BLE001 - a detached frame
            continue
    return None


def _submitted(state: dict[str, Any], late: list[dict[str, Any]]) -> bool:
    saves = [b for b in late if b["save"]]
    return bool(state.get("submits") or state.get("programmatic") or saves)


def _pairs(items: Any) -> list[tuple[str, str]]:
    return [(str(k), str(v)) for k, v in items or []]


def _verdict(
    state: dict[str, Any],
    late: list[dict[str, Any]],
    errors: list[str],
    dialogs: list[str],
    stray: list[str],
    expected_url: str,
    encoding: str,
    done: Any,
) -> FormEvaluation:
    """Decide from what the page did; any doubt is a reason for the GUI."""

    saves = [b for b in late if b["save"]]
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
            f"the form's script reached outside the scratch run ({channels[0]}; blocked)",
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
        if (
            save["body"] is None
            or "x-www-form-urlencoded" not in save["content_type"]
            or save["encoding"] != encoding
        ):
            return done(
                "unevaluable", "the page submitted a body the API step can't compare"
            )
        bodies.append(("POST", up.parse_qsl(save["body"], keep_blank_values=True)))
    for via, entries in (
        ("listener", state.get("submits") or []),
        ("form.submit()", state.get("programmatic") or []),
    ):
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or _destination(entry.get("destination")) != _destination(expected_url)
                or entry.get("method", "").lower() != "post"
                or entry.get("encoding") != encoding
                or entry.get("target", "").lower() not in ("", "_self")
            ):
                return done(
                    "unevaluable",
                    "the page's effective submission destination, method or encoding differs from the API save",
                )
            bodies.append((via, _pairs(entry.get("fields"))))
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
