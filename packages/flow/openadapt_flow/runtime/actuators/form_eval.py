"""Run a form's own script on the run's values, with every write blocked.

The ``web_form`` API step sends one request in place of the clicks that fill
and submit a form. Most forms run script on the way: field ``onchange``
handlers, an ``onsubmit`` validator, a click handler on the save button, a
page listener for ``submit``, custom validate.js rules such as OpenEMR's
``pastDate``. The API step can't read what that script does, so it runs it,
before each save:

1. open the form page in a scratch copy of the live session (a separate
   browser context with the session's cookies, service workers blocked);
2. while the page loads, let only same-origin GET requests through (no
   other origin at all); once it has loaded, block every request, so
   nothing the page does here can write or reach the session;
3. put the run's values into the fields the person typed or picked, the way
   a browser does (``input`` and ``change`` events, then ``blur``);
4. start the submit the way the person did: click the recorded control
   (``submitter``), or, when the recording names none, submit the form; a
   form whose buttons have click handlers and no recorded control is
   refused;
5. see what the page does: it submits the form (the submit event reaches a
   last listener, ``form.submit()`` is called, or the browser's own POST to
   the save reaches the blocked route), or it makes a request the person's
   click made in the recording (``submitter_requests``, such as OpenEMR's
   duplicate search). The body compared is the one the page submits, taken
   at the moment it submits; a request from the recording counts with the
   form as the page left it. Where the page declares validate.js
   ``constraints``, they run too.

The result is ``valid`` only when the page submitted or went on as
recorded, raised no error, showed no dialog, and tried nothing else.
Anything else is a reason for the GUI steps to run instead. Comparing the
body with the one the API step built is the caller's job
(:meth:`WebFormActuator.prepare`); any difference keeps the save on the GUI.
Model-free.
"""

from __future__ import annotations

import time
import urllib.parse as up
from dataclasses import dataclass, field
from typing import Any, Optional

#: Before any page script: what a form's own ``submit()`` would send is
#: recorded instead of navigating, window.open opens nothing, and every
#: click listener's target is noted.
_INSTRUMENT_JS = """
(() => {
  const pairs = (f, s) => Array.from(s ? new FormData(f, s) : new FormData(f)).map(
    ([k, v]) => [k, typeof v === 'string' ? v : '<file>']);
  const state = { programmatic: [], opened: [], clickTargets: [], pairs };
  Object.defineProperty(window, '__openadaptFormEval', { value: state });
  HTMLFormElement.prototype.submit = function () {
    try { state.programmatic.push(pairs(this)); } catch (e) { state.programmatic.push(null); }
  };
  window.open = function (url) { state.opened.push(String(url || '')); return null; };
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    if (['click', 'mousedown', 'mouseup', 'pointerdown', 'pointerup'].includes(type)) {
      state.clickTargets.push(this);
    }
    return add.call(this, type, fn, opts);
  };
})();
"""

