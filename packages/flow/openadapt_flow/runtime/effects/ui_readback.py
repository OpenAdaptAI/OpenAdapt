"""Separate-session UI read-back: check a saved record through the
application's own record page, in a browser session that shares nothing with
the session that typed.

Most teams that automate a legacy GUI have no API or database access to the
record. This verifier is the record check they can still run. After the save,
it:

1. launches its own browser (never the replay backend's browser) and opens a
   new, empty browser context: no cookies, no storage, no service workers;
2. signs in with its own credentials, read from environment variables;
3. navigates by URL to the record's page, built from the record identifier;
4. confirms that the page shows the requested record identifier;
5. reads the declared fields from the page structure (DOM text or attributes);
6. diffs them against the inputs with the shared effect judge, so counts,
   duplicates, lost updates, and field values are judged exactly as the REST,
   SQL, and FHIR verifiers judge them.

After sign-in the session is read-only at the network layer: every request
other than GET, HEAD, or OPTIONS is aborted, except POSTs to read endpoints an
operator declares explicitly. A blocked request makes the read uncertain, so
the verdict is INDETERMINATE and the run halts.

When the record's identifier is only known after the save (an application
that issues a claim, submission, or confirmation number), declare
``reference``: right after the save the verifier reads that identifier once
from the run's own session (the page URL, or the screen through local OCR),
then opens the record page by it in the separate session as above. The
captured value only says which record to open; the identity check on the
record page and the field diff decide the verdict. There is no record page to
read before the save, so effects that need a pre-action baseline (duplicate
counts, collateral loss, exact new sets) refuse to run in this mode.

What this is not. The read goes through the same application, so it is not a
system-of-record read. It declares
:attr:`~openadapt_flow.verification.VerificationTier.INDEPENDENT_SESSION`
(Flow tier 2; Seal oracle tier 1, "separate read-only session") and sets
``independent_system_of_record = False``. A record page that itself renders
from a cache the write populated, or that hides or reformats fields, limits
what the check can see. ``docs/EFFECT_KIT.md`` lists these limits and
``benchmark/ui_readback/`` measures them.

Fail-safe posture, as for every verifier: a page that does not load, a sign-in
that does not complete, a page for another record, a shortened or unparseable
value in a field the contract reads, or a blocked request each give
INDETERMINATE. Only an affirmative contradiction on a readable page gives
REFUTED. Nothing here guesses CONFIRMED.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Mapping, Optional, Protocol
from urllib.parse import quote, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.assurance import SCREEN_READBACK, UNCHECKED
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.adapter import ConnectionProbe, VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.ui_read_checks import (
    FinalCheck,
    HistoryView,
    ReadIndependence,
    assess_independence,
    judge_history,
    record_digest_payload,
    view_key,
)
from openadapt_flow.verification import VerificationTier

SUBSTRATE = "ui-readback"

#: Methods the read-only session may send after sign-in. OPTIONS is a CORS
#: preflight and carries no body.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Longest gap between reads while waiting for a lagging view.
_MAX_POLL_S = 2.0

#: HTTP statuses that mean the fetch failed in transit (a proxy or gateway),
#: retried a few times with backoff before the page counts as unreadable.
_TRANSIENT = frozenset({502, 503, 504})
_LOAD_ATTEMPTS = 3
_SIGN_IN_ATTEMPTS = 3


def _backoff(attempt: int) -> float:
    return min(0.5 * (2 ** (attempt - 1)), 4.0)


#: Displayed values that end with one of these are treated as shortened.
DEFAULT_TRUNCATION_MARKERS: tuple[str, ...] = ("…", "...")


# -- configuration ------------------------------------------------------------


class FieldReader(BaseModel):
    """How to read one value from the record page.

    ``selector`` is a CSS selector relative to the row (or to the page, for
    the identity check). An empty selector reads the row element itself.
    ``attribute`` reads that attribute instead of the rendered text, which is
    how a page that shortens long text can still expose the full value (for
    example a ``title`` attribute).
    """

    model_config = ConfigDict(extra="forbid")

    selector: str = ""
    attribute: Optional[str] = None
    #: ``strptime`` format of a displayed date (for example ``"%b %d, %Y"``).
    #: The read value is normalized to ISO 8601 (``YYYY-MM-DD``). Expected
    #: values are normalized the same way when they parse as ISO or with this
    #: format. A displayed date that does not parse is uncertain, never a
    #: contradiction.
    date_format: Optional[str] = None
    #: Compare case-insensitively (both sides are case-folded).
    casefold: bool = False


def _auth_url_key(url: str) -> tuple[str, str, int, str, str]:
    """Canonical authority/path with the query kept verbatim, never inferred."""

    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or "#" in url
            or "\\" in url
            or any(c.isspace() for c in url)
        ):
            raise ValueError("invalid auth endpoint")
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
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "sign_in requires an absolute HTTP(S) URL without userinfo or fragments"
        ) from exc


class SignInSpec(BaseModel):
    """The read-back session's own sign-in. Credentials are env-var names."""

    model_config = ConfigDict(extra="forbid")

    url: str
    #: Trusted deployment authority for one native form POST; never inferred.
    post_url: Optional[str] = None
    username_selector: str
    password_selector: str
    submit_selector: str
    #: An element that is present only after a successful sign-in.
    signed_in_selector: str
    username_env: str
    password_env: str

    @model_validator(mode="after")
    def _auth_endpoint(self) -> "SignInSpec":
        try:
            page = _auth_url_key(self.url)
        except ValueError as exc:
            raise ValueError(
                "ui_readback.sign_in.url must be an absolute http(s) URL"
            ) from exc
        if self.post_url is not None:
            try:
                post = _auth_url_key(self.post_url)
            except ValueError as exc:
                raise ValueError(
                    "sign_in.post_url must be an absolute HTTP(S) endpoint without userinfo or fragments"
                ) from exc
            if post[:3] != page[:3]:
                raise ValueError("sign_in.post_url must have the sign-in page's origin")
        return self


class RecordIdentity(BaseModel):
    """The check that the page shows the record the session asked for."""

    model_config = ConfigDict(extra="forbid")

    reader: FieldReader
    #: The identifier the page must show, usually a run parameter. Omitted
    #: when :attr:`UiReadbackSpec.reference` captures it after the save.
    value: Optional[ValueExpr] = None
    #: When set, the confirmed identifier is added to every row under this
    #: field name, so an effect can match on it (for example ``patient_id``).
    field: Optional[str] = None


#: Placeholder reference used only to bind the URL for configuration checks
#: and the sign-in probe, before a real reference has been captured.
_PROBE_REFERENCE = "reference-not-captured-yet"


class ReferenceCapture(BaseModel):
    """Read the saved record's identifier from the run's own session.

    Some applications only issue the identifier at the save: a claim number,
    a submission reference, a confirmation number. Right after the save the
    verifier reads it once, here, and opens the record page by it in the
    separate session. ``source`` is ``"url"`` (the page URL the run's session
    shows, from a backend that exposes ``url``) or ``"screen"`` (local OCR of
    the run's current screenshot, optionally limited to ``region``).
    ``pattern`` is a regular expression with exactly one capture group; it
    must match exactly one distinct value, or the check is INDETERMINATE.
    """

    model_config = ConfigDict(extra="forbid")

    #: The ``{name}`` placeholder in ``record_url`` that the reference fills.
    url_param: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    source: Literal["url", "screen"] = "url"
    pattern: str = Field(min_length=1, max_length=512)
    #: ``(x, y, w, h)`` screen region to OCR (``source: screen`` only).
    region: Optional[tuple[int, int, int, int]] = None

    @model_validator(mode="after")
    def _shape(self) -> "ReferenceCapture":
        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(
                f"ui_readback.reference.pattern is invalid: {exc}"
            ) from exc
        if compiled.groups != 1:
            raise ValueError(
                "ui_readback.reference.pattern must have exactly one capture group"
            )
        if self.region is not None:
            if self.source != "screen":
                raise ValueError("ui_readback.reference.region needs source: screen")
            if self.region[2] <= 0 or self.region[3] <= 0:
                raise ValueError("ui_readback.reference.region must have a size")
        return self


