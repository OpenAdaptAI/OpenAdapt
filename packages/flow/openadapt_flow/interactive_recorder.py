"""Interactive recorder: capture a demonstration the USER drives live.

``openadapt-flow record --url <app>`` opens a real (headed) Playwright browser
pointed at the user's OWN app. With ``--browser-cdp-endpoint`` it instead
attaches to one explicitly bound tab in an already-running local Chromium
browser, which preserves an existing SSO or 2FA session. Both modes listen to
the user's real clicks, typing, key presses and scrolls via in-page
capture-phase DOM listeners and write the EXACT recording format the compiler
already consumes (``meta.json`` + ``events.jsonl`` +
``frames/{i:04d}_before.png`` / ``_after.png``).

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

Secret literals never touch Python: a field is secret when it is ``input[type=
password]`` or its name/id is passed via ``--secret``. The page closure binds
the ELEMENT, masks it in every retained frame, and emits no value for it.

**Flow reports captured page text exactly, or it withholds that text and says
why. Flow never rewrites captured text.** Three earlier revisions of this file
tried instead to remove a remembered value from text that was already captured,
and three independent reviews each found a different defect in the retention
rule that approach needs. That is the expected outcome: Englehardt, Acar and
Narayanan measured every major session-replay vendor in 2017 and found that
none redacts displayed content automatically and that all of it leaked, and
PostHog and Sentry still carry open issues for secrets in replay URLs. The
working answer in production tools is capture-time, element-bound,
deny-by-default masking, which is what this module now implements.

Three rules follow from it:

* Matching uses ONLY the values that bound elements hold at match time, read
  live from the DOM. No value is kept after the field stops holding it, and a
  node the page detached is not a source of values.
* Identity evidence (selector, role, accessible name, clicked-row identity,
  and the receiving field's name) is EXACT or WITHHELD with a stated reason.
  Replay compares it against the live page, so a rewritten copy would compare
  against text the page never held, invisibly.
* Reflected evidence (the page URL and title) is sampled from Python at the
  settled boundary, never in the capture-phase listener, which runs before the
  page's own handlers and therefore reads the previous action's text. Flow
  reports it only while it has not changed since before the document held any
  declared value; otherwise it withholds an origin-only URL and an empty title.

Every withheld item is visible: an ``identity_withheld`` reason on the action,
``meta.json`` keys, and a CLI line. A DOM identity check never disarms
silently.

See ``ir.Step.secret`` and ``docs`` for the full contract and its boundary.
"""

from __future__ import annotations

import ctypes
import errno
import io
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from PIL import Image

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


class BrowserAttachError(RuntimeError):
    """A safe browser-attachment precondition was not met."""


_PARTIAL_RECORDING_PREFIX = ".openadapt-recording-partial-"

# One protocol object group scopes every remote object that the closed-shadow
# privacy scan resolves, so each scan can release its handles in one call.
_PRIVACY_SCAN_OBJECT_GROUP = "openadapt-flow-privacy-scan"


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a directory without replacing any destination."""

    if os.name == "nt":
        os.rename(source, destination)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    result: int
    if sys.platform == "linux":
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace directory promotion is unavailable",
                destination,
            ) from exc
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = int(
            renameat2(
                -100,  # AT_FDCWD
                source_bytes,
                -100,  # AT_FDCWD
                destination_bytes,
                1,  # RENAME_NOREPLACE
            )
        )
    elif sys.platform == "darwin":
        try:
            renamex_np = libc.renamex_np
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic no-replace directory promotion is unavailable",
                destination,
            ) from exc
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = int(
            renamex_np(
                source_bytes,
                destination_bytes,
                0x00000004,  # RENAME_EXCL
            )
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace directory promotion is unavailable",
            destination,
        )

    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _secret_screenshot_selectors(
    secret_fields: set[str],
    *,
    marker_attribute: Optional[str] = None,
) -> tuple[str, ...]:
    """Return selectors that mask password and declared-secret fields."""

    selectors = ["input[type='password']"]
    for field in sorted(secret_fields):
        encoded = _css_string_literal(field)
        selectors.append(f"[name={encoded}], [id={encoded}]")
    if marker_attribute is not None:
        if not re.fullmatch(r"data-oaflow-secret-[0-9a-f]{32}", marker_attribute):
            raise BrowserAttachError("the browser secret marker is invalid")
        selectors.append(f"[{marker_attribute}]")
    return tuple(selectors)


def _css_string_literal(value: str) -> str:
    """Serialize an exact value as a valid double-quoted CSS string.

    JSON ``\\u`` escapes are not CSS Unicode escapes. Using ``json.dumps``
    therefore made a declared field such as ``päss`` select a different name
    and left its later frames unmasked. CSS strings accept Unicode directly;
    quotes, backslashes, and control characters need CSS-specific escapes.
    """

    escaped: list[str] = ['"']
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"}:
            escaped.append("\\" + character)
        elif codepoint == 0:
            raise BrowserAttachError(
                "a declared secret field name contains a null character and "
                "cannot be bound to a safe browser mask"
            )
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise BrowserAttachError(
                "a declared secret field name contains an invalid Unicode "
                "surrogate and cannot be bound to a safe browser mask"
            )
        elif codepoint < 0x20 or codepoint == 0x7F:
            # The trailing space terminates the variable-width CSS hex escape.
            escaped.append(f"\\{codepoint:x} ")
        else:
            escaped.append(character)
    escaped.append('"')
    return "".join(escaped)


def _http_origin(url: str, *, label: str) -> tuple[str, str, int]:
    """Return a normalized HTTP origin or refuse an unsafe attach target."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BrowserAttachError(f"{label} is not a valid URL") from exc
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise BrowserAttachError(f"{label} must be an http:// or https:// URL")
    return (scheme, host, port or (443 if scheme == "https" else 80))


def _origin_label(origin: tuple[str, str, int]) -> str:
    scheme, host, port = origin
    default_port = 443 if scheme == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{rendered_host}{suffix}"


def _safe_page_label(url: str) -> str:
    """Describe a tab without exposing URL credentials, query, or fragment."""

    try:
        parsed = urlsplit(url)
        origin = _http_origin(url, label="browser tab URL")
    except BrowserAttachError:
        return "<non-web page>"
    path = parsed.path or "/"
    if len(path) > 120:
        path = path[:117] + "..."
    return _origin_label(origin) + path


