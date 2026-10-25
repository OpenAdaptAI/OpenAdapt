"""Run a form's own script on the run's values, with every write blocked.

The ``web_form`` API step sends one request in place of the clicks that fill
and submit a form. Most forms run script on the way: field ``onchange``
handlers, an ``onsubmit`` validator, a page listener for ``submit``, custom
validate.js rules such as OpenEMR's ``pastDate``. The API step can't read
what that script does, so it runs it, before each save:

1. open the form page in a scratch copy of the live session (a separate
   browser context with the session's cookies, or a scratch tab when the
   browser can't make one);
2. block every request that isn't a GET for the whole visit, and every
   script-sent request once the page has loaded, so nothing the page does
   here can write;
3. put the run's values into the fields the person typed or picked, the way
   a browser does (``input`` and ``change`` events, then ``blur``);
4. read back the body the browser would send (``FormData``), check the
   browser's own constraints, and submit the form with a listener that
   records whether the page's own handlers let the submit through and then
   stops it; where the page declares validate.js ``constraints``, run them
   too.

The result is ``valid`` only when nothing failed, nothing tried to write,
the page raised no error, asked no question, and its handlers let the submit
through. Anything else is a reason for the GUI steps to run instead.
Comparing the body the page would send with the one the API step built is
the caller's job (:meth:`WebFormActuator.prepare`). Model-free.
"""

from __future__ import annotations

import time
import urllib.parse as up
from dataclasses import dataclass, field
from typing import Any, Optional

#: Before any page script: a form's own ``submit()`` (which skips the submit
#: event) is counted instead of navigating, and window.open opens nothing.
_INSTRUMENT_JS = """
(() => {
  const state = { programmaticSubmits: 0, opened: [] };
  Object.defineProperty(window, '__openadaptFormEval', { value: state });
  HTMLFormElement.prototype.submit = function () { state.programmaticSubmits += 1; };
  window.open = function (url) { state.opened.push(String(url || '')); return null; };
})();
"""

_EVALUATE_JS = """
async ({ actionPath, leaf, fields, settleMs }) => {
  const state = window.__openadaptFormEval || { programmaticSubmits: 0, opened: [] };
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
  const serialized = Array.from(new FormData(form)).map(
    ([k, v]) => [k, typeof v === 'string' ? v : '<file>']);
  const invalid = form.noValidate ? [] : Array.from(form.elements)
    .filter((e) => e.willValidate && !e.checkValidity())
    .map((e) => `${e.name || e.id}: ${e.validationMessage}`);
  let reached = false, prevented = null, threw = null;
  const last = (e) => {
    if (e.target === form) { reached = true; prevented = e.defaultPrevented; e.preventDefault(); }
  };
  const before = state.programmaticSubmits;
  window.addEventListener('submit', last);
  try {
    if (!invalid.length) {
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    }
  } catch (e) { threw = String(e && e.message || e); }
  window.removeEventListener('submit', last);
  await sleep(settleMs);
  let rules = null, ruleKinds = false;
  if (typeof window.validate === 'function' && window.constraints && typeof window.constraints === 'object') {
    ruleKinds = true;
    try {
      const values = typeof window.validate.collectFormValues === 'function'
        ? window.validate.collectFormValues(form) : Object.fromEntries(new FormData(form));
      rules = window.validate(values, window.constraints) || null;
    } catch (e) { threw = threw || `validate.js: ${String(e && e.message || e)}`; }
  }
  return { serialized, invalid, reached, prevented, threw, rules, ruleKinds,
           programmaticSubmits: state.programmaticSubmits - before, opened: state.opened.slice() };
}
"""


@dataclass
class FormEvaluation:
    """What the form's own script made of the run's values."""

    #: ``valid``, ``invalid`` (the page would refuse these values) or
    #: ``unevaluable`` (the script couldn't be run faithfully).
    status: str
    reason: str = ""
    #: The body the browser would send, ``(name, value)`` in order.
    serialized: Optional[list[tuple[str, str]]] = None
    #: Requests the page tried to send and were blocked, ``METHOD /path``.
    blocked: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def valid(self) -> bool:
        return self.status == "valid"


def _tried(blocked: list[str]) -> str:
    sent = sorted({b.removeprefix("after load: ") for b in blocked})
    return f"the form's script tried to send {', '.join(sent)} while it ran (blocked)"


def _summary(request: Any) -> str:
    return f"{request.method} {up.urlsplit(request.url).path}"