class UiReadbackSpec(BaseModel):
    """Deployment config for ``effects.kind: ui_readback``."""

    model_config = ConfigDict(extra="forbid")

    #: URL of the record page. ``{name}`` placeholders are filled from
    #: :attr:`url_params` and URL-quoted.
    record_url: str
    url_params: dict[str, ValueExpr] = Field(default_factory=dict)
    sign_in: SignInSpec
    #: Present when the record page has loaded (with or without rows).
    ready_selector: str
    #: Optional: present when the application says the record does not exist.
    #: Read as a page with zero rows.
    not_found_selector: Optional[str] = None
    #: Optional but recommended: present when the record page shows no rows
    #: ("No encounters"). When set, a loaded page with zero matching rows and
    #: no such marker is unreadable (the layout probably changed), never an
    #: empty record.
    empty_selector: Optional[str] = None
    #: One element per saved item on the record page.
    row_selector: str
    #: Record field name -> how to read it from a row.
    fields: dict[str, FieldReader]
    identity: RecordIdentity
    #: Optional: capture the record identifier from the run's own session
    #: after the save, instead of taking it from a run parameter.
    reference: Optional[ReferenceCapture] = None
    #: Path prefixes on the record page's origin that the page may POST to
    #: in order to READ data (for example a GraphQL query endpoint). Empty by
    #: default: every POST after sign-in is blocked.
    read_post_paths: list[str] = Field(default_factory=list)
    truncation_markers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TRUNCATION_MARKERS)
    )
    navigation_timeout_s: float = 15.0
    #: Seconds a search or list view may lag the save (a replica or a
    #: full-text index), measured per application in its sandbox, never
    #: from evaluation data. The read polls with backoff for at least this
    #: long before a missing record counts.
    index_lag_s: float = Field(default=0.0, ge=0.0, le=300.0)
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    #: Optional: the application's history, audit or activity view, read
    #: before and after the save to catch collateral changes
    #: (:class:`~openadapt_flow.runtime.effects.ui_read_checks.HistoryView`).
    history: Optional[HistoryView] = None
    #: Optional: make this read provisional and settle it with a later read
    #: (:class:`~openadapt_flow.runtime.effects.ui_read_checks.FinalCheck`).
    final: Optional[FinalCheck] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_literals(cls, data: Any) -> Any:
        """A bare YAML string is a literal, as in every other effects config."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        params = data.get("url_params")
        if isinstance(params, dict):
            data["url_params"] = {
                k: ({"literal": v} if isinstance(v, str) else v)
                for k, v in params.items()
            }
        identity = data.get("identity")
        if isinstance(identity, dict) and isinstance(identity.get("value"), str):
            identity = dict(identity)
            identity["value"] = {"literal": identity["value"]}
            data["identity"] = identity
        return data

    @model_validator(mode="after")
    def _shape(self) -> "UiReadbackSpec":
        if not self.fields:
            raise ValueError("ui_readback.fields must name at least one field")
        if self.identity.field is not None and self.identity.field in self.fields:
            raise ValueError("ui_readback.identity.field must not also be a row field")
        if self.reference is None and self.identity.value is None:
            raise ValueError(
                "ui_readback.identity.value is required unless ui_readback.reference "
                "captures the identifier after the save"
            )
        if self.reference is not None:
            if self.identity.value is not None:
                raise ValueError(
                    "ui_readback.identity.value must be omitted when "
                    "ui_readback.reference captures the identifier"
                )
            name = self.reference.url_param
            if name in self.url_params:
                raise ValueError(
                    f"ui_readback.reference.url_param {name!r} is also in url_params"
                )
            if "{" + name + "}" not in self.record_url:
                raise ValueError(
                    f"ui_readback.record_url has no {{{name}}} placeholder for the "
                    "captured reference"
                )
        if not self.identity.reader.selector.strip():
            raise ValueError(
                "ui_readback.identity.reader.selector must name the element that "
                "shows the record identifier"
            )
        record_origin = _origin(self.record_url)
        if record_origin is None:
            raise ValueError("ui_readback.record_url must be an absolute http(s) URL")
        if _origin(self.sign_in.url) is None:
            raise ValueError("ui_readback.sign_in.url must be an absolute http(s) URL")
        for prefix in self.read_post_paths:
            if not prefix.startswith("/"):
                raise ValueError("ui_readback.read_post_paths entries start with '/'")
        if self.navigation_timeout_s <= 0:
            raise ValueError("ui_readback.navigation_timeout_s must be positive")
        if self.history is not None:
            if _origin(self.history.url.split("{", 1)[0]) != record_origin:
                raise ValueError(
                    "ui_readback.history.url must be on the record page's origin"
                )
            for name in re.findall(r"{([^{}]*)}", self.history.url):
                if name not in self.url_params:
                    raise ValueError(
                        f"ui_readback.history.url placeholder {{{name}}} must come "
                        "from url_params (the view is read before the save)"
                    )
        if self.final is not None and self.final.status_field is not None:
            if self.final.status_field not in self.fields:
                raise ValueError(
                    "ui_readback.final.status_field must be one of ui_readback.fields"
                )
        return self


@dataclass(frozen=True)
class ResolvedReadback:
    """A spec bound to one run: concrete URL and identifier, no secrets."""

    spec: UiReadbackSpec
    record_url: str
    identity_value: str


def resolve_spec(
    spec: UiReadbackSpec,
    params: Optional[Mapping[str, object]] = None,
    *,
    reference: Optional[str] = None,
) -> ResolvedReadback:
    """Bind ``spec`` to the run parameters, failing loud on a missing one.

    ``reference`` is the identifier captured after the save; it is required
    when ``spec.reference`` is declared and refused otherwise.
    """

    values = dict(params or {})
    resolved: dict[str, str] = {}
    if spec.reference is not None:
        if reference is None or not reference.strip():
            raise ValueError(
                "ui_readback.reference is declared but no reference was captured"
            )
        resolved[spec.reference.url_param] = quote(reference, safe="")
    elif reference is not None:
        raise ValueError("a reference was given but ui_readback.reference is not set")
    for name, expr in spec.url_params.items():
        value = expr.resolve(values)
        if value is None:
            raise ValueError(
                f"ui_readback.url_params[{name!r}] references run parameter "
                f"{expr.param!r}, which was not supplied"
            )
        resolved[name] = quote(value, safe="")
    try:
        record_url = spec.record_url.format(**resolved) if resolved else spec.record_url
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"ui_readback.record_url does not match ui_readback.url_params: {exc!r}"
        ) from exc
    if "{" in record_url or "}" in record_url:
        raise ValueError("ui_readback.record_url has an unfilled placeholder")
    if _origin(record_url) != _origin(spec.record_url.split("{", 1)[0] or record_url):
        raise ValueError("ui_readback.url_params must not change the URL origin")
    if spec.identity.value is None:
        identity_value: Optional[str] = reference
    else:
        identity_value = spec.identity.value.resolve(values)
    if identity_value is None:
        raise ValueError(
            "ui_readback.identity.value references run parameter "
            f"{getattr(spec.identity.value, 'param', None)!r}, which was not supplied"
        )
    return ResolvedReadback(
        spec=spec, record_url=record_url, identity_value=identity_value
    )


def _origin(url: str) -> Optional[str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


# -- what a driver returns ------------------------------------------------------


@dataclass
class RawPage:
    """One read of the record page, before normalization.

    ``ok`` is False when the page did not load or was not readable; ``reason``
    then says why (no record values). ``rows`` maps field name to the raw text
    or attribute; ``None`` means the row had no element for that field.
    """

    ok: bool
    reason: str = ""
    not_found: bool = False
    #: Whether the empty-state marker was present (None when not declared).
    empty_marker: Optional[bool] = None
    identity: Optional[str] = None
    rows: list[dict[str, Optional[str]]] = field(default_factory=list)
    blocked_requests: int = 0
    #: Whether the page's document came over the network from the server
    #: (not a cache or a service worker). None when the driver doesn't say.
    fetched: Optional[bool] = None
    #: The URL the session showed when it read the page.
    view_url: Optional[str] = None


@dataclass(frozen=True)
class SessionFacts:
    """What the driver can attest about the read-back session itself."""

    own_browser: bool
    fresh_context: bool
    signed_in: bool


class ReadbackSession(Protocol):
    """An open, signed-in, read-only session on the record page."""

    facts: SessionFacts

    def read(self) -> RawPage:
        """Load (or reload) the record page and read it once."""
        ...


class ReadbackDriver(Protocol):
    """Opens a read-back session. The default drives a real browser."""

    def open(
        self, target: ResolvedReadback, username: str, password: str
    ) -> AbstractContextManager[ReadbackSession]: ...


class ReadbackSessionError(RuntimeError):
    """The session could not be opened (launch, sign-in, or fresh-context
    check failed). The message never carries credentials or record values."""


# -- the default driver: a real browser in its own thread -----------------------

_READ_SCRIPT = """
(spec) => {
  const take = (root, r) => {
    if (!root) return null;
    const el = r.selector ? root.querySelector(r.selector) : root;
    if (!el) return null;
    if (r.attribute) return el.getAttribute(r.attribute);
    return el.innerText !== undefined ? el.innerText : el.textContent;
  };
  const notFound = spec.notFound ? document.querySelector(spec.notFound) : null;
  const ready = document.querySelector(spec.ready);
  const identity = take(document, spec.identity);
  const empty = spec.empty ? !!document.querySelector(spec.empty) : null;
  const rows = [];
  if (ready) {
    for (const row of ready.querySelectorAll(spec.row)) {
      const rec = {};
      for (const [name, r] of Object.entries(spec.fields)) rec[name] = take(row, r);
      rows.push(rec);
    }
  }
  const blockedWrites = window.__openadaptBlockedWrites || 0;
  return {ready: !!ready, notFound: !!notFound, empty: empty, identity: identity,
          rows: rows, blockedWrites: blockedWrites};
}
"""

#: True only when the page sees no client-side store a client cache could
#: serve the record from: Web Storage, IndexedDB databases, CacheStorage
#: caches, or a controlling service worker. A store the page can't inspect
#: counts as empty only when the API doesn't exist in this browser.
_EMPTY_CLIENT_STORE_SCRIPT = """
async () => {
  try {
    if (window.localStorage.length || window.sessionStorage.length) return false;
  } catch (e) {}
  if (window.indexedDB && indexedDB.databases) {
    if ((await indexedDB.databases()).length) return false;
  }
  if (window.caches) {
    if ((await caches.keys()).length) return false;
  }
  if (navigator.serviceWorker && navigator.serviceWorker.controller) return false;
  return true;
}
"""

#: Installed in every document the session loads after sign-in, before the
#: page's own scripts run. It refuses fetch, XHR, beacons, and form posts that
#: are not reads, inside the page itself, so a write never reaches the network
#: even when the network-layer guard would see it late. ``__CONFIG__`` is
#: replaced with the record origin and the declared read-POST paths.
_NO_WRITES_SCRIPT = """
(() => {
  const config = __CONFIG__;
  const READ = new Set(['GET', 'HEAD', 'OPTIONS']);
  let blocked = 0;
  Object.defineProperty(window, '__openadaptBlockedWrites', {get: () => blocked});
  const allowed = (method, url) => {
    const m = String(method || 'GET').toUpperCase();
    if (READ.has(m)) return true;
    if (m !== 'POST') return false;
    try {
      const u = new URL(String(url), location.href);
      return u.origin === config.origin &&
        config.readPostPaths.some((p) => u.pathname.startsWith(p));
    } catch (e) {
      return false;
    }
  };
  const realFetch = window.fetch;
  if (realFetch) {
    window.fetch = function (input, init) {
      const isRequest = input && typeof input === 'object';
      const method = (init && init.method) || (isRequest && input.method) || 'GET';
      const url = (isRequest && input.url) || input;
      if (!allowed(method, url)) {
        blocked += 1;
        return Promise.reject(new TypeError('blocked by the read-only session'));
      }
      return realFetch.apply(this, arguments);
    };
  }
  const xhr = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
  if (xhr) {
    const open = xhr.open;
    const send = xhr.send;
    xhr.open = function (method, url) {
      this.__openadaptAllowed = allowed(method, url);
      return open.apply(this, arguments);
    };
    xhr.send = function () {
      if (this.__openadaptAllowed === false) {
        blocked += 1;
        throw new DOMException('blocked by the read-only session', 'NetworkError');
      }
      return send.apply(this, arguments);
    };
  }
  if (window.Navigator && Navigator.prototype.sendBeacon) {
    Object.defineProperty(Navigator.prototype, 'sendBeacon', {
      value: function () { blocked += 1; return false; },
    });
  }
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (form && !allowed(form.method, form.action)) {
      blocked += 1;
      event.preventDefault();
    }
  }, true);
  const submit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    if (!allowed(this.method, this.action)) {
      blocked += 1;
      return undefined;
    }
    return submit.apply(this, arguments);
  };
})();
"""


def _no_writes_script(target: "ResolvedReadback") -> str:
    config = {
        "origin": _origin(target.record_url),
        "readPostPaths": list(target.spec.read_post_paths),
    }
    return _NO_WRITES_SCRIPT.replace("__CONFIG__", json.dumps(config))


def _reader_payload(reader: FieldReader) -> dict[str, Optional[str]]:
    return {"selector": reader.selector, "attribute": reader.attribute}


_NATIVE_SIGN_IN_FORM = """
e => {
  const descriptor = (prototype, name, node) =>
    Object.getOwnPropertyDescriptor(prototype, name).get.call(node);
  let prototype;
  if (e instanceof HTMLButtonElement) prototype = HTMLButtonElement.prototype;
  else if (e instanceof HTMLInputElement) prototype = HTMLInputElement.prototype;
  else return null;
  const type = descriptor(prototype, 'type', e);
  if (type !== 'submit' && type !== 'image') return null;
  const form = descriptor(prototype, 'form', e);
  if (!(form instanceof HTMLFormElement) || !form.isConnected) return null;
  const attribute = name => Element.prototype.getAttribute.call(e, name);
  const effective = (override, property) => attribute(override) !== null
    ? descriptor(prototype, property, e)
    : descriptor(HTMLFormElement.prototype, property.slice(4).toLowerCase(), form);
  return {
    method: effective('formmethod', 'formMethod'),
    action: effective('formaction', 'formAction'),
    target: effective('formtarget', 'formTarget')
  };
}
"""


class PlaywrightReadbackDriver:
    """Drive a separate browser with Playwright's sync API.

    Each session launches its own browser process and a new context, so the
    read shares no cookies, storage, cache, or service worker with any other
    browser, including the replay backend's. Playwright's sync API cannot run
    two drivers on one thread, so :class:`SeparateSessionReadbackVerifier`
    calls this driver on a worker thread.
    """

    def __init__(self, *, executable_path: Optional[str] = None) -> None:
        self.executable_path = executable_path

    def open(
        self, target: ResolvedReadback, username: str, password: str
    ) -> AbstractContextManager[ReadbackSession]:
        return _PlaywrightSession(self, target, username, password)


class _PlaywrightSession:
    def __init__(
        self,
        driver: PlaywrightReadbackDriver,
        target: ResolvedReadback,
        username: str,
        password: str,
    ) -> None:
        self._driver = driver
        self._target = target
        self._username = username
        self._password = password
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._phase = "sign_in"
        self._auth_refused = False
        self._auth_attempt = 0
        self._auth_frame: Any = None
        self._auth_read_redirect: Optional[tuple[Any, int, str]] = None
        self._auth_get_allowance: Optional[
            tuple[tuple[str, str, int, str, str], Any, int]
        ] = None
        self._auth_response_status: Optional[int] = None
        self._auth_allowance: Optional[
            tuple[tuple[str, str, int, str, str], Any, int]
        ] = None
        self._blocked = 0
        self._blocked_elsewhere = 0
        #: The URL of the page being read (None outside a read).
        self._reading: Optional[str] = None
        self._loaded: Optional[str] = None
        self.facts = SessionFacts(
            own_browser=True, fresh_context=False, signed_in=False
        )

    # -- context manager ------------------------------------------------------
    def __enter__(self) -> "_PlaywrightSession":
        if self._target.spec.sign_in.post_url is None:
            self._username = self._password = ""
            raise ReadbackSessionError(
                "sign_in.post_url must declare the permitted native form POST"
            )
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - env specific
            raise ReadbackSessionError(
                "the ui_readback verifier needs the 'browser' extra (playwright)"
            ) from exc
        spec = self._target.spec
        timeout_ms = int(spec.navigation_timeout_s * 1000)
        try:
            self._pw = sync_playwright().start()
            launcher = getattr(self._pw, spec.browser)
            launch_args: dict[str, Any] = {"headless": spec.headless}
            if self._driver.executable_path:
                launch_args["executable_path"] = self._driver.executable_path
            self._browser = launcher.launch(**launch_args)
            self._context = self._browser.new_context(
                service_workers="block", accept_downloads=False
            )
            fresh = self._context.cookies() == []
            self._context.set_default_timeout(timeout_ms)
            self._context.route("**/*", self._guard)
            self._page = self._context.new_page()
            self._page.goto(spec.sign_in.url, wait_until="domcontentloaded")
            fresh = fresh and self._page.evaluate(_EMPTY_CLIENT_STORE_SCRIPT)
            if not fresh:
                raise ReadbackSessionError("the read-back context was not empty")
            # Only a captured transient auth response permits another explicit
            # credential submission. Unknown delivery/navigation never does.
            for attempt in range(_SIGN_IN_ATTEMPTS):
                self._auth_attempt += 1
                self._auth_read_redirect = None
                self._auth_get_allowance = None
                self._auth_response_status = None
                self._auth_allowance = None
                self._phase = "sign_in"
                try:
                    if attempt:
                        time.sleep(_backoff(attempt))
                        self._page.goto(spec.sign_in.url, wait_until="domcontentloaded")
                    self._page.fill(spec.sign_in.username_selector, self._username)
                    self._page.fill(spec.sign_in.password_selector, self._password)
                    self._arm_sign_in()
                    try:
                        self._page.click(spec.sign_in.submit_selector)
                    except Exception:  # noqa: BLE001 - only our captured abort is expected
                        if not self._current_auth_redirect():
                            raise
                    redirect = self._consume_auth_redirect()
                    if redirect is not None:
                        frame, url = redirect
                        self._auth_get_allowance = (
                            _auth_url_key(url),
                            frame,
                            self._auth_attempt,
                        )
                        try:
                            frame.goto(url, wait_until="domcontentloaded")
                        except Exception:
                            raise ReadbackSessionError(
                                "the explicit authentication read failed"
                            ) from None
                        if self._auth_get_allowance is not None or self._auth_refused:
                            raise ReadbackSessionError(
                                "the authentication read did not pass its guard"
                            )
                    self._page.wait_for_selector(
                        spec.sign_in.signed_in_selector, state="attached"
                    )
                    if self._auth_refused:
                        raise ReadbackSessionError(
                            "the authentication response could not be safely delivered"
                        )
                    break
                except ReadbackSessionError:
                    raise
                except Exception:  # noqa: BLE001 - retried, then fails below
                    self._auth_allowance = None
                    if self._auth_refused:
                        raise ReadbackSessionError(
                            "the authentication response could not be safely delivered"
                        ) from None
                    if self._auth_response_status not in _TRANSIENT:
                        raise ReadbackSessionError(
                            "sign-in did not complete after the declared POST"
                        ) from None
                    if attempt == _SIGN_IN_ATTEMPTS - 1:
                        raise
            # From here on the session is read-only. The network guard
            # (``_guard``) refuses every non-read request; the init script
            # refuses them inside each page before they are sent.
            self._auth_allowance = None
            self._phase = "read"
            self._context.add_init_script(_no_writes_script(self._target))
        except ReadbackSessionError:
            self._close()
            raise
        except Exception as exc:  # noqa: BLE001 - any failure means no session
            self._close()
            raise ReadbackSessionError(
                f"could not open a signed-in read-back session ({type(exc).__name__})"
            ) from None
        finally:
            # Drop the secrets as soon as sign-in is over.
            self._username = ""
            self._password = ""
        self.facts = SessionFacts(own_browser=True, fresh_context=True, signed_in=True)
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()

    def _close(self) -> None:
        # Anything still in flight while the session closes is refused, and
        # the context goes offline first so a request released during
        # teardown cannot reach the application.
        self._auth_allowance = None
        self._auth_get_allowance = None
        self._auth_read_redirect = None
        self._phase = "closed"
        if self._context is not None:
            try:
                self._context.set_offline(True)
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._context = self._browser = self._pw = None

    # -- read-only guard --------------------------------------------------------
    def _arm_sign_in(self) -> None:
        """Bind the selected native submitter to the declared deployment POST."""

        self._auth_allowance = None
        sign_in = self._target.spec.sign_in
        if sign_in.post_url is None:
            raise ReadbackSessionError("sign_in.post_url is not declared")
        submitter = self._page.locator(sign_in.submit_selector).element_handle()
        if submitter is None:
            raise ReadbackSessionError("the selected sign-in submitter is unavailable")
        form = submitter.evaluate(_NATIVE_SIGN_IN_FORM)
        frame = submitter.owner_frame()
        if (
            not isinstance(form, dict)
            or form.get("method") != "post"
            or form.get("target") not in {"", "_self"}
            or not isinstance(form.get("action"), str)
            or _auth_url_key(form["action"]) != _auth_url_key(sign_in.post_url)
            or frame is None
        ):
            raise ReadbackSessionError(
                "the selected native sign-in form does not match its declared POST"
            )
        self._auth_frame = frame
        self._auth_allowance = (
            _auth_url_key(sign_in.post_url),
            frame,
            self._auth_attempt,
        )

    def _current_auth_redirect(self) -> bool:
        token = self._auth_read_redirect
        return (
            token is not None
            and not self._auth_refused
            and token[0] is self._auth_frame
            and token[1] == self._auth_attempt
            and self._phase == "read"
        )

    def _consume_auth_redirect(self) -> Optional[tuple[Any, str]]:
        token = self._auth_read_redirect
        current = self._current_auth_redirect()
        self._auth_read_redirect = None
        if token is None:
            return None
        if not current:
            raise ReadbackSessionError(
                "the captured authentication read is not current"
            )
        return token[0], token[2]

    def _send_auth_request(self, route: Any, request: Any, *, read: bool) -> None:
        """Send one bound auth request; redirects never reach the browser."""

        response = None
        handled = False
        try:
            response = route.fetch(
                max_redirects=0,
                max_retries=0,
                timeout=int(self._target.spec.navigation_timeout_s * 1000),
            )
            status = int(response.status)
            if not read:
                self._auth_response_status = status
            if 300 <= status < 400:
                if read or status not in {301, 302, 303}:
                    self._auth_refused = True
                else:
                    location = response.headers.get("location")
                    if not isinstance(location, str) or not location:
                        raise ValueError("missing auth read Location")
                    url = urljoin(str(request.url), location)
                    if _auth_url_key(url)[:3] != _auth_url_key(str(request.url))[:3]:
                        raise ValueError("auth read Location changes origin")
                    self._auth_read_redirect = (request.frame, self._auth_attempt, url)
                # Cancel only the captured native navigation without creating
                # a browser error document that can race the explicit read.
                route.abort(
                    "aborted" if self._current_auth_redirect() else "blockedbyclient"
                )
                handled = True
            else:
                route.fulfill(response=response)
                handled = True
        except Exception:  # noqa: BLE001 - unknown delivery never permits a resend
            self._auth_refused = True
            if not handled:
                try:
                    route.abort("blockedbyclient")
                except Exception:  # noqa: BLE001 - refusal remains mandatory
                    pass
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:  # noqa: BLE001 - cleanup cannot establish success
                    self._auth_refused = True

    def _guard(self, route: Any, request: Any) -> None:
        method = str(request.method).upper()
        if method in READ_METHODS:
            if self._auth_get_allowance is not None:
                endpoint, frame, attempt = self._auth_get_allowance
                if request.frame is frame and request.is_navigation_request():
                    self._auth_get_allowance = None
                    if (
                        method == "GET"
                        and attempt == self._auth_attempt
                        and _auth_url_key(str(request.url)) == endpoint
                        and self._phase == "read"
                    ):
                        self._send_auth_request(route, request, read=True)
                    else:
                        self._auth_refused = True
                        route.abort("blockedbyclient")
                    return
            route.continue_()
            return
        spec = self._target.spec
        url = str(request.url)
        if method == "POST" and self._auth_allowance is not None:
            endpoint, frame, attempt = self._auth_allowance
            try:
                native_auth = (
                    request.frame is frame
                    and attempt == self._auth_attempt
                    and request.is_navigation_request()
                    and request.resource_type == "document"
                    and request.redirected_from is None
                    and _auth_url_key(url) == endpoint
                )
            except Exception:  # noqa: BLE001 - unknown request authority refuses
                native_auth = False
            if native_auth:
                # Consume before the request can cause redirects or background writes.
                self._auth_allowance = None
                self._phase = "read"
                self._send_auth_request(route, request, read=False)
                return
        if (
            self._phase == "read"
            and method == "POST"
            and _origin(url) == _origin(self._target.record_url)
            and any(urlsplit(url).path.startswith(p) for p in spec.read_post_paths)
        ):
            route.continue_()
            return
        # Always refused. It counts against the page being read only when
        # that page sent it: a timer on a page the session already left
        # (a landing page polling a counter) says nothing about the record.
        if self._from_reading_page(request):
            self._blocked += 1
        else:
            self._blocked_elsewhere += 1
        route.abort("blockedbyclient")

    def _from_reading_page(self, request: Any) -> bool:
        if self._reading is None:
            return True
        try:
            frame_url = str(request.frame.url)
        except Exception:  # noqa: BLE001 - unknown origin counts against the page
            return True
        here = urlsplit(self._reading)
        there = urlsplit(frame_url)
        if not there.scheme.startswith("http"):
            return True
        return (there.netloc, there.path) == (here.netloc, here.path)

    # -- reading ------------------------------------------------------------------
    def _load(self, url: str) -> Optional[bool]:
        """Open ``url`` (reload it when it is already open); say whether the
        document came over the network."""

        self._reading = url
        for attempt in range(_LOAD_ATTEMPTS):
            if attempt:
                time.sleep(_backoff(attempt))
            if self._loaded == url:
                response = self._page.reload(wait_until="domcontentloaded")
            else:
                response = self._page.goto(url, wait_until="domcontentloaded")
                self._loaded = url
            # A gateway error is a failed fetch, not a page: load it again.
            if response is None or int(response.status) not in _TRANSIENT:
                break
        if response is None:
            return None
        if int(response.status) in _TRANSIENT:
            raise ReadbackSessionError("the page kept answering with a gateway error")
        try:
            from_worker = bool(response.from_service_worker)
        except Exception:  # noqa: BLE001 - older drivers lack the property
            from_worker = False
        return not from_worker and int(response.status) != 304

    def read(self, target: Optional[ResolvedReadback] = None) -> RawPage:
        """Read the record page; ``target`` reads another record in the same
        session (a batch of final reads signs in once)."""

        target = target or self._target
        spec = target.spec
        payload = {
            "ready": spec.ready_selector,
            "notFound": spec.not_found_selector,
            "empty": spec.empty_selector,
            "row": spec.row_selector,
            "identity": _reader_payload(spec.identity.reader),
            "fields": {
                name: _reader_payload(reader) for name, reader in spec.fields.items()
            },
        }
        waiting = spec.ready_selector
        if spec.not_found_selector:
            waiting = f"{spec.ready_selector}, {spec.not_found_selector}"
        return self._read(target.record_url, waiting, payload, "record page")

    def read_view(self, url: str, payload: dict[str, Any]) -> RawPage:
        """Read another view of the application (a history or audit list),
        submitting its GET filter form first when ``payload["submit"]`` names
        the control."""

        submit = payload.get("submit")
        if not submit:
            return self._read(url, payload["ready"], payload, "history view")
        blocked_before = self._blocked
        try:
            self._load(url)
            with self._page.expect_navigation(wait_until="domcontentloaded"):
                self._page.click(submit)
            self._loaded = None  # the form chose the URL; never reload it blind
        except Exception as exc:  # noqa: BLE001 - unreadable view
            return RawPage(
                ok=False,
                reason=f"the history view's form did not submit ({type(exc).__name__})",
                blocked_requests=self._blocked - blocked_before,
            )
        return self._read_current(
            payload["ready"], payload, "history view", None, blocked_before
        )

    def _read(
        self, url: str, waiting: str, payload: dict[str, Any], what: str
    ) -> RawPage:
        blocked_before = self._blocked
        try:
            fetched = self._load(url)
        except Exception as exc:  # noqa: BLE001 - unreadable page
            return RawPage(
                ok=False,
                reason=f"the {what} did not load or could not be read "
                f"({type(exc).__name__})",
                blocked_requests=self._blocked - blocked_before,
            )
        return self._read_current(waiting, payload, what, fetched, blocked_before)

    def _read_current(
        self,
        waiting: str,
        payload: dict[str, Any],
        what: str,
        fetched: Optional[bool],
        blocked_before: Optional[int] = None,
    ) -> RawPage:
        if blocked_before is None:
            blocked_before = self._blocked
        try:
            self._page.wait_for_selector(waiting, state="attached")
            raw = self._page.evaluate(_READ_SCRIPT, payload)
            view_url = str(self._page.url)
        except Exception as exc:  # noqa: BLE001 - unreadable page
            return RawPage(
                ok=False,
                reason=f"the {what} did not load or could not be read "
                f"({type(exc).__name__})",
                blocked_requests=self._blocked - blocked_before,
            )
        return RawPage(
            ok=bool(raw.get("ready")) or bool(raw.get("notFound")),
            reason="" if raw.get("ready") or raw.get("notFound") else "not ready",
            not_found=bool(raw.get("notFound")) and not raw.get("ready"),
            empty_marker=(None if raw.get("empty") is None else bool(raw.get("empty"))),
            identity=raw.get("identity"),
            rows=list(raw.get("rows") or []),
            blocked_requests=self._blocked
            - blocked_before
            + int(raw.get("blockedWrites") or 0),
            fetched=fetched,
            view_url=view_url,
        )


# -- normalization ----------------------------------------------------------------


@dataclass
class _NormalizedPage:
    readable: bool
    reason: str
    records: list[dict[str, Any]]
    #: (row index, field) pairs whose displayed value is shortened or unparseable.
    uncertain: set[tuple[int, str]]
    blocked_requests: int
    not_found: bool


def _collapse(text: str) -> str:
    return " ".join(text.split())


def normalize_value(
    value: Optional[str], reader: FieldReader
) -> tuple[Optional[str], bool]:
    """Normalize one displayed value. Returns ``(value, parsed_ok)``.

    Whitespace is always collapsed, because HTML rendering collapses it.
    ``parsed_ok`` is False when a declared date format does not parse.
    """

    if value is None:
        return None, True
    text = _collapse(str(value))
    if reader.date_format:
        try:
            text = datetime.strptime(text, reader.date_format).date().isoformat()
        except ValueError:
            return text, False
    if reader.casefold:
        text = text.casefold()
    return text, True


def normalize_expected(value: str, reader: FieldReader) -> str:
    """Normalize an expected input the same way a displayed value is."""

    text = _collapse(value)
    if reader.date_format:
        for fmt in ("%Y-%m-%d", reader.date_format):
            try:
                text = datetime.strptime(text, fmt).date().isoformat()
                break
            except ValueError:
                continue
    if reader.casefold:
        text = text.casefold()
    return text


def _is_truncated(text: Optional[str], markers: list[str]) -> bool:
    if not text:
        return False
    stripped = text.rstrip()
    return any(marker and stripped.endswith(marker) for marker in markers)


class _Normalizer(Protocol):
    date_format: Optional[str]
    casefold: bool


def normalized_effect(effect: Effect, readers: Mapping[str, _Normalizer]) -> Effect:
    """A local copy of ``effect`` whose expected values are normalized
    exactly as displayed values are. The caller's effect is not touched."""

    def norm(name: str, expr: Optional[ValueExpr]) -> Optional[ValueExpr]:
        if expr is None or expr.literal is None or name not in readers:
            return expr
        return ValueExpr(
            literal=normalize_expected(expr.literal, readers[name])  # type: ignore[arg-type]
        )

    match = {k: norm(k, v) or v for k, v in effect.match.items()}
    value = norm(effect.field, effect.value) if effect.field else effect.value
    return effect.model_copy(update={"match": match, "value": value}, deep=True)