_EVALUATE_JS = """
async ({ actionPath, leaf, fields, settleMs, submitter }) => {
  const state = window.__openadaptFormEval;
  if (!state) return { unevaluable: 'the scratch page could not be instrumented' };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const form = Array.from(document.forms).find((f) => {
    const p = new URL(f.getAttribute('action') || location.href, location.href).pathname;
    return p === actionPath || (leaf && p.split('/').pop() === leaf);
  });
  if (!form) return { unevaluable: 'the scratch page has no form that posts to the save' };
  for (const [name, value] of fields) {
    const el = form.elements.namedItem(name);
    if (!el || typeof el.tagName !== 'string') {
      return { unevaluable: `${name} is not one control in the form` };
    }
    const kind = (el.type || '').toLowerCase();
    if (kind === 'checkbox' || kind === 'radio' || kind === 'file') {
      return { unevaluable: `${name} is a ${kind} the API step doesn't fill` };
    }
    if (el.tagName === 'SELECT' && !Array.from(el.options).some((o) => o.value === value)) {
      return { unevaluable: `${name} has no option with value ${JSON.stringify(value)}` };
    }
    if (typeof el.focus === 'function') el.focus();
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    if (typeof el.blur === 'function') el.blur();
    await sleep(0);
  }
  await sleep(settleMs);
  const invalid = form.noValidate ? [] : Array.from(form.elements)
    .filter((e) => e.willValidate && !e.checkValidity())
    .map((e) => `${e.name || e.id}: ${e.validationMessage}`);
  const buttons = Array.from(form.querySelectorAll(
    'button, input[type=submit], input[type=button], input[type=image]'));
  const listened = new Set(state.clickTargets);
  const buttonScript = buttons.some((b) => b.hasAttribute('onclick') || listened.has(b));
  let reached = false, prevented = null, submitted = null, threw = null, clicked = false;
  const last = (e) => {
    if (e.target !== form) return;
    reached = true;
    prevented = e.defaultPrevented;
    try { submitted = state.pairs(form, e.submitter || null); } catch (x) { submitted = state.pairs(form); }
    e.preventDefault();
  };
  const before = state.programmatic.length;
  window.addEventListener('submit', last);
  try {
    if (submitter) {
      const el = document.querySelector(submitter);
      if (!el) return { unevaluable: `the scratch page has no ${submitter} to click` };
      clicked = true;
      el.click();
    } else if (buttonScript) {
      return { unevaluable: 'a button of the form runs script on click and the recording names no control to click' };
    } else if (!invalid.length) {
      form.requestSubmit();
    }
  } catch (e) { threw = String(e && e.message || e); }
  await sleep(settleMs);
  window.removeEventListener('submit', last);
  let rules = null;
  if (typeof window.validate === 'function' && window.constraints && typeof window.constraints === 'object') {
    try {
      const values = typeof window.validate.collectFormValues === 'function'
        ? window.validate.collectFormValues(form) : Object.fromEntries(new FormData(form));
      rules = window.validate(values, window.constraints) || null;
    } catch (e) { threw = threw || `validate.js: ${String(e && e.message || e)}`; }
  }
  return { invalid, reached, prevented, submitted, threw, rules, clicked,
           programmatic: state.programmatic.slice(before), after: state.pairs(form),
           opened: state.opened.slice() };
}
"""


@dataclass
class FormEvaluation:
    """What the form's own script made of the run's values."""

    #: ``valid``, ``invalid`` (the page would refuse these values) or
    #: ``unevaluable`` (the script couldn't be run faithfully).
    status: str
    reason: str = ""
    #: The body the page submits, ``(name, value)`` in order: taken when it
    #: submits, or the form as the page left it when it went on to a request
    #: from the recording.
    serialized: Optional[list[tuple[str, str]]] = None
    #: How the page went on: ``submit`` or the recorded request it made.
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