def evaluate_form(
    page: Any,
    base_url: str,
    form_path: str,
    action_path: str,
    fields: list[tuple[str, str]],
    *,
    timeout_s: float = 20.0,
    settle_ms: int = 150,
) -> FormEvaluation:
    """Run the form's script in a scratch copy of ``page``'s session.

    ``fields`` are the values the person would type or pick (the API step's
    ``param`` and ``select_label`` fields). Never raises: a failure to run
    the script is an ``unevaluable`` result.
    """

    started = time.monotonic()
    blocked: list[str] = []
    errors: list[str] = []
    dialogs: list[str] = []
    phase = {"loaded": False}
    origin = up.urlsplit(base_url)
    context = None
    scratch = None
    own_context = False

    def route(r: Any) -> None:
        req = r.request
        same_origin = up.urlsplit(req.url).netloc == origin.netloc
        script_sent = req.resource_type in ("xhr", "fetch", "eventsource", "websocket")
        if req.method not in ("GET", "HEAD") or (phase["loaded"] and script_sent):
            blocked.append(("after load: " if phase["loaded"] else "") + _summary(req))
            r.abort()
        elif req.resource_type in ("image", "media", "font") or (
            not same_origin and req.resource_type not in ("script", "stylesheet")
        ):
            r.abort()
        else:
            r.continue_()

    def done(status: str, reason: str = "", serialized: Any = None) -> FormEvaluation:
        return FormEvaluation(
            status=status,
            reason=reason,
            serialized=serialized,
            blocked=list(blocked),
            seconds=round(time.monotonic() - started, 3),
        )

    try:
        live = page.context
        browser = getattr(live, "browser", None)
        if browser is not None:
            # A separate context keeps the run's own tabs, history and page
            # count untouched; it shares the session through its cookies.
            context = browser.new_context(
                storage_state=live.storage_state(),
                viewport=page.viewport_size or {"width": 1280, "height": 800},
            )
            own_context = True
        else:
            context = live
        scratch = context.new_page()
        # Page-level, so a scratch tab in the live context leaves the run's
        # own pages alone.
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
        load_blocked = list(blocked)
        result = scratch.evaluate(
            _EVALUATE_JS,
            {
                "actionPath": action_path,
                "leaf": action_path.rsplit("/", 1)[-1],
                "fields": [[k, v] for k, v in fields],
                "settleMs": settle_ms,
            },
        )
        serialized = [(str(k), str(v)) for k, v in result.get("serialized") or []]
        late = [b for b in blocked if b not in load_blocked]
        if late:
            return done("unevaluable", _tried(late), serialized)
        if result.get("unevaluable"):
            return done("unevaluable", result["unevaluable"])
        if errors:
            return done(
                "unevaluable", f"the form's script raised an error ({errors[0]})"
            )
        if result.get("threw"):
            return done(
                "unevaluable", f"the form's script raised an error ({result['threw']})"
            )
        if result.get("opened"):
            return done("unevaluable", "the form's script opened a window while it ran")
        if dialogs:
            return done(
                "invalid",
                f"the form's script showed a dialog ({dialogs[0]})",
                serialized,
            )
        if result.get("invalid"):
            return done(
                "invalid",
                "the browser's own checks refuse these values ("
                + "; ".join(result["invalid"][:3])
                + ")",
                serialized,
            )
        if result.get("rules"):
            names = ", ".join(sorted(result["rules"])[:5])
            return done(
                "invalid",
                f"the form's validation rules refuse these values ({names})",
                serialized,
            )
        if not result.get("reached"):
            return done(
                "unevaluable",
                "the form's script stopped the submit before it could be seen",
                serialized,
            )
        if result.get("prevented") and not result.get("programmaticSubmits"):
            return done(
                "invalid",
                "the form's script refused the submit for these values",
                serialized,
            )
        return done("valid", "", serialized)
    except Exception as exc:  # noqa: BLE001 - a failure to run is unevaluable
        late = [b for b in blocked if b.startswith("after load: ")]
        if late:
            # For example the page let the browser's own submit through
            # without our listener seeing it: the route blocked it.
            return done("unevaluable", _tried(late))
        return done("unevaluable", f"the scratch page failed ({type(exc).__name__})")
    finally:
        try:
            if scratch is not None:
                scratch.close()
            if own_context and context is not None:
                context.close()
        except Exception:  # noqa: BLE001 - cleanup only
            pass
