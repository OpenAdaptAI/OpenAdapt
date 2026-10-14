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

import json
import os
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Mapping, Optional, Protocol
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from openadapt_flow.verification import VerificationTier

SUBSTRATE = "ui-readback"

#: Methods the read-only session may send after sign-in. OPTIONS is a CORS
#: preflight and carries no body.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

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


class SignInSpec(BaseModel):
    """The read-back session's own sign-in. Credentials are env-var names."""

    model_config = ConfigDict(extra="forbid")

    url: str
    username_selector: str
    password_selector: str
    submit_selector: str
    #: An element that is present only after a successful sign-in.
    signed_in_selector: str
    username_env: str
    password_env: str


class RecordIdentity(BaseModel):
    """The check that the page shows the record the session asked for."""

    model_config = ConfigDict(extra="forbid")

    reader: FieldReader
    #: The identifier the page must show, usually a run parameter.
    value: ValueExpr
    #: When set, the confirmed identifier is added to every row under this
    #: field name, so an effect can match on it (for example ``patient_id``).
    field: Optional[str] = None


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
    #: Path prefixes on the record page's origin that the page may POST to
    #: in order to READ data (for example a GraphQL query endpoint). Empty by
    #: default: every POST after sign-in is blocked.
    read_post_paths: list[str] = Field(default_factory=list)
    truncation_markers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TRUNCATION_MARKERS)
    )
    navigation_timeout_s: float = 15.0
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True

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
        return self


@dataclass(frozen=True)
class ResolvedReadback:
    """A spec bound to one run: concrete URL and identifier, no secrets."""

    spec: UiReadbackSpec
    record_url: str
    identity_value: str