def evaluate_form(
    page: Any,
    base_url: str,
    form_path: str,
    action_path: str,
    fields: list[tuple[str, str]],
    *,
    submitter: Optional[str] = None,
    submitter_requests: tuple[str, ...] = (),
    step_writes: tuple[str, ...] = (),
    timeout_s: float = 20.0,
    settle_ms: int = 250,
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
    phase = {"loaded": False}
    origin = up.urlsplit(base_url).netloc
    context = None
    scratch = None

    def route(r: Any) -> None:
        req = r.request
        same_origin = up.urlsplit(req.url).netloc == origin
        if phase["loaded"]:
            # Once the page has loaded, nothing leaves: no write, no read
            # that could reach the session, no other origin.
            try:
                body = req.post_data
            except Exception:  # noqa: BLE001 - a binary body has no text
                body = None
            late.append(
                {
                    "what": _summary(req.method, req.url),
                    "method": req.method,
                    "path": up.urlsplit(req.url).path,
                    "body": body,
                    "content_type": (req.headers or {}).get("content-type", ""),
                }
            )
            r.abort()
        elif (
            req.method not in ("GET", "HEAD")
            or not same_origin
            or req.resource_type in ("image", "media", "font")
        ):
            r.abort()
        else:
            r.continue_()

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
            # Without a separate context the scratch page would share the
            # run's tabs and storage; don't run the page's script there.
            return done(
                "unevaluable", "this session can't open a separate scratch context"
            )
        context = browser.new_context(
            storage_state=live.storage_state(),
            viewport=page.viewport_size or {"width": 1280, "height": 800},
            service_workers="block",
        )
        scratch = context.new_page()
        scratch.add_init_script(_INSTRUMENT_JS)
        scratch.route("**/*", route)
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
        try:
            result = scratch.evaluate(
                _EVALUATE_JS,
                {
                    "actionPath": action_path,
                    "leaf": action_path.rsplit("/", 1)[-1],
                    "fields": [[k, v] for k, v in fields],
                    "settleMs": settle_ms,
                    "submitter": submitter,
                },
            )
        except Exception as exc:  # noqa: BLE001 - e.g. the page navigated away
            result = {"navigated": type(exc).__name__}
        verdict = _verdict(
            result,
            [b for b in late if b["what"] not in step_writes],
            errors,
            dialogs,
            action_path,
            set(submitter_requests),
            done,
        )
        # The step writes the page's script sent (blocked), with their bodies.
        verdict.writes = [
            (
                b["what"],
                up.parse_qsl(b["body"], keep_blank_values=True)
                if b["body"] is not None
                and "x-www-form-urlencoded" in b["content_type"]
                else None,
            )
            for b in late
            if b["what"] in step_writes
        ]
        return verdict
    except Exception as exc:  # noqa: BLE001 - a failure to run is unevaluable
        return done("unevaluable", f"the scratch page failed ({type(exc).__name__})")
    finally:
        try:
            if scratch is not None:
                scratch.close()
            if context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup only
            pass


def _pairs(items: Any) -> list[tuple[str, str]]:
    return [(str(k), str(v)) for k, v in items or []]


def _verdict(
    result: dict[str, Any],
    late: list[dict[str, Any]],
    errors: list[str],
    dialogs: list[str],
    action_path: str,
    expected: set[str],
    done: Any,
) -> FormEvaluation:
    """Decide from what the page did; any doubt is a reason for the GUI."""

    if result.get("unevaluable"):
        return done("unevaluable", result["unevaluable"])
    saves = [b for b in late if b["method"] == "POST" and b["path"] == action_path]
    recorded = [b for b in late if b["what"] in expected and b not in saves]
    others = [b for b in late if b not in saves and b not in recorded]
    opened_all = list(result.get("opened") or [])
    opened = [u for u in opened_all if f"GET {up.urlsplit(u).path}" not in expected]
    if others:
        sent = sorted({b["what"] for b in others})
        return done(
            "unevaluable",
            f"the form's script tried to send {', '.join(sent)} while it ran (blocked)",
        )
    if errors:
        return done("unevaluable", f"the form's script raised an error ({errors[0]})")
    if result.get("threw"):
        return done(
            "unevaluable", f"the form's script raised an error ({result['threw']})"
        )
    if opened:
        return done("unevaluable", "the form's script opened a window while it ran")
    if dialogs:
        return done("invalid", f"the form's script showed a dialog ({dialogs[0]})")
    if result.get("rules"):
        names = ", ".join(sorted(result["rules"])[:5])
        return done(
            "invalid", f"the form's validation rules refuse these values ({names})"
        )
    # What the page submits, at the moment it submits.
    if saves:
        save = saves[0]
        if save["body"] is None or "x-www-form-urlencoded" not in save["content_type"]:
            return done(
                "unevaluable", "the page submitted a body the API step can't compare"
            )
        body = up.parse_qsl(save["body"], keep_blank_values=True)
        return done("valid", "", body, "submit")
    if result.get("reached") and not result.get("prevented"):
        return done("valid", "", _pairs(result.get("submitted")), "submit")
    if result.get("programmatic"):
        body = result["programmatic"][0]
        if body is None:
            return done("unevaluable", "the page's own submit could not be read")
        return done("valid", "", _pairs(body), "submit")
    if recorded or len(opened_all) > len(opened):
        via = recorded[0]["what"] if recorded else "a window from the recording"
        return done("valid", "", _pairs(result.get("after")), via)
    if result.get("navigated"):
        return done("unevaluable", "the page navigated while its script ran")
    if result.get("invalid"):
        return done(
            "invalid",
            "the browser's own checks refuse these values ("
            + "; ".join(result["invalid"][:3])
            + ")",
        )
    if result.get("prevented"):
        return done("invalid", "the form's script refused the submit for these values")
    if result.get("clicked"):
        return done(
            "invalid",
            "after the recorded click the page neither submitted nor went on as "
            "recorded (its script may have refused these values)",
        )
    return done(
        "unevaluable", "the form's script stopped the submit before it could be seen"
    )