def blocking_uncertain_fields(
    effect: Effect,
    records: list[dict[str, Any]],
    uncertain: set[tuple[int, str]],
) -> list[str]:
    """Fields whose shortened or unparseable display blocks a verdict.

    A row is a candidate for the effect unless one of its CERTAIN selector
    values rules it out. If a selector value is uncertain the row's
    candidacy is unknown, so the read cannot certify anything. For
    ``field_equals`` the compared field also must be certain on every
    candidate row. Uncertain values on rows the selector rules out (an
    older encounter with a long note, say) do not block the verdict.
    """

    if not uncertain:
        return []
    selector_fields = set(effect.match)
    selector_fields.update(effect.key_selector())
    if effect.kind is EffectKind.EXACT_NEW_SET:
        for member in effect.new_records:
            selector_fields.update(member)
        selector_fields.add(effect.identity_field)
    compared = (
        {effect.field}
        if effect.kind is EffectKind.FIELD_EQUALS and effect.field
        else set()
    )
    wanted = {k: str(v) for k, v in effect.match.items()}
    wanted.update({k: str(v) for k, v in effect.key_selector().items()})
    blocking: set[str] = set()
    for index, record in enumerate(records):
        unsure = {name for row, name in uncertain if row == index}
        if not unsure:
            continue
        ruled_out = any(
            name not in unsure and str(record.get(name)) != want
            for name, want in wanted.items()
        )
        if ruled_out and effect.kind is not EffectKind.EXACT_NEW_SET:
            continue
        blocking.update(unsure & (selector_fields | compared))
    return sorted(blocking)