def resolve_spec(
    spec: UiReadbackSpec, params: Optional[Mapping[str, object]] = None
) -> ResolvedReadback:
    """Bind ``spec`` to the run parameters, failing loud on a missing one."""

    values = dict(params or {})
    resolved: dict[str, str] = {}
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
    identity_value = spec.identity.value.resolve(values)
    if identity_value is None:
        raise ValueError(
            "ui_readback.identity.value references run parameter "
            f"{spec.identity.value.param!r}, which was not supplied"
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
        self._blocked = 0
        self._loaded = False
        self.facts = SessionFacts(
            own_browser=True, fresh_context=False, signed_in=False
        )

    # -- context manager ------------------------------------------------------
    def __enter__(self) -> "_PlaywrightSession":
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
            fresh = fresh and self._page.evaluate(
                "() => { try { return window.localStorage.length === 0 && "
                "window.sessionStorage.length === 0; } catch (e) { return true; } }"
            )
            if not fresh:
                raise ReadbackSessionError("the read-back context was not empty")
            self._page.fill(spec.sign_in.username_selector, self._username)
            self._page.fill(spec.sign_in.password_selector, self._password)
            self._page.click(spec.sign_in.submit_selector)
            self._page.wait_for_selector(
                spec.sign_in.signed_in_selector, state="attached"
            )
            # From here on the session is read-only. The network guard
            # (``_guard``) refuses every non-read request; the init script
            # refuses them inside each page before they are sent.
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
    def _guard(self, route: Any, request: Any) -> None:
        method = str(request.method).upper()
        if method in READ_METHODS:
            route.continue_()
            return
        spec = self._target.spec
        url = str(request.url)
        if (
            self._phase == "sign_in"
            and method == "POST"
            and _origin(url) == _origin(spec.sign_in.url)
        ):
            route.continue_()
            return
        if (
            method == "POST"
            and _origin(url) == _origin(self._target.record_url)
            and any(urlsplit(url).path.startswith(p) for p in spec.read_post_paths)
        ):
            route.continue_()
            return
        self._blocked += 1
        route.abort("blockedbyclient")

    # -- reading ------------------------------------------------------------------
    def read(self) -> RawPage:
        spec = self._target.spec
        blocked_before = self._blocked
        try:
            if self._loaded:
                self._page.reload(wait_until="domcontentloaded")
            else:
                self._page.goto(self._target.record_url, wait_until="domcontentloaded")
                self._loaded = True
            waiting = spec.ready_selector
            if spec.not_found_selector:
                waiting = f"{spec.ready_selector}, {spec.not_found_selector}"
            self._page.wait_for_selector(waiting, state="attached")
            raw = self._page.evaluate(
                _READ_SCRIPT,
                {
                    "ready": spec.ready_selector,
                    "notFound": spec.not_found_selector,
                    "empty": spec.empty_selector,
                    "row": spec.row_selector,
                    "identity": _reader_payload(spec.identity.reader),
                    "fields": {
                        name: _reader_payload(reader)
                        for name, reader in spec.fields.items()
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 - unreadable page
            return RawPage(
                ok=False,
                reason=f"the record page did not load or could not be read "
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
    ) -> None:
        self._target = target
        self._driver: ReadbackDriver = driver or PlaywrightReadbackDriver()
        self.poll_interval_s = poll_interval_s
        self._session_timeout_s = session_timeout_s
        self._environ = environ if environ is not None else os.environ
        sign_in = target.spec.sign_in
        for name in (sign_in.username_env, sign_in.password_env):
            if not self._environ.get(name):
                raise ValueError(
                    f"ui_readback.sign_in references environment variable {name!r}, "
                    "which is not set (or empty); refusing to wire a read-back "
                    "session that cannot sign in"
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
    ) -> tuple[Optional[Any], str, Optional[SessionFacts]]:
        """Run ``work`` in a fresh session on a worker thread.

        Returns ``(result, failure_reason, facts)``. ``result`` is None when
        the session could not be opened or ran past ``budget_s``.
        """

        outcome: dict[str, Any] = {}

        def run() -> None:
            username, password = self._credentials()
            try:
                with self._driver.open(self._target, username, password) as session:
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
    def _normalize(self, raw: RawPage) -> _NormalizedPage:
        spec = self._target.spec
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
            self._target.identity_value, spec.identity.reader
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
                record[spec.identity.field] = self._target.identity_value
            records.append(record)
        return _NormalizedPage(True, "", records, uncertain, 0, False)

    def _normalized_effect(self, effect: Effect) -> Effect:
        """A local copy of ``effect`` whose expected values are normalized
        exactly as displayed values are. The caller's effect is not touched."""

        readers = self._target.spec.fields

        def norm(name: str, expr: Optional[ValueExpr]) -> Optional[ValueExpr]:
            if expr is None or expr.literal is None or name not in readers:
                return expr
            return ValueExpr(literal=normalize_expected(expr.literal, readers[name]))

        match = {k: norm(k, v) or v for k, v in effect.match.items()}
        value = norm(effect.field, effect.value) if effect.field else effect.value
        return effect.model_copy(update={"match": match, "value": value}, deep=True)

    @staticmethod
    def _uncertain_fields(effect: Effect, page: _NormalizedPage) -> list[str]:
        """Fields whose shortened or unparseable display blocks a verdict.

        A row is a candidate for the effect unless one of its CERTAIN selector
        values rules it out. If a selector value is uncertain the row's
        candidacy is unknown, so the read cannot certify anything. For
        ``field_equals`` the compared field also must be certain on every
        candidate row. Uncertain values on rows the selector rules out (an
        older encounter with a long note, say) do not block the verdict.
        """

        if not page.uncertain:
            return []
        selector_fields = set(effect.match)
        if effect.idempotency_key is not None:
            selector_fields.add(effect.key_field)
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
        if effect.idempotency_key is not None:
            wanted[effect.key_field] = str(effect.idempotency_key)
        blocking: set[str] = set()
        for index, record in enumerate(page.records):
            unsure = {name for row, name in page.uncertain if row == index}
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
        self, effect: Effect, before: EffectState, page: _NormalizedPage
    ) -> EffectVerdict:
        if not page.readable:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=effect.kind,
                substrate=self.substrate,
                reason=f"{page.reason}; cannot certify the save; HALT",
                unavailable=True,
            )
        normalized = self._normalized_effect(effect)
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
        return _without_absence_claim(_relabel(verdict))

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
        """Sign in in a fresh session and read the record page once."""

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

        def work(session: ReadbackSession) -> EffectVerdict:
            deadline = time.monotonic() + timeout_s
            while True:
                verdict = self._judge(expected, before, self._normalize(session.read()))
                if verdict.confirmed or time.monotonic() >= deadline:
                    return verdict
                time.sleep(self.poll_interval_s)

        verdict, failure, _facts = self._in_session(
            work, budget_s=self._session_budget(timeout_s)
        )
        if verdict is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=f"{failure}; cannot certify the save; HALT",
                unavailable=True,
            )
        assert isinstance(verdict, EffectVerdict)
        return verdict


def _without_absence_claim(verdict: EffectVerdict) -> EffectVerdict:
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
    """Say "record page" where the shared judge says "system of record".

    The shared judge's reasons are written for system-of-record reads. This
    read is not one, and its evidence must not say it is.
    """

    reason = verdict.reason.replace(
        "the system of record", "the record page (separate session)"
    ).replace("system of record", "record page")
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
) -> SeparateSessionReadbackVerifier:
    """Build the verifier for one run (the ``effects.kind: ui_readback`` path).

    ``timeout_s`` is the deployment's ``effects.timeout_s``; it bounds session
    setup on top of the navigation timeout.
    """

    target = resolve_spec(spec, params)
    return SeparateSessionReadbackVerifier(
        target,
        driver=driver,
        poll_interval_s=poll_interval_s,
        session_timeout_s=4 * spec.navigation_timeout_s + max(0.0, timeout_s),
        environ=environ,
    )


__all__ = [
    "DEFAULT_TRUNCATION_MARKERS",
    "FieldReader",
    "PlaywrightReadbackDriver",
    "RawPage",
    "ReadbackDriver",
    "ReadbackSession",
    "ReadbackSessionError",
    "RecordIdentity",
    "ResolvedReadback",
    "SUBSTRATE",
    "SeparateSessionReadbackVerifier",
    "SessionFacts",
    "SignInSpec",
    "UiReadbackSpec",
    "build_ui_readback_verifier",
    "normalize_expected",
    "normalize_value",
    "resolve_spec",
]