def validate_browser_cdp_endpoint(endpoint: str) -> str:
    """Require an explicit loopback CDP endpoint.

    Browser attachment is local-only. A remote CDP endpoint is effectively a
    remote-control credential and can also expose every page in that browser.
    The supported recorder does not accept that boundary implicitly.
    """

    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise BrowserAttachError("the browser CDP endpoint is not a valid URL") from exc
    if parsed.scheme.lower() not in {"http", "https", "ws", "wss"}:
        raise BrowserAttachError(
            "the browser CDP endpoint must use http, https, ws, or wss"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BrowserAttachError(
            "the browser CDP endpoint must not contain credentials, a query, "
            "or a fragment"
        )
    host = (parsed.hostname or "").lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise BrowserAttachError(
            "the browser CDP endpoint must use localhost or a loopback IP address"
        )
    if port is None:
        raise BrowserAttachError("the browser CDP endpoint must include a port")
    return endpoint


def select_attached_page(
    browser: Any,
    *,
    app_url: str,
    page_url: Optional[str] = None,
) -> Any:
    """Bind one existing same-origin web tab without guessing.

    A sole tab on the declared application origin is unambiguous. If two or
    more tabs use that origin, the operator must give the exact current URL.
    Query and fragment values are never included in an error message.
    """

    app_origin = _http_origin(app_url, label="the declared app URL")
    if (
        page_url is not None
        and _http_origin(page_url, label="the selected browser page URL") != app_origin
    ):
        raise BrowserAttachError(
            "the selected browser page URL must have the same origin as "
            f"the declared app URL ({_origin_label(app_origin)})"
        )

    matches: list[tuple[Any, str]] = []
    for context in browser.contexts:
        for page in context.pages:
            try:
                current_url = str(page.url)
                current_origin = _http_origin(current_url, label="browser tab URL")
            except Exception:
                continue
            if current_origin == app_origin:
                matches.append((page, current_url))

    if page_url is not None:
        exact = [page for page, current_url in matches if current_url == page_url]
        if len(exact) == 1:
            return exact[0]
        if not exact:
            raise BrowserAttachError(
                "no open tab has the exact --browser-page-url on the declared "
                f"app origin ({_origin_label(app_origin)})"
            )
        raise BrowserAttachError(
            "more than one open tab has the exact --browser-page-url; close "
            "the duplicate tabs and retry"
        )

    if len(matches) == 1:
        return matches[0][0]
    if not matches:
        raise BrowserAttachError(
            "no open tab matches the declared app origin "
            f"({_origin_label(app_origin)}); open the app in the attached "
            "browser and retry"
        )
    labels = sorted({_safe_page_label(current_url) for _, current_url in matches})
    rendered = ", ".join(labels[:5])
    if len(labels) > 5:
        rendered += f", and {len(labels) - 5} more"
    raise BrowserAttachError(
        f"{len(matches)} open tabs match the declared app origin; supply the "
        "exact current URL with --browser-page-url. Candidate paths "
        f"(query and fragment hidden): {rendered}"
    )


# In-page recorder script. Installed via add_init_script so it re-arms on every
# document (navigations). Emits raw events to the Python side via the
# __oaflow_emit binding. __SECRET_NAMES__ / __SPECIAL_KEYS__ are substituted in.
# How long a recorded click stays pending before it is written, so the second
# click of a double-click gesture can supersede it. Chromium's double-click
# interval is 500 ms.
_DOUBLE_CLICK_WINDOW_MS = 500

_INIT_JS = r"""
(() => {
  const SESSION_ID = __SESSION_ID__;
  const BINDING_NAME = __BINDING_NAME__;
  const GLOBAL_KEY = '__oaflowRecorder';
  const CLEANUP_KEY = '__oaflowCleanup_' + SESSION_ID;
  const previous = window[GLOBAL_KEY];
  if (previous && previous.sessionId === SESSION_ID) return;
  if (previous && typeof previous.cleanup === 'function') {
    try { previous.cleanup(); } catch (e) {}
  }
  const SECRET_NAMES = __SECRET_NAMES__;
  const SECRET_MARKER = __SECRET_MARKER__;
  const IDENT_NAMES = __IDENT_NAMES__;
  const SPECIAL = __SPECIAL_KEYS__;
  // One identity for this DOCUMENT. The init script builds a fresh closure per
  // document, so this closure never saw a value an earlier document received.
  // Python compares this id against the document that received a secret and
  // withholds every later document's reflected text.
  const DOC_ID = SESSION_ID + ':doc:' + String(Date.now()) + ':'
    + Math.random().toString(36).slice(2);
  // A secret value shorter than this occurs inside ordinary page text by
  // chance, so a match cannot be read as a reflection of the secret. Flow
  // withholds the text either way, and reports the ambiguous case under its
  // own reason so the operator can tell a chance match from a real one.
  const MIN_UNAMBIGUOUS_SECRET = 6;
  //
  // ONE RULE GOVERNS EVERY TEXT THIS CLOSURE PRODUCES: Flow reports captured
  // text EXACTLY, or it withholds that text and states why. Flow never
  // rewrites captured text.
  //
  // Post-hoc removal of a secret value from already-captured text is the
  // approach the session-replay industry has never made work. Englehardt,
  // Acar and Narayanan measured every major replay vendor in 2017 and found
  // that no vendor redacts displayed content automatically and that all of it
  // leaked; PostHog and Sentry still carry open issues for secrets in replay
  // URLs today. The industry answer is capture-time, element-bound,
  // deny-by-default masking, which is what this closure does.
  //
  // A rewrite is also worse than a refusal for identity evidence. Replay
  // compares identity text against what the page shows, so a rewritten copy
  // is a comparison against text the page never held, and the substitution is
  // invisible to that comparison. Withholding disarms the check loudly.
  //
  const listeners = [];
  const secretStates = new WeakMap();
  const closedSecretHosts = new WeakMap();
  const secretBoundaryStates = new WeakMap();
  const inputSessions = new WeakMap();
  const trustedSecretFieldLabels = new WeakMap();
  const stickySecretElements = new Set();
  const observedSecretRoots = new WeakSet();
  const ambiguousSecretReplacements = new WeakSet();
  // Elements that discovery bound during the MutationObserver batch being
  // processed right now. A replacement discovery has just bound still needs to
  // inherit the state of the node it replaced, including its input session.
  let batchDiscoveredSecrets = null;
  let nextInputSession = 0;
  let activeSecretElement = null;
  let activeSecretState = null;
  let resizeTimer = null;
  let secretObserver = null;
  let privacyBoundaryError = null;
  let opaqueSecretActive = false;
  // Why Flow refused to build identity evidence from free identity text, for
  // the identity scope that is being built right now. See identityTextOrNull.
  let identityWithheldReason = null;
  // Reflected text (the URL and the title) that this document showed while no
  // declared secret field held a value. Text that has not changed since then
  // CANNOT be a reflection of a value that did not yet exist, so Flow reports
  // it exactly. Text that HAS changed may be a reflection of any version of
  // the value, including one the field no longer holds, and no rule that
  // reads only the current DOM can decide which. Flow withholds it.
  //
  // These two strings are page text, not secret values. Nothing ever matches
  // against them: they answer one question, "did this text exist before the
  // secret did", and they are never emitted.
  let preSecretUrl = null;
  let preSecretTitle = null;
  // Sticky: a document that has held a declared value stops refreshing the
  // baseline above. It never un-sticks, because a value the field no longer
  // holds can still be reflected somewhere in this document.
  let documentHeldSecretValue = false;
  // Whether the baseline above proves anything. It does not for a document
  // that was BORN after some earlier document already received a declared
  // value: the URL such a document loads with can already carry that value, so
  // its own first sample is not evidence of a time before the value existed.
  // Python knows the recording-wide history and reports it on the first read.
  let seedTrusted = true;
  let seedDecided = false;
  // Values a declared field held at a COMMIT POINT -- `change`, `focusout`,
  // `submit`, `pagehide`. Used for ONE purpose: deciding whether to WITHHOLD
  // identity text after the page removes the field. Never for the URL, never
  // for the title, and never to rewrite anything.
  //
  // Why this is safe where the rule that failed three reviews was not: that
  // rule REWROTE text, so a false match corrupted evidence or leaked. Nothing
  // here rewrites, so a false match can only withhold. Withholding costs
  // evidence; it cannot corrupt and it cannot leak. Identity text is also a
  // DIRECT match against the value, while a URL or a title reflects it
  // indirectly and can lag, which is why reflected text still uses live values
  // only. A commit point is a moment the operator stopped on, not a keystroke,
  // and only a CONNECTED element commits, so a controlled input that fires
  // focusout on the node it just replaced commits nothing.
  const committedSecretValues = new Set();
  // The most recent NON-EMPTY value each bound element held, observed in the
  // capture phase before the page's own `input` handler runs. ONE value per
  // element, REPLACED on every keystroke -- never a ladder of past values, and
  // never used to rewrite anything.
  //
  // It exists for the page that consumes its own field: a scanner input that
  // writes the badge into the URL and then clears the field in the same
  // handler, or a wizard that removes the field outright. By the time Python
  // samples at the settled boundary, nothing in the DOM holds the value, so
  // every other source is empty and the reflected text would be reported.
  //
  // It is REPLACED, never accumulated, so this is not a ladder of past values
  // and a password beginning with an ordinary word never leaves that word
  // behind. It is literally the value carried by the last `input` event on
  // that element -- NOT "the value the operator stopped on". A page that
  // consumes the value mid-stream leaves whatever was typed next, which is
  // exactly why a second scan into the same field must not displace the first
  // value while the first is still on show.
  const lastSecretValues = new WeakMap();
  let eventsStopped = false;
  let cleaned = false;

  function listenOn(target, type, handler) {
    target.addEventListener(type, handler, true);
    listeners.push([target, type, handler]);
  }

  function listen(type, handler) {
    listenOn(document, type, handler);
  }

  function stopEvents() {
    if (eventsStopped) return;
    eventsStopped = true;
    if (resizeTimer !== null) clearTimeout(resizeTimer);
    for (const [target, type, handler] of listeners) {
      try { target.removeEventListener(type, handler, true); } catch (e) {}
    }
  }

  function cleanup() {
    if (cleaned) return;
    cleaned = true;
    stopEvents();
    if (secretObserver !== null) secretObserver.disconnect();
    // The Set retains disconnected elements for this exact cleanup. Remove
    // the temporary marker before releasing those references so reinserting a
    // page-owned node after Flow detaches cannot expose recorder metadata.
    for (const el of stickySecretElements) {
      try { el.removeAttribute(SECRET_MARKER); } catch (e) {}
    }
    // No value is filed against these elements, so clearing the Set releases
    // everything this closure held about them.
    stickySecretElements.clear();
    activeSecretElement = null;
    activeSecretState = null;
    const current = window[GLOBAL_KEY];
    if (current && current.sessionId === SESSION_ID) {
      try { delete window[GLOBAL_KEY]; } catch (e) { window[GLOBAL_KEY] = null; }
    }
    try { delete window[CLEANUP_KEY]; } catch (e) { window[CLEANUP_KEY] = null; }
  }
  window[GLOBAL_KEY] = {
    sessionId: SESSION_ID,
    stopEvents,
    cleanup,
    privacyStatus: () => ({ok: privacyBoundaryError === null,
                           error: privacyBoundaryError}),
    registerExistingClosedShadowHost,
    structuralState: (secretSeenEarlier) => safePageState(secretSeenEarlier),
  };
  window[CLEANUP_KEY] = {sessionId: SESSION_ID, stopEvents, cleanup};

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

  function currentSecretValue(el) {
    try {
      if (el && el.value != null) return String(el.value);
      if (el && (el.isContentEditable
          || (el.getAttribute && el.getAttribute('role') === 'textbox'))) {
        return String(el.innerText || el.textContent || '');
      }
    } catch (e) {}
    return '';
  }

  function isConnectedElement(el) {
    try { return !!el.isConnected; } catch (e) { return false; }
  }

  function liveSecretValues() {
    // Every declared value a bound element holds RIGHT NOW, read from the DOM
    // at match time. Nothing here survives the call.
    //
    // Only a CONNECTED element counts. A controlled input that swaps its node
    // on every keystroke leaves detached nodes behind, each still holding the
    // keystroke prefix it had when the page dropped it. Those prefixes are not
    // values the operator entered, and three earlier revisions of this file
    // each shipped a different defect while trying to decide which of them to
    // keep. This closure keeps none of them: the page tells Flow what the
    // field holds, and Flow asks the page every time.
    const values = new Set();
    for (const el of stickySecretElements) {
      if (!isConnectedElement(el)) continue;
      const value = currentSecretValue(el);
      if (value) values.add(value);
    }
    return values;
  }

  function declaredSecretParameterNames() {
    // Names Flow KNOWS name a secret: the operator's --secret declarations,
    // and the name or id of every field Flow bound, which includes every
    // auto-detected input[type=password]. This list is structure, not value:
    // it is the same whatever the operator typed, so a rule keyed on it is
    // deterministic. Sentry and Datadog redact URLs the same way, by parameter
    // NAME, because searching for a value inside arbitrary text does not work.
    const names = new Set();
    for (const name of SECRET_NAMES) if (name) names.add(String(name));
    for (const el of stickySecretElements) {
      const state = secretStates.get(el) || closedSecretHosts.get(el) || null;
      if (state && state.field) names.add(String(state.field));
      const attribute = elementName(el);
      if (attribute) names.add(String(attribute));
      try { if (el.id) names.add(String(el.id)); } catch (e) {}
    }
    return names;
  }

  function inboundSecretParameterValues() {
    // A same-origin GET submit carries the field's value under the field's own
    // NAME, because that is how an HTML form works. A document that loads such
    // a URL therefore holds a declared value it never saw typed, and its
    // closure has no bound element to read it from. Recover it from the URL by
    // NAME and use it to WITHHOLD identity text in this document.
    //
    // Only values long enough to identify. A short one matches ordinary page
    // text by chance, and this is a net, not a proof: withholding every
    // identity that contains a 3-character query value would cost far more
    // evidence than it protects.
    const values = new Set();
    const parsed = parseHttpUrl(location.href);
    if (parsed === null) return values;
    const declared = declaredSecretParameterNames();
    for (const search of [parsed.search, fragmentQuery(parsed.hash)]) {
      if (!search) continue;
      let params = null;
      try { params = new URLSearchParams(search); } catch (e) { params = null; }
      if (params === null) continue;
      for (const [name, value] of params) {
        if (!declared.has(name)) continue;
        if (value && value.length >= MIN_UNAMBIGUOUS_SECRET) values.add(value);
      }
    }
    return values;
  }

  function identityMatchValues() {
    // Every value Flow may use to WITHHOLD text. Never to rewrite it.
    //
    // The cached value is added PER ELEMENT, whenever THAT element holds
    // nothing right now. An earlier revision instead skipped the whole cache
    // as soon as ANY declared field held anything, so a second scan into the
    // same cleared field, or a second declared field holding a PIN, re-exposed
    // the first value that was still sitting in the URL. The document-wide
    // guard was the defect; the per-element test is the same idea applied
    // where it belongs.
    //
    // CONNECTED and empty: always add. That is the page that consumed its own
    // field -- a scanner writing the badge into the URL and clearing the input
    // -- and the element is still there to speak for.
    //
    // DETACHED: add only when NO connected bound element holds anything. A
    // page that REMOVED its field leaves the value nowhere else, so it must be
    // added. A controlled input that SWAPS its node leaves a trail of detached
    // elements that still report `c`, `ch`, `cha`, and adding those would
    // withhold any text containing a one-letter match -- but in that case a
    // connected successor does hold the value, so this test excludes them.
    const values = liveSecretValues();
    const anythingLive = values.size > 0;
    for (const el of stickySecretElements) {
      const connected = isConnectedElement(el);
      if (connected && currentSecretValue(el)) continue;
      if (!connected && anythingLive) continue;
      const last = lastSecretValues.get(el) || (connected ? '' : currentSecretValue(el));
      if (last) values.add(last);
    }
    for (const value of committedSecretValues) values.add(value);
    for (const value of inboundSecretParameterValues()) values.add(value);
    return values;
  }

  function commitConnectedValue(el) {
    if (!el || !stickySecretElements.has(el) || !isConnectedElement(el)) return;
    const value = currentSecretValue(el);
    if (value) committedSecretValues.add(value);
  }

  function commitSecretValueFor(node) {
    // A commit point that names its element: `change` and `focusout`.
    //
    // DECIDE AT THE MICROTASK CHECKPOINT, not now. A controlled input that
    // replaces its focused node on every keystroke dispatches focusout DURING
    // the replacement, while the node can still report itself as connected.
    // One checkpoint later the page has certainly dropped it, and the value it
    // holds is a keystroke prefix -- not a value the operator stopped on.
    // Committing prefixes would withhold identity evidence on every chance
    // match, which is the failure this rule exists to avoid.
    //
    // A real blur is unaffected: the element the operator left is still in the
    // document at the checkpoint, and a page handler that removes it runs in a
    // LATER task.
    const el = secretTextEntryForNode(node) || node;
    if (!el || !stickySecretElements.has(el)) return;
    try {
      Promise.resolve().then(() => commitConnectedValue(el));
    } catch (e) {
      commitConnectedValue(el);
    }
  }

  function commitSecretValues() {
    // `submit` and `pagehide`. The document is leaving, so there is no later
    // checkpoint to defer to.
    for (const el of stickySecretElements) commitConnectedValue(el);
  }

  function secretVariants(secret) {
    const variants = new Set([secret]);
    try { variants.add(encodeURIComponent(secret)); } catch (e) {}
    try {
      variants.add(new URLSearchParams([['value', secret]]).toString().slice(6));
    } catch (e) {}
    return Array.from(variants).filter(Boolean);
  }

  function secretValueIn(value, secretValues) {
    // DETECT, never rewrite. Returns the reason this text cannot be reported,
    // or null when the text holds no declared value and is safe to report
    // exactly as the page shows it.
    const text = String(value == null ? '' : value);
    if (!text) return null;
    // Compare case-insensitively. An application that upper-cases or
    // lower-cases an identifier before showing it is doing normalisation, not
    // an application-defined transform, and it is common enough that an
    // exact-case match would miss it. Widening a WITHHOLD test can only
    // withhold more; it can never leak and never rewrites anything.
    const folded = text.toLowerCase();
    let ambiguousMatch = false;
    for (const secret of secretValues) {
      if (!secret) continue;
      const ambiguous = secret.length < MIN_UNAMBIGUOUS_SECRET;
      for (const variant of secretVariants(secret)) {
        if (!variant || folded.indexOf(variant.toLowerCase()) < 0) continue;
        // A definite match outranks an ambiguous one: the operator reads a
        // different meaning into "this text held your value" than into "this
        // text could have held your value by chance". A value shorter than
        // MIN_UNAMBIGUOUS_SECRET occurs inside ordinary page text often
        // enough that its match proves nothing. Either one withholds.
        if (!ambiguous) return 'secret-value-in-identity';
        ambiguousMatch = true;
      }
    }
    return ambiguousMatch ? 'ambiguous-secret-in-identity' : null;
  }

  function identityTextOrNull(value) {
    // IDENTITY / MACHINE EVIDENCE: the accessible name, the control role, the
    // clicked row's identity characters, and the receiving field's name.
    // Replay re-reads the page and compares it against this text, so the text
    // must be what the page held or nothing at all. A rewritten copy would
    // make replay compare against characters the page never showed, and
    // nothing downstream could see that the substitution happened.
    //
    // EXACT, or WITHHELD with a reason. Never rewritten.
    if (value == null) return value;
    const text = String(value);
    if (!text) return text;
    if (opaqueSecretActive) {
      // A closed shadow root does not expose its literal, so Flow cannot rule
      // out that this text contains it.
      if (identityWithheldReason === null) {
        identityWithheldReason = 'opaque-secret-boundary';
      }
      return null;
    }
    const reason = secretValueIn(text, identityMatchValues());
    if (reason === null) return text;
    if (identityWithheldReason === null) identityWithheldReason = reason;
    return null;
  }

  function labelTextOrNull(value) {
    // The receiving field's human label. Passive compile-time evidence, not an
    // identity tier, so a withheld label does not disarm an identity check and
    // is not counted as one. It still follows the one rule: exact or nothing.
    if (value == null) return value;
    const text = String(value);
    if (!text) return text;
    if (opaqueSecretActive) return null;
    return secretValueIn(text, identityMatchValues()) === null ? text : null;
  }

  function originOnlyUrl() {
    // The declared origin with an empty path: a URL that carries no value.
    try { return location.origin + '/'; } catch (e) { return ''; }
  }

  function parseHttpUrl(href) {
    try {
      const parsed = new URL(String(href));
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        return parsed;
      }
    } catch (e) {}
    return null;
  }

  function fragmentQuery(hash) {
    // A fragment is either a bare anchor (`#section`) or a second query
    // (`#a=1&b=2`). Treat it as a query only when it names things.
    const text = String(hash || '').replace(/^#/, '');
    return text.indexOf('=') >= 0 ? text : '';
  }

  function reduceParams(search, context, where, dropped) {
    // Return the parameter list with the VALUE of each parameter Flow cannot
    // report emptied. NAMES always survive: a name is app structure, and the
    // operator needs it to read the evidence. Nothing is invented -- a dropped
    // value becomes empty. Flow removes characters; it never adds characters
    // the page did not show.
    let params = null;
    try { params = new URLSearchParams(search); } catch (e) { params = null; }
    if (params === null) return null;
    const parts = [];
    for (const [name, value] of params) {
      let reason = null;
      if (context.declared.has(name)) {
        // By NAME, always, whatever the value is. A same-origin GET submit
        // carries a declared field under its own name, so this is the exact
        // channel that leaked, closed by structure rather than by matching.
        reason = 'declared-secret-parameter';
      } else if (context.requireProof && value) {
        const baseline = context.baseline;
        const proven = baseline !== null && baseline.get(name) === value;
        if (!proven) reason = 'unproven-parameter-value';
      }
      if (reason === null) {
        parts.push(encodeURIComponent(name) + '=' + encodeURIComponent(value));
        continue;
      }
      parts.push(encodeURIComponent(name) + '=');
      if (value) dropped.push({name: name, where: where, reason: reason});
    }
    return parts.join('&');
  }

  function baselineParams(search) {
    try { return new URLSearchParams(search); } catch (e) { return null; }
  }

  function pathHoldsDeclaredValue(pathname, values) {
    // The NET for the PATH, run in BOTH directions.
    //
    // Direction 1, a segment holds a whole value, is step 5 and the whole-URL
    // check already covers it. Direction 2 is the one that matters here: a
    // page that writes the field into its path as the operator types leaves a
    // segment the value CONTAINS but no longer equals. Matching only the live
    // value would miss it, and Flow will not keep a previous value to catch
    // it, so it asks the opposite question instead -- does a value Flow can
    // see contain this segment? That reads the CURRENT value against the
    // CURRENT text. It is not a history and not a prefix ladder, and it only
    // ever withholds.
    //
    // A segment too short to identify is ignored: `/app/edit` inside a long
    // passphrase would otherwise withhold the URL of an ordinary page.
    const segments = String(pathname || '').split('/').filter(Boolean);
    for (const segment of segments) {
      if (segment.length < MIN_UNAMBIGUOUS_SECRET) continue;
      let decoded = segment;
      try { decoded = decodeURIComponent(segment); } catch (e) {}
      const folded = segment.toLowerCase();
      const foldedDecoded = decoded.toLowerCase();
      for (const value of values) {
        if (!value) continue;
        const foldedValue = value.toLowerCase();
        if (
          foldedValue.indexOf(folded) >= 0
          || foldedValue.indexOf(foldedDecoded) >= 0
        ) {
          return true;
        }
      }
    }
    return false;
  }

  function reportedUrl(rawUrl, dropped) {
    // A URL is STRUCTURE, not one opaque string. Report the origin and the
    // path, which is what a single-page application changes when it routes,
    // and which is app-controlled structure rather than operator input.
    // Reduce only the parameter values Flow cannot stand behind.
    //
    // Returns null when Flow cannot parse the URL at all; the caller withholds.
    const parsed = parseHttpUrl(rawUrl);
    if (parsed === null) return null;
    // Once this document has held a declared value, a parameter value Flow
    // cannot prove predates that value is dropped. A document whose seed
    // baseline Flow does not trust proves nothing at all.
    const requireProof = documentHeldSecretValue || !seedTrusted;
    const baselineUrl = seedTrusted ? parseHttpUrl(preSecretUrl) : null;
    const context = {
      declared: declaredSecretParameterNames(),
      requireProof: requireProof,
      baseline: null,
    };
    context.baseline = baselineUrl === null
      ? null : baselineParams(baselineUrl.search);
    const query = reduceParams(parsed.search, context, 'query', dropped);
    const rawFragment = fragmentQuery(parsed.hash);
    context.baseline = baselineUrl === null
      ? null : baselineParams(fragmentQuery(baselineUrl.hash));
    const fragment = rawFragment
      ? reduceParams(rawFragment, context, 'fragment', dropped) : '';
    if (query === null || fragment === null) return null;
    // A BARE fragment names nothing (`#section`), so structure cannot reduce
    // it. Treat the whole fragment as one unnamed value and apply the same
    // proof a named parameter value gets.
    let bareFragment = rawFragment ? '' : String(parsed.hash || '');
    if (bareFragment && requireProof) {
      const baselineHash = baselineUrl === null
        ? '' : String(baselineUrl.hash || '');
      if (baselineHash !== bareFragment) {
        dropped.push({
          name: '', where: 'fragment', reason: 'unproven-parameter-value',
        });
        bareFragment = '';
      }
    }
    // Rebuilding normalises percent-encoding. Return exactly what the page
    // reports when Flow dropped nothing, so evidence does not drift. User
    // information in the authority is never returned: the rebuilt form drops
    // it with the rest of the authority.
    if (!dropped.length && !parsed.username && !parsed.password) {
      return String(rawUrl);
    }
    const hash = rawFragment ? '#' + fragment : bareFragment;
    return parsed.origin + parsed.pathname
      + (parsed.search ? '?' + query : '') + hash;
  }

  function safePageState(secretSeenEarlier) {
    // REFLECTED / CONTEXT EVIDENCE: the page URL and the document title.
    //
    // STRUCTURE FIRST, DETECTION AS A NET.
    //
    // The URL is parsed, not treated as one opaque string. The origin and the
    // path are reported: a path change is the single-page-application case,
    // and the path is app structure, not operator input. Parameter NAMES are
    // always reported. A parameter VALUE is dropped when its NAME is a
    // declared secret field name -- deterministically, with no reference to
    // the value -- or when Flow cannot prove the value predates the moment
    // this document first held a declared value. Sentry and Datadog redact
    // URLs by parameter name for the same reason: searching for a value inside
    // arbitrary text does not work, and OWASP notes that a secret in a URL is
    // already exposed through history, logs, proxies and Referer headers.
    //
    // The net: if the URL Flow is about to report still contains a value a
    // bound field holds right now, Flow withholds the whole URL and tells the
    // operator that the application put a declared secret into it. That
    // matching is sound here and was not sound before, because this function
    // now runs only from Python at the settled boundary, where the page has
    // processed the action and its reflection matches the value the field
    // holds. It needs no history, and the direction is fail-safe: a match
    // withholds, so a false positive costs evidence and cannot leak.
    //
    // The title has no structure to exploit, so it keeps the plain rule:
    // report it while it has not changed since before this document held a
    // declared value, and withhold it otherwise, plus the same net.
    //
    // STATED LIMITS, both documented in docs/BROWSER_RECORDING.md:
    //  * An application that debounces its URL update beyond the settle window
    //    can still show an earlier value. The net will not match it. Flow does
    //    NOT keep a previous value to catch that.
    //  * Text that already carried the value BEFORE any declared field held it
    //    predates the value and is reported.
    if (!seedDecided) {
      seedDecided = true;
      if (secretSeenEarlier === true) seedTrusted = false;
    }
    const rawUrl = String(location.href);
    const rawTitle = String(document.title == null ? '' : document.title);
    const values = liveSecretValues();
    if (values.size > 0) documentHeldSecretValue = true;
    if (!documentHeldSecretValue && !opaqueSecretActive && seedTrusted) {
      preSecretUrl = rawUrl;
      preSecretTitle = rawTitle;
    }
    const state = {
      url: rawUrl,
      title: rawTitle,
      doc: DOC_ID,
      // Whether this document has EVER held a declared value. Python uses it
      // to bind the recording-wide secret boundary across documents.
      secret: documentHeldSecretValue || opaqueSecretActive,
      url_withheld: null,
      title_withheld: null,
      dropped: [],
      secret_in_url: false,
      secret_in_title: false,
    };
    if (opaqueSecretActive) {
      // A closed shadow root exposes its value to no check at all.
      state.url_withheld = 'opaque-secret-boundary';
      state.title_withheld = 'opaque-secret-boundary';
    } else {
      const dropped = [];
      const reduced = reportedUrl(rawUrl, dropped);
      // The net runs against every value Flow can see, not only the live one.
      // Adding a committed value can only ADD a match, and a match only
      // withholds, so this direction cannot leak and cannot corrupt. The
      // proof rule above still uses the live value alone.
      const netValues = identityMatchValues();
      const parsed = reduced === null ? null : parseHttpUrl(reduced);
      if (reduced === null) {
        state.url_withheld = 'url-cannot-be-parsed';
      } else if (
        parsed !== null
        && pathHoldsDeclaredValue(parsed.pathname, netValues)
      ) {
        state.url_withheld = 'declared-value-in-url';
        state.secret_in_url = true;
      } else if (secretValueIn(reduced, netValues) !== null) {
        // The application put a declared secret somewhere structure cannot
        // reach -- a path segment, or a parameter it did not name after the
        // field. Withhold the whole URL and say so: this is an application
        // defect the operator needs to know about.
        state.url_withheld = 'declared-value-in-url';
        state.secret_in_url = true;
      } else {
        state.url = reduced;
        state.dropped = dropped;
      }
      if (documentHeldSecretValue) {
        if (secretValueIn(rawTitle, identityMatchValues()) !== null) {
          state.title_withheld = 'declared-value-in-title';
          state.secret_in_title = true;
        } else if (rawTitle !== preSecretTitle) {
          state.title_withheld = 'title-changed-after-a-secret-value';
        }
      }
    }
    if (state.url_withheld !== null) {
      state.url = originOnlyUrl();
      state.dropped = [];
    }
    if (state.title_withheld !== null) state.title = '';
    return state;
  }

  function structuredIdentityEvidence(px, py, eventTarget) {
    identityWithheldReason = null;
    const sid = structuredIdentity(px, py, eventTarget);
    return {
      sid: sid,
      withheld: sid === null ? identityWithheldReason : null,
    };
  }

  function structuredIdentity(px, py, eventTarget = null) {
    // Mirrors PlaywrightBackend.structured_text_at: the REAL characters of the
    // clicked row (MRN/name/DOB), excluding the clicked target's own cell.
    try {
      refreshSecretBindings();
      const el = eventTarget || document.elementFromPoint(px, py);
      if (!el) return null;
      const row = el.closest('tr, [role="row"], li, [role="listitem"]');
      if (!row) return null;
      const declared = (
        row.getAttribute('data-openadapt-identity')
        || row.getAttribute('aria-label')
        || ''
      ).replace(/\s+/g, ' ').trim();
      if (declared) return identityTextOrNull(declared);
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
      const joined = identityTextOrNull(body.replace(/\s+/g, ' ').trim());
      return joined || null;
    } catch (e) { return null; }
  }

  function targetRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return identityTextOrNull(explicit);
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
    // Never inspect visible text from a secret-bound contenteditable or one of
    // its descendants. Its innerText is the secret value, not target identity.
    // That is a WITHHELD name, not an absent one: the element may well have an
    // aria-label, and a control field beside it returns its name normally. Say
    // so, or the DOM identity tier disarms silently, exactly as a bare null
    // selector once did.
    if (secretTextEntryForNode(el)) {
      if (identityWithheldReason === null) {
        identityWithheldReason = 'secret-field-name-not-read';
      }
      return null;
    }
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return identityTextOrNull(aria);
    const labelledBy = (el.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {
      const value = labelledBy.split(/\s+/).map((id) => {
        const node = document.getElementById(id);
        return node ? (node.textContent || '').trim() : '';
      }).filter(Boolean).join(' ');
      if (value) return identityTextOrNull(value);
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label && (label.textContent || '').trim()) {
        return identityTextOrNull(label.textContent.trim());
      }
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping !== el && (wrapping.textContent || '').trim()) {
      return identityTextOrNull(wrapping.textContent.trim());
    }
    for (const attr of ['alt', 'title', 'placeholder']) {
      const value = (el.getAttribute(attr) || '').trim();
      if (value) return identityTextOrNull(value);
    }
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    return text ? identityTextOrNull(text.slice(0, 200)) : null;
  }

  function identityRefusal(value) {
    // Why this identity attribute cannot become evidence, or null when it can.
    // A selector is machine evidence in the strictest sense: replay resolves it
    // against the live DOM, so a rewritten selector resolves to nothing, or to
    // the wrong element. Exact or withheld, never rewritten.
    if (opaqueSecretActive) return value ? 'opaque-secret-boundary' : null;
    // The SAME value set its siblings use. A selector is the strictest machine
    // evidence there is -- replay resolves it against the live DOM -- so it
    // must not be the one identity field built from a narrower set. Widening
    // this can only withhold more.
    return secretValueIn(value, identityMatchValues());
  }

  function uniqueSelector(el) {
    // Returns {selector, withheld}. `withheld` names WHY Flow refused to build
    // identity evidence from a secret-bearing attribute. The DOM identity tier
    // must never disarm silently, so the reason travels with the event instead
    // of leaving a bare null selector that looks healthy.
    let withheld = null;
    if (el.id) {
      const refusal = identityRefusal(el.id);
      if (refusal !== null) {
        withheld = refusal;
      } else {
        const selector = `#${CSS.escape(el.id)}`;
        if (document.querySelectorAll(selector).length === 1) {
          return {selector: selector, withheld: null};
        }
      }
    }
    for (const attr of ['data-testid', 'data-test', 'name']) {
      const value = el.getAttribute(attr);
      if (!value) continue;
      const refusal = identityRefusal(value);
      if (refusal !== null) {
        if (withheld === null) withheld = refusal;
        continue;
      }
      const selector = `${el.tagName.toLowerCase()}[${attr}="${CSS.escape(value)}"]`;
      if (document.querySelectorAll(selector).length === 1) {
        return {selector: selector, withheld: null};
      }
    }
    return {selector: null, withheld: withheld};
  }

  function structuralTarget(px, py, eventTarget = null) {
    try {
      refreshSecretBindings();
      const el = eventTarget || document.elementFromPoint(px, py);
      if (!el) return null;
      identityWithheldReason = null;
      const identity = uniqueSelector(el);
      const target = {
        selector: identity.selector,
        role: targetRole(el),
        name: targetName(el),
      };
      // A withheld accessible name or role disarms an identity check exactly
      // as a withheld selector does, so report either one.
      const withheld = identity.withheld || identityWithheldReason;
      if (withheld) target.identity_withheld = withheld;
      return (target.selector || target.role || target.name || withheld)
        ? target : null;
    } catch (e) { return null; }
  }

  function isTextEntry(el) {
    try {
      if (!el || !el.matches) return false;
      if (el.matches('textarea, [contenteditable=""], [contenteditable="true"],'
          + ' [role="textbox"]')) return true;
      if (!el.matches('input')) return false;
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      return [
        'button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio',
        'range', 'reset', 'submit',
      ].indexOf(type) < 0;
    } catch (e) { return false; }
  }

  function secretTextEntryForNode(node) {
    try {
      const el = isTextEntry(node) ? node : (
        node && node.closest && node.closest(
          'input, textarea, [contenteditable=""], [contenteditable="true"],' +
          ' [role="textbox"]'
        )
      );
      if (!el) return null;
      let state = secretStates.get(el) || declaredSecretState(el);
      if (!state) state = secretBoundaryStates.get(el.getRootNode()) || null;
      if (!state) return null;
      if (!secretStates.has(el)) bindSecretState(el, state, false);
      return el;
    } catch (e) { return null; }
  }

  function bindSecretState(el, state, activate = true) {
    if (!secretStates.has(el) && !currentSecretValue(el)) {
      // A static label observed before typing is field identity, not secret
      // text. Cache it before short password prefixes could over-redact it.
      const label = fieldLabel(el);
      if (label) trustedSecretFieldLabels.set(el, label);
    }
    secretStates.set(el, state);
    // The value stays inside this closure. The element reference is retained
    // here, so a pre-filled or still-typed value is read live at match time
    // and cannot enter URL, title, or structural metadata. Binding retains the
    // ELEMENT, never the value.
    stickySecretElements.add(el);
    if (activate) {
      activeSecretElement = el;
      activeSecretState = state;
    }
    try {
      el.setAttribute(SECRET_MARKER, '');
      return el.hasAttribute(SECRET_MARKER);
    } catch (e) { return false; }
  }

  function inputSessionFor(el) {
    let session = inputSessions.get(el) || null;
    if (!session) {
      nextInputSession += 1;
      session = SESSION_ID + ':input:' + String(nextInputSession);
      inputSessions.set(el, session);
    }
    return session;
  }

  function elementName(el) {
    if (!el) return '';
    try {
      return (el.getAttribute && el.getAttribute('name')) || el.name || '';
    } catch (e) { return ''; }
  }

  function declaredSecretState(el) {
    if (!el) return null;
    const n = elementName(el), i = el.id || '';
    if ((el.type || '').toLowerCase() !== 'password'
        && SECRET_NAMES.indexOf(n) < 0 && SECRET_NAMES.indexOf(i) < 0) {
      return null;
    }
    return {field: n || i || null, inputSession: inputSessionFor(el)};
  }

  function declaredSecretHostState(host) {
    if (!host) return null;
    const n = elementName(host);
    const i = host.id || '';
    if (SECRET_NAMES.indexOf(n) < 0 && SECRET_NAMES.indexOf(i) < 0) return null;
    return {field: n || i, inputSession: inputSessionFor(host)};
  }

  function registerExistingClosedShadowHost(host) {
    const state = declaredSecretHostState(host);
    if (!state) return false;
    // A closed root does not expose its literal. Remove all text metadata for
    // the rest of this recording because no check can see its value.
    opaqueSecretActive = true;
    closedSecretHosts.set(host, state);
    return bindSecretState(host, state, false);
  }

  function secretStateForInput(el) {
    let state = secretStates.get(el) || null;
    if (!state) state = closedSecretHosts.get(el) || null;
    if (!state) state = declaredSecretState(el);
    if (!state && el && el.getRootNode) {
      state = secretBoundaryStates.get(el.getRootNode()) || null;
    }
    if (!state && el && el.getRootNode) {
      const root = el.getRootNode();
      const hostState = root && root.host
        ? declaredSecretHostState(root.host) : null;
      if (hostState) {
        closedSecretHosts.set(root.host, hostState);
        bindSecretState(root.host, hostState, false);
        observeSecretRoot(root, hostState);
        state = hostState;
      }
    }
    if (!state && activeSecretState && activeSecretElement
        && !activeSecretElement.isConnected && isTextEntry(el)) {
      // Some controlled inputs replace their DOM element after each change.
      // Programmatic focus transfer is still the same input session.
      state = activeSecretState;
    }
    if (!state) return null;
    return {state, maskBound: bindSecretState(el, state)};
  }

  function textEntryCandidates(root) {
    const candidates = [];
    try {
      if (root && root.nodeType === 1 && isTextEntry(root)) candidates.push(root);
      if (root && root.querySelectorAll) {
        candidates.push(...root.querySelectorAll(
          'input, textarea, [contenteditable=""], [contenteditable="true"],' +
          ' [role="textbox"]'
        ));
      }
    } catch (e) {}
    return candidates;
  }

  function discoverDeclaredSecretHosts(root) {
    const candidates = [];
    try {
      if (root && root.nodeType === 1) candidates.push(root);
      if (root && root.querySelectorAll) {
        candidates.push(...root.querySelectorAll('[name], [id]'));
      }
    } catch (e) {}
    for (const host of candidates) {
      if (isTextEntry(host) || closedSecretHosts.has(host)) continue;
      const state = declaredSecretHostState(host);
      if (!state) continue;
      // A declared non-text element is a complete shadow-host boundary. Its
      // internal value can be opaque, so do not retain later text metadata.
      opaqueSecretActive = true;
      closedSecretHosts.set(host, state);
      bindSecretState(host, state, false);
    }
  }

  function discoverOpenShadowRoots(root) {
    if (!root || !root.querySelectorAll) return;
    let elements = [];
    try { elements = Array.from(root.querySelectorAll('*')); } catch (e) {}
    if (root.nodeType === 1) elements.unshift(root);
    for (const el of elements) {
      try {
        if (el.shadowRoot) observeSecretRoot(el.shadowRoot, null);
      } catch (e) {}
    }
  }

  function observeSecretRoot(root, boundaryState) {
    if (!root) return;
    if (boundaryState) secretBoundaryStates.set(root, boundaryState);
    if (!observedSecretRoots.has(root)) {
      secretObserver.observe(root, {
        attributes: true, attributeOldValue: true, childList: true, subtree: true,
      });
      observedSecretRoots.add(root);
    }
    discoverDeclaredSecrets(root);
    discoverOpenShadowRoots(root);
  }

  function discoverDeclaredSecrets(root) {
    discoverDeclaredSecretHosts(root);
    const candidates = textEntryCandidates(root);
    for (const el of candidates) {
      if (secretStates.has(el)) continue;
      const state = declaredSecretState(el)
        || secretBoundaryStates.get(el.getRootNode()) || null;
      if (!state) continue;
      bindSecretState(el, state, false);
      if (batchDiscoveredSecrets) batchDiscoveredSecrets.add(el);
    }
  }

  function stateFromPriorDeclaration(mutation) {
    const el = mutation.target;
    if (!isTextEntry(el) || typeof mutation.oldValue !== 'string') return null;
    const oldValue = mutation.oldValue;
    const oldDeclaredName = (mutation.attributeName === 'name'
      || mutation.attributeName === 'id')
      && SECRET_NAMES.indexOf(oldValue) >= 0;
    const oldPasswordType = mutation.attributeName === 'type'
      && oldValue.toLowerCase() === 'password';
    if (!oldDeclaredName && !oldPasswordType) return null;
    const currentField = elementName(el) || el.id || null;
    return {
      field: oldDeclaredName ? oldValue : currentField,
      inputSession: inputSessionFor(el),
    };
  }

  function processSecretMutations(mutations) {
    batchDiscoveredSecrets = new Set();
    try {
      processSecretMutationBatch(mutations);
    } finally {
      batchDiscoveredSecrets = null;
    }
  }

  function processSecretMutationBatch(mutations) {
    // Apply every attribute record first. A removed declared field can lose its
    // name after removal but before its replacement is appended in the same
    // task. The old-value record binds that removed node before rewrite
    // matching runs across the complete MutationObserver batch.
    for (const mutation of mutations) {
      if (mutation.type !== 'attributes') continue;
      if (!secretStates.has(mutation.target)) {
        const priorState = stateFromPriorDeclaration(mutation);
        if (priorState) bindSecretState(mutation.target, priorState, false);
      }
      discoverDeclaredSecrets(mutation.target);
    }
    const removedEntries = [];
    const addedEntries = [];
    for (const mutation of mutations) {
      if (mutation.type !== 'childList') continue;
      for (const node of mutation.removedNodes) {
        removedEntries.push(...textEntryCandidates(node));
      }
      for (const node of mutation.addedNodes) {
        discoverDeclaredSecrets(node);
        addedEntries.push(...textEntryCandidates(node));
      }
    }
    const detachedRemovedEntries = removedEntries.filter(
      (el) => !el.isConnected
    );
    const liveAddedEntries = addedEntries.filter((el) => el.isConnected);
    const removedSecretEntries = detachedRemovedEntries.filter(
      (el) => secretStates.has(el)
    );
    const unboundAddedEntries = liveAddedEntries.filter(
      // A node discovery bound in THIS batch still counts: discovery derives a
      // NEW input session, and a field with no name and no ID has no other
      // stable identity, so a controlled input that swaps its node would look
      // like a new declared field on every keystroke.
      (el) => !secretStates.has(el) || batchDiscoveredSecrets.has(el)
    );
    if (removedSecretEntries.length && unboundAddedEntries.length) {
      if (detachedRemovedEntries.length === 1 && liveAddedEntries.length === 1
          && removedSecretEntries.length === 1
          && unboundAddedEntries.length === 1) {
        bindSecretState(
          unboundAddedEntries[0],
          secretStates.get(removedSecretEntries[0]),
          false
        );
      } else {
        // A multi-node rewrite has no proven field mapping. Mask every
        // possible replacement, but refuse its first input. Assigning one
        // removed field/session to all candidates can merge distinct actions
        // and replay a secret into the wrong field.
        const maskState = secretStates.get(removedSecretEntries[0]);
        for (const el of unboundAddedEntries) {
          bindSecretState(el, maskState, false);
          ambiguousSecretReplacements.add(el);
        }
      }
    }
    for (const el of stickySecretElements) {
      if (!el.isConnected) continue;
      try {
        if (!el.hasAttribute(SECRET_MARKER)) el.setAttribute(SECRET_MARKER, '');
      } catch (e) {}
    }
  }

  function refreshSecretBindings() {
    if (secretObserver === null) return;
    // A document observer cannot see inside a shadow tree. Traverse every open
    // root at the event boundary, then synchronously consume its queued records.
    discoverOpenShadowRoots(document);
    processSecretMutations(secretObserver.takeRecords());
  }

  secretObserver = new MutationObserver((mutations) => {
    processSecretMutations(mutations);
  });
  observeSecretRoot(document.documentElement || document, null);
  discoverOpenShadowRoots(document);
  // Seed the reflected-text baseline as soon as discovery has bound whatever
  // declared fields this document already has. Flow installs this closure at
  // document start, so the URL and the title here are what the document showed
  // before it could reflect anything the operator typed into it.
  //
  // A field that ALREADY holds a value is the exception. Flow cannot tell
  // whether the text on show already reflects that value, so it takes no
  // baseline: every later URL and title from this document is withheld.
  if (liveSecretValues().size > 0) {
    documentHeldSecretValue = true;
  } else {
    preSecretUrl = String(location.href);
    preSecretTitle = String(document.title == null ? '' : document.title);
  }
  // NOTE on the title seed. Flow installs this closure at document start, so
  // `<title>` has usually not parsed yet and the seeded title is ''. That is
  // deliberate and it is not what carries the URL rule: safePageState refreshes
  // both baselines at every sample taken while no declared value is held, so
  // an ordinary page reaches its real title before the operator types. A
  // document that ALREADY holds a value at install keeps the '' baseline and
  // therefore withholds its title. That is the fail-closed side.

  function fieldLabel(el) {
    // Best available human label for the receiving field, best-first:
    // associated <label for=...>, wrapping <label>, aria-label,
    // aria-labelledby, placeholder, name attribute, title. Mirrors
    // PlaywrightBackend.focused_field_label exactly. Passive metadata for
    // the compile-time parameter-proposal pass; NEVER the field's value.
    // Exact or withheld, like every other text this closure reports: a label
    // rewritten to a placeholder proposes a parameter name the page never
    // showed, and the operator confirms that name without being told.
    try {
      if (trustedSecretFieldLabels.has(el)) {
        // Check the cached label against EVERY declared secret value, not only
        // this field's own value. Discovery walks the document in order, so a
        // field bound before another declared field caches a label that can
        // contain the OTHER field's pre-filled value.
        return labelTextOrNull(trustedSecretFieldLabels.get(el));
      }
      const clean = (s) => labelTextOrNull(
        (s || '').replace(/\s+/g, ' ').trim()
      );
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

  // ---- frame-space composition -------------------------------------------
  // Every emitted x/y/rect is TOP-DOCUMENT (page viewport) space: frames are
  // captured with page.screenshot(), and replay projects a top-level point
  // down the frame chain (PlaywrightBackend._FramePoint). A DOM event inside
  // an iframe delivers clientX/clientY relative to ITS OWN frame viewport, so
  // each listener adds the accumulated frame offset before emitting. DOM
  // reads (elementFromPoint, identity evidence) stay frame-local.

  function frameElementSelector(el, doc) {
    // Per-document unique selector for an iframe/frame element, in the exact
    // format replay's frame descent resolves (see _FRAME_SELECTOR_JS in
    // backends/playwright_backend.py): #id when unique, else an nth-of-type
    // chain. Returns null when no unique selector exists in `doc`.
    try {
      const unique = (selector) => {
        try { return doc.querySelectorAll(selector).length === 1; }
        catch (e) { return false; }
      };
      if (el.id) {
        const byId = '#' + CSS.escape(el.id);
        if (unique(byId)) return byId;
      }
      const segments = [];
      let node = el;
      while (node && node !== doc.documentElement) {
        const tag = node.tagName.toLowerCase();
        let index = 1;
        for (let sibling = node.previousElementSibling; sibling;
            sibling = sibling.previousElementSibling) {
          if (sibling.tagName === node.tagName) index += 1;
        }
        segments.unshift(tag + ':nth-of-type(' + index + ')');
        const candidate = segments.join(' > ');
        if (unique(candidate)) return candidate;
        node = node.parentElement;
      }
    } catch (e) {}
    return null;
  }

  function frameSpace() {
    // The accumulated top-document offset and frame chain for THIS document.
    // Each level adds the frame element's border-box position plus its left/
    // top border and padding: the child viewport's origin in the parent's
    // viewport. Returns null when the chain crosses an origin boundary
    // (window.frameElement is null or throws) or exceeds the replay descent
    // limit -- the caller refuses the event instead of emitting a point in an
    // unprovable coordinate space.
    if (window === window.top) return {dx: 0, dy: 0, path: [], complete: true};
    try {
      let dx = 0, dy = 0;
      const path = [];
      let complete = true;
      let win = window;
      for (let depth = 0; depth < 8 && win !== win.top; depth += 1) {
        const fe = win.frameElement;
        if (!fe) return null;
        const r = fe.getBoundingClientRect();
        const cs = win.parent.getComputedStyle(fe);
        dx += r.left + (parseFloat(cs.borderLeftWidth) || 0)
          + (parseFloat(cs.paddingLeft) || 0);
        dy += r.top + (parseFloat(cs.borderTopWidth) || 0)
          + (parseFloat(cs.paddingTop) || 0);
        const selector = frameElementSelector(fe, fe.ownerDocument);
        if (selector === null) complete = false;
        path.unshift(selector);
        win = win.parent;
      }
      if (win !== win.top) return null;
      return {dx: dx, dy: dy, path: path, complete: complete};
    } catch (e) { return null; }
  }

  function refuseFrame() {
    emit({kind: 'frame_refusal'});
  }

  function frameStructural(fs, target) {
    // Attach the frame chain to a structural target. A selector is resolved
    // WITHIN its frame scope at replay, so a subframe target without an exact
    // frame_path would be resolved against the wrong document: drop the
    // structural evidence entirely rather than emit a chain replay cannot
    // re-prove. The visual anchor (page-space point) remains.
    if (target === null) return null;
    if (!fs.path.length) return target;
    if (!fs.complete) return null;
    target.frame_path = fs.path;
    return target;
  }

  function composeRect(fs, rect) {
    if (!rect) return rect;
    return [rect[0] + Math.round(fs.dx), rect[1] + Math.round(fs.dy),
            rect[2], rect[3]];
  }

  function emitInFrameSpace(o) {
    // Declares that this subframe event's coordinates were composed into the
    // top-document space. Python refuses subframe events without this marker.
    if (window !== window.top) o.__oaflow_frame_composed = true;
    emit(o);
  }

  function emit(o) {
    try {
      // AN EVENT CARRIES NO REFLECTED TEXT. This handler runs in the CAPTURE
      // phase, before the page's own listeners, so `location.href` and
      // `document.title` here still hold what they held BEFORE this action.
      // Sampling them here produced evidence one action out of date, and the
      // value history that existed only to repair that staleness is what three
      // reviews found separate defects in.
      //
      // Flow samples the URL and the title from Python instead, through
      // structuralState(), at the same settled boundary that captures the
      // after-frame. Drop any reflected text a caller attached.
      delete o.url;
      delete o.title;
      // The ORIGIN is structural, not evidence text: Flow already declared it,
      // and a secret never becomes part of it. It cannot go stale inside a
      // document, because leaving the declared origin stops the recording.
      // window.origin, not location.origin: they are identical for an
      // ordinary http(s) document, but a same-origin srcdoc/about:blank frame
      // reports its ACTUAL (inherited) origin only through window.origin --
      // location.origin serializes as "null" there. A sandboxed frame without
      // allow-same-origin stays "null" through both and is refused.
      o.__oaflow_origin = String(window.origin || location.origin);
      o.__oaflow_doc = DOC_ID;
      o.__oaflow_doc_holds_secret = (
        opaqueSecretActive || documentHeldSecretValue
        || liveSecretValues().size > 0
      );
      o.__oaflow_session = SESSION_ID;
      o.__oaflow_top_level = window === window.top;
      // Geometry describes the TOP viewport -- the space every emitted
      // coordinate is composed into. A cross-origin document cannot read it;
      // its events are refusals, which carry no coordinates, so the local
      // fallback is never used as coordinate evidence.
      let vp = window;
      if (!o.__oaflow_top_level) {
        try { void window.top.innerWidth; vp = window.top; } catch (e) {}
      }
      o.__oaflow_viewport = [Math.round(vp.innerWidth),
                             Math.round(vp.innerHeight)];
      o.__oaflow_dpr = Number(vp.devicePixelRatio || 1);
      const binding = window[BINDING_NAME];
      if (typeof binding === 'function') binding(o);
    } catch (e) {}
  }

  function inputEventTarget(event) {
    try {
      const path = event.composedPath ? event.composedPath() : [];
      if (path.length && isTextEntry(path[0])) return path[0];
    } catch (e) {}
    return event.target;
  }

  function deepEventTarget(event) {
    try {
      const path = event.composedPath ? event.composedPath() : [];
      if (path.length && path[0] && path[0].nodeType === 1) return path[0];
    } catch (e) {}
    return event.target;
  }

  let pointerDown = null;
  let suppressClick = false;
  listenOn(window, 'resize', () => {
    if (resizeTimer !== null) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      resizeTimer = null;
      emit({kind: 'viewport'});
    }, 100);
  });
  listen('focusin', (e) => {
    // MutationObserver callbacks run at the microtask checkpoint. Drain queued
    // records now so a page cannot add a declared field, remove its identity,
    // and focus or type into it in one JavaScript task before classification.
    refreshSecretBindings();
    const el = inputEventTarget(e);
    if (activeSecretState && activeSecretElement && el !== activeSecretElement
        && !activeSecretElement.isConnected && isTextEntry(el)) {
      // The page replaced the element the operator was typing into. Continue
      // the SAME input session instead of deriving a new one: a field with no
      // name and no ID has no other stable identity, so a new session per swap
      // would make every keystroke look like a new declared field.
      bindSecretState(
        el, secretStates.get(activeSecretElement) || activeSecretState
      );
      return;
    }
    const declared = declaredSecretState(el);
    if (declared) {
      // Bind before the first key event. Application code can remove a
      // declared name/id during keydown, beforeinput, or input dispatch.
      bindSecretState(el, declared);
      return;
    }
    if (!activeSecretState || el === activeSecretElement) return;
    const retained = secretStates.get(el) || null;
    if (retained) {
      bindSecretState(el, retained);
    } else if (activeSecretElement && !activeSecretElement.isConnected
               && isTextEntry(el)) {
      bindSecretState(el, activeSecretState);
    } else {
      activeSecretElement = null;
      activeSecretState = null;
    }
  });
  listen('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (activeSecretState && e.target !== activeSecretElement
        && !secretStates.has(e.target)) {
      // An explicit operator action on another target ends the sticky input
      // session. Programmatic replacement without a pointer action does not.
      activeSecretElement = null;
      activeSecretState = null;
    }
    const fs = frameSpace();
    if (fs === null) { refuseFrame(); return; }
    const target = deepEventTarget(e);
    const rowIdentity = structuredIdentityEvidence(e.clientX, e.clientY, target);
    pointerDown = {
      x: Math.round(e.clientX + fs.dx), y: Math.round(e.clientY + fs.dy),
      sid: rowIdentity.sid,
      sid_withheld: rowIdentity.withheld,
      structural: frameStructural(
        fs, structuralTarget(e.clientX, e.clientY, target)
      ),
      idr: composeRect(fs, identifierRect()),
    };
  });

  listen('pointerup', (e) => {
    if (e.button !== 0 || !pointerDown) return;
    const start = pointerDown;
    pointerDown = null;
    const fs = frameSpace();
    if (fs === null) { refuseFrame(); return; }
    const endX = Math.round(e.clientX + fs.dx);
    const endY = Math.round(e.clientY + fs.dy);
    if (Math.hypot(endX - start.x, endY - start.y) < 5) return;
    suppressClick = true;
    setTimeout(() => { suppressClick = false; }, 0);
    const dragEvent = {
      kind: 'drag', x: start.x, y: start.y, end_x: endX, end_y: endY,
      sid: start.sid, structural: start.structural,
      end_structural: frameStructural(
        fs, structuralTarget(e.clientX, e.clientY, deepEventTarget(e))
      ),
      idr: start.idr,
    };
    if (start.sid_withheld) dragEvent.sid_withheld = start.sid_withheld;
    emitInFrameSpace(dragEvent);
  });

  listen('click', (e) => {
    if (e.button !== 0) return;
    if (suppressClick) { suppressClick = false; return; }
    const fs = frameSpace();
    if (fs === null) { refuseFrame(); return; }
    const target = deepEventTarget(e);
    const rowIdentity = structuredIdentityEvidence(e.clientX, e.clientY, target);
    const pointerEvent = {
      // The second click of a double-click gesture (e.detail === 2) becomes
      // an explicit double_click event; Python absorbs the pending first
      // click so the demonstration compiles to ONE DOUBLE_CLICK step, and
      // replay delivers exactly the two demonstrated clicks.
      kind: e.detail === 2 ? 'double_click' : 'click',
      x: Math.round(e.clientX + fs.dx), y: Math.round(e.clientY + fs.dy),
      sid: rowIdentity.sid,
      structural: frameStructural(
        fs, structuralTarget(e.clientX, e.clientY, target)
      ),
      idr: composeRect(fs, identifierRect()),
    };
    if (rowIdentity.withheld) pointerEvent.sid_withheld = rowIdentity.withheld;
    emitInFrameSpace(pointerEvent);
  });

  listen('contextmenu', (e) => {
    const fs = frameSpace();
    if (fs === null) { refuseFrame(); return; }
    const target = deepEventTarget(e);
    const rowIdentity = structuredIdentityEvidence(e.clientX, e.clientY, target);
    const pointerEvent = {
      kind: 'right_click',
      x: Math.round(e.clientX + fs.dx), y: Math.round(e.clientY + fs.dy),
      sid: rowIdentity.sid,
      structural: frameStructural(
        fs, structuralTarget(e.clientX, e.clientY, target)
      ),
      idr: composeRect(fs, identifierRect()),
    };
    if (rowIdentity.withheld) pointerEvent.sid_withheld = rowIdentity.withheld;
    emitInFrameSpace(pointerEvent);
  });

  // Commit points. After each of these the page can remove the field while
  // still showing the value somewhere -- an SPA wizard that replaces its form
  // with a summary row is the ordinary case. The value committed here is used
  // for ONE purpose, deciding whether to WITHHOLD identity text, and never for
  // the URL, the title, or any rewrite. See committedSecretValues.
  listen('change', (e) => {
    commitSecretValueFor(inputEventTarget(e));
    // A native <select> commits its option through browser-native dropdown UI
    // that produces no recordable action events, so the demonstrated choice
    // would be silently absent from the compiled workflow and replay would
    // proceed with whatever value the field happens to hold. Refuse loudly at
    // the moment the operator makes the selection.
    const el = deepEventTarget(e);
    if (el && el.matches && el.matches('select')) {
      emit({kind: 'control_refusal', control: 'select'});
    }
  });
  listen('focusout', (e) => commitSecretValueFor(inputEventTarget(e)));
  listen('submit', commitSecretValues);
  listenOn(window, 'pagehide', commitSecretValues);

  listen('input', (e) => {
    const fs = frameSpace();
    if (fs === null) { refuseFrame(); return; }
    refreshSecretBindings();
    const el = inputEventTarget(e);
    if (ambiguousSecretReplacements.has(el)) {
      privacyBoundaryError = (
        'a DOM rewrite made a declared secret field identity ambiguous'
      );
      emit({kind: 'privacy_refusal'});
      return;
    }
    const root = el && el.getRootNode ? el.getRootNode() : null;
    const isUnboundShadowInput = root && root.host
      && !secretStates.has(el) && !secretBoundaryStates.has(root)
      && !declaredSecretState(el) && !declaredSecretHostState(root.host);
    const isNativeNonTextControl = !!el && !!el.matches && el.matches(
      'select, input, button, option'
    ) && !isTextEntry(el);
    if (!isTextEntry(el) && !closedSecretHosts.has(el)
        && !declaredSecretHostState(el)) {
      if (isNativeNonTextControl) return;
      privacyBoundaryError = (
        'a shadow input event did not have a declared secret host boundary'
      );
      emit({kind: 'privacy_refusal'});
      return;
    }
    if (isUnboundShadowInput && SECRET_NAMES.length) {
      privacyBoundaryError = (
        'a shadow input event did not have a declared secret host boundary'
      );
      emit({kind: 'privacy_refusal'});
      return;
    }
    const secretBinding = secretStateForInput(el);
    const secret = secretBinding !== null;
    if (secret) {
      // CAPTURE PHASE, so this runs BEFORE the page's own `input` handler. A
      // page that writes the value somewhere and then clears its own field
      // does both in that handler; this is the last moment the DOM still holds
      // the value. Replace, never accumulate.
      const observed = currentSecretValue(el);
      if (observed) {
        // The cache holds ONE value per element and this is about to replace
        // it. A value that the next one does not CONTINUE was not edited away
        // by the operator -- the page took it and started the field over. A
        // scanner that writes the badge into the URL and clears the field does
        // exactly that, and the first badge is still on show while the second
        // is being typed. Promote it to the withhold-only committed set, which
        // is not per element, so the second scan cannot displace the first.
        //
        // This cannot promote a keystroke prefix: while the operator types,
        // each value continues the one before it. It is checked here, at the
        // next input event, rather than at a microtask checkpoint after this
        // one -- a checkpoint runs BETWEEN listeners, so it would observe the
        // field before the page's own handler had cleared it.
        const previous = lastSecretValues.get(el);
        if (previous && previous !== observed && observed.indexOf(previous) !== 0) {
          committedSecretValues.add(previous);
        }
        lastSecretValues.set(el, observed);
        // ARM THE DOCUMENT BOUNDARY HERE, not from what the DOM holds at the
        // settled read. A scanner that clears its own field inside its own
        // `input` handler never holds a value at any moment Python samples, so
        // a flag derived from the live DOM stayed false for the whole
        // recording: the title net never ran, and Python never learned that
        // this document had received a declared value at all. This is the
        // moment the document PROVABLY held one, and it is what the
        // documentation already says -- "once a declared secret field RECEIVES
        // INPUT".
        documentHeldSecretValue = true;
      }
    }
    if (secret && !isTextEntry(el) && closedSecretHosts.has(el)) {
      opaqueSecretActive = true;
    }
    const localRect = (el.getBoundingClientRect && el.getBoundingClientRect())
      || { left: 0, top: 0, width: 0, height: 0 };
    // Page-space, like every other emitted geometry: this rect names the
    // redaction region for a secret field, so a frame-local rect would black
    // out the wrong pixels and leave the real field visible.
    const r = {
      left: localRect.left + fs.dx, top: localRect.top + fs.dy,
      width: localRect.width, height: localRect.height,
    };
    // The receiving field's NAME is machine evidence: it becomes the parameter
    // the compiler binds and the replayer fills. A rewritten name would name a
    // parameter the page does not have, so it is exact or withheld.
    identityWithheldReason = null;
    const rawField = elementName(el) || el.id || null;
    const field = secret ? secretBinding.state.field : identityTextOrNull(rawField);
    const o = {
      kind: 'input',
      field: field,
      label: fieldLabel(el),
      secret: secret,
      __oaflow_input_session: secret
        ? secretBinding.state.inputSession : inputSessionFor(el),
      rect: [Math.round(r.left), Math.round(r.top),
             Math.round(r.width), Math.round(r.height)],
    };
    if (!secret && rawField !== null && field === null) {
      o.identity_withheld = identityWithheldReason;
    }
    // A secret literal never leaves the page closure. The element reference is
    // retained so the value can be READ LIVE at the next match; the value
    // itself is not retained anywhere.
    if (secret) {
      o.__oaflow_secret_mask_bound = secretBinding.maskBound;
    } else {
      o.value = currentSecretValue(el);
    }
    emitInFrameSpace(o);
  });

  listen('keydown', (e) => {
    const fs = frameSpace();
    if (fs === null) { refuseFrame(); return; }
    refreshSecretBindings();
    const el = inputEventTarget(e);
    if (ambiguousSecretReplacements.has(el)) {
      privacyBoundaryError = (
        'a DOM rewrite made a declared secret field identity ambiguous'
      );
      emit({kind: 'privacy_refusal'});
      return;
    }
    const keySecretBinding = secretStateForInput(el);
    if (keySecretBinding && e.key.length === 1) return;
    const modifiers = [];
    if (e.ctrlKey) modifiers.push('ctrl');
    if (e.altKey) modifiers.push('alt');
    if (e.shiftKey) modifiers.push('shift');
    if (e.metaKey) modifiers.push('meta');
    const pureModifier = ['Control', 'Alt', 'Shift', 'Meta'].indexOf(e.key) >= 0;
    const shiftedText = modifiers.length === 1
      && modifiers[0] === 'shift' && e.key.length === 1;
    if (modifiers.length && !pureModifier && !shiftedText && !e.repeat) {
      emitInFrameSpace({
        kind: 'hotkey', key: e.key, modifiers,
        });
      return;
    }
    if (SPECIAL.indexOf(e.key) < 0) return;
    emitInFrameSpace({kind: 'key', key: e.key});
  });

  listen('wheel', (e) => {
    const fs = frameSpace();
    if (fs === null) { refuseFrame(); return; }
    emitInFrameSpace({
      kind: 'scroll',
      dx: Math.round(e.deltaX), dy: Math.round(e.deltaY),
    });
  });
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
        cdp_endpoint: Optional[str] = None,
        browser_page_url: Optional[str] = None,
        poll_ms: int = 60,
        settle_timeout_s: float = 5.0,
        settle_stable_frames: int = 2,
        settle_interval_s: float = 0.15,
        viewport: tuple[int, int] = (1280, 800),
        system_of_record_reader: Optional[
            Callable[[], Optional[list[dict[str, Any]]]]
        ] = None,
        stop_when: Optional[Callable[[], bool]] = None,
        surface: str = "web",
    ) -> None:
        self._url = url
        self._surface = surface
        self._out_dir = Path(out_dir)
        self._secret_fields = set(secret_fields)
        self._param_fields = set(param_fields)
        self._identifier_fields = set(identifier_fields)
        self._headless = headless
        if browser_page_url and not cdp_endpoint:
            raise BrowserAttachError("browser_page_url requires a browser CDP endpoint")
        if cdp_endpoint and headless:
            raise BrowserAttachError(
                "headless mode cannot be combined with an attached browser"
            )
        self._cdp_endpoint = (
            validate_browser_cdp_endpoint(cdp_endpoint) if cdp_endpoint else None
        )
        self._attached_origin = (
            _http_origin(url, label="the declared app URL")
            if self._cdp_endpoint
            else None
        )
        self._browser_page_url = browser_page_url
        self._owns_browser = self._cdp_endpoint is None
        self._session_id = uuid.uuid4().hex
        self._binding_name = f"__oaflow_emit_{self._session_id}"
        self._secret_marker_attribute = f"data-oaflow-secret-{self._session_id}"
        self._poll_ms = poll_ms
        self._viewport = viewport
        # Recording-only, read-only observation. This does not add an effect
        # verifier to the browser backend or grant the later runtime authority.
        self._system_of_record_reader = system_of_record_reader
        # Optional recording completion condition. It is evaluated only after
        # queued browser events have been persisted, while the page is open.
        self._stop_when = stop_when
        self._settle = dict(
            settle_timeout_s=settle_timeout_s,
            settle_stable_frames=settle_stable_frames,
            settle_interval_s=settle_interval_s,
        )
        self._pyq: list[dict[str, Any]] = []
        self._pending_type: Optional[dict[str, Any]] = None
        self._pending_scroll: Optional[dict[str, Any]] = None
        self._pending_click: Optional[dict[str, Any]] = None
        self._pending_click_pumps = 0
        self._listener_error: Optional[BrowserAttachError] = None
        self.done = False

        # Set on start().
        self._recording_dir: Optional[Path] = None
        self._pw = None
        self._browser = None
        self.page = None
        self._page_close_listener = self._handle_page_close
        self._frame_navigation_listener = self._handle_frame_navigation
        self._frame_attached_listener = self._handle_frame_tree_change
        self._frame_detached_listener = self._handle_frame_tree_change
        self._popup_listener = self._handle_popup
        self._context_page_listener = self._handle_context_page
        self._page_lifecycle_listeners_installed = False
        self._context_page_listener_installed = False
        self._context_page_latches: list[tuple[Any, Any]] = []
        self._context_page_baselines: list[tuple[Any, tuple[Any, ...]]] = []
        self._context = None
        self._privacy_cdp = None
        self._context_pages_at_start: tuple[Any, ...] = ()
        self._finalizing = False
        self.backend: Optional[PlaywrightBackend] = None
        self.recorder: Optional[Recorder] = None
        self._last_frame: bytes = b""
        self._last_structural: dict[str, Any] = {}
        self._attached_geometry: Optional[tuple[int, int, float]] = None
        self._initial_attached_viewport: Optional[tuple[int, int]] = None
        self._viewport_dirty = False
        self._viewport_history: list[dict[str, Any]] = []
        # Source-time secret boundary state. Each document builds a fresh
        # closure, so a later document never saw the value an earlier one
        # received. Once a declared secret receives input, reflected text from
        # any LATER document is withheld.
        self._secret_doc_ids: set[str] = set()
        self._first_secret_doc_id: Optional[str] = None
        self._structural_text_withheld = False
        self._structural_text_withheld_reasons: set[str] = set()
        self._identity_withheld_events = 0
        self._dropped_url_parameters: set[tuple[str, str, str]] = set()
        self._app_placed_secret_in_url = False
        self._app_placed_secret_in_title = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch or attach, install listeners, and capture the first frame."""

        self._prepare_recording_dir()
        try:
            if self._owns_browser:
                from openadapt_flow._browser_setup import ensure_chromium_installed

                ensure_chromium_installed()
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            if self._owns_browser:
                self._browser = self._pw.chromium.launch(headless=self._headless)
                self.page = self._browser.new_page(
                    viewport={
                        "width": self._viewport[0],
                        "height": self._viewport[1],
                    },
                    device_scale_factor=1,
                )
            else:
                try:
                    self._browser = self._pw.chromium.connect_over_cdp(
                        self._cdp_endpoint
                    )
                except Exception as exc:
                    raise BrowserAttachError(
                        "could not connect to the local Chromium CDP endpoint; "
                        "confirm that the browser was started with remote "
                        "debugging and that the endpoint is ready"
                    ) from exc
                self._install_candidate_context_page_latches()
                self.page = select_attached_page(
                    self._browser,
                    app_url=self._url,
                    page_url=self._browser_page_url,
                )

            self._context = self.page.context
            if self._owns_browser:
                self._context.on("page", self._context_page_listener)
                self._context_page_latches = [
                    (self._context, self._context_page_listener)
                ]
                self._context_page_listener_installed = True
                self._context_pages_at_start = tuple(self._context.pages)
            else:
                self._bind_selected_context_page_latch()
            if self._listener_error is not None:
                raise self._listener_error
            self.page.on("close", self._page_close_listener)
            self.page.on("framenavigated", self._frame_navigation_listener)
            self.page.on("frameattached", self._frame_attached_listener)
            self.page.on("framedetached", self._frame_detached_listener)
            self.page.on("popup", self._popup_listener)
            self._page_lifecycle_listeners_installed = True
            self.page.expose_binding(
                self._binding_name,
                lambda source, detail: self._enqueue_browser_event(
                    detail,
                    source=source,
                ),
            )
            init_js = (
                _INIT_JS.replace("__SESSION_ID__", json.dumps(self._session_id))
                .replace("__BINDING_NAME__", json.dumps(self._binding_name))
                .replace("__SECRET_NAMES__", json.dumps(sorted(self._secret_fields)))
                .replace(
                    "__SECRET_MARKER__",
                    json.dumps(self._secret_marker_attribute),
                )
                .replace(
                    "__IDENT_NAMES__",
                    json.dumps(sorted(self._identifier_fields)),
                )
                .replace("__SPECIAL_KEYS__", json.dumps(list(_SPECIAL_KEYS)))
            )
            self.page.add_init_script(init_js)
            if self._owns_browser:
                self.page.goto(self._url)
                try:
                    self.page.wait_for_load_state("load")
                except Exception:
                    pass
            else:
                # add_init_script applies after the next navigation. Install
                # the same session in every already-open document now. This
                # includes child frames: a same-origin frame composes its
                # events into the top-document space, and a frame that cannot
                # prove that space emits an explicit refusal instead of
                # disappearing from the recording.
                for frame in list(self.page.frames):
                    try:
                        frame.evaluate(init_js)
                    except Exception as exc:
                        try:
                            detached = frame.is_detached()
                        except Exception:
                            detached = False
                        if detached:
                            continue
                        raise BrowserAttachError(
                            "could not install the recording listener in every "
                            "existing browser frame; recording was refused"
                        ) from exc
            self._guard_screenshot_privacy()

            self.backend = PlaywrightBackend(
                self.page,
                screenshot_scale="device" if self._owns_browser else "css",
                screenshot_mask_selectors=_secret_screenshot_selectors(
                    self._secret_fields,
                    marker_attribute=self._secret_marker_attribute,
                ),
                structural_state_reader=self._read_scrubbed_page_state,
                screenshot_guard=self._guard_screenshot_privacy,
            )
            assert self._recording_dir is not None
            self.recorder = Recorder(
                self.backend,
                self._recording_dir,
                app_url=self._url,
                system_of_record_reader=self._system_of_record_reader,
                **self._settle,
            )
            if self._owns_browser:
                self._last_frame = self.recorder._wait_settled()
                self._last_structural = self._structural_state()
            else:
                self._rebaseline_attached_viewport()
        except Exception:
            self.abort()
            raise

    def _register_existing_closed_shadow_boundaries(self) -> None:
        """Bind or refuse declared fields inside pre-existing closed roots.

        Page JavaScript cannot traverse a closed shadow root that existed before
        attachment. Chromium's DOM search can identify only the declared field
        nodes without returning their values. A function executed on each match
        performs the root/host check inside that document. No node content or
        secret literal crosses the CDP boundary.
        """

        assert self.page is not None
        queries = ["input[type='password']"]
        for field in sorted(self._secret_fields):
            encoded = _css_string_literal(field)
            queries.append(f"[name={encoded}], [id={encoded}]")
        cdp = self._privacy_cdp
        if cdp is None:
            assert self._context is not None
            try:
                cdp = self._context.new_cdp_session(self.page)
                cdp.send("DOM.enable")
            except Exception as exc:
                raise BrowserAttachError(
                    "could not inspect closed shadow boundaries; recording was "
                    "refused before retaining a frame"
                ) from exc
            self._privacy_cdp = cdp
        try:
            # Populate stable frontend node ids. Depth zero returns only the
            # document node; it does not send page attributes or text to Python.
            cdp.send("DOM.getDocument", {"depth": 0, "pierce": True})
            for query in queries:
                search = cdp.send(
                    "DOM.performSearch",
                    {"query": query, "includeUserAgentShadowDOM": False},
                )
                search_id = str(search["searchId"])
                try:
                    count = int(search.get("resultCount", 0))
                    if count <= 0:
                        continue
                    results = cdp.send(
                        "DOM.getSearchResults",
                        {
                            "searchId": search_id,
                            "fromIndex": 0,
                            "toIndex": count,
                        },
                    )
                    for node_id in results.get("nodeIds", []):
                        resolved = cdp.send(
                            "DOM.resolveNode",
                            {
                                "nodeId": int(node_id),
                                "objectGroup": _PRIVACY_SCAN_OBJECT_GROUP,
                            },
                        )
                        object_id = resolved.get("object", {}).get("objectId")
                        if not object_id:
                            raise BrowserAttachError(
                                "a declared secret node could not be bound before "
                                "the first frame"
                            )
                        outcome = cdp.send(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": object_id,
                                "objectGroup": _PRIVACY_SCAN_OBJECT_GROUP,
                                "functionDeclaration": r"""function(sessionId) {
                                  const root = this.getRootNode && this.getRootNode();
                                  if (!root || root.mode !== 'closed') {
                                    return {closed: false, registered: true};
                                  }
                                  const ownerWindow = this.ownerDocument.defaultView;
                                  const recorder = ownerWindow.__oaflowRecorder;
                                  if (!recorder || recorder.sessionId !== sessionId
                                      || typeof recorder.registerExistingClosedShadowHost
                                         !== 'function') {
                                    return {closed: true, registered: false};
                                  }
                                  return {
                                    closed: true,
                                    registered: Boolean(
                                      recorder.registerExistingClosedShadowHost(root.host)
                                    ),
                                  };
                                }""",
                                "arguments": [{"value": self._session_id}],
                                "returnByValue": True,
                            },
                        )
                        value = outcome.get("result", {}).get("value", {})
                        if value.get("closed") and not value.get("registered"):
                            raise BrowserAttachError(
                                "a declared secret is inside a pre-existing or "
                                "newly added closed shadow root whose host is not "
                                "declared with the same --secret name or id; "
                                "recording was refused before retaining a frame"
                            )
                finally:
                    cdp.send("DOM.discardSearchResults", {"searchId": search_id})
            # This guard runs before every retained screenshot. Release the
            # scan's resolved objects so a long recording cannot accumulate
            # protocol handles inside the attached browser.
            cdp.send(
                "Runtime.releaseObjectGroup",
                {"objectGroup": _PRIVACY_SCAN_OBJECT_GROUP},
            )
        except BrowserAttachError:
            raise
        except Exception as exc:
            raise BrowserAttachError(
                "could not prove closed shadow secret boundaries; recording was "
                "refused before retaining a frame"
            ) from exc

    def _guard_screenshot_privacy(self) -> None:
        """Bind or refuse every secret boundary before screenshot bytes exist."""

        self._register_existing_closed_shadow_boundaries()
        self._assert_page_privacy_safe()

    def _assert_page_privacy_safe(self) -> None:
        """Refuse a screenshot after an undeclared closed root appears."""

        if self.page is None:
            raise BrowserAttachError("the browser page is unavailable")
        try:
            frames = list(self.page.frames)
        except Exception as exc:
            raise BrowserAttachError(
                "could not inventory browser privacy guards"
            ) from exc
        for frame in frames:
            try:
                status = frame.evaluate(
                    """sessionId => {
                      const recorder = window.__oaflowRecorder;
                      if (!recorder || recorder.sessionId !== sessionId
                          || typeof recorder.privacyStatus !== 'function') {
                        return {ok: false, error: 'the privacy guard is unavailable'};
                      }
                      return recorder.privacyStatus();
                    }""",
                    self._session_id,
                )
            except Exception as exc:
                try:
                    detached = frame.is_detached()
                except Exception:
                    detached = False
                if detached:
                    continue
                raise BrowserAttachError(
                    "could not verify every browser secret boundary before "
                    "retaining a frame"
                ) from exc
            if not isinstance(status, dict) or status.get("ok") is not True:
                raise BrowserAttachError(
                    str(status.get("error") if isinstance(status, dict) else "")
                    or "the browser secret boundary is not safe"
                )

    def run(self) -> Path:
        """Pump until completion, an operator stop, or a closed window."""
        if self._stop_when is None:
            finish_instruction = (
                "Press Ctrl-C here to finish. Keep the selected browser tab open "
                "until Flow confirms the recording."
                if not self._owns_browser
                else "Press Ctrl-C here (or close the browser window) to finish."
            )
        else:
            finish_instruction = (
                "Complete the workflow. Recording stops automatically after "
                "the configured result is observed. Press Ctrl-C only to stop early."
            )
        print(
            f"Recording {self._url}\n"
            + (
                "  Perform your workflow in the selected existing browser tab.\n"
                if not self._owns_browser
                else "  Perform your workflow in the browser window.\n"
            )
            + f"  {finish_instruction}"
        )
        try:
            while not self.done:
                if not self._pump():
                    break
        except KeyboardInterrupt:
            print("\n[record] stopping…")
        except Exception:
            self.abort()
            raise
        return self.finish()

    def run_script(self, script: Callable[[Any, Callable[[], None]], None]) -> Path:
        """Scripted loop (tests): run ``script(page, pump)`` — which performs
        synthetic input and calls ``pump()`` to let the recorder drain — then
        flush and finish."""
        try:
            script(self.page, self.pump)
        except Exception:
            self.abort()
            raise
        return self.finish()

    def finish(self) -> Path:
        """Flush input, write metadata, and close or detach as appropriate."""
        try:
            if self._listener_error is not None:
                raise self._listener_error
            # Bind or refuse every secret boundary first. These page
            # round-trips also deliver lifecycle events that Chromium queued
            # during the recording, so a stale pre-finalization event is
            # judged by recording-time rules instead of aliasing a change
            # after the final evidence.
            self._guard_screenshot_privacy()
            if self._listener_error is not None:
                raise self._listener_error
            # The operations below retain the final evidence. Arm every
            # irreversible lifecycle latch before cleanup and the last
            # queue drain.
            self._finalizing = True
            self._cleanup_page_listeners()
            self._drain_event_queue()
            self._flush_click()
            self._flush_type()
            self._flush_scroll()
            assert self.recorder is not None
            out = self.recorder.finish()
            if self._listener_error is not None:
                raise self._listener_error
            meta_path = out / "meta.json"
            meta = json.loads(meta_path.read_text())
            meta["source"] = (
                "openadapt-flow-playwright"
                if self._owns_browser
                else "openadapt-flow-playwright-cdp"
            )
            # Stamp the surface BEFORE the atomic publish. A recording that the
            # publish step still has to modify is not complete when it appears
            # at the final path, and a crash in that window publishes a
            # surface-unbound recording.
            meta["surface"] = self._surface
            if self._structural_text_withheld:
                # The operator must be able to see that Flow dropped URL and
                # title evidence, and why. Silence here would read as evidence
                # the page simply did not have. Every distinct reason is named:
                # one recording can hit more than one.
                meta["structural_text_withheld"] = ",".join(
                    sorted(self._structural_text_withheld_reasons)
                )
            if self._identity_withheld_events:
                meta["identity_withheld_events"] = self._identity_withheld_events
            if self._dropped_url_parameters:
                # Which URL parameter VALUES the recording does not carry, and
                # why. The names are app structure and are safe to report.
                meta["url_dropped_params"] = [
                    {"name": name, "where": where, "reason": reason}
                    for name, where, reason in sorted(self._dropped_url_parameters)
                ]
            if self._app_placed_secret_in_url:
                meta["application_placed_secret_in_url"] = True
            if self._app_placed_secret_in_title:
                meta["application_placed_secret_in_title"] = True
            if not self._owns_browser:
                assert self._initial_attached_viewport is not None
                meta["viewport"] = list(self._initial_attached_viewport)
                meta["viewport_mode"] = "per-event"
                meta["viewport_history"] = list(self._viewport_history)
            meta_path.write_text(json.dumps(meta, indent=2))
            if self._listener_error is not None:
                raise self._listener_error
            self._assert_no_new_pages()
            self._stop_browser_connection()
            if self._listener_error is not None:
                raise self._listener_error
            return self._promote_recording()
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        """Detach and remove only this session's unpublished temporary output."""

        self.done = True
        self._pyq.clear()
        try:
            self._cleanup_page_listeners()
        finally:
            try:
                self._stop_browser_connection()
            finally:
                self._discard_recording_dir()

    def _prepare_recording_dir(self) -> None:
        """Reserve a fresh sibling directory without changing the final path."""

        if self._recording_dir is not None:
            return
        if os.path.lexists(self._out_dir):
            raise BrowserAttachError(
                "the recording output already exists; choose a new --out directory"
            )
        try:
            self._out_dir.parent.mkdir(parents=True, exist_ok=True)
            temporary = tempfile.mkdtemp(
                prefix=f"{_PARTIAL_RECORDING_PREFIX}{self._out_dir.name}-",
                dir=self._out_dir.parent,
            )
        except OSError as exc:
            raise BrowserAttachError(
                "the temporary recording output could not be created"
            ) from exc
        self._recording_dir = Path(temporary)

    def _promote_recording(self) -> Path:
        """Atomically publish the complete recording at the requested path."""

        assert self._recording_dir is not None
        try:
            _rename_directory_noreplace(self._recording_dir, self._out_dir)
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                raise BrowserAttachError(
                    "the recording output appeared during capture; Flow refused to "
                    "replace it"
                ) from exc
            raise BrowserAttachError(
                "the complete recording could not be published atomically"
            ) from exc
        self._recording_dir = None
        return self._out_dir

    def _discard_recording_dir(self) -> None:
        """Delete only the temporary directory that this session created."""

        recording_dir, self._recording_dir = self._recording_dir, None
        if recording_dir is None or not recording_dir.exists():
            return
        shutil.rmtree(recording_dir)

    def _handle_page_close(self, _page: Any = None) -> None:
        """Retain a refusal when an attached tab closes before finalization."""

        self.done = True
        if not self._owns_browser and self._listener_error is None:
            self._listener_error = BrowserAttachError(
                "the selected browser tab closed before Flow could retain the "
                "final evidence; recording stopped without complete metadata"
            )

    def _handle_frame_navigation(self, frame: Any) -> None:
        """Retain the first selected-main-frame origin violation."""

        if self._owns_browser or self.page is None or self._listener_error is not None:
            return
        if self._finalizing:
            # The final evidence is already bound. Refuse without another page
            # round-trip: an evaluate here would re-enter event dispatch while
            # the latch is armed.
            self._retain_late_frame_error()
            return
        try:
            if frame is not self.page.main_frame:
                return
            current_origin = _http_origin(
                str(frame.evaluate("() => location.origin")),
                label="the selected browser tab URL",
            )
        except Exception:
            current_origin = None
        if current_origin != self._attached_origin:
            self._listener_error = BrowserAttachError(
                "the selected browser tab left the declared application "
                "origin; recording was refused"
            )
            self.done = True

    def _retain_late_frame_error(self) -> None:
        """Retain one refusal for a post-snapshot frame-tree change."""

        if self._listener_error is None:
            self._listener_error = BrowserAttachError(
                "the selected browser tab changed frame state after Flow "
                "retained its final evidence; recording was refused"
            )
        self.done = True

    def _handle_frame_tree_change(self, _frame: Any = None) -> None:
        """Refuse a frame attach/detach after the final evidence snapshot."""

        if not self._owns_browser and self._finalizing:
            self._retain_late_frame_error()

    def _handle_popup(self, _popup: Any = None) -> None:
        """Refuse a second page that the selected recording tab opens."""

        self.done = True
        if self._listener_error is None:
            self._listener_error = BrowserAttachError(
                "the selected browser tab opened a popup or new tab; this "
                "recording is bound to one tab, so Flow stopped before "
                "publishing incomplete metadata"
            )

    def _handle_context_page(self, _page: Any = None) -> None:
        """Irreversibly refuse any page created after context binding."""

        self.done = True
        if self._listener_error is None:
            self._listener_error = BrowserAttachError(
                "the selected browser context opened a popup or new tab; this "
                "recording is bound to its accepted page baseline, so Flow "
                "stopped before publishing incomplete metadata"
            )

    def _install_candidate_context_page_latches(self) -> None:
        """Latch new pages on every context before attached-page selection."""

        assert self._browser is not None
        try:
            contexts = tuple(self._browser.contexts)
        except Exception as exc:
            raise BrowserAttachError(
                "the attached browser context inventory could not be read"
            ) from exc
        for context in contexts:
            try:
                context.on("page", self._context_page_listener)
            except Exception as exc:
                raise BrowserAttachError(
                    "the attached browser page baseline could not be guarded"
                ) from exc
            self._context_page_latches.append((context, self._context_page_listener))
        self._context_page_listener_installed = bool(self._context_page_latches)
        baselines: list[tuple[Any, tuple[Any, ...]]] = []
        for context in contexts:
            try:
                baseline = tuple(context.pages)
            except Exception as exc:
                raise BrowserAttachError(
                    "the attached browser page baseline could not be read"
                ) from exc
            baselines.append((context, baseline))
        self._context_page_baselines = baselines
        if self._listener_error is not None:
            raise self._listener_error

    def _bind_selected_context_page_latch(self) -> None:
        """Keep the selected context latch and its pre-listener baseline."""

        assert self._context is not None
        selected_baseline = next(
            (
                baseline
                for context, baseline in self._context_page_baselines
                if context is self._context
            ),
            None,
        )
        if selected_baseline is None:
            raise BrowserAttachError(
                "the selected browser context was not in the guarded baseline"
            )
        retained: list[tuple[Any, Any]] = []
        for context, listener in self._context_page_latches:
            if context is self._context:
                retained.append((context, listener))
                continue
            try:
                context.remove_listener("page", listener)
            except Exception:
                pass
        self._context_page_latches = retained
        self._context_page_baselines = [(self._context, selected_baseline)]
        self._context_pages_at_start = selected_baseline
        self._context_page_listener_installed = True
        self._assert_no_new_pages()

    def _assert_no_new_pages(self) -> None:
        """Retain a refusal if this recording context gained another page."""

        if self.page is None or not self._context_pages_at_start:
            return
        try:
            current_pages = tuple(self.page.context.pages)
        except Exception as exc:
            raise BrowserAttachError(
                "the selected browser tab page inventory could not be read; "
                "recording was refused"
            ) from exc
        for candidate in current_pages:
            if not any(
                candidate is existing for existing in self._context_pages_at_start
            ):
                self._handle_popup(candidate)
                break
        if self._listener_error is not None:
            raise self._listener_error

    def _enqueue_browser_event(
        self,
        detail: Any,
        *,
        source: Optional[dict[str, Any]] = None,
    ) -> None:
        """Accept only a bounded event from this recorder session."""

        if not isinstance(detail, dict):
            return
        event = dict(detail)
        if event.pop("__oaflow_session", None) != self._session_id:
            return
        raw_event_origin = event.pop("__oaflow_origin", None)
        raw_doc_id = event.pop("__oaflow_doc", None)
        doc_holds_secret = event.pop("__oaflow_doc_holds_secret", None) is True
        kind = event.get("kind")
        if kind == "privacy_refusal":
            self._listener_error = BrowserAttachError(
                "a shadow input did not have a declared secret host boundary; "
                "recording stopped before accepting its value or retaining "
                "another frame"
            )
            self.done = True
            return
        if kind == "frame_refusal":
            self._listener_error = BrowserAttachError(
                "an action happened inside an iframe whose page-space position "
                "could not be proven (a cross-origin or too-deep frame chain); "
                "recording stopped before accepting the event"
            )
            self.done = True
            return
        if kind == "control_refusal":
            control = str(event.get("control") or "control")
            self._listener_error = BrowserAttachError(
                f"a native <{control}> selection is not a qualified browser "
                "recording action; recording stopped so the demonstrated "
                "selection cannot be silently absent from the compiled "
                "workflow"
            )
            self.done = True
            return
        secret_mask_bound = event.pop("__oaflow_secret_mask_bound", None)
        if (
            kind == "input"
            and bool(event.get("secret"))
            and secret_mask_bound is not True
        ):
            self._listener_error = BrowserAttachError(
                "a secret input could not retain its screenshot mask identity; "
                "recording stopped before accepting the event"
            )
            self.done = True
            return
        raw_input_session = event.pop("__oaflow_input_session", None)
        if kind == "input":
            expected_prefix = f"{self._session_id}:input:"
            if (
                not isinstance(raw_input_session, str)
                or not raw_input_session.startswith(expected_prefix)
                or not raw_input_session.removeprefix(expected_prefix).isdigit()
            ):
                self._listener_error = BrowserAttachError(
                    "the browser emitted an input without a valid bound field "
                    "session; recording stopped before accepting the event"
                )
                self.done = True
                return
            event["_oaflow_input_session"] = raw_input_session
        reported_top_level = bool(event.pop("__oaflow_top_level", True))
        frame_composed = event.pop("__oaflow_frame_composed", None) is True
        source_page_matches = True
        source_is_selected_top_level = reported_top_level
        if source is not None:
            try:
                source_page_matches = source.get("page") is self.page
                source_is_selected_top_level = (
                    source_page_matches and source.get("frame") is self.page.main_frame
                )
            except Exception:
                source_page_matches = False
                source_is_selected_top_level = False
        if not source_is_selected_top_level and kind == "viewport":
            return
        # A same-origin subframe event is accepted only when the in-page
        # closure proved and composed its top-document coordinate space
        # (frame_composed). Anything else -- another page, or a subframe event
        # without the composition marker -- is refused, never reinterpreted.
        if not source_page_matches or not (
            source_is_selected_top_level or frame_composed
        ):
            self._listener_error = BrowserAttachError(
                "an event came from a frame outside this recording's "
                "page-space contract; recording stopped before accepting "
                "the event"
            )
            self.done = True
            return
        if (
            not source_is_selected_top_level
            and kind == "input"
            and bool(event.get("secret"))
        ):
            # The secret pipeline (closed-shadow scans, mask identity, value
            # withholding) is qualified against the top document only. Refuse
            # rather than risk retaining an unmasked secret frame.
            self._listener_error = BrowserAttachError(
                "a declared secret field inside an iframe is not qualified "
                "for browser recording; recording stopped before accepting "
                "its value"
            )
            self.done = True
            return
        if kind not in {
            "click",
            "double_click",
            "right_click",
            "drag",
            "input",
            "key",
            "hotkey",
            "scroll",
            "viewport",
        }:
            return
        self._track_secret_document(
            kind, event, raw_doc_id, holds_secret=doc_holds_secret
        )
        if not self._owns_browser:
            if not isinstance(raw_event_origin, str):
                self._listener_error = BrowserAttachError(
                    "a browser event did not report its document origin; "
                    "recording stopped before accepting the event"
                )
                self.done = True
                return
            try:
                # The page sends location.origin beside every event, as its
                # own field. The guard must never read reflected evidence
                # text: that text can be withheld, and an origin parsed out of
                # a withheld URL would refuse a valid recording.
                event_origin = _http_origin(
                    str(raw_event_origin),
                    label="the browser event origin",
                )
            except Exception:
                event_origin = None
            if event_origin != self._attached_origin:
                self._listener_error = BrowserAttachError(
                    "a browser event came from outside the declared application "
                    "origin; recording stopped before accepting the event"
                )
                self.done = True
                return
            raw_viewport = event.pop("__oaflow_viewport", None)
            raw_dpr = event.pop("__oaflow_dpr", None)
            try:
                event_geometry = (
                    int(raw_viewport[0]),
                    int(raw_viewport[1]),
                    round(float(raw_dpr), 6),
                )
            except (IndexError, TypeError, ValueError):
                event_geometry = (0, 0, 0.0)
            if (
                event_geometry[0] <= 0
                or event_geometry[1] <= 0
                or not 0.1 <= event_geometry[2] <= 16.0
            ):
                self._listener_error = BrowserAttachError(
                    "the browser emitted invalid viewport evidence; recording "
                    "stopped before accepting the event"
                )
                self.done = True
                return
            event["_oaflow_geometry"] = event_geometry
            if kind == "viewport":
                self._viewport_dirty = True
                return
        else:
            event.pop("__oaflow_viewport", None)
            event.pop("__oaflow_dpr", None)
        try:
            encoded_size = len(json.dumps(event).encode("utf-8"))
        except (TypeError, ValueError):
            return
        if encoded_size > 1_000_000:
            self._listener_error = BrowserAttachError(
                "the browser emitted an event larger than 1 MB; recording "
                "stopped without accepting the event"
            )
            self.done = True
            return
        self._pyq.append(event)

    def _origin_only_url(self) -> str:
        """The declared origin with an empty path: a URL that holds no value."""

        origin = self._attached_origin
        if origin is None:
            try:
                origin = _http_origin(self._url, label="the declared app URL")
            except BrowserAttachError:
                return ""
        scheme, host, port = origin
        if port == (443 if scheme == "https" else 80):
            return f"{scheme}://{host}/"
        return f"{scheme}://{host}:{port}/"

    def _track_secret_document(
        self,
        kind: str,
        event: dict[str, Any],
        raw_doc_id: Any,
        *,
        holds_secret: bool = False,
    ) -> None:
        """Bind the secret boundary to the document that received the value."""

        doc_id = raw_doc_id if isinstance(raw_doc_id, str) else None
        received_secret_input = kind == "input" and event.get("secret") is True
        if doc_id is not None and (holds_secret or received_secret_input):
            self._mark_secret_document(doc_id)
        # Count the action ONCE, whichever identity evidence Flow withheld: a
        # selector, an accessible name or role, the receiving field's name, or
        # the clicked row's identity characters. Each one disarms an identity
        # check the same way.
        withheld_identity = bool(
            event.get("sid_withheld") or event.get("identity_withheld")
        )
        for key in ("structural", "end_structural"):
            target = event.get(key)
            if isinstance(target, dict) and target.get("identity_withheld"):
                withheld_identity = True
        if withheld_identity:
            self._identity_withheld_events += 1
        # An event carries no URL or title of its own: Flow samples reflected
        # text at the settled boundary instead (see _read_scrubbed_page_state).
        # Note the reason here so the operator still learns that an action came
        # from a document whose reflected text Flow could no longer prove safe.
        if self._secret_document_left(doc_id):
            self._note_withheld_structural_text("secret-value-left-its-document")

    def _mark_secret_document(self, doc_id: str) -> None:
        """Record that this document received a declared value.

        BOTH markers move together. An earlier revision added to
        ``_secret_doc_ids`` from the input-event path but set
        ``_first_secret_doc_id`` only from the settled page read, so a document
        that never HELD a value at a sampling instant -- a scanner that clears
        its own field inside its own ``input`` handler -- reached
        ``_secret_doc_ids`` while the marker the cross-document rule keys off
        stayed ``None``. Every later document then reported its URL.
        """

        self._secret_doc_ids.add(doc_id)
        if self._first_secret_doc_id is None:
            self._first_secret_doc_id = doc_id

    def _secret_document_left(self, doc_id: Optional[str]) -> bool:
        """True for every document AFTER the one that first held a value.

        Each document builds its own recorder closure, so a later document
        never saw the value an earlier one received: no bound element holds it,
        nothing was committed in that closure, and a value carried in a PATH
        segment has no parameter name to identify it. Such a document cannot
        prove the URL it loaded with predates the value, so its reflected text
        is withheld.

        A document that receives a declared value of its own is NOT exempt. It
        can still have loaded with an earlier document's value in its path, and
        holding a value of its own says nothing about that. Only the FIRST
        document to hold a declared value reports its own reflected text, and
        that document reports it under the in-page rules.
        """

        if self._first_secret_doc_id is None:
            return False
        return doc_id is None or doc_id != self._first_secret_doc_id

    def _note_withheld_structural_text(self, reason: str) -> None:
        """Record WHY Flow withheld reflected text, for the operator notice."""

        self._structural_text_withheld = True
        self._structural_text_withheld_reasons.add(reason)

    def _cleanup_page_listeners(self) -> None:
        """Remove this session's current-document listeners before detach."""

        if self.page is None:
            return
        try:
            frames = list(self.page.frames)
        except Exception:
            frames = []
        for frame in frames:
            try:
                frame.evaluate(
                    """sessionId => {
                      const current = window.__oaflowRecorder;
                      const fallback = window['__oaflowCleanup_' + sessionId];
                      const owner = current && current.sessionId === sessionId
                        ? current : fallback;
                      if (owner && owner.sessionId === sessionId
                          && typeof owner.stopEvents === 'function') {
                        owner.stopEvents();
                      }
                    }""",
                    self._session_id,
                )
            except Exception:
                continue

    def _cleanup_secret_markers(self) -> None:
        """Remove this session's temporary secret-mask attributes."""

        if self.page is None:
            return
        try:
            frames = list(self.page.frames)
        except Exception:
            frames = []
        for frame in frames:
            try:
                frame.evaluate(
                    """([sessionId, marker]) => {
                      const current = window.__oaflowRecorder;
                      const fallback = window['__oaflowCleanup_' + sessionId];
                      const owner = current && current.sessionId === sessionId
                        ? current : fallback;
                      if (owner && owner.sessionId === sessionId
                          && typeof owner.cleanup === 'function') {
                        owner.cleanup();
                      }
                      const roots = [document];
                      while (roots.length) {
                        const root = roots.pop();
                        for (const element of root.querySelectorAll('*')) {
                          if (element.hasAttribute(marker)) {
                            element.removeAttribute(marker);
                          }
                          if (element.shadowRoot) {
                            roots.push(element.shadowRoot);
                          }
                        }
                      }
                    }""",
                    [self._session_id, self._secret_marker_attribute],
                )
            except Exception:
                continue

    def _stop_browser_connection(self) -> None:
        """Close an owned browser or detach without closing an external one."""

        self._cleanup_page_listeners()
        self._cleanup_secret_markers()
        privacy_cdp, self._privacy_cdp = self._privacy_cdp, None
        browser, self._browser = self._browser, None
        playwright, self._pw = self._pw, None
        try:
            if privacy_cdp is not None:
                try:
                    privacy_cdp.detach()
                except Exception:
                    pass
            if self._owns_browser and browser is not None:
                browser.close()
        finally:
            try:
                if playwright is not None:
                    playwright.stop()
            finally:
                # Keep all local lifecycle latches active until the external
                # Playwright connection has detached. They do not remain in
                # Chromium after the connection closes.
                if self.backend is not None:
                    self.backend.stop_screenshot_mask_tracking()
                self._page_lifecycle_listeners_installed = False
                self._context_page_listener_installed = False
                self._context_page_latches.clear()
                self._context_page_baselines.clear()
                self._context = None

    def _drain_event_queue(self) -> bool:
        """Process all events already delivered by the page binding."""

        if self._listener_error is not None:
            raise self._listener_error
        self._assert_no_new_pages()
        batch = self._pyq[:]
        del self._pyq[:]
        self._validate_event_batch(batch)
        rebased = False
        if not self._owns_browser:
            current_geometry = self._read_attached_geometry()
            if self._viewport_dirty or current_geometry != self._attached_geometry:
                if batch:
                    raise BrowserAttachError(
                        "an action overlapped a browser resize or monitor-scale "
                        "change; recording stopped because no exact pre-action "
                        "frame exists in the new coordinate space"
                    )
                self._rebaseline_attached_viewport()
                rebased = True
        for event in batch:
            if not self._owns_browser:
                event_geometry = event.pop("_oaflow_geometry", None)
                if event_geometry != self._attached_geometry:
                    raise BrowserAttachError(
                        "an action overlapped a browser resize or monitor-scale "
                        "change; recording stopped because no exact pre-action "
                        "frame exists in the new coordinate space"
                    )
            self._process(event)
            # A binding callback can arrive while Playwright captures this
            # action's after-frame. Revalidate it against the action in flight
            # so a later click/key cannot share that frame and then appear as a
            # separate, falsely exact step. Same-field input and one scroll
            # batch remain safe to coalesce.
            self._validate_event_batch([event, *self._pyq])
            if not self._owns_browser and (
                self._viewport_dirty
                or self._read_attached_geometry() != self._attached_geometry
            ):
                raise BrowserAttachError(
                    "the browser resized or changed monitor scale while an "
                    "action was being retained; recording stopped without "
                    "complete metadata"
                )
            self._validate_event_batch([event, *self._pyq])
            self._assert_no_new_pages()
        if self._listener_error is not None:
            raise self._listener_error
        return bool(batch) or rebased

    @staticmethod
    def _validate_event_batch(batch: list[dict[str, Any]]) -> None:
        """Refuse a batch that lacks an exact frame between logical actions."""

        if len(batch) <= 1:
            return
        kinds = {event.get("kind") for event in batch}
        if kinds == {"scroll"}:
            return
        if len(batch) == 2:
            first, second = batch
            # A double-click gesture delivers its first click and the
            # double_click marker inside one gesture window with no
            # intermediate settled frame; they merge into ONE step.
            try:
                same_point = (
                    abs(int(first.get("x", 0)) - int(second.get("x", 0))) <= 5
                    and abs(int(first.get("y", 0)) - int(second.get("y", 0))) <= 5
                )
            except (TypeError, ValueError):
                same_point = False
            if (
                first.get("kind") == "click"
                and second.get("kind") == "double_click"
                and same_point
            ):
                return
        if kinds == {"input"}:
            sessions = {event.get("_oaflow_input_session") for event in batch}
            fields = {event.get("field") for event in batch}
            if len(sessions) == 1 and None not in sessions and len(fields) == 1:
                return
        raise BrowserAttachError(
            "more than one logical browser action arrived before Flow could "
            "retain an exact intermediate frame; recording was refused"
        )

    # -- event pump ----------------------------------------------------------

    def pump(self) -> bool:
        """One public pump tick (used by scripted tests). Returns False when
        the page/browser is gone."""
        return self._pump()

    def _pump(self) -> bool:
        if self._listener_error is not None:
            raise self._listener_error
        if self.done:
            return False
        try:
            self.page.wait_for_timeout(self._poll_ms)
        except Exception:
            self.done = True
            return False
        if not self._drain_event_queue():
            # Distinct scroll gestures are separated by pauses; flush a
            # completed scroll on idle so each becomes its own step. A type run
            # is NOT idle-flushed (a mid-word pause must not split it) — it
            # flushes on the next boundary event or at finish().
            self._flush_scroll()
            # A click outlives the double-click window unmerged: it is a
            # single click.
            if self._pending_click is not None:
                self._pending_click_pumps -= 1
                if self._pending_click_pumps <= 0:
                    self._flush_click()
            return not self._stop_condition_reached()
        return not self._stop_condition_reached()

    def _process(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "input":
            self._flush_click()
            self._flush_scroll()
            self._accumulate_input(ev)
        elif kind == "scroll":
            self._flush_click()
            self._flush_type()
            self._accumulate_scroll(ev)
        elif kind == "click":
            self._flush_click()
            self._flush_type()
            self._flush_scroll()
            # Held briefly: the second click of a double-click gesture
            # supersedes it (see _absorb_pending_click). Any other event, an
            # idle double-click window, or finish() flushes it unchanged. The
            # settled after-frame is captured NOW, exactly as an immediate
            # write would, so nothing that happens during the hold can enter
            # this click's evidence.
            assert self.recorder is not None
            ev["_oaflow_after_frame"] = self.recorder._wait_settled()
            ev["_oaflow_structural_after"] = self._structural_state()
            self._pending_click = ev
            self._pending_click_pumps = max(
                1, -(-_DOUBLE_CLICK_WINDOW_MS // max(1, self._poll_ms))
            )
        elif kind == "double_click":
            self._flush_type()
            self._flush_scroll()
            self._absorb_pending_click(ev)
            self._record_pointer(ev)
        elif kind in {"right_click", "drag"}:
            self._flush_click()
            self._flush_type()
            self._flush_scroll()
            self._record_pointer(ev)
        elif kind in {"key", "hotkey"}:
            self._flush_click()
            self._flush_type()
            self._flush_scroll()
            self._record_key(ev)

    def _flush_click(self) -> None:
        pending = self._pending_click
        self._pending_click = None
        if pending is not None:
            self._record_pointer(pending)

    def _absorb_pending_click(self, ev: dict[str, Any]) -> None:
        """Discard the held first click of this double-click gesture.

        A pending click at another point is a separate action: record it in
        its demonstrated order instead."""

        pending = self._pending_click
        self._pending_click = None
        if pending is None:
            return
        if (
            abs(int(pending["x"]) - int(ev["x"])) <= 5
            and abs(int(pending["y"]) - int(ev["y"])) <= 5
        ):
            return
        self._record_pointer(pending)

    # -- accumulation / flush ------------------------------------------------

    def _accumulate_input(self, ev: dict[str, Any]) -> None:
        field = ev.get("field")
        input_session = ev.get("_oaflow_input_session")
        if self._pending_type is not None and (
            self._pending_type.get("field") != field
            or self._pending_type.get("input_session") != input_session
        ):
            self._flush_type()  # focus moved to a different field
        if self._pending_type is None:
            self._pending_type = {
                "field": field,
                "input_session": input_session,
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
        after_png = ev.pop("_oaflow_after_frame", None)
        structural_after = ev.pop("_oaflow_structural_after", None)
        self.recorder.record_observed(
            event,
            before_png=self._last_frame,
            structural_before=self._last_structural,
            structured_identity=ev.get("sid"),
            after_png=after_png,
            structural_after=structural_after,
        )
        if after_png is not None:
            self._set_last(after_png, structural_after)
        else:
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

    def _read_attached_geometry(self) -> tuple[int, int, float]:
        """Read the selected tab's origin, CSS viewport, and monitor scale."""

        assert not self._owns_browser
        assert self.page is not None
        try:
            raw = self.page.evaluate(
                """() => ({
                  origin: location.origin,
                  width: window.innerWidth,
                  height: window.innerHeight,
                  dpr: window.devicePixelRatio || 1,
                })"""
            )
            current_origin = _http_origin(
                str(raw["origin"]),
                label="the selected browser tab URL",
            )
            geometry = (
                int(raw["width"]),
                int(raw["height"]),
                round(float(raw["dpr"]), 6),
            )
        except Exception as exc:
            raise BrowserAttachError(
                "the attached tab geometry could not be read; recording was refused"
            ) from exc
        if current_origin != self._attached_origin:
            raise BrowserAttachError(
                "the selected browser tab left the declared application origin; "
                "recording was refused"
            )
        if geometry[0] <= 0 or geometry[1] <= 0 or not 0.1 <= geometry[2] <= 16.0:
            raise BrowserAttachError(
                "the attached tab reported invalid viewport or monitor-scale "
                "geometry; recording was refused"
            )
        return geometry

    def _rebaseline_attached_viewport(self) -> None:
        """Resume after an idle resize with a fresh exact CSS-pixel baseline."""

        assert not self._owns_browser
        assert self.recorder is not None
        # A deferred input or scroll already has its exact old-space after
        # frame. Persist it before the new coordinate space becomes current.
        self._flush_type()
        self._flush_scroll()
        for _attempt in range(3):
            before = self._read_attached_geometry()
            frame = self.recorder._wait_settled()
            after = self._read_attached_geometry()
            if self._pyq or self._listener_error is not None:
                raise BrowserAttachError(
                    "an action occurred before the resized browser viewport was "
                    "rebound to a fresh frame; recording stopped without "
                    "complete metadata"
                )
            with Image.open(io.BytesIO(frame)) as image:
                frame_size = image.size
            if before == after and frame_size == after[:2]:
                self._attached_geometry = after
                self._last_frame = frame
                self._last_structural = self._structural_state()
                self._viewport_dirty = False
                viewport = after[:2]
                if self._initial_attached_viewport is None:
                    self._initial_attached_viewport = viewport
                entry = {
                    "before_event": self.recorder.event_count,
                    "viewport": list(viewport),
                    "device_scale_factor": after[2],
                }
                if (
                    self._viewport_history
                    and self._viewport_history[-1]["before_event"]
                    == entry["before_event"]
                ):
                    self._viewport_history[-1] = entry
                elif not self._viewport_history or (
                    self._viewport_history[-1]["viewport"] != entry["viewport"]
                    or self._viewport_history[-1]["device_scale_factor"]
                    != entry["device_scale_factor"]
                ):
                    self._viewport_history.append(entry)
                return
        raise BrowserAttachError(
            "the attached browser viewport did not settle long enough to bind "
            "a new exact frame and coordinate space"
        )

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

    def _read_scrubbed_page_state(self) -> dict[str, Any]:
        """Sample the page-closure URL and title at a SETTLED boundary.

        This is the only sampling point for reflected evidence. Python calls it
        after the page has processed the action, so what the page shows is what
        the action produced. The in-page capture-phase listeners deliberately
        emit no URL and no title: they run BEFORE the page's own handlers, so
        anything they read is one action out of date.

        The page reduces its own URL by STRUCTURE -- origin and path reported,
        parameter names kept, the values Flow cannot stand behind emptied --
        and says what it dropped and what it withheld.

        Python supplies the one fact the page cannot know: whether some EARLIER
        document already received a declared value. A document born after that
        moment can load with the value already in its URL, so its own first
        sample proves nothing about a time before the value existed.
        """

        assert self.page is not None
        secret_seen_earlier = bool(self._secret_doc_ids)
        try:
            safe_page_state = self.page.evaluate(
                """([sessionId, secretSeenEarlier]) => {
                  const recorder = window.__oaflowRecorder;
                  if (!recorder || recorder.sessionId !== sessionId
                      || typeof recorder.structuralState !== 'function') {
                    return null;
                  }
                  return recorder.structuralState(secretSeenEarlier);
                }""",
                [self._session_id, secret_seen_earlier],
            )
        except Exception as exc:
            raise BrowserAttachError(
                "could not read scrubbed browser structural state"
            ) from exc
        if not isinstance(safe_page_state, dict):
            raise BrowserAttachError(
                "the page-local secret scrubber did not return structural state"
            )
        state: dict[str, Any] = {}
        for key in ("url", "title"):
            value = safe_page_state.get(key)
            if not isinstance(value, str):
                raise BrowserAttachError(
                    "the page-local secret scrubber did not return structural state"
                )
            state[key] = value
        raw_doc_id = safe_page_state.get("doc")
        doc_id = raw_doc_id if isinstance(raw_doc_id, str) else None
        if doc_id is not None and safe_page_state.get("secret") is True:
            self._mark_secret_document(doc_id)
        for key in ("url_withheld", "title_withheld"):
            reason = safe_page_state.get(key)
            if isinstance(reason, str) and reason:
                self._note_withheld_structural_text(reason)
        # An application that puts a declared secret into its own URL has a
        # defect that exists with or without Flow: OWASP lists browser history,
        # server logs, proxies, CDNs and the Referer header as places it is
        # already exposed. Tell the operator.
        if safe_page_state.get("secret_in_url") is True:
            self._app_placed_secret_in_url = True
        if safe_page_state.get("secret_in_title") is True:
            self._app_placed_secret_in_title = True
        # A LATER document builds a fresh closure that never saw the value an
        # earlier document received, and it cannot prove that the URL it loaded
        # with predates that value. A server that answers a form submit with a
        # redirect to `/results/<value>` puts the value in the PATH, where no
        # parameter name identifies it and no value in the new closure matches
        # it. Structure protects the query channel, not this one, so the whole
        # URL and the title are withheld.
        #
        # This does NOT cost the single-page-application evidence: an SPA route
        # change is a SAME-document `history.pushState`, so the document that
        # held the value is the document being sampled, and its URL is still
        # reported exactly. This rule bites only on a real navigation, which is
        # exactly where the redirect leak lives.
        if self._secret_document_left(doc_id):
            self._note_withheld_structural_text("secret-value-left-its-document")
            state["url"] = self._origin_only_url()
            state["title"] = ""
            return state
        # Record the drop only for a URL that Flow actually reports. Naming a
        # dropped parameter of a URL that never reached disk would tell the
        # operator less than nothing.
        #
        # The drop is a RECORDING-level fact, not a per-action one: a parameter
        # named after a declared field loses its value in every URL Flow
        # reports, whatever the value is. `meta.json` carries the list. Putting
        # it on each event would land it inconsistently, because the Recorder
        # builds an action's after-state from the backend's url/title/page-count
        # seam alone.
        if not safe_page_state.get("url_withheld"):
            dropped = safe_page_state.get("dropped")
            if isinstance(dropped, list):
                for entry in dropped:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    if not isinstance(name, str):
                        continue
                    self._dropped_url_parameters.add(
                        (name, str(entry.get("where")), str(entry.get("reason")))
                    )
        return state

    def _structural_state(self) -> dict[str, Any]:
        state: dict[str, Any] = dict(self._read_scrubbed_page_state())
        try:
            page_count = self.backend.page_count
        except Exception:
            page_count = None
        if page_count is not None:
            state["pages"] = page_count
        if self._system_of_record_reader is not None:
            try:
                records = self._system_of_record_reader()
            except Exception:
                records = None
            if records is not None:
                state["sor"] = records
        return state

    def _stop_condition_reached(self) -> bool:
        if self._stop_when is None:
            return False
        try:
            done = bool(self._stop_when())
        except Exception:
            done = False
        if done:
            self.done = True
        return done


def record_interactive(
    url: str,
    out_dir: Path | str,
    *,
    secret_fields: tuple[str, ...] = (),
    param_fields: tuple[str, ...] = (),
    identifier_fields: tuple[str, ...] = (),
    headless: bool = False,
    cdp_endpoint: Optional[str] = None,
    browser_page_url: Optional[str] = None,
    script: Optional[Callable[[Any, Callable[[], None]], None]] = None,
    system_of_record_reader: Optional[
        Callable[[], Optional[list[dict[str, Any]]]]
    ] = None,
    stop_when: Optional[Callable[[], bool]] = None,
    surface: str = "web",
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
        cdp_endpoint: Optional local-loopback Chromium DevTools endpoint. When
            set, the recorder attaches to an existing browser and never
            launches, navigates, or closes it.
        browser_page_url: Exact current tab URL used to disambiguate two or
            more open tabs on the declared app origin. Requires
            ``cdp_endpoint``. Query and fragment values are not written to
            recorder diagnostics.
        script: Test hook — ``script(page, pump)`` drives synthetic input and
            pumps the loop; when given, the human wait loop is skipped.
        system_of_record_reader: Optional read-only observation of the
            authoritative records. The recorder retains the observed
            before/after state so compilation can propose effect contracts.
            This observer is recording-only and does not become a runtime
            verifier.
        stop_when: Optional recording completion condition. It is evaluated
            after queued events are persisted and before the browser closes.
        surface: Recorded surface stamped into ``meta.json`` before the
            recording is published. The compiler binds the bundle to it.

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
        cdp_endpoint=cdp_endpoint,
        browser_page_url=browser_page_url,
        system_of_record_reader=system_of_record_reader,
        stop_when=stop_when,
        surface=surface,
        **kwargs,
    )
    session.start()
    if script is not None:
        return session.run_script(script)
    return session.run()