# -- the verifier -------------------------------------------------------------------


class SeparateSessionReadbackVerifier(VerifierAdapterBase):
    """Check a saved record through the application's record page, read in a
    separate, freshly signed-in, read-only browser session.

    Args:
        target: The spec bound to this run (:func:`resolve_spec`).
        driver: Opens sessions; defaults to :class:`PlaywrightReadbackDriver`.
        poll_interval_s: Gap between page reloads while waiting for a save to
            appear, within the effect's ``timeout_s``.
        session_timeout_s: Hard cap on one session (launch, sign-in, reads).
            A session that runs past it is abandoned and the verdict is
            INDETERMINATE.
        environ: Where credentials are read from (``os.environ`` by default).
        params: The run parameters, kept so a captured reference can be bound
            to the spec after the save.
        backend: The run's own replay backend, read once after the save when
            the spec declares ``reference``. Usually bound later through
            :meth:`bind_backend`.
        vision: Namespace exposing ``ocr(png, region=...)`` for
            ``reference.source: screen``; defaults to
            :mod:`openadapt_flow.vision`.
    """

    substrate = SUBSTRATE
    verification_tier = VerificationTier.INDEPENDENT_SESSION
    #: The read goes through the same application's screens. It is
    #: independent of the session that typed, not of the application.
    independent_system_of_record = False
    adapter_version = "1"

    def __init__(
        self,
        target: ResolvedReadback,
        *,
        driver: Optional[ReadbackDriver] = None,
        poll_interval_s: float = 0.25,
        session_timeout_s: Optional[float] = None,
        environ: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, object]] = None,
        backend: Any = None,
        vision: Any = None,
    ) -> None:
        self._target = target
        self._params: dict[str, object] = dict(params or {})
        self._backend = backend
        self._vision = vision
        self._driver: ReadbackDriver = driver or PlaywrightReadbackDriver()
        self.poll_interval_s = poll_interval_s
        self._session_timeout_s = session_timeout_s
        self._environ = environ if environ is not None else os.environ
        #: The history view as read before the save (None: not read).
        self._history_before: Optional[list[dict[str, Optional[str]]]] = None
        sign_in = target.spec.sign_in
        if (
            isinstance(self._driver, PlaywrightReadbackDriver)
            and sign_in.post_url is None
        ):
            raise ValueError(
                "sign_in.post_url must declare the permitted native form POST"
            )
        for name in (sign_in.username_env, sign_in.password_env):
            if not self._environ.get(name):
                raise ValueError(
                    f"ui_readback.sign_in references environment variable {name!r}, "
                    "which is not set (or empty); refusing to wire a read-back "
                    "session that cannot sign in"
                )

    # -- the reference captured after the save -------------------------------------
    @property
    def _reference(self) -> Optional[ReferenceCapture]:
        return self._target.spec.reference

    def bind_backend(self, backend: Any) -> None:
        """Attach the run's own replay backend (read only for ``reference``)."""
        self._backend = backend

    def requires_readable_pre_state_for(self, effect: Effect) -> bool:
        """Whether ``effect`` needs a pre-action read of the record page.

        With a captured reference there is no record page before the save, so
        any effect that judges against a baseline must refuse before input
        rather than halt after it.
        """
        if self._waiting_entry() is not None:
            return True  # refuse before input: this item is still provisional
        base = bool(effect.requires_baseline or effect.forbid_collateral_loss)
        if self._reference is None:
            return base
        return base or effect.count_new_only or effect.kind is EffectKind.EXACT_NEW_SET

    def _capture_reference(self) -> tuple[Optional[str], str]:
        """Read the record's reference from the run's own session, once.

        Returns ``(reference, failure_reason)``. Never raises; the reason
        never carries the screen text.
        """
        capture = self._reference
        assert capture is not None
        if self._backend is None:
            return None, "no run session is bound to read the record reference from"
        text: Optional[str] = None
        try:
            if capture.source == "url":
                url = getattr(self._backend, "url", None)
                text = url() if callable(url) else url
                if not isinstance(text, str):
                    return None, "the run's session does not expose its page URL"
            else:
                vision = self._vision
                if vision is None:
                    from openadapt_flow import vision as _vision

                    vision = _vision
                png = self._backend.screenshot()
                lines = vision.ocr(png, region=capture.region)
                text = "\n".join(line.text for line in lines)
        except Exception as exc:  # noqa: BLE001 - never raise out of a verdict
            return None, f"reading the record reference failed ({type(exc).__name__})"
        found = {
            m.group(1).strip()
            for m in re.finditer(capture.pattern, text)
            if m.group(1) and m.group(1).strip()
        }
        where = "page URL" if capture.source == "url" else "screen"
        if not found:
            return None, f"the run's {where} shows no record reference"
        if len(found) > 1:
            return None, (
                f"the run's {where} shows {len(found)} different record references; "
                "refusing to pick one"
            )
        return found.pop(), ""

    def _bound_target(self) -> tuple[Optional[ResolvedReadback], str]:
        if self._reference is None:
            return self._target, ""
        reference, failure = self._capture_reference()
        if reference is None:
            return None, failure
        try:
            return (
                resolve_spec(self._target.spec, self._params, reference=reference),
                "",
            )
        except ValueError as exc:
            return None, f"the captured record reference cannot be used ({exc})"

    # -- independence, history, provisional ------------------------------------------
    def _acting_view(self) -> tuple[bool, bool, Optional[str]]:
        """``(bound, has_url, url)`` of the acting session right after the save."""

        if self._backend is None:
            return False, False, None
        missing = object()
        try:
            url = getattr(self._backend, "url", missing)
            if url is missing:
                if inspect.getattr_static(self._backend, "url", missing) is not missing:
                    return True, True, None
                return True, False, None
            value = url() if callable(url) else url
        except Exception:  # noqa: BLE001 - unknown, never a pass by itself
            return True, True, None
        return True, True, value if isinstance(value, str) else None

    def _history_url(self) -> Optional[str]:
        view = self._target.spec.history
        if view is None:
            return None
        values: dict[str, str] = {}
        for name, expr in self._target.spec.url_params.items():
            value = expr.resolve(self._params)
            if value is not None:
                values[name] = quote(value, safe="")
        try:
            return view.url.format(**values) if values else view.url
        except (KeyError, IndexError, ValueError):
            return None

    def _read_history(
        self, session: ReadbackSession
    ) -> Optional[list[dict[str, Optional[str]]]]:
        view = self._target.spec.history
        url = self._history_url()
        reader = getattr(session, "read_view", None)
        if view is None or url is None or not callable(reader):
            return None
        raw = reader(url, view.script_spec())
        if not isinstance(raw, RawPage) or not raw.ok or raw.blocked_requests:
            return None
        return list(raw.rows)

    def _history_verdict(
        self,
        verdict: EffectVerdict,
        after: Optional[list[dict[str, Optional[str]]]],
        target: ResolvedReadback,
    ) -> tuple[EffectVerdict, Optional[dict[str, Any]]]:
        view = self._target.spec.history
        if view is None or not verdict.confirmed:
            return verdict, None
        acting = (
            self._environ.get(view.acting_user_env) if view.acting_user_env else None
        )
        record_id = (
            view.record.resolve(self._params) if view.record is not None else None
        )
        judged = judge_history(
            view,
            self._history_before,
            after,
            record_id=record_id or target.identity_value,
            acting_user=acting,
        )
        detail = {"read": True, "ok": judged.ok, **judged.detail}
        if judged.ok:
            return verdict, detail
        return (
            verdict.model_copy(
                update={
                    "verdict": Verdict.REFUTED
                    if judged.refuted
                    else Verdict.INDETERMINATE,
                    "reason": f"{judged.reason}; HALT",
                    # A collateral change says nothing about whether the
                    # declared record is absent.
                    "observed_count": None
                    if not judged.refuted
                    else verdict.observed_count,
                }
            ),
            detail,
        )

    def _params_digest(self, ledger: Any) -> str:
        return ledger.key_digest(
            json.dumps(
                {k: str(v) for k, v in sorted(self._params.items())},
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _waiting_entry(self) -> Optional[str]:
        """The id of a provisional entry for these same inputs, still waiting.

        The ledger is on disk, so this holds across restarts: re-running the
        worklist never sends an item again while its final read is owed.
        """

        final = self._target.spec.final
        if final is None:
            return None
        from openadapt_flow.runtime.effects.deferred_report import (
            UI_READBACK_FORMAT,
            PendingLedger,
        )

        try:
            ledger = PendingLedger(final.ledger)
            digest = self._params_digest(ledger)
            for entry in ledger.entries():
                if (
                    entry.state != "waiting"
                    or entry.report_format != UI_READBACK_FORMAT
                ):
                    continue
                if entry.params_digest == digest:
                    return entry.id
                if entry.params_digest is None:
                    # Older fixed-target entries omitted the empty-input digest.
                    # A changed config alone cannot release the same record.
                    # Unknown or incomplete legacy identity requires a stop.
                    locator = entry.locator or {}
                    reader = self._target.spec.identity.reader
                    old_identity, old_readable = normalize_value(
                        locator.get("identity"), reader
                    )
                    identity, readable = normalize_value(
                        self._target.identity_value, reader
                    )
                    other_target = (
                        view_key(self._target.record_url) is not None
                        and view_key(locator.get("record_url")) is not None
                        and old_readable
                        and readable
                        and bool(old_identity)
                        and bool(identity)
                        and old_identity != identity
                    )
                    if (
                        not other_target
                        or entry.report_format_digest == self._final_digest()
                    ):
                        return entry.id
        except (OSError, ValueError):
            return "unreadable-ledger"  # can't rule it out: refuse
        return None

    def _final_digest(self) -> str:
        spec = self._target.spec
        payload = spec.model_dump(mode="json", exclude={"sign_in"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _provisional(
        self,
        expected: Effect,
        verdict: EffectVerdict,
        page: Optional["_NormalizedPage"],
        target: ResolvedReadback,
    ) -> tuple[EffectVerdict, Optional[dict[str, Any]]]:
        """Under ``final``, turn a confirmation into a waiting entry."""

        final = self._target.spec.final
        if final is None or not verdict.confirmed or page is None:
            return verdict, None
        status = None
        if final.status_field is not None:
            values = {r.get(final.status_field) for r in page.records}
            kinds = {final.status_of(v) for v in values}
            if "rejected" in kinds:
                return (
                    verdict.model_copy(
                        update={
                            "verdict": Verdict.REFUTED,
                            "observed_count": None,
                            "reason": "the record page shows the record turned "
                            "down (a rejected status); HALT",
                        }
                    ),
                    {"status": "rejected"},
                )
            if "unknown" in kinds or len(kinds) != 1:
                return (
                    verdict.model_copy(
                        update={
                            "verdict": Verdict.INDETERMINATE,
                            "reason": "the record page shows a status the "
                            "deployment doesn't declare; HALT",
                        }
                    ),
                    {"status": "unknown"},
                )
            status = kinds.pop()
        from openadapt_flow.runtime.effects.deferred_report import PendingLedger

        exclude = (final.status_field,) if final.status_field else ()
        try:
            ledger = PendingLedger(final.ledger)
            entry = ledger.add_ui(
                effect_contract_hash=expected.contract_hash(),
                spec_digest=self._final_digest(),
                describe=final.describe,
                deadline_hours=final.deadline_hours,
                due_after_s=final.after_s,
                locator={
                    "record_url": target.record_url,
                    "identity": target.identity_value,
                },
                record_payload=record_digest_payload(page.records, exclude=exclude),
                params_digest=self._params_digest(ledger),
            )
        except (OSError, ValueError) as exc:
            return (
                verdict.model_copy(
                    update={
                        "verdict": Verdict.INDETERMINATE,
                        "reason": "could not record the provisional check "
                        f"({type(exc).__name__}); HALT",
                        "unavailable": True,
                    }
                ),
                None,
            )
        return (
            EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=(
                    "provisionally confirmed through the record page"
                    + (f" (status {status})" if status else "")
                    + f"; the final read is due after {entry.due_at} and by "
                    f"{entry.deadline}; not counted until it settles"
                ),
                deferred_check=entry.id,
                assurance=UNCHECKED,
            ),
            {"provisional": True, "status": status, "due_at": entry.due_at},
        )

    # -- session plumbing ----------------------------------------------------------
    def _credentials(self) -> tuple[str, str]:
        sign_in = self._target.spec.sign_in
        return (
            self._environ.get(sign_in.username_env, ""),
            self._environ.get(sign_in.password_env, ""),
        )

    def _session_budget(self, effect_timeout_s: float = 0.0) -> float:
        if self._session_timeout_s is not None:
            return self._session_timeout_s + effect_timeout_s
        return 4 * self._target.spec.navigation_timeout_s + effect_timeout_s

    def _in_session(
        self,
        work: Callable[[ReadbackSession], Any],
        *,
        budget_s: float,
        target: Optional[ResolvedReadback] = None,
    ) -> tuple[Optional[Any], str, Optional[SessionFacts]]:
        """Run ``work`` in a fresh session on a worker thread.

        Returns ``(result, failure_reason, facts)``. ``result`` is None when
        the session could not be opened or ran past ``budget_s``.
        """

        outcome: dict[str, Any] = {}
        bound = target or self._target

        def run() -> None:
            username, password = self._credentials()
            try:
                with self._driver.open(bound, username, password) as session:
                    outcome["facts"] = session.facts
                    if not (
                        session.facts.own_browser
                        and session.facts.fresh_context
                        and session.facts.signed_in
                    ):
                        outcome["error"] = (
                            "the read-back session is not a fresh, signed-in session "
                            "in its own browser"
                        )
                        return
                    outcome["result"] = work(session)
            except ReadbackSessionError as exc:
                outcome["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001 - never raise out of a verdict
                outcome["error"] = f"read-back session failed ({type(exc).__name__})"

        worker = threading.Thread(target=run, name="ui-readback-session", daemon=True)
        worker.start()
        worker.join(timeout=budget_s)
        if worker.is_alive():
            return None, "the read-back session did not finish in time", None
        if "error" in outcome:
            return None, str(outcome["error"]), outcome.get("facts")
        return outcome.get("result"), "", outcome.get("facts")

    # -- normalization of one read ----------------------------------------------------
    def _normalize(
        self, raw: RawPage, target: Optional[ResolvedReadback] = None
    ) -> _NormalizedPage:
        target = target or self._target
        spec = target.spec
        if not raw.ok:
            return _NormalizedPage(
                False,
                raw.reason or "the record page was not readable",
                [],
                set(),
                raw.blocked_requests,
                False,
            )
        if raw.blocked_requests:
            return _NormalizedPage(
                False,
                f"the record page tried to send {raw.blocked_requests} request(s) "
                "that the read-only session blocked; the page may be incomplete",
                [],
                set(),
                raw.blocked_requests,
                False,
            )
        if raw.not_found:
            return _NormalizedPage(True, "", [], set(), 0, True)
        identity, parsed = normalize_value(raw.identity, spec.identity.reader)
        expected_identity = normalize_expected(
            target.identity_value, spec.identity.reader
        )
        if identity is None or not parsed:
            return _NormalizedPage(
                False,
                "the record page does not show a readable record identifier",
                [],
                set(),
                0,
                False,
            )
        if identity != expected_identity:
            return _NormalizedPage(
                False,
                "the record page shows a different record identifier than the one "
                "requested; nothing on it can be attributed to this record",
                [],
                set(),
                0,
                False,
            )
        if not raw.rows and raw.empty_marker is False:
            return _NormalizedPage(
                False,
                "the record page shows neither rows matching the declared row "
                "selector nor its empty-state marker; the layout may have changed",
                [],
                set(),
                0,
                False,
            )
        records: list[dict[str, Any]] = []
        uncertain: set[tuple[int, str]] = set()
        for index, row in enumerate(raw.rows):
            record: dict[str, Any] = {}
            for name, reader in spec.fields.items():
                value = row.get(name)
                if value is None:
                    return _NormalizedPage(
                        False,
                        f"row {index + 1} on the record page has no element for "
                        f"field {name!r}; the page layout differs from the "
                        "declared readers",
                        [],
                        set(),
                        0,
                        False,
                    )
                if _is_truncated(value, spec.truncation_markers):
                    uncertain.add((index, name))
                normalized, ok = normalize_value(value, reader)
                if not ok:
                    uncertain.add((index, name))
                record[name] = normalized
            if spec.identity.field is not None:
                record[spec.identity.field] = target.identity_value
            records.append(record)
        return _NormalizedPage(True, "", records, uncertain, 0, False)

    def _normalized_effect(
        self, effect: Effect, target: Optional[ResolvedReadback] = None
    ) -> Effect:
        return normalized_effect(effect, (target or self._target).spec.fields)

    @staticmethod
    def _uncertain_fields(effect: Effect, page: _NormalizedPage) -> list[str]:
        return blocking_uncertain_fields(effect, page.records, page.uncertain)

    def _detail(
        self, facts: Optional[SessionFacts], page: Optional[_NormalizedPage]
    ) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "read": "separate browser session through the application's record page",
            "record_url_template": self._target.spec.record_url,
        }
        if facts is not None:
            detail.update(
                own_browser=facts.own_browser,
                fresh_context=facts.fresh_context,
                signed_in=facts.signed_in,
            )
        if page is not None:
            detail.update(
                rows=len(page.records),
                blocked_requests=page.blocked_requests,
                record_not_found=page.not_found,
            )
        return detail

    def _judge(
        self,
        effect: Effect,
        before: EffectState,
        page: _NormalizedPage,
        target: Optional[ResolvedReadback] = None,
    ) -> EffectVerdict:
        if not page.readable:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=f"{page.reason}; cannot certify the save; HALT",
                unavailable=True,
            )
        normalized = self._normalized_effect(effect, target)
        shortened = self._uncertain_fields(normalized, page)
        if shortened:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=(
                    "the record page shows a shortened or unparseable value for "
                    f"{', '.join(repr(n) for n in shortened)}; a partial value "
                    "can neither confirm nor contradict the input. Read the full "
                    "value (for example from an attribute) or a detail page; HALT"
                ),
            )
        verdict = judge_records(
            normalized,
            before,
            page.records,
            substrate=self.substrate,
        )
        if verdict.confirmed and not page.records:
            # A search that finds nothing never proves anything, so it never
            # confirms (for example an effect that expects no record).
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=(
                    "the record page shows no records; a search that finds nothing is "
                    "not proof of what was saved; HALT"
                ),
            )
        return without_absence_claim(_relabel(verdict))

    # -- adapter lifecycle -------------------------------------------------------------
    def _read_once(
        self,
    ) -> tuple[Optional[_NormalizedPage], str, Optional[SessionFacts]]:
        raw, failure, facts = self._in_session(
            lambda session: session.read(), budget_s=self._session_budget()
        )
        if raw is None:
            return None, failure, facts
        return self._normalize(raw), "", facts

    def capture_pre_state(self, context: Any = None) -> EffectState:
        waiting = self._waiting_entry()
        if waiting is not None:
            return EffectState(
                substrate=self.substrate,
                reachable=False,
                records=[],
                detail={
                    **self._detail(None, None),
                    "reason": "this item was saved earlier and is still provisional "
                    f"({waiting}); it is not sent again before its final read",
                },
            )
        if self._target.spec.history is not None:
            rows, _failure, _facts = self._in_session(
                self._read_history, budget_s=self._session_budget()
            )
            self._history_before = rows if isinstance(rows, list) else None
        if self._reference is not None:
            return EffectState(
                substrate=self.substrate,
                reachable=False,
                records=[],
                detail={
                    **self._detail(None, None),
                    "reason": "the record reference is captured after the save, so "
                    "there is no record page to read before it",
                    "reference_source": self._reference.source,
                },
            )
        page, failure, facts = self._read_once()
        if page is None or not page.readable:
            return EffectState(
                substrate=self.substrate,
                reachable=False,
                records=[],
                detail={
                    **self._detail(facts, page),
                    "reason": failure or (page.reason if page else ""),
                },
            )
        return EffectState(
            substrate=self.substrate,
            reachable=True,
            records=page.records,
            detail=self._detail(facts, page),
        )

    def test_connection(self, context: Any = None) -> ConnectionProbe:
        """Sign in in a fresh session and read the record page once.

        With a captured reference there is no record to read yet, so the
        probe stops after a fresh, signed-in session opens.
        """

        if self._reference is not None:
            opened, failure, _facts = self._in_session(
                lambda session: True, budget_s=self._session_budget()
            )
            return ConnectionProbe(
                ok=opened is True,
                substrate=self.substrate,
                reason=(
                    "signed in to a separate session; the record page is opened "
                    "by the reference captured after the save"
                    if opened is True
                    else failure
                ),
            )
        page, failure, _facts = self._read_once()
        if page is None:
            return ConnectionProbe(ok=False, substrate=self.substrate, reason=failure)
        if not page.readable:
            return ConnectionProbe(
                ok=False, substrate=self.substrate, reason=page.reason
            )
        return ConnectionProbe(
            ok=True,
            substrate=self.substrate,
            reason="record page readable in a separate signed-in session",
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Read the record page in a fresh session until the effect is
        CONFIRMED or ``expected.timeout_s`` passes, reloading between reads."""

        timeout_s = max(0.0, expected.timeout_s)
        spec = self._target.spec
        target, failure = self._bound_target()
        if target is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=f"{failure}; cannot find the saved record to check; HALT",
                unavailable=True,
                assurance=UNCHECKED,
            )

        bound, has_url, acting_url = self._acting_view()

        def work(session: ReadbackSession) -> tuple[Any, ...]:
            # Search and list views often read a replica or an index that
            # lags the save. Poll with backoff for the lag the deployment
            # measured in its sandbox before taking "not there" as the read.
            deadline = time.monotonic() + max(timeout_s, spec.index_lag_s)
            wait = self.poll_interval_s
            found = False
            while True:
                raw = session.read()
                page = self._normalize(raw, target)
                shows = page.readable and not page.not_found and bool(page.records)
                if found and page.readable and not shows:
                    # Once seen, a record never quietly becomes "not saved".
                    verdict = EffectVerdict(
                        verdict=Verdict.INDETERMINATE,
                        kind=expected.kind,
                        substrate=self.substrate,
                        reason="the view showed the record, then stopped showing "
                        "it (a lagging or inconsistent index); can't certify the "
                        "save; HALT",
                    )
                    break
                found = found or shows
                verdict = self._judge(expected, before, page, target)
                if verdict.confirmed or time.monotonic() >= deadline:
                    break
                time.sleep(wait)
                wait = min(max(wait * 2, 0.05), _MAX_POLL_S)
            history = (
                self._read_history(session)
                if verdict.confirmed and self._target.spec.history is not None
                else None
            )
            return verdict, raw, page, history

        started = time.monotonic()
        result, failure, facts = self._in_session(
            work,
            budget_s=self._session_budget(max(timeout_s, spec.index_lag_s)),
            target=target,
        )
        if result is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=f"{failure}; cannot certify the save; HALT",
                unavailable=True,
                assurance=UNCHECKED,
            )
        verdict, raw, page, history = result
        assert isinstance(verdict, EffectVerdict)
        independence = assess_independence(
            own_browser=bool(facts and facts.own_browser),
            fresh_context=bool(facts and facts.fresh_context),
            signed_in=bool(facts and facts.signed_in),
            fetched=getattr(raw, "fetched", None),
            read_url=getattr(raw, "view_url", None) or target.record_url,
            acting_url=acting_url,
            acting_bound=bound,
            acting_has_url=has_url,
            read_seconds=time.monotonic() - started,
        )
        evidence: dict[str, Any] = {"independence": independence.to_dict()}
        verdict = _require_independent(verdict, independence)
        verdict, history_detail = self._history_verdict(verdict, history, target)
        if history_detail is not None:
            evidence["history"] = history_detail
        verdict, final_detail = self._provisional(expected, verdict, page, target)
        if final_detail is not None:
            evidence["final"] = final_detail
        # A separate session reads the same application's screens, so under
        # the policy rule in openadapt_flow.assurance this is a screen
        # read-back, not a system-of-record read (tier 2).
        return verdict.model_copy(
            update={
                "assurance": SCREEN_READBACK if verdict.confirmed else UNCHECKED,
                "read_evidence": evidence,
            }
        )


def _require_independent(
    verdict: EffectVerdict, independence: ReadIndependence
) -> EffectVerdict:
    """A read that isn't independent can refute a save, never confirm one."""

    if not verdict.confirmed or independence.qualifies:
        return verdict
    return verdict.model_copy(
        update={
            "verdict": Verdict.INDETERMINATE,
            "reason": (
                "the record page agreed, but the read is not independent of the "
                f"save ({', '.join(independence.failed)}: "
                + "; ".join(independence.basis[k] for k in independence.failed)
                + "); reading the page the save showed never counts; HALT"
            ),
        }
    )


def without_absence_claim(verdict: EffectVerdict) -> EffectVerdict:
    """Never let a record page establish that nothing was saved.

    A REFUTED verdict with an observed count of zero means "affirmatively
    absent" to the transaction classifier, which may then report that nothing
    was written and allow a retry. A record page can hide a saved row
    (pagination, a "recent items" list, a filter, a slow replica), so its
    zero is not that proof. Dropping the count turns the observed effect into
    ``unknown``: the run still halts, and it ends in reconciliation instead of
    a claim that the record did not change.
    """

    if verdict.verdict is not Verdict.REFUTED or verdict.observed_count != 0:
        return verdict
    return verdict.model_copy(
        update={
            "observed_count": None,
            "reason": verdict.reason
            + "; the record page does not show it, which is not proof that "
            "nothing was saved",
        }
    )


def _relabel(verdict: EffectVerdict) -> EffectVerdict:
    return relabel_screen_read(
        verdict, "the record page (separate session)", "record page"
    )


def relabel_screen_read(
    verdict: EffectVerdict, long_name: str, short_name: str
) -> EffectVerdict:
    """Say "record page" where the shared judge says "system of record".

    The shared judge's reasons are written for system-of-record reads. This
    read is not one, and its evidence must not say it is.
    """

    reason = verdict.reason.replace("the system of record", long_name).replace(
        "system of record", short_name
    )
    if reason == verdict.reason:
        return verdict
    return verdict.model_copy(update={"reason": reason})


def build_ui_readback_verifier(
    spec: UiReadbackSpec,
    params: Optional[Mapping[str, object]] = None,
    *,
    timeout_s: float = 5.0,
    poll_interval_s: float = 0.25,
    driver: Optional[ReadbackDriver] = None,
    environ: Optional[Mapping[str, str]] = None,
    backend: Any = None,
    vision: Any = None,
) -> SeparateSessionReadbackVerifier:
    """Build the verifier for one run (the ``effects.kind: ui_readback`` path).

    ``timeout_s`` is the deployment's ``effects.timeout_s``; it bounds session
    setup on top of the navigation timeout. With ``spec.reference`` the URL is
    bound to a placeholder now (to check the configuration and to sign in for
    the connection probe) and to the captured reference after the save.
    """

    target = resolve_spec(
        spec,
        params,
        reference=_PROBE_REFERENCE if spec.reference is not None else None,
    )
    return SeparateSessionReadbackVerifier(
        target,
        driver=driver,
        poll_interval_s=poll_interval_s,
        session_timeout_s=4 * spec.navigation_timeout_s + max(0.0, timeout_s),
        environ=environ,
        params=params,
        backend=backend,
        vision=vision,
    )


def settle_ui_readback(
    spec: UiReadbackSpec,
    *,
    driver: Optional[ReadbackDriver] = None,
    environ: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> list[Any]:
    """Run the final read for every due entry this configuration left waiting.

    One fresh, signed-in session reads every due record in turn, so a batch
    of N saves costs one sign-in and N page loads. Each entry settles:

    - VERIFIED: the record shows a final status (when ``status_field`` is
      declared) and exactly the values the provisional read saw;
    - RECONCILIATION_REQUIRED: a rejected or undeclared status, a value that
      changed after the provisional read, a record the page no longer shows,
      or (first, before any read) a deadline that passed;
    - still waiting: a status still pending, or a page that couldn't be read.

    Returns the :class:`~.deferred_report.Settlement` list.
    """

    from openadapt_flow.runtime.effects.deferred_report import (
        UI_READBACK_FORMAT,
        PendingLedger,
        _now,
    )

    final = spec.final
    if final is None:
        raise ValueError("ui_readback.final is not configured")
    ledger = PendingLedger(final.ledger)
    moment = now or _now()
    settled: list[Any] = list(ledger.settle(None, None, now=moment))
    due = []
    for entry in ledger.entries():
        if (
            entry.state != "waiting"
            or entry.report_format != UI_READBACK_FORMAT
            or entry.locator is None
            or entry.record_mac is None
        ):
            continue
        if entry.due_at and datetime.fromisoformat(entry.due_at) > moment:
            continue
        target = ResolvedReadback(
            spec=spec,
            record_url=str(entry.locator.get("record_url", "")),
            identity_value=str(entry.locator.get("identity", "")),
        )
        due.append((entry, target))
    if not due:
        return settled
    verifier = SeparateSessionReadbackVerifier(
        due[0][1], driver=driver, environ=environ
    )
    digest = verifier._final_digest()
    due = [(e, t) for e, t in due if e.report_format_digest == digest]
    if not due:
        return settled
    exclude = (final.status_field,) if final.status_field else ()

    def work(session: ReadbackSession) -> list[tuple[Any, Any, str, str]]:
        decisions = []
        for entry, target in due:
            raw = session.read(target)  # type: ignore[call-arg]
            independence = assess_independence(
                own_browser=session.facts.own_browser,
                fresh_context=session.facts.fresh_context,
                signed_in=session.facts.signed_in,
                fetched=raw.fetched,
                read_url=raw.view_url or target.record_url,
                acting_url=None,
                acting_bound=False,
                acting_has_url=False,
            )
            if not independence.qualifies:
                continue  # refused document: keep waiting until the deadline
            page = verifier._normalize(raw, target)
            if not page.readable:
                continue  # can't tell now; the deadline still applies
            if page.not_found or not page.records:
                decisions.append(
                    (
                        entry,
                        "reconciliation_required",
                        "gone",
                        "the record page no longer shows the record",
                    )
                )
                continue
            if final.status_field is not None:
                kinds = {
                    final.status_of(r.get(final.status_field)) for r in page.records
                }
                if kinds == {"pending"}:
                    continue
                if kinds != {"final"}:
                    decisions.append(
                        (
                            entry,
                            "reconciliation_required",
                            "status",
                            "the record shows a rejected or undeclared status",
                        )
                    )
                    continue
            if page.uncertain:
                continue
            mac = ledger.key_digest(
                record_digest_payload(page.records, exclude=exclude)
            )
            if mac != entry.record_mac:
                decisions.append(
                    (
                        entry,
                        "reconciliation_required",
                        "changed",
                        "the record changed after the provisional check",
                    )
                )
                continue
            decisions.append(
                (
                    entry,
                    "verified",
                    "final",
                    "the final read shows the record unchanged"
                    + (" with a final status" if final.status_field else ""),
                )
            )
        return decisions

    decisions, failure, facts = verifier._in_session(
        work,
        budget_s=verifier._session_budget() + len(due) * spec.navigation_timeout_s,
        target=due[0][1],
    )
    if decisions is None:
        return settled  # no session: nothing settles but the deadlines
    for entry, state, code, reason in decisions:
        item = ledger.settle_entry(
            entry.id, state, code=code, reason=reason, now=moment
        )
        if item is not None:
            settled.append(item)
    return settled


__all__ = [
    "DEFAULT_TRUNCATION_MARKERS",
    "FieldReader",
    "PlaywrightReadbackDriver",
    "RawPage",
    "ReadbackDriver",
    "ReadbackSession",
    "ReadbackSessionError",
    "RecordIdentity",
    "ReferenceCapture",
    "ResolvedReadback",
    "SUBSTRATE",
    "SeparateSessionReadbackVerifier",
    "SessionFacts",
    "SignInSpec",
    "UiReadbackSpec",
    "blocking_uncertain_fields",
    "build_ui_readback_verifier",
    "normalize_expected",
    "normalized_effect",
    "relabel_screen_read",
    "without_absence_claim",
    "normalize_value",
    "resolve_spec",
    "settle_ui_readback",
]
